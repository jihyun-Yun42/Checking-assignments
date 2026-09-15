import re
from datetime import date as date_cls

import requests

from . import config, db_client
from .time_window import dashboard_check_date

LOGIN_URL = f"{config.DASHBOARD_BASE_URL}/api/admin.php"
BOOTCAMP_URL = f"{config.DASHBOARD_BASE_URL}/api/bootcamp.php"

# 예전에는 표시이름 입력칸에 "(4회)"처럼 회차를 직접 타이핑해서 저장해뒀었음.
# 지금은 회차를 항상 새로 계산해서 붙이므로, 남아있는 옛날 회차 표기는 떼어내고
# 새 값을 붙여야 "이름(4회)(4회차)"처럼 중복 표기되지 않는다.
_ROUND_SUFFIX_RE = re.compile(r"\(\d+\s*회?\s*차?\)\s*$")


class DashboardLoginError(RuntimeError):
    pass


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def login(phone: str):
    phone = _normalize_phone(phone)
    if not phone:
        raise DashboardLoginError("휴대폰 번호를 입력해주세요.")

    session = requests.Session()
    resp = session.post(
        LOGIN_URL,
        params={"action": "login_phone"},
        json={"phone": phone},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise DashboardLoginError(data.get("error") or "등록되지 않은 휴대폰 번호입니다.")

    admin = data.get("admin") or {}
    roles = admin.get("admin_roles") or []
    if not any(r in ("leader", "subleader") for r in roles):
        raise DashboardLoginError("조장/부조장 계정만 이 서비스를 사용할 수 있습니다.")

    group_id = admin.get("bootcamp_group_id")
    if not group_id:
        raise DashboardLoginError("배정된 조를 찾을 수 없습니다. 운영진에게 문의해주세요.")

    admin_info = {
        "name": admin.get("admin_name") or "",
        "role": "조장" if "leader" in roles else "부조장",
        "group_id": group_id,
        "group_name": admin.get("team") or f"조ID{group_id}",
        "cohort_id": admin.get("admin_view_cohort_id"),
    }
    return session, admin_info


def fetch_status_board(session: requests.Session, cohort_id, date_str: str, group_id) -> dict:
    resp = session.get(
        BOOTCAMP_URL,
        params={"action": "status_board", "cohort_id": cohort_id, "date": date_str, "group_id": group_id},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise DashboardLoginError(data.get("error") or "현황 데이터를 가져오지 못했습니다.")
    return data


def _name_with_nickname(m: dict, overrides: dict = None) -> str:
    round_count = m.get("round_count")
    round_suffix = f"({round_count}회차)" if round_count else ""
    if overrides:
        override = overrides.get(str(m.get("id")))
        if override:
            override = _ROUND_SUFFIX_RE.sub("", override).strip()
            return f"{override}{round_suffix}"
    name = m.get("real_name") or "이름미상"
    nickname = m.get("nickname")
    return f"{name}_{nickname}{round_suffix}" if nickname else f"{name}{round_suffix}"


def _tag_name(m: dict, overrides: dict = None) -> str:
    if overrides:
        override = overrides.get(str(m.get("id")))
        if override:
            return _ROUND_SUFFIX_RE.sub("", override).strip()
    return m.get("real_name") or "이름미상"


def compute_report(status_board: dict, check_date: date_cls) -> dict:
    ids = config.MISSION_TYPE_IDS
    thresholds = status_board.get("thresholds") or {}
    out_threshold = thresholds.get("out", config.OUT_SCORE_THRESHOLD)
    checks = status_board.get("checks") or {}
    is_monday = check_date.weekday() == 0

    both_missing, naemat_missing, zoom_missing, complete, needs_check = [], [], [], [], []
    speak_missing = []

    for m in status_board.get("members") or []:
        if m.get("member_status") == config.OUT_MEMBER_STATUS:
            continue
        score = m.get("current_score")
        if isinstance(score, (int, float)) and score <= out_threshold:
            continue

        member_checks = checks.get(str(m.get("id")))
        if member_checks is None:
            needs_check.append(m)
            continue

        zoom_ok = member_checks.get(str(ids["zoom_daily"])) == 1
        daily_ok = member_checks.get(str(ids["daily_mission"])) == 1
        zoom_done = zoom_ok or daily_ok
        naemat_done = member_checks.get(str(ids["inner33"])) == 1

        if not zoom_done and not naemat_done:
            both_missing.append(m)
        elif not naemat_done:
            naemat_missing.append(m)
        elif not zoom_done:
            zoom_missing.append(m)
        else:
            complete.append(m)

        if is_monday and member_checks.get(str(ids["speak_mission"])) != 1:
            speak_missing.append(m)

    return {
        "both_missing": both_missing,
        "naemat_missing": naemat_missing,
        "zoom_missing": zoom_missing,
        "complete": complete,
        "needs_check": needs_check,
        "speak_missing": speak_missing,
        "speak_applicable": is_monday,
    }


def missing_report_sections(report: dict, overrides: dict = None) -> list:
    naemat = report["both_missing"] + report["naemat_missing"]
    zoom = report["both_missing"] + report["zoom_missing"]

    sections = [
        ("줌특강/데일리미션", zoom),
        ("내맛33", naemat),
    ]
    if report["speak_applicable"]:
        sections.append(("말까미션", report["speak_missing"]))

    result = []
    for label, members in sections:
        if not members:
            continue
        result.append({
            "label": label,
            "count": len(members),
            "members": [
                {"id": m.get("id"), "name": _name_with_nickname(m, overrides)}
                for m in members
            ],
        })
    return result


def missing_list_text(group_name: str, check_date: date_cls, report: dict, overrides: dict = None) -> str:
    date_str = check_date.strftime("%m/%d")
    lines = [f"[{group_name}] {date_str} 미제출자"]

    sections = missing_report_sections(report, overrides)
    for sec in sections:
        names = ", ".join(m["name"] for m in sec["members"])
        lines.append(f"- {sec['label']} ({sec['count']}명): {names}")

    if not sections:
        lines.append("미제출자 없음 🎉")

    if report.get("needs_check"):
        lines.append(f"⚠️ 확인 필요 {len(report['needs_check'])}명 (현황판에 체크 데이터가 아직 없음)")

    return "\n".join(lines)


def format_broadcast_tags(group_name: str, check_date: date_cls, report: dict, overrides: dict = None) -> str:
    date_str = check_date.strftime("%Y년 %m월 %d일")
    lines = [f"📅 {date_str} {group_name} 과제 현황"]
    has_section = False

    def _section(emoji, title, members):
        nonlocal has_section
        if not members:
            return
        has_section = True
        lines.append("")
        lines.append(f"{emoji} {title}")
        for m in members:
            lines.append(f"@{_tag_name(m, overrides)}")

    _section("🚨", "줌/데일리, 내맛 안 하신 분", report["both_missing"])
    _section("🟠", "내맛 안 하신 분", report["naemat_missing"])
    _section("🟡", "줌/데일리 안 하신 분", report["zoom_missing"])
    if report["speak_applicable"]:
        _section("📌", "말까 안 하신 분", report["speak_missing"])

    if not has_section:
        lines.append("")
        lines.append("오늘 미완료 과제가 없습니다 🎉")
        return "\n".join(lines)

    lines.append("")
    lines.append("아직 미완료 과제 리스트입니다!")
    return "\n".join(lines)


def save_checks(session: requests.Session, items: list, check_date: str = "") -> dict:
    resp = session.post(
        BOOTCAMP_URL,
        params={"action": "check_bulk_save"},
        json={"check_date": check_date, "items": items},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise DashboardLoginError(data.get("error") or "저장에 실패했습니다.")
    return data


def fetch_reports(phone: str) -> dict:
    session, admin_info = login(phone)
    check_date = dashboard_check_date()
    status_board = fetch_status_board(
        session, admin_info["cohort_id"], check_date.isoformat(), admin_info["group_id"]
    )
    report = compute_report(status_board, check_date)
    overrides = db_client.get_display_name_overrides(admin_info["group_name"])
    missing_text = missing_list_text(admin_info["group_name"], check_date, report, overrides)
    tag_text = format_broadcast_tags(admin_info["group_name"], check_date, report, overrides)
    return {
        "admin": admin_info,
        "check_date": check_date.isoformat(),
        "missing_text": missing_text,
        "tag_text": tag_text,
        "report": report,
        "overrides": overrides,
    }
