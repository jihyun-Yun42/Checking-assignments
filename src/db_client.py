import json
import uuid
from datetime import datetime

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
            to_char(end_date, 'YYYY-MM-DD') AS "종료일",
            hamummal_review_visible AS "하멈말공개여부",
            hamummal_apply_enabled AS "하멈말반영허용여부",
            to_char(hamummal_start_date, 'YYYY-MM-DD') AS "하멈말시작일"
        FROM cohorts
        ORDER BY id
        '''
    )


def get_active_cohort(today=None):
    today = today or now_kst().date()
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


def is_hamummal_review_visible(cohort) -> bool:
    # 예전엔 기수 시작일 기준으로 자동 계산한 기간(hamummal_window)으로 노출 여부를
    # 판단했는데, 지금은 관리자가 하멈말 업로드 페이지에서 수동으로 켜고 끄는
    # 방식으로 바뀌었다 (기수별 cohorts.hamummal_review_visible 컬럼).
    if not cohort:
        return False
    return bool(cohort.get("하멈말공개여부"))


def set_hamummal_review_visible(cohort_name: str, visible: bool) -> None:
    _query(
        "UPDATE cohorts SET hamummal_review_visible = %s WHERE name = %s",
        (bool(visible), str(cohort_name)),
        fetch="none",
    )


def is_hamummal_apply_enabled(cohort) -> bool:
    # "소리튠 조장페이지에 반영하기" 버튼을 실제로 누를 수 있게 할지도 관리자가
    # 업로드 페이지에서 수동으로 켜고 끈다 (예전엔 HAMUMMAL_TEST_MODE 환경변수로
    # 배포 전체를 한번에 켜고 껐는데, 개발 중엔 로컬/실서비스를 번갈아 켜보기
    # 불편해서 DB 토글로 바꿨다).
    if not cohort:
        return False
    return bool(cohort.get("하멈말반영허용여부"))


def set_hamummal_apply_enabled(cohort_name: str, enabled: bool) -> None:
    _query(
        "UPDATE cohorts SET hamummal_apply_enabled = %s WHERE name = %s",
        (bool(enabled), str(cohort_name)),
        fetch="none",
    )


def set_hamummal_start_date(cohort_name: str, date_str: str) -> None:
    _query(
        "UPDATE cohorts SET hamummal_start_date = %s WHERE name = %s",
        (date_str, str(cohort_name)),
        fetch="none",
    )


def get_hamummal_date_windows(cohort_name: str) -> dict:
    rows = _query(
        '''
        SELECT to_char(check_date, 'YYYY-MM-DD') AS check_date, start_time, end_time
        FROM hamummal_date_windows WHERE cohort = %s
        ''',
        (str(cohort_name),),
    )
    return {r["check_date"]: {"start": r["start_time"], "end": r["end_time"]} for r in rows}


def set_hamummal_date_window(cohort_name: str, date_str: str, start_time: str, end_time: str) -> None:
    _query(
        '''
        INSERT INTO hamummal_date_windows (cohort, check_date, start_time, end_time, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cohort, check_date) DO UPDATE SET
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            updated_at = EXCLUDED.updated_at
        ''',
        (str(cohort_name), date_str, start_time, end_time, now_kst()),
        fetch="none",
    )


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


# --- 하멈말 리뷰 결과 (예전엔 로컬 파일(data/hamummal_review/*.json)에 저장했는데,
# Vercel 등 서버리스 환경에서는 요청 간에 로컬 디스크가 유지된다는 보장이 없어서
# Postgres로 옮김) ---

def save_review(
    review: dict, check_date_str: str, window_desc: str, submitter_count: int = 0, chat_senders=None,
) -> str:
    review_id = uuid.uuid4().hex[:12]
    _query(
        '''
        INSERT INTO hamummal_reviews (id, check_date, window_desc, review, submitter_count, chat_senders)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb)
        ''',
        (
            review_id, check_date_str, window_desc, json.dumps(review, ensure_ascii=False),
            submitter_count, json.dumps(chat_senders or [], ensure_ascii=False),
        ),
        fetch="none",
    )
    return review_id


def load_review(review_id: str):
    row = _query(
        '''
        SELECT
            to_char(check_date, 'YYYY-MM-DD') AS check_date,
            window_desc, review, submitter_count, applied_teams, chat_senders
        FROM hamummal_reviews WHERE id = %s
        ''',
        (review_id,),
        fetch="one",
    )
    if not row:
        return None
    return {
        "check_date": row["check_date"],
        "window_desc": row["window_desc"],
        "review": row["review"],
        "submitter_count": row["submitter_count"],
        "applied_teams": row["applied_teams"] or {},
        "chat_senders": row.get("chat_senders") or [],
    }


def get_latest_review_id():
    # 대표 한 명이 업로드하면 다른 조장들은 가장 최근 리뷰를 바로 볼 수 있다
    # (각자 다시 업로드할 필요 없음).
    row = _query(
        "SELECT id FROM hamummal_reviews ORDER BY created_at DESC LIMIT 1",
        fetch="one",
    )
    return row["id"] if row else None


def has_any_review() -> bool:
    row = _query("SELECT 1 FROM hamummal_reviews LIMIT 1", fetch="one")
    return bool(row)


def list_review_dates() -> list:
    # 한 번의 업로드로 여러 날짜가 한꺼번에 생길 수 있어서, 날짜별로 가장 최근
    # 업로드 1건씩만 추려서 보여준다 (같은 날짜를 다시 업로드했으면 최신 것으로 교체).
    return _query(
        '''
        SELECT DISTINCT ON (check_date)
            id, to_char(check_date, 'YYYY-MM-DD') AS check_date
        FROM hamummal_reviews
        ORDER BY check_date DESC, created_at DESC
        '''
    )


def list_team_reviews(team: str) -> list:
    # 이 조 데이터가 들어있는 모든 검토를 날짜순으로 반환한다. 한 번의 업로드로
    # 여러 날짜(check_date)가 한꺼번에 분석돼서 각각 별도의 row로 저장되므로,
    # fix-name에서 같은 조원이 여러 날짜에 걸쳐 미제출로 남아있을 때 전부
    # 한꺼번에 고치려면 이렇게 team 데이터가 있는 모든 row를 가져와야 한다
    # ("?" 연산자: review jsonb가 team을 top-level 키로 가지고 있는지 검사).
    return _query(
        '''
        SELECT
            id, to_char(check_date, 'YYYY-MM-DD') AS check_date,
            review, applied_teams, chat_senders
        FROM hamummal_reviews
        WHERE review ? %s
        ORDER BY check_date
        ''',
        (str(team),),
    )


def update_review_team_entry(review_id: str, team: str, entry: dict) -> None:
    # 미제출로 뜬 조원의 이름을 "결과확인" 페이지에서 바로 고쳤을 때처럼, 이미
    # 저장된 검토의 특정 조 데이터만 콕 집어서 덮어쓸 때 쓴다 (jsonb_set으로
    # review 컬럼 중 해당 조 키만 교체 — 다른 조 데이터는 건드리지 않음).
    _query(
        '''
        UPDATE hamummal_reviews
        SET review = jsonb_set(review, ARRAY[%s], %s::jsonb)
        WHERE id = %s
        ''',
        (str(team), json.dumps(entry, ensure_ascii=False), review_id),
        fetch="none",
    )


def mark_review_team_applied(review_id: str, team: str, apply_results: dict) -> None:
    # 반영은 조별로 각자 진행하므로(조장마다 자기 조만 반영), 적용 여부도
    # 검토 전체가 아니라 조 단위로 기록한다. jsonb 병합 연산자(||)로
    # applied_teams 안의 해당 조 키만 갱신한다.
    payload = {
        "applied_at": now_kst().isoformat(timespec="seconds"),
        "apply_results": apply_results,
    }
    _query(
        '''
        UPDATE hamummal_reviews
        SET applied_teams = applied_teams || jsonb_build_object(%s, %s::jsonb)
        WHERE id = %s
        ''',
        (team, json.dumps(payload, ensure_ascii=False), review_id),
        fetch="none",
    )
