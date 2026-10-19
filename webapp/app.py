import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, request, redirect, render_template, flash, session, url_for, jsonify

from src import (
    config, db_client, leader_dashboard_client, hamummal_dashboard, hamummal_parser, hamummal_matcher,
)
from src.time_window import TZ, format_kst_timestamp, now_kst

# 진행 시간이 날짜별로 따로 저장돼 있지 않은 날짜에 쓰는 기본값
# (관리자가 한 번도 '진행 시간 저장'을 누르지 않았어도 분석이 건너뛰어지지 않도록).
DEFAULT_HAMUMMAL_START = "13:30"
DEFAULT_HAMUMMAL_END = "14:01"

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)


@app.template_filter("hname")
def _hamummal_display_name(m):
    # 하멈말 화면 전체에서 조원 이름 표시 형식을 하나로 통일한다: "실명_닉네임(N회)"
    # (카카오톡에서 실제로 쓰는 표기와 같은 형식이라 알아보기 쉽다).
    if not m:
        return ""
    real_name = m.get("real_name") or "이름미상"
    nickname = m.get("nickname")
    base = f"{real_name}_{nickname}" if nickname else real_name
    round_count = m.get("round_count")
    return f"{base}({round_count}회)" if round_count else base


@app.route("/health")
def health():
    return "ok", 200


def _active_cohort_or_none():
    return db_client.get_active_cohort()


def _current_member_or_none(cohort=None):
    phone = session.get("phone")
    if not phone:
        return None
    if cohort is None:
        cohort = _active_cohort_or_none()
    return db_client.get_member_by_phone(phone, str(cohort.get("기수명")) if cohort else None)


