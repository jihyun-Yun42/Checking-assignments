"""
일회성 마이그레이션 스크립트: 구글시트에 있던 데이터를 Postgres(DATABASE_URL)로 옮긴다.

실행 전 준비물:
  - config/google_service_account.json (기존 구글시트 접근용, 지금까지 쓰던 그대로)
  - 환경변수 GOOGLE_SHEET_NAME (기존 시트 이름)
  - 환경변수 DATABASE_URL (Supabase 프로젝트의 Session pooler 연결 문자열)

실행:
  python -m scripts.migrate_sheets_to_db

여러 번 실행해도 안전하다(이미 있는 행은 건너뛰거나 최신값으로 덮어쓸 뿐, 중복 생성되지 않음).
"""
from src import sheets_client, db_client


def migrate_members():
    members = sheets_client.get_all_members()
    print(f"[명단] 구글시트에서 {len(members)}명 읽음")
    for m in members:
        db_client._query(
            '''
            INSERT INTO members (cohort, name, phone, team, role, joined_at)
            VALUES (%s, %s, %s, %s, %s, COALESCE(NULLIF(%s, '')::timestamptz, now()))
            ON CONFLICT (cohort, phone) DO UPDATE SET
                name = EXCLUDED.name, team = EXCLUDED.team, role = EXCLUDED.role
            ''',
            (
                m.get("기수"), m.get("이름"), db_client.normalize_phone(m.get("휴대폰번호")),
                m.get("조"), m.get("역할"), m.get("가입일시") or "",
            ),
            fetch="none",
        )
    print(f"[명단] {len(members)}명 이관 완료")


def migrate_cohorts():
    cohorts = sheets_client.get_cohorts()
    print(f"[기수] 구글시트에서 {len(cohorts)}개 읽음")
    for c in cohorts:
        db_client._query(
            '''
            INSERT INTO cohorts (name, start_date, end_date)
            VALUES (%s, NULLIF(%s, '')::date, NULLIF(%s, '')::date)
            ON CONFLICT (name) DO UPDATE SET
                start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date
            ''',
            (c.get("기수명"), c.get("시작일") or "", c.get("종료일") or ""),
            fetch="none",
        )
    print(f"[기수] {len(cohorts)}개 이관 완료")


def migrate_cache_rows():
    rows = sheets_client.all_cache_rows()
    print(f"[현황캐시] 구글시트에서 {len(rows)}개 조 읽음")
    for team, row in rows.items():
        db_client._query(
            '''
            INSERT INTO cache_rows (team, missing_text, tag_text, updated_at)
            VALUES (%s, %s, %s, COALESCE(NULLIF(%s, '')::timestamptz, now()))
            ON CONFLICT (team) DO UPDATE SET
                missing_text = EXCLUDED.missing_text,
                tag_text = EXCLUDED.tag_text,
                updated_at = EXCLUDED.updated_at
            ''',
            (team, row.get("미제출텍스트") or "", row.get("태그리스트") or "", row.get("갱신시각") or ""),
            fetch="none",
        )
    print(f"[현황캐시] {len(rows)}개 조 이관 완료")


def migrate_confirmed_matches():
    ws = sheets_client._confirmed_worksheet()
    rows = ws.get_all_records(numericise_ignore=["all"])
    print(f"[하멈말확정매칭] 구글시트에서 {len(rows)}건 읽음")
    for row in rows:
        team = row.get("조")
        if not team:
            continue
        db_client._query(
            '''
            INSERT INTO confirmed_matches (team, member_id, nickname, real_name, kakao_display_name, confirmed_at)
            VALUES (%s, %s, %s, %s, %s, COALESCE(NULLIF(%s, '')::timestamptz, now()))
            ON CONFLICT (team, member_id, kakao_display_name) DO NOTHING
            ''',
            (
                str(team), str(row.get("member_id")), row.get("닉네임"), row.get("실명"),
                row.get("카카오표시이름"), row.get("확정일시") or "",
            ),
            fetch="none",
        )
    print(f"[하멈말확정매칭] {len(rows)}건 이관 완료")


def migrate_display_name_overrides():
    total = 0
    for team, members in sheets_client.members_by_team().items():
        overrides = sheets_client.get_display_name_overrides(team)
        for member_id, name in overrides.items():
            db_client.set_display_name_override(team, member_id, name)
            total += 1
    print(f"[표시이름] {total}건 이관 완료")


def main():
    print("=== 구글시트 -> Postgres 마이그레이션 시작 ===")
    migrate_cohorts()
    migrate_members()
    migrate_cache_rows()
    migrate_confirmed_matches()
    migrate_display_name_overrides()
    print("=== 완료 ===")

    print()
    print("--- 검증 (DB에서 다시 읽은 개수) ---")
    print("기수:", len(db_client.get_cohorts()))
    print("명단:", len(db_client.get_all_members()))
    print("현황캐시:", len(db_client.all_cache_rows()))


if __name__ == "__main__":
    main()
