from . import db_client, leader_dashboard_client, hamummal_parser, hamummal_matcher


def compute_team_entry(
    team: str, members: list, check_date_str: str, submitters, all_submitters, submission_times: dict = None,
) -> dict:
    """
    한 조(team)만 콕 집어서, 그 조 조장/부조장 번호로 소리튠에 로그인해 현황판을
    받아오고 제출자 목록(submitters)과 매칭한 결과를 돌려준다.

    업로드 시점에는 조별 매칭을 미리 하지 않는다 — 결과확인/출석률/조원일괄매칭
    페이지 중 하나에서 그 조 데이터가 실제로 필요해지는 순간, 그 자리에서
    (lazily) 이 함수로 계산해서 캐시해둔다(webapp/app.py의 _fill_team_entry
    참고). submitters/all_submitters/submission_times는 그 날짜 업로드 시점에
    이미 뽑아서 DB에 저장해둔 chat_senders/all_chat_senders/submission_times를
    그대로 넘기면 되므로, 카카오톡 원본 파일이 없어도(업로드 요청이 끝나면 바로
    지워버리므로) 계산할 수 있다. submission_times는 {카카오 발신자 이름:
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


def compute_team_entries_for_dates(team: str, members: list, dated_rows: list):
    """
    compute_team_entry와 같은 방식으로 날짜별 matched/unmatched/ambiguous를 다시
    계산하지만, 조장/부조장 로그인은 한 번만 하고 여러 날짜에 그 세션을 그대로
    써서 날짜 수가 많아도 로그인을 날짜마다 반복하지 않는다. "조원 일괄 매칭"
    페이지의 "전체 리셋"처럼 그 조의 모든 날짜를 한꺼번에 다시 계산할 때 쓴다.

    dated_rows: [(check_date_str, submitters, all_submitters, submission_times), ...]
    반환값: (results, login_error) — results는 {check_date_str: entry_dict},
    로그인 자체가 안 되면 results는 빈 dict이고 login_error에 이유가 담긴다.
    """
    leader_like = [
        m for m in (members or [])
        if m.get("역할") in ("조장", "부조장") and m.get("휴대폰번호")
    ]
    if not leader_like:
        return {}, "조장/부조장이 아직 이 웹앱(과제체킹)에 로그인한 적이 없어요."

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
        return {}, (last_error or "가입된 번호로 로그인하지 못했습니다.")

    confirmed_rows = db_client.get_confirmed_matches(team)
    display_name_overrides = db_client.get_display_name_overrides(team)

    results = {}
    for check_date_str, submitters, all_submitters, submission_times in dated_rows:
        try:
            board = leader_dashboard_client.fetch_status_board(
                session, admin_info["cohort_id"], check_date_str, admin_info["group_id"]
            )
        except Exception as e:
            results[check_date_str] = {
                "matched": [], "unmatched_members": [], "ambiguous": [],
                "error": str(e), "_phone": login_phone,
            }
            continue

        match = hamummal_matcher.match_submitters(
            submitters,
            board.get("members") or [],
            confirmed_rows=confirmed_rows,
            display_name_overrides=display_name_overrides,
            all_chat_senders=sorted(all_submitters),
        )
        times = submission_times or {}
        for m in match["matched"]:
            raw = m.get("_matched_raw")
            formatted = times.get(raw)
            if formatted:
                m["_submitted_at"] = formatted
        match["error"] = None
        match["_phone"] = login_phone
        results[check_date_str] = match

    return results, None


def extract_review_data(txt_path: str, window_start, window_end) -> dict:
    """
    업로드 시점에 저장해둘, 조와 무관한 날짜별 데이터만 뽑는다(그 날짜 시간대
    기준 제출자/제출시각). 조별 매칭(compute_team_entry, 소리튠 로그인+조회가
    필요)은 여기서 하지 않는다 — 결과확인/출석률/조원일괄매칭 페이지가 실제로
    필요할 때 그 자리에서 계산하고 캐시해둔다. all_chat_senders(전체 참가자
    명단)는 날짜/시간대와 무관하게 파일 전체에서 한 번만 뽑으면 되므로, 여기서
    계산하지 않는다 — 호출하는 쪽(hamummal_upload)이 업로드당 한 번만 따로
    계산해서 모든 날짜에 재사용한다.
    """
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
    return {"submitters": submitters, "submission_times": submission_times}


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