@app.route("/", methods=["GET", "POST"])
def phone_entry():
    if request.method == "GET":
        if session.get("phone"):
            return redirect(url_for("dashboard"))
        return render_template("phone_entry.html")

    phone = db_client.normalize_phone(request.form.get("phone"))
    if not phone:
        flash("휴대폰 번호를 입력해주세요.")
        return redirect(url_for("phone_entry"))

    cohort = _active_cohort_or_none()
    if not cohort:
        flash("지금은 진행 중인 기수가 없어요. 기수 시작 후 다시 시도해주세요.")
        return redirect(url_for("phone_entry"))
    cohort_name = str(cohort.get("기수명"))

    existing = db_client.get_member_by_phone(phone, cohort_name)
    if existing:
        session.permanent = True
        session["phone"] = phone
        return redirect(url_for("dashboard"))

    try:
        _, admin_info = leader_dashboard_client.login(phone)
    except leader_dashboard_client.DashboardLoginError as e:
        flash(str(e))
        return redirect(url_for("phone_entry"))
    except Exception:
        app.logger.exception("로그인 중 현황판 서버 호출 실패")
        flash("현황판 서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.")
        return redirect(url_for("phone_entry"))

    db_client.create_member_signup(
        phone, admin_info["name"], admin_info["group_name"], admin_info["role"], cohort_name
    )
    try:
        fetched = leader_dashboard_client.fetch_reports(phone)
        db_client.set_cache_row(
            fetched["admin"]["group_name"], fetched["missing_text"], fetched["tag_text"]
        )
    except Exception:
        app.logger.exception("로그인 직후 현황판 캐시 갱신 실패")

    session.permanent = True
    session["phone"] = phone
    flash(f"{admin_info['name']}님, {admin_info['group_name']} · {admin_info['role']} 확인됐어요!", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    phone = session.get("phone")
    if not phone:
        return redirect(url_for("phone_entry"))
    cohort = _active_cohort_or_none()
    member = db_client.get_member_by_phone(phone, str(cohort.get("기수명")) if cohort else None)
    if not member:
        session.pop("phone", None)
        return redirect(url_for("phone_entry"))

    hamummal_visible = bool(cohort) and db_client.is_hamummal_review_visible(cohort)
    return render_template(
        "dashboard.html", member=member, hamummal_visible=hamummal_visible,
    )


@app.route("/dashboard/data")
def dashboard_data():
    phone = session.get("phone")
    if not phone:
        return jsonify(ok=False, redirect=url_for("phone_entry")), 401
    cohort = _active_cohort_or_none()
    member = db_client.get_member_by_phone(phone, str(cohort.get("기수명")) if cohort else None)
    if not member:
        session.pop("phone", None)
        return jsonify(ok=False, redirect=url_for("phone_entry")), 401

    team = member.get("조")

    try:
        fetched = leader_dashboard_client.fetch_reports(phone)
        # set_cache_row가 방금 쓴 갱신시각을 그대로 돌려주므로, 굳이 시트를 다시
        # 읽어서 확인할 필요가 없다 (구글시트 읽기 쿼터를 아끼기 위함).
        updated_at = db_client.set_cache_row(
            fetched["admin"]["group_name"], fetched["missing_text"], fetched["tag_text"]
        )
        return jsonify(
            ok=True,
            missing_sections=leader_dashboard_client.missing_report_sections(
                fetched["report"], fetched.get("overrides")
            ),
            tag_text=fetched["tag_text"],
            last_updated=format_kst_timestamp(updated_at),
        )
    except leader_dashboard_client.DashboardLoginError as e:
        error = str(e)
    except Exception:
        app.logger.exception("대시보드 데이터 갱신 중 현황판 서버 호출 실패")
        error = "현황판 서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요."

    cache_row = db_client.get_cache_row(team)
    fallback = None
    if cache_row:
        fallback = {
            "missing_text": cache_row.get("미제출텍스트"),
            "tag_text": cache_row.get("태그리스트"),
            "last_updated": format_kst_timestamp(cache_row.get("갱신시각")),
        }
    return jsonify(ok=False, error=error, fallback=fallback)


@app.route("/dashboard/rename", methods=["POST"])
def dashboard_rename():
    phone = session.get("phone")
    if not phone:
        return jsonify(ok=False, redirect=url_for("phone_entry")), 401
    cohort = _active_cohort_or_none()
    member = db_client.get_member_by_phone(phone, str(cohort.get("기수명")) if cohort else None)
    if not member:
        session.pop("phone", None)
        return jsonify(ok=False, redirect=url_for("phone_entry")), 401

    data = request.get_json(silent=True) or {}
    member_id = data.get("member_id")
    name = str(data.get("name") or "").strip()
    if not member_id:
        return jsonify(ok=False, error="member_id가 없습니다."), 400
    if len(name) > 30:
        return jsonify(ok=False, error="이름이 너무 길어요. 30자 이내로 입력해주세요."), 400

    db_client.set_display_name_override(member.get("조"), member_id, name)
    return jsonify(ok=True)


@app.route("/hamummal-upload/login", methods=["POST"])
def hamummal_upload_login():
    password = request.form.get("password")
    if password != config.UPLOAD_PASSWORD:
        flash("비밀번호가 틀렸습니다.")
        return redirect(url_for("hamummal_upload"))
    session["hamummal_admin"] = True
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload/toggle-visibility", methods=["POST"])
def hamummal_upload_toggle_visibility():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403
    want_visible = request.form.get("visible") == "1"
    db_client.set_hamummal_review_visible(str(cohort.get("기수명")), want_visible)
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload/toggle-apply", methods=["POST"])
def hamummal_upload_toggle_apply():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403
    want_enabled = request.form.get("enabled") == "1"
    db_client.set_hamummal_apply_enabled(str(cohort.get("기수명")), want_enabled)
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload/set-windows", methods=["POST"])
def hamummal_upload_set_windows():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403
    cohort_name = str(cohort.get("기수명"))
    saved = 0
    for key in request.form:
        if not key.startswith("start__"):
            continue
        date_str = key[len("start__"):]
        start_time = request.form.get(f"start__{date_str}")
        end_time = request.form.get(f"end__{date_str}")
        if start_time and end_time:
            db_client.set_hamummal_date_window(cohort_name, date_str, start_time, end_time)
            saved += 1
    flash(f"{saved}개 날짜의 진행 시간을 저장했어요.", "success")
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload", methods=["GET", "POST"])
def hamummal_upload():
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403

    if request.method == "POST":
        if not session.get("hamummal_admin"):
            return "관리자 인증이 필요합니다.", 403

        cohort_name = str(cohort.get("기수명"))
        f = request.files["chat_file"]

        # 업로드 폼에 있는 "오늘 진행 시간"을 올리기 직전에 바로 고칠 수 있게
        # 해뒀으니, 분석 전에 먼저 그 값으로 오늘 날짜 설정을 저장해둔다.
        today_str = now_kst().date().isoformat()
        today_start_time = request.form.get("today_start_time")
        today_end_time = request.form.get("today_end_time")
        if today_start_time and today_end_time:
            db_client.set_hamummal_date_window(cohort_name, today_str, today_start_time, today_end_time)

        # 업로드 파일은 이 요청 안에서 분석하고 바로 버리면 되므로, 요청 간
        # 유지가 보장되지 않는 임시 디렉토리(/tmp 등)에 저장한다.
        tmp_fd, save_path = tempfile.mkstemp(suffix=f"_{f.filename}")
        os.close(tmp_fd)
        f.save(save_path)

        try:
            candidate_dates = hamummal_parser.detect_candidate_dates(str(save_path))
            date_windows = db_client.get_hamummal_date_windows(cohort_name)
            processed = []
            for date_str in candidate_dates:
                # 이 날짜에 명시적으로 저장된 진행 시간이 없으면 기본값(13:30~14:01)으로
                # 분석한다 — 화면에 기본값이 미리 채워져 있어도 "저장" 버튼을 따로
                # 누르지 않으면 DB에는 그 값이 없어서, 예전에는 여기서 건너뛰고 있었다.
                win = date_windows.get(date_str) or {
                    "start": DEFAULT_HAMUMMAL_START, "end": DEFAULT_HAMUMMAL_END,
                }
                y, mo, d = map(int, date_str.split("-"))
                sh, sm = map(int, win["start"].split(":"))
                eh, em = map(int, win["end"].split(":"))
                window_start = TZ.localize(datetime(y, mo, d, sh, sm))
                window_end = TZ.localize(datetime(y, mo, d, eh, em))
                computed = hamummal_dashboard.compute_review(str(save_path), window_start, window_end, date_str)
                db_client.save_review(
                    computed["teams"], date_str, f"{win['start']}~{win['end']}",
                    submitter_count=computed["submitter_count"],
                    chat_senders=computed["chat_senders"],
                    all_chat_senders=computed["all_chat_senders"],
                )
                processed.append(date_str)
        finally:
            try:
                os.remove(save_path)
            except OSError:
                pass

        if processed:
            flash(f"{len(processed)}개 날짜 분석 완료: {', '.join(processed)}", "success")
        else:
            flash("음성메시지를 20명 이상 보낸 날짜를 찾지 못했어요.")
        return redirect(url_for("hamummal_upload"))

    cohort_name = str(cohort.get("기수명"))
    today_str = now_kst().date().isoformat()
    existing_windows = db_client.get_hamummal_date_windows(cohort_name)
    default_start, default_end = DEFAULT_HAMUMMAL_START, DEFAULT_HAMUMMAL_END

    # "시작일"을 따로 정해두지 않아도, 실제로 분석된(=하멈말이 진행된) 날짜들을
    # 기준으로 바로 진행 시간을 설정할 수 있게 한다 (list_review_dates가 최근
    # 날짜부터 내림차순으로 돌려준다).
    date_window_dates = []
    for row in db_client.list_review_dates():
        date_str = row["check_date"]
        win = existing_windows.get(date_str) or {}
        date_window_dates.append({
            "date": date_str,
            "start": win.get("start") or default_start,
            "end": win.get("end") or default_end,
        })
    today_window = existing_windows.get(today_str) or {}

    return render_template(
        "hamummal_upload.html",
        is_admin=bool(session.get("hamummal_admin")),
        hamummal_review_visible=db_client.is_hamummal_review_visible(cohort),
        hamummal_apply_enabled=db_client.is_hamummal_apply_enabled(cohort),
        date_window_dates=date_window_dates,
        has_reviews=db_client.has_any_review(),
        today_str=today_str,
        today_start_time=today_window.get("start") or default_start,
        today_end_time=today_window.get("end") or default_end,
    )


@app.route("/hamummal-review")
def hamummal_review_dates():
    member = _current_member_or_none()
    if not member:
        return redirect(url_for("phone_entry"))
    dates = db_client.list_review_dates()
    return render_template(
        "hamummal_review_dates.html", dates=dates, today_str=now_kst().date().isoformat(),
        team=str(member.get("조")),
    )


def _team_attendance(team: str):
    """
    팀의 지금까지 분석된 날짜들을 기준으로, 지금 조원 명단(가장 최근 날짜
    기준)에 있는 사람별 출석율을 계산한다. "출석"은 그 날짜에 제출자(matched)로
    잡혔는지를 기준으로 하고, 분모는 그 조원이 실제로 명단에 있었던 날짜
    수만 센다 — 중간에 들어온 조원이 들어오기 전 날짜까지 미제출로 깎이지
    않도록.
    """
    rows = db_client.list_team_reviews(team)
    rows = [r for r in rows if not ((r.get("review") or {}).get("error"))]

    stats = {}
    for row in rows:
        entry = row.get("review") or {}
        matched_today = set()
        for m in entry.get("matched") or []:
            mid = str(m.get("id"))
            matched_today.add(mid)
            st = stats.setdefault(mid, {"total": 0, "matched": 0, "member": m})
            st["total"] += 1
            st["matched"] += 1
            st["member"] = m
        for m in entry.get("unmatched_members") or []:
            mid = str(m.get("id"))
            if mid in matched_today:
                continue
            st = stats.setdefault(mid, {"total": 0, "matched": 0, "member": m})
            st["total"] += 1
            st["member"] = m

    latest_entry = (rows[-1].get("review") or {}) if rows else {}
    latest_ids = {str(m.get("id")) for m in (latest_entry.get("matched") or [])} | \
        {str(m.get("id")) for m in (latest_entry.get("unmatched_members") or [])}

    people = []
    for mid in latest_ids:
        st = stats.get(mid)
        if not st or not st["total"]:
            continue
        rate = round(st["matched"] / st["total"] * 100)
        people.append({"member": st["member"], "total": st["total"], "matched": st["matched"], "rate": rate})
    people.sort(key=lambda p: (p["rate"], p["member"].get("real_name") or ""))

    return people, len(rows)


@app.route("/hamummal-review/attendance")
def hamummal_review_attendance():
    member = _current_member_or_none()
    if not member:
        return redirect(url_for("phone_entry"))
    my_team = str(member.get("조"))
    people, total_dates = _team_attendance(my_team)
    return render_template(
        "hamummal_attendance.html", team=my_team, people=people, total_dates=total_dates,
    )


def _current_team_roster(team: str):
    """
    "조원 이름 일괄 매칭" 팝업에서 쓸 지금 조원 명단. 아직 한 번도 메시지를
    안 보낸 사람도 매칭 대상에 넣어야 하므로, 가장 최근 날짜 리뷰의
    제출자+미제출자를 합쳐서 돌려준다(둘을 합치면 그 날짜의 전체 조원 명단과
    같다).
    """
    rows = db_client.list_team_reviews(team)
    rows = [r for r in rows if not ((r.get("review") or {}).get("error"))]
    if not rows:
        return []
    latest_entry = rows[-1].get("review") or {}
    roster = {}
    for m in (latest_entry.get("matched") or []) + (latest_entry.get("unmatched_members") or []):
        roster[str(m.get("id"))] = m
    return list(roster.values())


def _register_display_name_override(team: str, member_id, raw: str):
    db_client.set_display_name_override(team, member_id, raw)
    # None을 review_id로 넘기면 "건너뛸 현재 리뷰"가 없는 셈이라, 이 조의
    # 모든 날짜를 다 검사해서 이미 그 이름으로 보낸 메시지가 있던 날짜가
    # 있으면 그 자리에서 바로 제출완료로 바꿔준다.
    return _retroactively_apply_other_dates(None, team, member_id, raw)


@app.route("/hamummal-review/bulk-match-names", methods=["POST"])
def hamummal_review_bulk_match_names():
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "")
    raw_names = [n.strip() for n in re.split(r"[,\n]+", text) if n.strip()]
    if not raw_names:
        return jsonify(ok=False, error="이름을 입력해주세요."), 400

    dashboard_members = _current_team_roster(my_team)
    if not dashboard_members:
        return jsonify(ok=False, error="아직 분석된 날짜가 없어요. 먼저 하멈말 파일을 업로드해주세요."), 404

    match = hamummal_matcher.match_submitters(raw_names, dashboard_members)

    matched_out = []
    consumed_raws = set()
    for m in match["matched"]:
        raw = m.get("_matched_raw")
        consumed_raws.add(raw)
        fixed_dates = _register_display_name_override(my_team, m["id"], raw)
        matched_out.append({
            "raw": raw, "member_id": m["id"], "display": _hamummal_display_name(m),
            "fixed_dates": sorted(fixed_dates),
        })

    ambiguous_out = []
    for amb in match["ambiguous"]:
        consumed_raws.add(amb["raw"])
        ambiguous_out.append({
            "raw": amb["raw"],
            "candidates": [
                {"member_id": c["id"], "display": _hamummal_display_name(c)} for c in amb["candidates"]
            ],
        })

    no_match = [r for r in raw_names if r not in consumed_raws]

    return jsonify(ok=True, matched=matched_out, ambiguous=ambiguous_out, no_match=no_match)


