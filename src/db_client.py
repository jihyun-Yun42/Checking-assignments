from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras

from . import config
from .time_window import now_kst

# 구글시트를 쓰던 sheets_client.py를 대체하는 모듈.
# Postgres(Supabase)에 직접 연결해서 같은 함수 이름/반환 형태(한글 키 dict)를
# 그대로 유지하므로, app.py 등 호출부는 최소한만 고쳐도 된다.
#
# 구글시트 API와 달리 분당 읽기 쿼터 같은 게 없어서(대역폭 안에서는 요청 무제한),
# sheets_client.py에 있던 TTL 캐시/워크시트 캐시 로직은 여기선 필요 없다 —
# 매번 그냥 최신값을 바로 읽는다.

_conn = None


def _connect():
    conn = psycopg2.connect(config.DATABASE_URL)
    conn.autocommit = True
    return conn


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = _connect()
    return _conn


def _query(sql, params=None, fetch="all"):
    """
    fetch: "all"(여러 행) | "one"(한 행 또는 None) | "none"(쓰기 전용, 반환값 없음)

    커넥션이 유휴 상태로 끊겨 있으면(풀러가 오래된 연결을 정리하는 경우 등) 한 번
    재연결해서 다시 시도한다.
    """
    global _conn
    last_err = None
    for attempt in (1, 2):
        conn = _get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or ())
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                return None
        except psycopg2.OperationalError as e:
            last_err = e
            _conn = None
            if attempt == 2:
                raise
    raise last_err  # pragma: no cover


