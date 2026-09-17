"""
현황판(boot.soritune.com)에서 member_status가 out_of_group_management(조 관리 제외)인
멤버 목록을 뽑아서 출력하는 일회성 스크립트.

- 이 스크립트는 네트워크(외부 API 호출 + DB 조회)가 필요해서, Cowork 브릿지(device_bash) 안에서는
  DNS/외부 접속이 막혀 있어 실행이 안 된다. 반드시 본인 Mac의 실제 터미널(Terminal.app 등)에서
  직접 실행해야 한다.

실행 준비:
  - .env에 DATABASE_URL이 설정돼 있어야 함 (이미 되어 있음)
  - Postgres members 테이블에 조회하려는 조의 조장/부조장 휴대폰번호가 등록돼 있어야 함
    (이 앱에 로그인한 적 있으면 이미 들어있음)

실행:
  cd ~/Desktop/Coding/과제체킹문자알림/프로그램
  source venv/bin/activate   # 안 되면: pip install -r requirements.txt 먼저
  python -m scripts.find_out_of_group 루틴조

  (조 이름을 안 넘기면 기본값으로 "루틴조"를 씀)
"""
import sys

from src import config, db_client, leader_dashboard_client


def find_leader_phone(team: str):
    row = db_client._query(
        """
        SELECT phone, name, role
        FROM members
        WHERE team = %s AND role IN ('조장', '부조장')
        ORDER BY joined_at DESC
        LIMIT 1
        """,
        (team,),
        fetch="one",
    )
    return row


def main():
    team = sys.argv[1] if len(sys.argv) > 1 else "루틴조"

    leader = find_leader_phone(team)
    if not leader:
        print(f"[에러] '{team}' 조의 조장/부조장 번호를 members 테이블에서 못 찾았어요.")
        return

    print(f"[정보] {leader['name']}({leader['role']}) 번호로 로그인해서 '{team}' 현황을 조회합니다.")

    session, admin_info = leader_dashboard_client.login(leader["phone"])
    check_date = leader_dashboard_client.dashboard_check_date()
    status_board = leader_dashboard_client.fetch_status_board(
        session, admin_info["cohort_id"], check_date.isoformat(), admin_info["group_id"]
    )

    out_members = [
        m for m in (status_board.get("members") or [])
        if m.get("member_status") == config.OUT_MEMBER_STATUS
    ]

    print(f"\n=== '{team}' 조 관리 제외(out_of_group_management) 멤버: {len(out_members)}명 ===")
    for m in out_members:
        print(
            f"- {m.get('real_name')}"
            f"{'_' + m.get('nickname') if m.get('nickname') else ''}"
            f" | 점수: {m.get('current_score')}"
            f" | id: {m.get('id')}"
        )

    if not out_members:
        print("(해당하는 멤버가 없습니다)")


if __name__ == "__main__":
    main()