@app.route("/hamummal-review/bulk-match-resolve", methods=["POST"])
def hamummal_review_bulk_match_resolve():
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    payload = request.get_json(silent=True) or {}
    team = payload.get("team")
    member_id = payload.get("member_id")
    raw = str(payload.get("raw") or "").strip()
    if not (team and member_id and raw):
        return jsonify(ok=False, error="필수 값이 없습니다."), 400
    if str(team) != my_team:
        return jsonify(ok=False, error="본인 조만 수정할 수 있어요."), 403

    fixed_dates = _register_display_name_override(team, member_id, raw)
    return jsonify(ok=True, fixed_dates=sorted(fixed_dates))


@app.route("/hamummal-review/<review_id>")
def hamummal_review(review_id):
    cohort = _active_cohort_or_none()
    member = _current_member_or_none(cohort)
    if not member:
        return redirect(url_for("phone_entry"))
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return "존재하지 않거나 만료된 검토입니다. 다시 업로드해주세요.", 404

    full_review = data["review"]
    if my_team not in full_review:
        return f"이 검토에는 '{my_team}'의 데이터가 없어요. 조장/부조장 번호가 시트에 등록돼 있는지 확인해주세요.", 404

    entry = full_review[my_team]

    # 안전장치: 어떤 경로로든 같은 조원이 제출자/미제출자 양쪽에 동시에 들어가
    # 있거나, 이미 확정된 사람(raw)과 같은 메시지를 놓고 "확인 필요"가 남아
    # 있으면(정상이라면 있을 수 없지만, 과거 수정 과정에서 생긴 데이터가 남아있을
    # 수 있음) 정리해서 그 상태로 저장해두고, 다음부터는 다시 안 보이게 한다.
    if _purge_resolved_ambiguous(entry):
        try:
            db_client.update_review_team_entry(review_id, my_team, entry)
        except Exception:
            pass  # 화면엔 바로 반영하고, 저장은 실패해도 다음에 다시 시도하면 됨

    team_applied = (data.get("applied_teams") or {}).get(my_team) or {}
    is_today = data["check_date"] == now_kst().date().isoformat()
    y, mo, d = map(int, data["check_date"].split("-"))
    check_date_label = f"{mo}월 {d}일자"
    window_desc_label = (data["window_desc"] or "").replace("~", " ~ ")

    # "이름 고치기" 팝업 검색용 — 날짜 상관없이 업로드 파일 전체에서 제출한
    # 적 있는 사람 전체 목록. 매칭 판정(그 날짜에 정말 보냈는지)에는 이게 아니라
    # 날짜별 chat_senders(entry 저장 시점의 그 날짜분)만 쓴다 — confirm-match/
    # fix-name 라우트가 리뷰를 다시 불러와서 그쪽에서 직접 꺼내 쓴다.
    all_chat_senders = data.get("all_chat_senders") or []
    matched_raw_names = [
        m.get("_matched_raw") for m in (entry.get("matched") or [])
        if m.get("_matched_raw")
    ]
    # 미제출 목록에서, 전에 "이름 고치기"로 등록해둔 적은 있지만 이 날짜엔 그
    # 이름으로 보낸 메시지를 못 찾은 사람에게 등록된 이름을 같이 보여주기 위함.
    display_name_overrides = db_client.get_display_name_overrides(my_team)

    return render_template(
        "hamummal_review.html",
        review_id=review_id,
        check_date=data["check_date"],
        check_date_label=check_date_label,
        window_desc=data["window_desc"],
        window_desc_label=window_desc_label,
        review={my_team: entry},
        submitter_count=data.get("submitter_count", 0),
        chat_senders=all_chat_senders,
        matched_raw_names=matched_raw_names,
        display_name_overrides=display_name_overrides,
        apply_enabled=bool(cohort) and db_client.is_hamummal_apply_enabled(cohort),
        applied=bool(team_applied),
        applied_at=format_kst_timestamp(team_applied.get("applied_at")),
        is_today=is_today,
    )


