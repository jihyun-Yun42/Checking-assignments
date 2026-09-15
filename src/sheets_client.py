from datetime import datetime, timedelta
import time as _time

import gspread
from google.oauth2.service_account import Credentials

from . import config
from .time_window import now_kst

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_HEADER = ["기수", "이름", "휴대폰번호", "조", "역할", "가입일시"]
_COHORT_SHEET_NAME = "기수"
_CACHE_SHEET_NAME = "현황캐시"
_CACHE_HEADER = ["조", "미제출텍스트", "태그리스트", "갱신시각"]
_CONFIRMED_SHEET_NAME = "하멈말확정매칭"
_CONFIRMED_HEADER = ["조", "member_id", "닉네임", "실명", "카카오표시이름", "확정일시"]
_DISPLAY_NAME_SHEET_NAME = "표시이름"
_DISPLAY_NAME_HEADER = ["조", "member_id", "표시이름", "수정일시"]

_gc = None
_ss = None


def _client():
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
        _gc = gspread.authorize(creds)
    return _gc


def _spreadsheet():
    global _ss
    if _ss is None:
        _ss = _client().open(config.GOOGLE_SHEET_NAME)
    return _ss


_main_ws = None


def _worksheet():
    global _main_ws
    if _main_ws is None:
        _main_ws = _spreadsheet().sheet1
    return _main_ws


# worksheet() 호출은 매번 구글시트 API 읽기 요청을 1회 쓴다(fetch_sheet_metadata).
# 매 요청마다 반복 호출되는 보조 시트들(현황캐시/하멈말확정매칭/표시이름/기수)을
# 프로세스 안에서 캐시해두고 재사용하면 이 불필요한 메타데이터 조회와 헤더 검사를
# 최초 1회로 줄일 수 있다 (분당 읽기 쿼터 초과로 대시보드가 429/500이 나던 원인).
_ws_cache = {}
_header_checked = set()


def _cached_worksheet(name):
    if name not in _ws_cache:
        _ws_cache[name] = _spreadsheet().worksheet(name)
    return _ws_cache[name]


# --- 임시 TTL 캐시 (2026-09, 구글시트 분당 읽기 쿼터 초과 임시 대응용) ---
# 동시에 여러 조장이 새로고침하면 이 데이터들을 매번 새로 읽는 것만으로도
# 쿼터(분당 60회)를 넘겨서 500 에러가 났다. 짧은 시간(기본 25초) 동안은
# 프로세스 메모리에 캐시해두고 재사용한다. 표시이름(이름 변경)은 사용자가
# 바로 반영되는 걸 확인하는 기능이라 이 캐시 대상에서 일부러 제외했다.
# TODO: 구글 클라우드 쿼터 증설이 승인되거나 더 나은 구조로 바뀌면 이 캐시는 재검토/제거할 것.
_TTL_SECONDS = 25

_members_cache = {"data": None, "ts": 0.0}
_cohorts_cache = {"data": None, "ts": 0.0}
_cache_rows_cache = {"data": None, "ts": 0.0}


def _ttl_get(cache):
    if cache["data"] is not None and (_time.time() - cache["ts"]) < _TTL_SECONDS:
        return cache["data"]
    return None


def _ttl_set(cache, data):
    cache["data"] = data
    cache["ts"] = _time.time()


def _ttl_invalidate(cache):
    cache["data"] = None
    cache["ts"] = 0.0


def _get_or_create_worksheet(name, header):
    try:
        ws = _cached_worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = _spreadsheet().add_worksheet(title=name, rows=100, cols=max(len(header), 3))
        ws.append_row(header)
        _ws_cache[name] = ws
        _header_checked.add(name)
        return ws

    if name not in _header_checked:
        current_header = ws.row_values(1)
        if current_header != header:
            if ws.col_count < len(header):
                ws.resize(cols=len(header))
            ws.update(values=[header], range_name="A1")
        _header_checked.add(name)
    return ws


def _cache_worksheet():
    return _get_or_create_worksheet(_CACHE_SHEET_NAME, _CACHE_HEADER)


def _confirmed_worksheet():
    return _get_or_create_worksheet(_CONFIRMED_SHEET_NAME, _CONFIRMED_HEADER)


def _display_name_worksheet():
    return _get_or_create_worksheet(_DISPLAY_NAME_SHEET_NAME, _DISPLAY_NAME_HEADER)


def normalize_phone(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())



def get_all_members():
    cached = _ttl_get(_members_cache)
    if cached is not None:
        return cached
    ws = _worksheet()
    rows = ws.get_all_records(numericise_ignore=['all'])
    members = []
    for i, row in enumerate(rows, start=2):
        row["row_index"] = i
        members.append(row)
    _ttl_set(_members_cache, members)
    return members


