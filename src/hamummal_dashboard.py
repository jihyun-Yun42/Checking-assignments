from . import db_client, leader_dashboard_client, hamummal_parser, hamummal_matcher


def compute_team_entry(
    team: str, members: list, check_date_str: str, submitters, all_submitters, submission_times: dict = None,
) -> dict:
    """
    한 조(team)만 콕 집어서, 그 조 조장/부조장 번호로 소리튠에 로그인해 현황판을
    받아오고 제출자 목록(submitters)과 매칭한 결과를 돌려준다.

    업로드 시점(compute_review)뿐 아니라, 업로드 당시엔 조장/부조장이 아직 이
    웹앱에 가입 전이라 통째로 빠졌던 조를, 나중에 그 조장/부조장이 가입한 뒤
    조원이 결과확인 페이지를 열어볼 때 그 자리에서(lazily) 계산할 때도 쓴다 —
    submitters/all_submitters/submission_times는 그 날짜 업로드 시점에 이미
    뽑아서 DB에 저장해둔 chat_senders/all_chat_senders/submission_times를 그대로
    넘기면 되므로, 카카오톡 원본 파일이 없어도(업로드 요청이 끝나면 바로
    지워버리므로) 다시 계산할 수 있다. submission_times는 {카카오 발신자 이름:
    "HH:MM"} 형태의 이미 포맷된 문자열 딕셔너리를 받는다(호출하는 쪽에서 미리
    포맷해서 넘긴다 — DB에 저장할 때도 이 형태 그대로 저장한다).

    조장/부조장이 단 한 명도 이 웹앱에 가입한 적 없으면(members 안에 역할·
    번호를 가진 사람이 아예 없으면) None을 돌려준다 — 이건 이 함수가 뭘 어떻게
    해도 해결할 수 없는, 진짜로 "아직 아무도 가입 안 한" 경우라서 호출한 쪽에서
    구분해서 안내해야 한다.
    """
    leader_like = [
        m for m in (members or [])
        if m.get("역할") in ("조장", "부조장") and m.get("휴대폰번호")
    ]
    if not leader_like:
        return None

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
        return {
            "matched": [], "unmatched_members": [], "ambiguous": [],
            "error": last_error or "가입된 번호로 로그인하지 못했습니다.", "_phone": None,
        }

    try:
        board = leader_dashboard_client.fetch_status_board(
            session, admin_info["cohort_id"], check_date_str, admin_info["group_id"]
        )
    except Exception as e:
        return {
            "matched": [], "unmatched_members": [], "ambiguous": [],
            "error": str(e), "_phone": login_phone,
        }

    confirmed_rows = db_client.get_confirmed_matches(team)
    display_name_overrides = db_client.get_display_name_overrides(team)
    match = hamummal_matcher.match_submitters(
        submitters,
        board.get("members") or [],
        confirmed_rows=confirmed_rows,
        display_name_overrides=display_name_overrides,
        all_chat_senders=sorted(all_submitters),
    )
    submission_times = submission_times or {}
    for m in match["matched"]:
        raw = m.get("_matched_raw")
        formatted = submission_times.get(raw)
        if formatted:
            m["_submitted_at"] = formatted
    match["error"] = None
    match["_phone"] = login_phone
    return match


def compute_review(txt_path: str, window_start, window_end, check_date_str: str) -> dict:
    submitters = hamummal_parser.extract_submitters(txt_path, window_start, window_end)
    # "제출자 목록에 몇 시에 제출했는지" 표시용 — 판정(제출했는지 자체)에는
    # 영향 없고 화면에 보여줄 시각만 따로 뽑는다. 지금 당장 매칭되는 조뿐 아니라
    # 나중에 조장/부조장이 새로 가입해서 다시 매칭할 조를 위해서도 그대로
    # DB에 저장해둘 거라, 여기서 바로 "HH:MM" 문자열로 포맷해서 들고 있는다
    # (원본 카톡 파일은 업로드 요청이 끝나면 지워지므로, 나중엔 이 값만 남는다).
    submission_times = {
        sender: hamummal_parser.format_submission_time(ts)
        for sender, ts in hamummal_parser.extract_submission_times(txt_path, window_start, window_end).items()
    }
    # "이름 고치기" 팝업 검색용 — 날짜/시간, 하멈말 제출 여부 상관없이 파일
    # 전체에서 한 번이라도 메시지를 보냈거나 입장한 적 있는 모든 사람. 매칭
    # 판정에는 안 쓰고(그건 위 submitters만 씀), 검색 목록을 넓혀주기 위해서만
    # 쓴다(텍스트로만 대화하고 하멈말엔 참여 안 한 사람도 여기엔 포함돼야 함).
    all_submitters = hamummal_parser.extract_all_chat_participants(txt_path)

    result = {}
    for team, members in db_client.active_members_by_team().items():
        entry = compute_team_entry(team, members, check_date_str, submitters, all_submitters, submission_times)
        if entry is not None:
            result[team] = entry

    return {
        "teams": result,
        "submitter_count": len(submitters),
        "chat_senders": sorted(submitters),
        "all_chat_senders": sorted(all_submitters),
        "submission_times": submission_times,
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