def _purge_resolved_ambiguous(entry: dict) -> bool:
    """
    카카오톡 원문 메시지(raw) 하나는 결국 한 사람만 보낸 거라서, 그 raw가 이미
    누군가에게 확정 매칭됐으면 "이 raw, 혹시 이 사람 아니에요?"라고 묻는 다른
    확인필요 카드는 더 이상 의미가 없다 — 지워야 새로고침해도 다시 안 뜬다.
    (예: 닉네임이 겹치는 두 사람이 있을 때 한쪽을 확정해도, 같은 raw를 놓고
    다른 쪽을 제안하는 카드가 예전엔 안 지워지고 남아있었다.)
    같은 조원이 제출자/미제출자 양쪽에 동시에 들어있는 것도 같이 정리한다.
    entry를 제자리에서 고치고, 뭔가 바뀌었으면 True를 돌려준다.
    """
    matched_ids = {str(x.get("id")) for x in (entry.get("matched") or [])}
    matched_raws = {
        m.get("_matched_raw") for m in (entry.get("matched") or []) if m.get("_matched_raw")
    }
    changed = False

    clean_unmatched = [
        m for m in (entry.get("unmatched_members") or []) if str(m.get("id")) not in matched_ids
    ]
    if len(clean_unmatched) != len(entry.get("unmatched_members") or []):
        entry["unmatched_members"] = clean_unmatched
        changed = True

    clean_ambiguous = []
    for amb in entry.get("ambiguous") or []:
        if amb.get("raw") in matched_raws:
            changed = True
            continue  # 이 raw는 이미 다른 후보로 확정됐음 — 카드 전체를 치운다
        candidates = amb.get("candidates") or []
        kept = [c for c in candidates if str(c.get("id")) not in matched_ids]
        if len(kept) != len(candidates):
            changed = True
        amb["candidates"] = kept
        if kept:
            clean_ambiguous.append(amb)
    if changed:
        entry["ambiguous"] = clean_ambiguous
    elif len(clean_ambiguous) != len(entry.get("ambiguous") or []):
        entry["ambiguous"] = clean_ambiguous
        changed = True

    return changed


