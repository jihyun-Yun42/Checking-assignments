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


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("phone", None)
    flash("로그아웃됐어요.", "success")
    return redirect(url_for("phone_entry"))


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


@app.route("/hamummal-upload/reset-all-reviews", methods=["POST"])
def hamummal_upload_reset_all_reviews():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    n = db_client.delete_all_hamummal_reviews()
    flash(f"하멈말 분석 데이터 {n}건을 전부 지웠어요.", "success")
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload/reset-matches", methods=["POST"])
def hamummal_upload_reset_matches():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    n_override, n_confirmed = db_client.delete_all_hamummal_matches()
    flash(f"조원 매칭 정보를 초기화했어요. (수동 등록 {n_override}건, 확인 완료 {n_confirmed}건)", "success")
    return redirect(url_for("hamummal_upload"))


@app.route("/hamummal-upload/delete-date", methods=["POST"])
def hamummal_upload_delete_date():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    date_str = request.form.get("date")
    if not date_str:
        flash("삭제할 날짜가 없습니다.")
        return redirect(url_for("hamummal_upload"))
    n = db_client.delete_hamummal_review_date(date_str)
    if n:
        flash(f"{date_str} 날짜의 분석 결과를 지웠어요.", "success")
    else:
        flash(f"{date_str} 날짜의 분석 결과를 찾지 못했어요.")
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


@app.route("/hamummal-review/bulk-match-page")
def hamummal_review_bulk_match_page():
    # "조원 이름 일괄 매칭"은 내용이 많아져서(붙여넣기, 확인 필요, 현황판 등)
    # 작은 팝업보다 자기 페이지로 보여주는 게 낫다.
    member = _current_member_or_none()
    if not member:
        return redirect(url_for("phone_entry"))
    return render_template("hamummal_bulk_match.html", team=str(member.get("조")))


