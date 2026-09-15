from . import db_client, leader_dashboard_client, hamummal_parser, hamummal_matcher


def compute_review(txt_path: str, window_start, window_end, check_date_str: str) -> dict:
    submitters = hamummal_parser.extract_submitters(txt_path, window_start, window_end)

    result = {}
    for team, members in db_client.active_members_by_team().items():
        leader_like = [
            m for m in members
            if m.get("역할") in ("조장", "부조장") and m.get("휴대폰번호")
        ]
        if not leader_like:
            continue

        session = admin_info = login_phone = None
        last_error = None
        for m in leader_like:
            phone = m.get("휴대폰번호")
            try:
                session, admin_info = leader_dashboard_client.login(phone)
                login_phone = phone
                break
            except Exception as e:
                last_error = str(e)
                continue

        if not session:
            result[team] = {
                "matched": [], "unmatched_members": [], "ambiguous": [],
                "error": last_error or "가입된 번호로 로그인하지 못했습니다.", "_phone": None,
            }
            continue

        try:
            board = leader_dashboard_client.fetch_status_board(
                session, admin_info["cohort_id"], check_date_str, admin_info["group_id"]
            )
        except Exception as e:
            result[team] = {
                "matched": [], "unmatched_members": [], "ambiguous": [],
                "error": str(e), "_phone": login_phone,
            }
            continue

        confirmed_rows = db_client.get_confirmed_matches(team)
        display_name_overrides = db_client.get_display_name_overrides(team)
        match = hamummal_matcher.match_submitters(
            submitters,
            board.get("members") or [],
            confirmed_rows=confirmed_rows,
            display_name_overrides=display_name_overrides,
        )
        match["error"] = None
        match["_phone"] = login_phone
        result[team] = match

    return {"teams": result, "submitter_count": len(submitters)}


def apply_review(review: dict, check_date_str: str) -> dict:
    results = {}
    for team, data in review.items():
        matched = data.get("matched") or []
        phone = data.get("_phone")
        if not matched:
            results[team] = "제출자 없음 (변경사항 없음)"
            continue
        if not phone:
            results[team] = "저장 실패: 로그인 정보 없음"
            continue
        try:
            session, _ = leader_dashboard_client.login(phone)
            items = [
                {
                    "member_id": m["id"],
                    "mission_type_code": "hamemmal",
                    "check_date": check_date_str,
                    "status": True,
                }
                for m in matched
            ]
            leader_dashboard_client.save_checks(session, items, check_date=check_date_str)
            results[team] = f"{len(items)}명 체크 완료"
        except Exception as e:
            results[team] = f"저장 실패: {e}"
    return results