def _sync_if_applied(data: dict, team: str, member_id, status: bool):
    """
    이미 "반영하기"를 눌러서 소리튠 조장페이지에 체크가 올라간 뒤에 조원 한
    명의 제출/미제출 상태가 바뀌면, 전체를 다시 반영할 필요 없이 그 한 명만
    바로 소리튠에 다시 올린다. 아직 한 번도 반영 전이면 손댈 게 없으므로
    (처음 반영할 때 한꺼번에 올라감) None을 돌려준다.
    에러가 나도 목록(리뷰 데이터)은 이미 저장됐으니, 에러 메시지만 돌려주고
    호출한 쪽에서 사용자에게 보여주게 한다.
    """
    if not (data.get("applied_teams") or {}).get(team):
        return None
    entry = (data.get("review") or {}).get(team) or {}
    phone = entry.get("_phone")
    if not phone:
        return "이미 반영된 날짜인데 로그인 정보가 없어서 소리튠에 바로 반영하지 못했어요. 목록은 고쳐졌어요."
    try:
        session, _ = leader_dashboard_client.login(phone)
        leader_dashboard_client.save_checks(
            session,
            [{
                "member_id": member_id,
                "mission_type_code": "hamemmal",
                "check_date": data["check_date"],
                "status": status,
            }],
            check_date=data["check_date"],
        )
        return None
    except Exception as e:
        return f"목록은 고쳤는데 소리튠에 바로 반영하지는 못했어요: {e}"


