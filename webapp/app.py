import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, request, redirect, render_template, flash, session, url_for, jsonify

from src import (
    config, db_client, leader_dashboard_client, hamummal_dashboard,
)
from src.time_window import TZ, format_kst_timestamp, now_kst

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)


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

    hamummal_active = bool(cohort) and db_client.is_hamummal_active(cohort)
    hamummal_review_id = db_client.get_latest_review_id() if hamummal_active else None
    return render_template(
        "dashboard.html", member=member, hamummal_active=hamummal_active,
        hamummal_review_id=hamummal_review_id,
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


@app.route("/hamummal-upload", methods=["GET", "POST"])
def hamummal_upload():
    cohort = _active_cohort_or_none()
    if not cohort:
        return "지금은 진행 중인 기수가 없어요.", 403
    if not config.HAMUMMAL_TEST_MODE and not db_client.is_hamummal_active(cohort):
        hs, he = db_client.hamummal_window(cohort)
        window_str = f" (이번 기수 하멈말 기간: {hs} ~ {he})" if hs and he else ""
        return f"지금은 하멈말 미션 기간이 아니에요.{window_str}", 403

    if request.method == "POST":
        password = request.form.get("password")
        if password != config.UPLOAD_PASSWORD:
            return "비밀번호가 틀렸습니다.", 403

        date_str = request.form.get("date")
        start_str = request.form.get("start_time")
        end_str = request.form.get("end_time")
        f = request.files["chat_file"]

        # 업로드 파일은 이 요청 안에서 분석하고 바로 버리면 되므로, 요청 간
        # 유지가 보장되지 않는 임시 디렉토리(/tmp 등)에 저장한다.
        tmp_fd, save_path = tempfile.mkstemp(prefix=f"{date_str}_", suffix=f"_{f.filename}")
        os.close(tmp_fd)
        f.save(save_path)

        y, mo, d = map(int, date_str.split("-"))
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        window_start = TZ.localize(datetime(y, mo, d, sh, sm))
        window_end = TZ.localize(datetime(y, mo, d, eh, em))

        try:
            computed = hamummal_dashboard.compute_review(str(save_path), window_start, window_end, date_str)
        finally:
            try:
                os.remove(save_path)
            except OSError:
                pass
        review_id = db_client.save_review(
            computed["teams"], date_str, f"{start_str}~{end_str}",
            submitter_count=computed["submitter_count"],
        )
        return redirect(url_for("hamummal_review", review_id=review_id))

    return render_template(
        "hamummal_upload.html",
        default_date=now_kst().date().isoformat(),
        hamummal_review_id=db_client.get_latest_review_id(),
    )


@app.route("/hamummal-review/<review_id>")
def hamummal_review(review_id):
    member = _current_member_or_none()
    if not member:
        return redirect(url_for("phone_entry"))
    my_team = str(member.get("조"))

    data = db_client.load_review(review_id)
    if not data:
        return "존재하지 않거나 만료된 검토입니다. 다시 업로드해주세요.", 404

    full_review = data["review"]
    if my_team not in full_review:
        return f"이 검토에는 '{my_team}'의 데이터가 없어요. 조장/부조장 번호가 시트에 등록돼 있는지 확인해주세요.", 404

    team_applied = (data.get("applied_teams") or {}).get(my_team) or {}
    return render_template(
        "hamummal_review.html",
        review_id=review_id,
        check_date=data["check_date"],
        window_desc=data["window_desc"],
        review={my_team: full_review[my_team]},
        submitter_count=data.get("submitter_count", 0),
        hamummal_test_mode=config.HAMUMMAL_TEST_MODE,
        applied=bool(team_applied),
        applied_at=format_kst_timestamp(team_applied.get("applied_at")),
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


@app.route("/hamummal-review/<review_id>/apply", methods=["POST"])
def hamummal_review_apply(review_id):
    if not config.HAMUMMAL_TEST_MODE:
        return "지금은 테스트 모드에서만 반영할 수 있어요. 검증이 끝나면 실제 서비스에도 열 예정입니다.", 403
    member = _current_member_or_none()
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
