import os
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


@app.route("/hamummal-upload/set-start-date", methods=["POST"])
def hamummal_upload_set_start_date():
    if not session.get("hamummal_admin"):
        return "관리자 인증이 필요합니다.", 403
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403
    date_str = request.form.get("start_date")
    if date_str:
        db_client.set_hamummal_start_date(str(cohort.get("기수명")), date_str)
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
    hamummal_start_date = cohort.get("하멈말시작일")
    today_str = now_kst().date().isoformat()
    existing_windows = db_client.get_hamummal_date_windows(cohort_name)
    default_start, default_end = DEFAULT_HAMUMMAL_START, DEFAULT_HAMUMMAL_END

    # 표는 "오늘"까지가 아니라 실제로 하멈말이 진행된(=분석된) 마지막 날짜까지만
    # 보여준다 — 그래야 아직 하멈말을 하지도 않은 먼 미래 날짜까지 쭉 나오지 않는다.
    last_analyzed_date = db_client.get_max_review_date()
    today_date = now_kst().date()
    if last_analyzed_date:
        end_date = min(datetime.strptime(last_analyzed_date, "%Y-%m-%d").date(), today_date)
    else:
        end_date = today_date

    date_window_dates = []
    if hamummal_start_date:
        start = datetime.strptime(hamummal_start_date, "%Y-%m-%d").date()
        d = start
        while d <= end_date:
            date_str = d.isoformat()
            win = existing_windows.get(date_str) or {}
            date_window_dates.append({
                "date": date_str,
                "start": win.get("start") or default_start,
                "end": win.get("end") or default_end,
            })
            d += timedelta(days=1)

    date_window_dates.reverse()  # 최근 날짜가 위로 오도록
    today_window = existing_windows.get(today_str) or {}

    return render_template(
        "hamummal_upload.html",
        is_admin=bool(session.get("hamummal_admin")),
        hamummal_review_visible=db_client.is_hamummal_review_visible(cohort),
        hamummal_apply_enabled=db_client.is_hamummal_apply_enabled(cohort),
        hamummal_start_date=hamummal_start_date,
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
    )


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
    # 있으면(정상이라면 있을 수 없지만, 과거 수정 과정에서 생긴 데이터가 남아있을
    # 수 있음) 제출자 쪽을 우선해서 미제출 목록에서 빼주고, 그 상태로 저장해서
    # 다음부터는 다시 안 보이게 한다.
    _matched_ids_now = {str(x.get("id")) for x in (entry.get("matched") or [])}
    _clean_unmatched = [
        x for x in (entry.get("unmatched_members") or []) if str(x.get("id")) not in _matched_ids_now
    ]
    if len(_clean_unmatched) != len(entry.get("unmatched_members") or []):
        entry["unmatched_members"] = _clean_unmatched
        try:
            db_client.update_review_team_entry(review_id, my_team, entry)
        except Exception:
            pass  # 화면엔 바로 반영하고, 저장은 실패해도 다음에 다시 시도하면 됨

    _clean_ambiguous = []
    for _amb in entry.get("ambiguous") or []:
        _kept = [c for c in _amb.get("candidates") or [] if str(c.get("id")) not in _matched_ids_now]
        if _kept:
            _amb["candidates"] = _kept
            _clean_ambiguous.append(_amb)
    if len(_clean_ambiguous) != len(entry.get("ambiguous") or []):
        entry["ambiguous"] = _clean_ambiguous
        try:
            db_client.update_review_team_entry(review_id, my_team, entry)
        except Exception:
            pass

    team_applied = (data.get("applied_teams") or {}).get(my_team) or {}
    is_today = data["check_date"] == now_kst().date().isoformat()
    y, mo, d = map(int, data["check_date"].split("-"))
    check_date_label = f"{mo}월 {d}일자"
    window_desc_label = (data["window_desc"] or "").replace("~", " ~ ")

    # "이름 고치기" 팝업에서 쓸 자료: 이 날짜 대화에서 실제로 제출 메시지를 보낸
    # 사람 이름 목록(chat_senders)과, 그 중 이미 우리 조의 누군가로 매칭된 이름
    # (매칭 안 된 이름에는 팝업에서 회색으로 "(매칭안됨)" 표시를 해주기 위함).
    chat_senders = data.get("chat_senders") or []
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
        chat_senders=chat_senders,
        matched_raw_names=matched_raw_names,
        display_name_overrides=display_name_overrides,
        apply_enabled=bool(cohort) and db_client.is_hamummal_apply_enabled(cohort),
        applied=bool(team_applied),
        applied_at=format_kst_timestamp(team_applied.get("applied_at")),
        is_today=is_today,
    )


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
            try:
                db_client.update_review_team_entry(review_id, team, entry)
            except Exception:
                pass  # confirmed_matches 저장은 이미 끝났으니 여기서 실패해도 치명적이진 않음

    return jsonify(ok=True)


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
    if (data.get("applied_teams") or {}).get(my_team):
        return jsonify(ok=False, error="이미 반영된 검토라 이름을 고칠 수 없어요."), 409

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
        target["_matched_raw"] = matched_raw or display_name
        current_matched = True
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
            cleaned_ambiguous = []
            for amb in entry.get("ambiguous") or []:
                kept = [c for c in amb.get("candidates") or [] if str(c.get("id")) != str(member_id)]
                if kept:
                    amb["candidates"] = kept
                    cleaned_ambiguous.append(amb)
            entry["ambiguous"] = cleaned_ambiguous
            entry.setdefault("matched", []).append(target)
            try:
                db_client.update_review_team_entry(review_id, team, entry)
            except Exception as e:
                return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500

    # 한 번의 업로드로 여러 날짜가 한꺼번에 분석되는 경우가 많아서(업로드 1건 =
    # 검토 row 여러 개), 이 조원이 "이 조"의 다른 날짜에서도 아직 미제출로
    # 남아 있는데 그 날짜 대화에도 이 이름으로 보낸 메시지가 있으면 같이
    # 고쳐준다 — 다음 업로드를 기다리지 않고 이미 업로드한 파일 기준으로 바로
    # 반영되도록. (정말 그 날짜에 메시지가 없으면 그대로 미제출로 남는다.)
    try:
        other_rows = db_client.list_team_reviews(team)
    except Exception as e:
        return jsonify(ok=False, error=f"다른 날짜를 확인하지 못했습니다: {e}"), 500

    fixed_dates = [data["check_date"]] if (found_in in ("unmatched", "ambiguous") and current_matched) else []
    for row in other_rows:
        if str(row["id"]) == str(review_id):
            continue  # 지금 보고 있는 날짜는 위에서 이미 처리함

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
            continue  # 이 날짜엔 애초에 "미제출"/"확인필요"로 남아있지 않음 (건드릴 필요 없음)

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
        row_target["_matched_raw"] = row_matched_raw
        row_entry.setdefault("matched", []).append(row_target)

        try:
            db_client.update_review_team_entry(row["id"], team, row_entry)
        except Exception as e:
            return jsonify(ok=False, error=f"저장하지 못했습니다: {e}"), 500

        fixed_dates.append(row["check_date"])

    return jsonify(
        ok=True,
        found_in=found_in,
        matched=current_matched,
        display_name=display_name,
        member=target,
        fixed_dates=sorted(fixed_dates),
    )


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

    apply_results = hamummal_dashboard.apply_review({team: entry}, check_date_str)
    db_client.mark_review_team_applied(review_id, team, apply_results)

    return render_template("hamummal_done.html", results=apply_results)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