def _retroactively_apply_other_dates(review_id, team, member_id, display_name):
    try:
        other_rows = db_client.list_team_reviews(team)
    except Exception:
        return []

    fixed_dates = []
    for row in other_rows:
        if str(row["id"]) == str(review_id):
            continue  # 지금 보고 있는 날짜는 호출한 쪽에서 이미 처리함

        if (row.get("applied_teams") or {}).get(team):
            continue  # 이미 반영된 날짜는 건드리지 않음

        row_entry = (row.get("review") or {}).get(team)
        if not row_entry:
            continue

        row_target = None
        for m in row_entry.get("unmatched_members") or []:
            if str(m.get("id")) == str(member_id):
                row_target = m
                break
        if row_target is None:
            for amb in row_entry.get("ambiguous") or []:
                for c in amb.get("candidates") or []:
                    if str(c.get("id")) == str(member_id):
                        row_target = c
                        break
                if row_target is not None:
                    break
        if row_target is None:
            continue  # 이 날짜엔 애초에 "미제출"/"확인필요"로 남아있지 않음

        row_matched_raw = hamummal_matcher.find_chat_sender_match(display_name, row.get("chat_senders") or [])
        if row_matched_raw is None:
            continue  # 이 날짜엔 이 이름으로 보낸 메시지가 없음 — 진짜 미제출일 수 있으니 안 건드림

        row_entry["unmatched_members"] = [
            m for m in row_entry.get("unmatched_members") or [] if str(m.get("id")) != str(member_id)
        ]
        row_cleaned_ambiguous = []
        for amb in row_entry.get("ambiguous") or []:
            kept = [c for c in amb.get("candidates") or [] if str(c.get("id")) != str(member_id)]
            if kept:
                amb["candidates"] = kept
                row_cleaned_ambiguous.append(amb)
        row_entry["ambiguous"] = row_cleaned_ambiguous

        row_matched_ids = {str(x.get("id")) for x in (row_entry.get("matched") or [])}
        if str(member_id) not in row_matched_ids:
            row_target["_matched_raw"] = row_matched_raw
            row_entry.setdefault("matched", []).append(row_target)
        _purge_resolved_ambiguous(row_entry)

        try:
            db_client.update_review_team_entry(row["id"], team, row_entry)
        except Exception:
            continue  # 한 날짜 저장에 실패해도 나머지 날짜는 계속 시도

        fixed_dates.append(row["check_date"])

    return fixed_dates


@app.route("/hamummal-review/<review_id>/confirm-match", methods=["POST"])
def hamummal_review_confirm_match(review_id):
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return jsonify(ok=False, error="존재하지 않거나 만료된 검토입니다."), 404

    payload = request.get_json(silent=True) or {}
    team = payload.get("team")
    member_id = payload.get("member_id")
    raw = payload.get("raw")
    if not (team and member_id and raw):
        return jsonify(ok=False, error="필수 값이 없습니다."), 400
    if str(team) != my_team:
        return jsonify(ok=False, error="본인 조의 검토만 수정할 수 있어요."), 403

    member_row = {
        "id": member_id,
        "nickname": payload.get("nickname"),
        "real_name": payload.get("real_name"),
    }
    try:
        db_client.add_confirmed_match(team, member_row, raw)
    except Exception as e:
        return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500

    # 이 검토 자체에도 바로 반영해둔다 — 그래야 새로고침해도 유지되고, 미제출
    # 목록에 같은 조원이 동시에 남아있는 일도 없다 (제출자/미제출자 중복 방지).
    entry = (data.get("review") or {}).get(team)
    if entry:
        target = None
        for m in entry.get("unmatched_members") or []:
            if str(m.get("id")) == str(member_id):
                target = m
                break
        if target is not None:
            target["_matched_raw"] = raw
            entry["unmatched_members"] = [
                m for m in entry.get("unmatched_members") or [] if str(m.get("id")) != str(member_id)
            ]
            matched_ids = {str(x.get("id")) for x in (entry.get("matched") or [])}
            if str(member_id) not in matched_ids:
                entry.setdefault("matched", []).append(target)
            _purge_resolved_ambiguous(entry)
            try:
                db_client.update_review_team_entry(review_id, team, entry)
            except Exception:
                pass  # confirmed_matches 저장은 이미 끝났으니 여기서 실패해도 치명적이진 않음

    sync_error = _sync_if_applied(data, team, member_id, True)
    fixed_dates = _retroactively_apply_other_dates(review_id, team, member_id, raw)

    return jsonify(ok=True, fixed_dates=sorted(fixed_dates), sync_error=sync_error)


@app.route("/hamummal-review/<review_id>/unconfirm-match", methods=["POST"])
def hamummal_review_unconfirm_match(review_id):
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    payload = request.get_json(silent=True) or {}
    team = payload.get("team")
    member_id = payload.get("member_id")
    raw = payload.get("raw")
    if not (team and member_id and raw):
        return jsonify(ok=False, error="필수 값이 없습니다."), 400
    if str(team) != my_team:
        return jsonify(ok=False, error="본인 조의 검토만 수정할 수 있어요."), 403
    try:
        db_client.remove_confirmed_match(team, member_id, raw)
    except Exception as e:
        return jsonify(ok=False, error=f"삭제하지 못했습니다: {e}"), 500
    return jsonify(ok=True)


