import time

from . import sheets_client, leader_dashboard_client


def refresh_all():
    cohort = sheets_client.get_active_cohort()
    if not cohort:
        return {}

    results = {}
    for idx, (team, members) in enumerate(sheets_client.active_members_by_team().items()):
        if idx > 0:
            # 조별로 약간 텀을 둬서 구글시트 분당 읽기 쿼터를 한 번에 다 쓰지 않게 한다.
            time.sleep(1)
        leader_like = [m for m in members if m.get("역할") in ("조장", "부조장")]
        if not leader_like:
            continue
        last_error = None
        for m in leader_like:
            phone = m.get("휴대폰번호")
            if not phone:
                continue
            try:
                fetched = leader_dashboard_client.fetch_reports(phone)
            except leader_dashboard_client.DashboardLoginError as e:
                last_error = str(e)
                continue
            except Exception as e:
                last_error = str(e)
                continue
            sheets_client.set_cache_row(
                fetched["admin"]["group_name"], fetched["missing_text"], fetched["tag_text"]
            )
            results[team] = "ok"
            break
        else:
            results[team] = f"실패: {last_error or '가입된 번호 없음'}"
    return results


if __name__ == "__main__":
    print(refresh_all())