def get_member_by_phone(phone: str, cohort_name: str = None):
    phone = normalize_phone(phone)
    for m in get_all_members():
        if normalize_phone(m.get("휴대폰번호")) == phone:
            if cohort_name and str(m.get("기수")) != str(cohort_name):
                continue
            return m
    return None


def create_member_signup(phone: str, name: str, team: str, role: str, cohort_name: str) -> dict:
    existing = get_member_by_phone(phone, cohort_name)
    if existing:
        return existing
    ws = _worksheet()
    row = [cohort_name, name, normalize_phone(phone), team, role, datetime.now().isoformat(timespec="seconds")]
    ws.append_row(row)
    _ttl_invalidate(_members_cache)
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
    cached = _ttl_get(_cohorts_cache)
    if cached is not None:
        return cached
    ws = _cached_worksheet(_COHORT_SHEET_NAME)
    cohorts = ws.get_all_records(numericise_ignore=['all'])
    _ttl_set(_cohorts_cache, cohorts)
    return cohorts


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



def _cache_rows_raw():
    cached = _ttl_get(_cache_rows_cache)
    if cached is not None:
        return cached
    ws = _cache_worksheet()
    rows = ws.get_all_records(numericise_ignore=['all'])
    _ttl_set(_cache_rows_cache, rows)
    return rows


def get_cache_row(team: str):
    rows = _cache_rows_raw()
    for i, row in enumerate(rows, start=2):
        if str(row.get("조")) == str(team):
            row["row_index"] = i
            return row
    return None


def set_cache_row(team: str, text: str, tag_text: str = "") -> str:
    ws = _cache_worksheet()
    now_str = now_kst().isoformat(timespec="seconds")
    existing = get_cache_row(team)
    if existing:
        ws.update_cell(existing["row_index"], 2, text)
        ws.update_cell(existing["row_index"], 3, tag_text)
        ws.update_cell(existing["row_index"], 4, now_str)
    else:
        ws.append_row([team, text, tag_text, now_str])
    _ttl_invalidate(_cache_rows_cache)
    return now_str


def all_cache_rows() -> dict:
    result = {}
    for row in _cache_rows_raw():
        team = str(row.get("조"))
        if team:
            result[team] = row
    return result



def get_confirmed_matches(team: str) -> list:
    ws = _confirmed_worksheet()
    return [row for row in ws.get_all_records(numericise_ignore=['all']) if str(row.get("조")) == str(team)]


def add_confirmed_match(team: str, member: dict, kakao_raw: str):
    ws = _confirmed_worksheet()
    for row in ws.get_all_records(numericise_ignore=['all']):
        if (
            str(row.get("조")) == str(team)
            and str(row.get("member_id")) == str(member.get("id"))
            and str(row.get("카카오표시이름")) == str(kakao_raw)
        ):
            return
    ws.append_row([
        team, member.get("id"), member.get("nickname"), member.get("real_name"),
        kakao_raw, now_kst().isoformat(timespec="seconds"),
    ])


def remove_confirmed_match(team: str, member_id, kakao_raw: str) -> bool:
    ws = _confirmed_worksheet()
    rows = ws.get_all_records(numericise_ignore=['all'])
    for i, row in enumerate(rows, start=2):
        if (
            str(row.get("조")) == str(team)
            and str(row.get("member_id")) == str(member_id)
            and str(row.get("카카오표시이름")) == str(kakao_raw)
        ):
            ws.delete_rows(i)
            return True
    return False



def get_display_name_overrides(team: str) -> dict:
    ws = _display_name_worksheet()
    result = {}
    for row in ws.get_all_records(numericise_ignore=['all']):
        if str(row.get("조")) != str(team):
            continue
        member_id = row.get("member_id")
        name = str(row.get("표시이름") or "").strip()
        if member_id and name:
            result[str(member_id)] = name
    return result


def set_display_name_override(team: str, member_id, name: str):
    ws = _display_name_worksheet()
    now_str = now_kst().isoformat(timespec="seconds")
    rows = ws.get_all_records(numericise_ignore=['all'])
    for i, row in enumerate(rows, start=2):
        if str(row.get("조")) == str(team) and str(row.get("member_id")) == str(member_id):
            if not name:
                # 기본값으로 되돌리는 경우: 공란 행을 남겨두지 않고 행 자체를 삭제한다.
                ws.delete_rows(i)
            else:
                ws.update_cell(i, 3, name)
                ws.update_cell(i, 4, now_str)
            return
    if not name:
        # 원래 override가 없던 사람이 공란으로 저장한 경우: 기록할 내용이 없으므로 아무것도 쓰지 않는다.
        return
    ws.append_row([team, str(member_id), name, now_str])