@app.route("/hamummal-review/<review_id>/fix-name", methods=["POST"])
def hamummal_review_fix_name(review_id):
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return jsonify(ok=False, error="존재하지 않거나 만료된 검토입니다."), 404

    payload = request.get_json(silent=True) or {}
    team = payload.get("team")
    member_id = payload.get("member_id")
    display_name = str(payload.get("display_name") or "").strip()
    if not (team and member_id and display_name):
        return jsonify(ok=False, error="필수 값이 없습니다."), 400
    if str(team) != my_team:
        return jsonify(ok=False, error="본인 조의 검토만 수정할 수 있어요."), 403

    full_review = data["review"]
    entry = full_review.get(team)
    if not entry:
        return jsonify(ok=False, error="이 검토에는 해당 조 데이터가 없어요."), 404

    # 미제출 목록뿐 아니라, 이미 제출자로 잡힌 사람의 카카오 이름이 잘못
    # 매칭됐을 때도 같은 팝업으로 고칠 수 있어야 해서 양쪽 다 찾아본다.
    target = None
    found_in = None
    for m in entry.get("unmatched_members") or []:
        if str(m.get("id")) == str(member_id):
            target = m
            found_in = "unmatched"
            break
    if target is None:
        for m in entry.get("matched") or []:
            if str(m.get("id")) == str(member_id):
                target = m
                found_in = "matched"
                break
    if target is None:
        for amb in entry.get("ambiguous") or []:
            for c in amb.get("candidates") or []:
                if str(c.get("id")) == str(member_id):
                    target = c
                    found_in = "ambiguous"
                    break
            if target is not None:
                break
    if target is None:
        return jsonify(ok=False, error="이 조에서 해당 조원을 찾지 못했어요."), 404

    try:
        # 카카오톡에서 실제로 쓰는 이름을 등록해두면, 다음 업로드부터는 자동으로
        # 매칭된다 (display_name_overrides — 기존 대시보드 "이름 수정" 기능과 같은 테이블).
        db_client.set_display_name_override(team, member_id, display_name)
    except Exception as e:
        return jsonify(ok=False, error=f"이름 저장 실패: {e}"), 500

    # 이름을 등록한다고 무조건 "제출함"으로 바뀌면 안 되고, 실제로 이 날짜
    # 대화에 그 이름(회차만 다르게 적힌 경우도 포함)으로 보낸 메시지가 있을
    # 때만 제출자 목록으로 옮긴다. 이미 제출자였던 사람은(found_in=="matched")
    # 그 상태를 유지하면서 표시용 원문 이름만 고쳐준다.
    sync_error = None
    matched_raw = hamummal_matcher.find_chat_sender_match(display_name, data.get("chat_senders") or [])
    if found_in == "matched":
        current_matched = matched_raw is not None
        if current_matched:
            target["_matched_raw"] = matched_raw
        else:
            # 고친 이름으로는 이 날짜에 보낸 메시지를 찾지 못했다 — 지금까지의
            # 매칭이 잘못됐던 것이므로 제출자 목록에서 빼서 미제출로 내려보낸다.
            target.pop("_matched_raw", None)
            entry["matched"] = [
                m for m in entry.get("matched") or [] if str(m.get("id")) != str(member_id)
            ]
            entry.setdefault("unmatched_members", []).append(target)
            sync_error = _sync_if_applied(data, team, member_id, False)
        try:
            db_client.update_review_team_entry(review_id, team, entry)
        except Exception as e:
            return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500
    else:
        current_matched = matched_raw is not None
        if current_matched:
            target["_matched_raw"] = matched_raw
            entry["unmatched_members"] = [
                m for m in entry.get("unmatched_members") or [] if str(m.get("id")) != str(member_id)
            ]
            entry.setdefault("matched", []).append(target)
            _purge_resolved_ambiguous(entry)
            try:
                db_client.update_review_team_entry(review_id, team, entry)
            except Exception as e:
                return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500
            sync_error = _sync_if_applied(data, team, member_id, True)

    # 한 번의 업로드로 여러 날짜가 한꺼번에 분석되는 경우가 많아서(업로드 1건 =
    # 검토 row 여러 개), 이 조원이 "이 조"의 다른 날짜에서도 아직 미제출/확인필요로
    # 남아 있는데 그 날짜 대화에도 이 이름으로 보낸 메시지가 있으면 같이
    # 고쳐준다 — 다음 업로드를 기다리지 않고 이미 업로드한 파일 기준으로 바로
    # 반영되도록. (정말 그 날짜에 메시지가 없으면 그대로 미제출로 남는다.)
    fixed_dates = [data["check_date"]] if (found_in in ("unmatched", "ambiguous") and current_matched) else []
    fixed_dates.extend(_retroactively_apply_other_dates(review_id, team, member_id, display_name))

    return jsonify(
        ok=True,
        found_in=found_in,
        matched=current_matched,
        display_name=display_name,
        member=target,
        fixed_dates=sorted(fixed_dates),
        sync_error=sync_error,
    )