def _team_attendance(team: str):
    """
    팀의 지금까지 분석된 날짜들을 기준으로, 지금 조원 명단(가장 최근 날짜
    기준)에 있는 사람별 출석율을 계산한다. "출석"은 그 날짜에 제출자(matched)로
    잡혔는지를 기준으로 하고, 분모는 분석된 날짜 수 전체다(조원은 중간에
    들어오는 일이 없으므로 다들 같은 분모를 쓴다).
    """
    rows = db_client.list_team_reviews(team)
    rows = [r for r in rows if not ((r.get("review") or {}).get(team) or {}).get("error")]
    total_dates = len(rows)

    stats = {}
    for row in rows:
        entry = (row.get("review") or {}).get(team) or {}
        for m in entry.get("matched") or []:
            mid = str(m.get("id"))
            st = stats.setdefault(mid, {"matched": 0, "member": m})
            st["matched"] += 1
            st["member"] = m
        for m in entry.get("unmatched_members") or []:
            mid = str(m.get("id"))
            st = stats.setdefault(mid, {"matched": 0, "member": m})
            st["member"] = m

    latest_entry = ((rows[-1].get("review") or {}).get(team) or {}) if rows else {}
    latest_ids = {str(m.get("id")) for m in (latest_entry.get("matched") or [])} | \
        {str(m.get("id")) for m in (latest_entry.get("unmatched_members") or [])}

    people = []
    for mid in latest_ids:
        st = stats.get(mid)
        if not st or not total_dates:
            continue
        rate = round(st["matched"] / total_dates * 100)
        people.append({"member": st["member"], "total": total_dates, "matched": st["matched"], "rate": rate})
    people.sort(key=lambda p: (-p["rate"], p["member"].get("real_name") or ""))

    return people, total_dates


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
    "조원 이름 일괄 매칭" 팝업에서 쓸 지금 조원 명단. 하멈말 파일을 분석한
    기록이 있는지와는 상관없이, 소리튠 조장페이지에 지금 등록돼 있는 조원
    명단을 그 자리에서 바로 불러온다(아직 한 번도 소통방에 메시지를 안 보낸
    사람도 매칭 대상으로 포함하기 위해서다).
    """
    members = (db_client.active_members_by_team() or {}).get(str(team)) or []
    leader_like = [
        m for m in members
        if m.get("역할") in ("조장", "부조장") and m.get("휴대폰번호")
    ]
    for m in leader_like:
        phone = m.get("휴대폰번호")
        try:
            session, admin_info = leader_dashboard_client.login(phone)
        except Exception:
            continue
        try:
            board = leader_dashboard_client.fetch_status_board(
                session, admin_info["cohort_id"], now_kst().date().isoformat(), admin_info["group_id"],
            )
        except Exception:
            continue
        return board.get("members") or []
    return []


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
        return jsonify(ok=False, error="조원 명단을 불러오지 못했어요. 조장/부조장 번호가 소리튠에 등록돼 있는지 확인해주세요."), 404

    match = hamummal_matcher.match_submitters(raw_names, dashboard_members)

    matched_out = []
    consumed_raws = set()
    for m in match["matched"]:
        raw = m.get("_matched_raw")
        consumed_raws.add(raw)
        db_client.set_display_name_override(my_team, m["id"], raw)
        matched_out.append({
            "raw": raw, "member_id": m["id"], "display": _hamummal_display_name(m),
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

    # 자동으로 못 찾은 이름을 직접 골라서 매칭할 수 있도록, 지금 조원 명단
    # 전체를 같이 내려준다(화면에서 "매칭 안 됨" 항목을 누르면 이 목록에서
    # 고를 수 있게).
    roster_out = sorted(
        (
            {"member_id": m["id"], "display": _hamummal_display_name(m)}
            for m in dashboard_members
        ),
        key=lambda r: r["display"],
    )

    return jsonify(
        ok=True, matched=matched_out, ambiguous=ambiguous_out, no_match=no_match, roster=roster_out,
    )


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

    db_client.set_display_name_override(team, member_id, raw)
    fixed_dates = _retroactively_apply_other_dates(None, team, member_id, raw)
    return jsonify(ok=True, fixed_dates=sorted(fixed_dates))


@app.route("/hamummal-review/roster-status")
def hamummal_review_roster_status():
    # "조원 이름 일괄 매칭" 팝업 밑에, 지금까지 누가 이미 매칭됐고 누가 아직
    # 안 됐는지 한눈에 보여주기 위한 목록. 매칭 안 된 사람은 여기서 바로
    # 카톡 이름을 입력해서 하나씩 매칭할 수도 있게 한다.
    member = _current_member_or_none()
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    roster = _current_team_roster(my_team)
    overrides = db_client.get_display_name_overrides(my_team)

    # override로 등록한 적 없어도, 실제 카톡 대화로 이미 자동 매칭된 사람이
    # 많다(이름/닉네임이 카톡 표시이름과 자연스럽게 겹치는 경우) — 그런 사람도
    # "아직 매칭 안 됨"으로 잘못 분류되지 않도록, 가장 최근 분석된 날짜의
    # 제출자 목록도 같이 확인한다.
    # rows는 날짜 오름차순이라, 순서대로 덮어쓰면 사람별로 가장 최근 날짜의
    # 자동 매칭 원문이 남는다 — 최근 분석에서 안 보인(그날 미제출인) 사람도
    # 예전에 자동으로 식별된 적 있으면 그 값을 쓴다.
    auto_matched = {}
    rows = db_client.list_team_reviews(my_team)
    for row in rows:
        entry = (row.get("review") or {}).get(my_team) or {}
        for m in entry.get("matched") or []:
            raw = m.get("_matched_raw")
            if raw:
                auto_matched[str(m.get("id"))] = raw

    people = []
    for m in roster:
        mid = str(m.get("id"))
        override_raw = overrides.get(mid)
        auto_raw = auto_matched.get(mid)
        # 실제 대화로 식별된 자동 매칭이 있으면 그게 더 정확하다(이름/닉네임이
        # 바뀌기 전에 일괄매칭으로 등록해둔 override가 오래돼서 틀어진 경우가
        # 있다) — 자동 매칭을 우선으로 쓰고, 자동 매칭이 아예 없는 사람만
        # override로 보여준다.
        matched_raw = auto_raw or override_raw
        people.append({
            "member_id": m.get("id"),
            "display": _hamummal_display_name(m),
            "matched_raw": matched_raw,
            "auto": bool(auto_raw),
        })
    people.sort(key=lambda p: p["display"])
    return jsonify(ok=True, people=people)


def _submitted_at_24h(raw):
    """"오전/오후 h:mm" 또는 "HH:MM" 형식을 24시간제 "HH:MM"으로 정규화한다.
    예전에 분석된 날짜들은 DB에 "오후 1:34" 형식으로 이미 저장돼 있어서,
    새로 분석하는 날짜부터 24시간제로 바뀌어도 화면에 그대로 보여주면 옛날
    날짜는 계속 12시간제로 보인다 — 그래서 보여줄 때 한 번 더 정규화한다.
    """
    if not raw:
        return raw
    m = re.match(r"^(오전|오후)\s*(\d{1,2}):(\d{2})$", raw.strip())
    if not m:
        return raw
    ampm, h, mm = m.group(1), int(m.group(2)), m.group(3)
    if ampm == "오전":
        h = 0 if h == 12 else h
    else:
        h = 12 if h == 12 else h + 12
    return f"{h:02d}:{mm}"


def _submitted_at_minutes(member):
    # 제출시각 문자열(24시간제로 정규화된 뒤)을 자정 기준 분으로 바꿔 정렬용으로
    # 쓴다. 시각 정보가 없는 사람(수동 매칭 등)은 시간순 목록의 맨 뒤로 보낸다.
    raw = member.get("_submitted_at")
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw) if raw else None
    if not m:
        return 10 ** 6
    return int(m.group(1)) * 60 + int(m.group(2))


def _hamummal_live_in_sync(entry, check_date_str):
    """
    아직 한 번도 반영한 적이 없는 경우(스냅샷이 없는 경우)에도, 실제 소리튠
    조장페이지에 이미 지금 화면과 똑같이 체크돼 있으면 "반영하기" 버튼을
    미리 비활성화해주기 위한 라이브 비교.

    로그인/조회에 실패하거나(네트워크 오류 등) 확신할 수 없으면 None을
    돌려주고, 이 경우 버튼은 평소처럼(활성화 상태로) 둔다 — 잘못 판단해서
    실제로 바뀐 게 있는데도 버튼을 막아버리는 일은 없어야 하므로, "모르면
    그냥 평소대로(활성화)"가 기본값이다.
    """
    phone = entry.get("_phone")
    if not phone:
        return None
    try:
        session, admin_info = leader_dashboard_client.login(phone)
        board = leader_dashboard_client.fetch_status_board(
            session, admin_info["cohort_id"], check_date_str, admin_info["group_id"]
        )
    except Exception:
        return None
    checks = board.get("checks") or {}

    for m in entry.get("matched") or []:
        member_checks = checks.get(str(m.get("id"))) or {}
        if member_checks.get("hamemmal") != 1:
            return False
    for m in entry.get("unmatched_members") or []:
        member_checks = checks.get(str(m.get("id"))) or {}
        if member_checks.get("hamemmal") == 1:
            return False
    return True


@app.route("/hamummal-review/<review_id>")
def hamummal_review(review_id):
    cohort = _active_cohort_or_none()
    member = _current_member_or_none(cohort)
    if not member:
        return redirect(url_for("phone_entry"))
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return "존재하지 않거나 만료된 검토입니다. 관리자에게 문의해주세요.", 404

    full_review = data["review"]
    if my_team not in full_review:
        # 업로드 당시엔 우리 조 조장/부조장이 아직 이 웹앱에 가입 전이라서
        # 통째로 빠졌을 수 있다 — 그 날짜 제출자 목록 자체는 이미 DB에 저장돼
        # 있으니(chat_senders/all_chat_senders), 지금 시점 기준으로 우리 조
        # 현황판과 다시 매칭해본다. 조장/부조장이 그 사이에 가입했다면 바로
        # 결과가 나오고, 다음부터는 저장해둬서 다시 계산할 필요가 없다.
        members = db_client.active_members_by_team().get(my_team) or []
        live_entry = hamummal_dashboard.compute_team_entry(
            my_team, members, data["check_date"],
            data.get("chat_senders") or [], data.get("all_chat_senders") or [],
        )
        if live_entry is None:
            return (
                f"이 검토에는 '{my_team}'의 데이터가 없어요. "
                "우리 조 조장/부조장이 아직 이 웹앱(과제체킹)에 로그인한 적이 없어서, "
                "현황판을 불러올 수가 없어요. 조장 또는 부조장이 로그인한 뒤 다시 들어와 주세요.",
                404,
            )
        full_review[my_team] = live_entry
        try:
            db_client.update_review_team_entry(review_id, my_team, live_entry)
        except Exception:
            pass  # 화면엔 바로 반영하고, 저장은 실패해도 다음에 다시 시도하면 됨

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
    applied_matched_ids = team_applied.get("matched_ids") or []
    applied_had_snapshot = bool(team_applied) and team_applied.get("matched_ids") is not None
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

    # 결과확인 화면에서만 쓸 표시용 사본: 제출시각을 24시간제로 정규화하고,
    # 제출자 목록을 시간순으로 정렬한다. DB에 저장된 entry 자체는 그대로 둔다
    # (매칭/반영 로직은 여전히 원본 순서·형식을 쓰면 되므로 건드릴 필요가 없다).
    matched_for_display = [
        dict(m, _submitted_at=_submitted_at_24h(m.get("_submitted_at")))
        for m in (entry.get("matched") or [])
    ]
    matched_for_display.sort(key=_submitted_at_minutes)
    display_entry = dict(entry, matched=matched_for_display)

    apply_enabled = bool(cohort) and db_client.is_hamummal_apply_enabled(cohort)

    # 한 번도 반영한 적 없는 경우(스냅샷 없음)에만, 소리튠 조장페이지에 실제로
    # 이미 지금 화면과 똑같이 체크돼 있는지 라이브로 확인한다. 반영한 적이 있으면
    # 그때의 스냅샷으로 훨씬 가볍게(네트워크 호출 없이) 같은 걸 판단할 수 있고,
    # 반영 기능 자체가 꺼져있으면 어차피 버튼이 비활성화라 확인할 필요가 없다.
    live_already_applied = False
    if apply_enabled and not applied_had_snapshot:
        live_already_applied = bool(_hamummal_live_in_sync(entry, data["check_date"]))

    return render_template(
        "hamummal_review.html",
        review_id=review_id,
        check_date=data["check_date"],
        check_date_label=check_date_label,
        window_desc=data["window_desc"],
        window_desc_label=window_desc_label,
        review={my_team: display_entry},
        submitter_count=data.get("submitter_count", 0),
        chat_senders=all_chat_senders,
        matched_raw_names=matched_raw_names,
        display_name_overrides=display_name_overrides,
        apply_enabled=apply_enabled,
        applied=bool(team_applied),
        applied_at=format_kst_timestamp(team_applied.get("applied_at")),
        applied_matched_ids=applied_matched_ids,
        applied_had_snapshot=applied_had_snapshot,
        live_already_applied=live_already_applied,
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

    fixed_dates = _retroactively_apply_other_dates(review_id, team, member_id, raw)

    return jsonify(ok=True, fixed_dates=sorted(fixed_dates))


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

    known_raw = None
    if direction == "exclude":
        target = None
        for m in entry.get("matched") or []:
            if str(m.get("id")) == str(member_id):
                target = m
                break
        if target is None:
            return jsonify(ok=False, error="제출자 목록에서 해당 조원을 찾지 못했어요."), 404
        target = dict(target)
        # "매칭(이 사람이 누구인지 식별)"과 "오늘 제출했는지"는 별개 — 미제출로
        # 바뀐다고 이 사람이 누구인지 모르게 되는 건 아니므로, 알고 있던
        # 카카오 이름은 _known_raw 힌트로 남겨서 화면에 계속 보여준다.
        known_raw = target.pop("_matched_raw", None)
        if known_raw:
            target["_known_raw"] = known_raw
        entry["matched"] = [
            m for m in entry.get("matched") or [] if str(m.get("id")) != str(member_id)
        ]
        unmatched_ids = {str(m.get("id")) for m in entry.get("unmatched_members") or []}
        if str(member_id) not in unmatched_ids:
            entry.setdefault("unmatched_members", []).append(target)
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
        # "매칭"은 제출/미제출을 왔다갔다 해도 계속 유지돼야 한다 — 전에 알고
        # 있던 카카오 이름(_known_raw)이 있으면 그걸 오늘의 매칭 이름
        # (_matched_raw)으로 그대로 가져와서, 다시 제출자로 옮겨도 "매칭이
        # 풀린" 것처럼 보이거나 같은 raw를 두고 확인 필요가 다시 뜨지 않게 한다.
        known_raw_existing = target.pop("_known_raw", None)
        if known_raw_existing:
            target["_matched_raw"] = known_raw_existing
            known_raw = known_raw_existing
        else:
            target.pop("_matched_raw", None)
        matched_ids = {str(x.get("id")) for x in (entry.get("matched") or [])}
        if str(member_id) not in matched_ids:
            entry.setdefault("matched", []).append(target)
        _purge_resolved_ambiguous(entry)

    try:
        db_client.update_review_team_entry(review_id, team, entry)
    except Exception as e:
        return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500

    return jsonify(ok=True, known_raw=known_raw)


@app.route("/hamummal-review/<review_id>/apply", methods=["POST"])
def hamummal_review_apply(review_id):
    cohort = _active_cohort_or_none()
    if not (cohort and db_client.is_hamummal_apply_enabled(cohort)):
        return jsonify(ok=False, error="지금은 반영 기능이 꺼져 있어요. 관리자(하멈말 업로드 페이지)에서 켜야 사용할 수 있어요."), 403
    member = _current_member_or_none(cohort)
    if not member:
        return jsonify(ok=False, error="로그인이 필요합니다."), 401
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return jsonify(ok=False, error="존재하지 않거나 만료된 검토입니다. 관리자에게 문의해주세요."), 404

    full_review = data["review"]
    if my_team not in full_review:
        return jsonify(ok=False, error=f"이 검토에는 '{my_team}'의 데이터가 없어요."), 404

    check_date_str = data["check_date"]
    team = my_team
    entry = full_review[team]

    # 반영은 한 번만 할 수 있는 게 아니다 — 이미 반영한 뒤에 이름을 고치거나
    # 제출/미제출을 바꾼 경우, 그 변경 내역(지난 반영 때와 달라진 사람)만 다시
    # 골라서 소리튠에 반영한다. 지난 반영 기록이 없으면(최초 반영) "달라진 사람"이
    # 곧 지금 제출자 전체가 된다.
    prev_applied = (data.get("applied_teams") or {}).get(team) or {}
    had_snapshot = prev_applied.get("matched_ids") is not None
    current_matched_ids = {str(m.get("id")) for m in entry.get("matched") or []}
    current_unmatched_ids = {str(m.get("id")) for m in entry.get("unmatched_members") or []}

    if had_snapshot:
        prev_matched_ids = {str(x) for x in (prev_applied.get("matched_ids") or [])}
        removed_ids = prev_matched_ids - current_matched_ids
        added_ids = current_matched_ids - prev_matched_ids
        if prev_applied and not added_ids and not removed_ids:
            return jsonify(ok=True, results={team: "마지막 반영 이후로 바뀐 내용이 없어요."})
    else:
        # 반영 기록은 있지만(예전 버전이라) 그때 누굴 반영했는지 스냅샷이 없거나,
        # 아예 첫 반영인 경우 — 뭐가 바뀌었는지 정확히 알 수 없으니 이번 한 번은
        # 지금 제출자 전체를 다시 체크하고 지금 미제출자 전체를 체크 해제해서
        # 소리튠 조장페이지를 지금 화면 상태와 완전히 맞춰놓는다. 이후부터는
        # 스냅샷이 생겨서 바뀐 사람만 다시 반영한다.
        added_ids = current_matched_ids
        removed_ids = current_unmatched_ids

    apply_results = hamummal_dashboard.apply_review(
        {team: entry}, check_date_str,
        add_ids_by_team={team: added_ids}, remove_ids_by_team={team: removed_ids},
    )
    db_client.mark_review_team_applied(review_id, team, apply_results, matched_ids=sorted(current_matched_ids))

    return jsonify(ok=True, results=apply_results)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