def normalize_phone(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


_MEMBER_COLS = '''
    cohort AS "기수",
    name AS "이름",
    phone AS "휴대폰번호",
    team AS "조",
    role AS "역할",
    to_char(joined_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD"T"HH24:MI:SS') AS "가입일시"
'''


def get_all_members():
    return _query(f"SELECT {_MEMBER_COLS} FROM members ORDER BY id")


def get_member_by_phone(phone: str, cohort_name: str = None):
    phone = normalize_phone(phone)
    if cohort_name:
        return _query(
            f"SELECT {_MEMBER_COLS} FROM members WHERE phone = %s AND cohort = %s ORDER BY id LIMIT 1",
            (phone, str(cohort_name)),
            fetch="one",
        )
    return _query(
        f"SELECT {_MEMBER_COLS} FROM members WHERE phone = %s ORDER BY id LIMIT 1",
        (phone,),
        fetch="one",
    )


def create_member_signup(phone: str, name: str, team: str, role: str, cohort_name: str) -> dict:
    existing = get_member_by_phone(phone, cohort_name)
    if existing:
        return existing
    _query(
        '''
        INSERT INTO members (cohort, name, phone, team, role, joined_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (cohort, phone) DO NOTHING
        ''',
        (cohort_name, name, normalize_phone(phone), team, role, now_kst()),
        fetch="none",
    )
    return get_member_by_phone(phone, cohort_name)


def members_by_team():
    teams = {}
    for m in get_all_members():
        team = str(m.get("조"))
        teams.setdefault(team, []).append(m)
    return teams


def _parse_date(value):
    value = str(value).strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def get_cohorts():
    return _query(
        '''
        SELECT
            name AS "기수명",
            to_char(start_date, 'YYYY-MM-DD') AS "시작일",
            to_char(end_date, 'YYYY-MM-DD') AS "종료일"
        FROM cohorts
        ORDER BY id
        '''
    )


def get_active_cohort(today=None):
    from datetime import date
    today = today or date.today()
    cohorts = get_cohorts()
    for cohort in cohorts:
        start = _parse_date(cohort.get("시작일"))
        end = _parse_date(cohort.get("종료일"))
        if start and end and start <= today <= end:
            return cohort
    if config.HAMUMMAL_TEST_MODE:
        dated = [(c, _parse_date(c.get("시작일"))) for c in cohorts]
        dated = [(c, d) for c, d in dated if d]
        if dated:
            return max(dated, key=lambda x: x[1])[0]
    return None


def hamummal_window(cohort):
    start = _parse_date(cohort.get("시작일")) if cohort else None
    if not start:
        return None, None
    this_week_monday = start - timedelta(days=start.weekday())
    hamummal_start = this_week_monday + timedelta(days=7)
    hamummal_end = hamummal_start + timedelta(days=20)
    return hamummal_start, hamummal_end


def is_hamummal_active(cohort, today=None):
    from datetime import date
    if not cohort:
        return False
    today = today or date.today()
    start, end = hamummal_window(cohort)
    return bool(start and end and start <= today <= end)


def get_active_members():
    cohort = get_active_cohort()
    if not cohort:
        return []
    cohort_name = str(cohort.get("기수명"))
    return [m for m in get_all_members() if str(m.get("기수")) == cohort_name]


def active_members_by_team():
    teams = {}
    for m in get_active_members():
        team = str(m.get("조"))
        teams.setdefault(team, []).append(m)
    return teams


_CACHE_COLS = '''
    team AS "조",
    missing_text AS "미제출텍스트",
    tag_text AS "태그리스트",
    to_char(updated_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD"T"HH24:MI:SS') AS "갱신시각"
'''


def get_cache_row(team: str):
    return _query(
        f"SELECT {_CACHE_COLS} FROM cache_rows WHERE team = %s",
        (str(team),),
        fetch="one",
    )


def set_cache_row(team: str, text: str, tag_text: str = "") -> str:
    now = now_kst()
    _query(
        '''
        INSERT INTO cache_rows (team, missing_text, tag_text, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (team) DO UPDATE SET
            missing_text = EXCLUDED.missing_text,
            tag_text = EXCLUDED.tag_text,
            updated_at = EXCLUDED.updated_at
        ''',
        (str(team), text, tag_text, now),
        fetch="none",
    )
    return now.isoformat(timespec="seconds")


def all_cache_rows() -> dict:
    rows = _query(f"SELECT {_CACHE_COLS} FROM cache_rows")
    result = {}
    for row in rows:
        team = str(row.get("조"))
        if team:
            result[team] = row
    return result


def get_confirmed_matches(team: str) -> list:
    return _query(
        '''
        SELECT
            team AS "조",
            member_id AS "member_id",
            nickname AS "닉네임",
            real_name AS "실명",
            kakao_display_name AS "카카오표시이름",
            to_char(confirmed_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD"T"HH24:MI:SS') AS "확정일시"
        FROM confirmed_matches WHERE team = %s
        ''',
        (str(team),),
    )


def add_confirmed_match(team: str, member: dict, kakao_raw: str):
    _query(
        '''
        INSERT INTO confirmed_matches (team, member_id, nickname, real_name, kakao_display_name, confirmed_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (team, member_id, kakao_display_name) DO NOTHING
        ''',
        (
            str(team), str(member.get("id")), member.get("nickname"), member.get("real_name"),
            str(kakao_raw), now_kst(),
        ),
        fetch="none",
    )


def remove_confirmed_match(team: str, member_id, kakao_raw: str) -> bool:
    rows = _query(
        '''
        DELETE FROM confirmed_matches
        WHERE team = %s AND member_id = %s AND kakao_display_name = %s
        RETURNING id
        ''',
        (str(team), str(member_id), str(kakao_raw)),
    )
    return bool(rows)


def get_display_name_overrides(team: str) -> dict:
    rows = _query(
        "SELECT member_id, display_name FROM display_name_overrides WHERE team = %s",
        (str(team),),
    )
    result = {}
    for row in rows:
        member_id = row.get("member_id")
        name = str(row.get("display_name") or "").strip()
        if member_id and name:
            result[str(member_id)] = name
    return result


def set_display_name_override(team: str, member_id, name: str):
    if not name:
        # 기본값으로 되돌리는 경우: 공란 행을 남겨두지 않고 행 자체를 삭제한다.
        # (없던 override를 공란으로 저장한 경우도 그냥 DELETE가 no-op이라 안전하다.)
        _query(
            "DELETE FROM display_name_overrides WHERE team = %s AND member_id = %s",
            (str(team), str(member_id)),
            fetch="none",
        )
        return
    _query(
        '''
        INSERT INTO display_name_overrides (team, member_id, display_name, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (team, member_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            updated_at = EXCLUDED.updated_at
        ''',
        (str(team), str(member_id), name, now_kst()),
        fetch="none",
    )