@app.route("/hamummal-review/<review_id>/toggle-submission", methods=["POST"])
def hamummal_review_toggle_submission(review_id):
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return jsonify(ok=False, error="존재하지 않거나 만료된 검토입니다."), 404

    payload = request.get_json(silent=True) or {}
    team = payload.get("team")
    member_id = payload.get("member_id")
    direction = payload.get("direction")  # "exclude"(제출자→미제출) | "include"(미제출→제출자)
    nickname = payload.get("nickname")
    real_name = payload.get("real_name")
    if not (team and member_id and direction in ("exclude", "include")):
        return jsonify(ok=False, error="필수 값이 없습니다."), 400
    if str(team) != my_team:
        return jsonify(ok=False, error="본인 조의 검토만 수정할 수 있어요."), 403

    entry = (data.get("review") or {}).get(team)
    if not entry:
        return jsonify(ok=False, error="이 검토에는 해당 조 데이터가 없어요."), 404

    if direction == "exclude":
        target = None
        for m in entry.get("matched") or []:
            if str(m.get("id")) == str(member_id):
                target = m
                break
        if target is None:
            return jsonify(ok=False, error="제출자 목록에서 해당 조원을 찾지 못했어요."), 404
        target = dict(target)
        target.pop("_matched_raw", None)
        entry["matched"] = [
            m for m in entry.get("matched") or [] if str(m.get("id")) != str(member_id)
        ]
        unmatched_ids = {str(m.get("id")) for m in entry.get("unmatched_members") or []}
        if str(member_id) not in unmatched_ids:
            entry.setdefault("unmatched_members", []).append(target)
        new_status = False
    else:
        target = None
        for m in entry.get("unmatched_members") or []:
            if str(m.get("id")) == str(member_id):
                target = m
                break
        if target is None:
            target = {"id": member_id, "nickname": nickname, "real_name": real_name}
        entry["unmatched_members"] = [
            m for m in entry.get("unmatched_members") or [] if str(m.get("id")) != str(member_id)
        ]
        target = dict(target)
        target.pop("_matched_raw", None)
        matched_ids = {str(x.get("id")) for x in (entry.get("matched") or [])}
        if str(member_id) not in matched_ids:
            entry.setdefault("matched", []).append(target)
        _purge_resolved_ambiguous(entry)
        new_status = True

    try:
        db_client.update_review_team_entry(review_id, team, entry)
    except Exception as e:
        return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500

    sync_error = _sync_if_applied(data, team, member_id, new_status)

    return jsonify(ok=True, sync_error=sync_error)


@app.route("/hamummal-review/<review_id>/apply", methods=["POST"])
def hamummal_review_apply(review_id):
    cohort = _active_cohort_or_none()
    if not (cohort and db_client.is_hamummal_apply_enabled(cohort)):
        return "지금은 반영 기능이 꺼져 있어요. 관리자(하멈말 업로드 페이지)에서 켜야 사용할 수 있어요.", 403
    member = _current_member_or_none(cohort)
    if not member:
        return redirect(url_for("phone_entry"))
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return "존재하지 않거나 만료된 검토입니다. 다시 업로드해주세요.", 404

    full_review = data["review"]
    if my_team not in full_review:
        return f"이 검토에는 '{my_team}'의 데이터가 없어요.", 404
    if (data.get("applied_teams") or {}).get(my_team):
        return "우리 조는 이미 반영됐어요. 추가로 반영할 게 있으면 다시 업로드해서 새로 검토해주세요.", 409

    check_date_str = data["check_date"]
    team = my_team
    entry = full_review[team]

    extra_ids = set()
    exclude_ids = set()
    for key in request.form:
        if key.startswith(f"extra__{team}__"):
            extra_ids.add(key.split("__")[-1])
        elif key.startswith(f"exclude__{team}__"):
            exclude_ids.add(key.split("__")[-1])

    matched_ids_so_far = {str(m.get("id")) for m in entry.get("matched") or []}

    for amb in entry.get("ambiguous") or []:
        for cand in amb.get("candidates") or []:
            if str(cand["id"]) in extra_ids and str(cand["id"]) not in matched_ids_so_far:
                entry["matched"].append(cand)
                matched_ids_so_far.add(str(cand["id"]))
                try:
                    db_client.add_confirmed_match(team, cand, amb.get("raw"))
                except Exception:
                    pass

    # 미제출 추정 목록에서 "사실 제출했어요"로 수동 표시한 사람도 반영 대상에 포함
    for m in entry.get("unmatched_members") or []:
        if str(m["id"]) in extra_ids and str(m["id"]) not in matched_ids_so_far:
            entry["matched"].append(m)
            matched_ids_so_far.add(str(m["id"]))

    if exclude_ids:
        entry["matched"] = [
            m for m in (entry.get("matched") or []) if str(m.get("id")) not in exclude_ids
        ]

    if extra_ids or exclude_ids:
        _purge_resolved_ambiguous(entry)
        try:
            db_client.update_review_team_entry(review_id, team, entry)
        except Exception:
            pass  # 명단 반영 자체는 계속 진행 — 여기서 실패해도 치명적이진 않음

    apply_results = hamummal_dashboard.apply_review({team: entry}, check_date_str)
    db_client.mark_review_team_applied(review_id, team, apply_results)

    return render_template("hamummal_done.html", results=apply_results)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
