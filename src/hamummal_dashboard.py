from . import db_client, leader_dashboard_client, hamummal_parser, hamummal_matcher


def compute_review(txt_path: str, window_start, window_end, check_date_str: str) -> dict:
    submitters = hamummal_parser.extract_submitters(txt_path, window_start, window_end)
    # "제출자 목록에 몇 시에 제출했는지" 표시용 — 판정(제출했는지 자체)에는
    # 영향 없고 화면에 보여줄 시각만 따로 뽑는다.
    submission_times = hamummal_parser.extract_submission_times(txt_path, window_start, window_end)
    # "이름 고치기" 팝업 검색용 — 날짜/시간 상관없이 파일 전체에서 제출한 적
    # 있는 모든 사람. 매칭 판정에는 안 쓰고(그건 위 submitters만 씀), 검색
    # 목록을 넓혀주기 위해서만 쓴다.
    all_submitters = hamummal_parser.extract_all_submitters(txt_path)

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
            all_chat_senders=sorted(all_submitters),
        )
        for m in match["matched"]:
            raw = m.get("_matched_raw")
            ts = submission_times.get(raw)
            if ts:
                m["_submitted_at"] = hamummal_parser.format_submission_time(ts)
        match["error"] = None
        match["_phone"] = login_phone
        result[team] = match

    return {
        "teams": result,
        "submitter_count": len(submitters),
        "chat_senders": sorted(submitters),
        "all_chat_senders": sorted(all_submitters),
    }


def apply_review(
    review: dict, check_date_str: str, add_ids_by_team: dict = None, remove_ids_by_team: dict = None,
) -> dict:
    """
    add_ids_by_team에 담긴 사람(이번에 새로 제출자가 된 사람)만 status=True로
    체크하고, remove_ids_by_team에 담긴 사람(전에는 반영됐는데 지금은
    "미제출"로 바뀐 사람)만 status=False로 체크를 해제한다. 이미 반영돼 있고
    이번에 바뀌지 않은 사람은 다시 보내지 않는다 — 그래야 반영 결과(몇 명
    체크됐는지)가 실제로 "이번에 바뀐 사람 수"와 일치한다.
    """
    add_ids_by_team = add_ids_by_team or {}
    remove_ids_by_team = remove_ids_by_team or {}
    results = {}
    for team, data in review.items():
        add_ids = {str(x) for x in (add_ids_by_team.get(team) or set())}
        remove_ids = {str(x) for x in (remove_ids_by_team.get(team) or set())}
        phone = data.get("_phone")
        if not add_ids and not remove_ids:
            results[team] = "변경사항 없음"
            continue
        if not phone:
            results[team] = "저장 실패: 로그인 정보 없음"
            continue
        try:
            session, _ = leader_dashboard_client.login(phone)
            items = [
                {
                    "member_id": mid,
                    "mission_type_code": "hamemmal",
                    "check_date": check_date_str,
                    "status": True,
                }
                for mid in add_ids
            ] + [
                {
                    "member_id": mid,
                    "mission_type_code": "hamemmal",
                    "check_date": check_date_str,
                    "status": False,
                }
                for mid in remove_ids
            ]
            leader_dashboard_client.save_checks(session, items, check_date=check_date_str)
            parts = []
            if add_ids:
                parts.append(f"{len(add_ids)}명 체크")
            if remove_ids:
                parts.append(f"{len(remove_ids)}명 체크 해제")
            results[team] = (", ".join(parts) + " 완료") if parts else "변경사항 없음"
        except Exception as e:
            results[team] = f"저장 실패: {e}"
    return results
