import csv
import re
from datetime import datetime

import pytz

TZ = pytz.timezone("Asia/Seoul")

LINE_PATTERN = re.compile(
    r"^(?P<date>\d{4}년 \d{1,2}월 \d{1,2}일) (?P<ampm>오전|오후) (?P<hour>\d{1,2}):(?P<minute>\d{2}), "
    r"(?P<sender>[^:]+) : (?P<content>.*)$"
)

CSV_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

SUBMISSION_TAG_KEYWORDS = ["음성메시지", "동영상"]

MEDIA_EXTENSIONS = {
    ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".wma",
    ".mp4", ".mov", ".3gp", ".avi", ".webm", ".mkv",
}


def _extension_of(text: str) -> str:
    text = text.strip()
    if "." not in text:
        return ""
    return "." + text.rsplit(".", 1)[-1].lower()


def _is_submission(content: str) -> bool:
    content = content.strip()
    if any(k in content for k in SUBMISSION_TAG_KEYWORDS):
        return True
    if content.startswith("파일:"):
        filename = content[len("파일:"):].strip()
        return _extension_of(filename) in MEDIA_EXTENSIONS
    return _extension_of(content) in MEDIA_EXTENSIONS


def _to_datetime(date_str, ampm, hour, minute):
    y, mo, d = re.match(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일", date_str).groups()
    hour = int(hour) % 12
    if ampm == "오후":
        hour += 12
    return TZ.localize(datetime(int(y), int(mo), int(d), hour, int(minute)))


def _iter_messages(txt_path: str):
    """
    (전송시각 datetime, 발신자, 메시지내용) 튜플을 시간순으로 yield한다.

    카카오톡 "대화 내보내기" txt 포맷
      (예: "2026년 8월 25일 오후 1:30, 닉네임 : 내용")
    과, Date,User,Message 3컬럼 CSV 포맷(최근에 쓰기 시작한 내보내기 방식,
      예: '2026-08-25 13:30:12,"닉네임","음성메시지"')
    둘 다 지원한다. 파일 맨 앞 줄을 보고 어느 포맷인지 자동으로 판단한다.
    """
    with open(txt_path, encoding="utf-8-sig", newline="") as f:
        first_line = f.readline()
        f.seek(0)
        header_cells = next(csv.reader([first_line]), [])
        is_csv = [c.strip().lower() for c in header_cells[:3]] == ["date", "user", "message"]

        if is_csv:
            reader = csv.reader(f)
            next(reader, None)  # 헤더 줄 건너뛰기
            for row in reader:
                if len(row) < 3:
                    continue
                date_str, sender, content = row[0], row[1], row[2]
                try:
                    ts = TZ.localize(datetime.strptime(date_str.strip(), CSV_DATE_FORMAT))
                except ValueError:
                    continue
                yield ts, sender.strip(), content
        else:
            for line in f:
                line = line.strip()
                m = LINE_PATTERN.match(line)
                if not m:
                    continue
                ts = _to_datetime(m.group("date"), m.group("ampm"), m.group("hour"), m.group("minute"))
                yield ts, m.group("sender").strip(), m.group("content")


def extract_submitters(txt_path: str, window_start: datetime, window_end: datetime) -> set:
    submitters = set()
    for ts, sender, content in _iter_messages(txt_path):
        if not _is_submission(content):
            continue
        if window_start <= ts <= window_end:
            submitters.add(sender)
    return submitters


def extract_submission_times(txt_path: str, window_start: datetime, window_end: datetime) -> dict:
    """
    extract_submitters와 같은 날짜/시간 범위에서, 보낸 사람별로 "처음" 제출
    메시지를 보낸 시각을 뽑아 돌려준다("제출자 목록에 몇 시 몇 분에 제출했는지"
    표시용). 제출 인정 여부 판정 자체는 여전히 extract_submitters만 쓴다 —
    여긴 시각 정보만 덧붙이는 용도라, 같은 사람이 그 시간대에 여러 번 보냈으면
    가장 이른 시각을 돌려준다.
    """
    times = {}
    for ts, sender, content in _iter_messages(txt_path):
        if not _is_submission(content):
            continue
        if not (window_start <= ts <= window_end):
            continue
        if sender not in times or ts < times[sender]:
            times[sender] = ts
    return times


def format_submission_time(ts) -> str:
    # "13:34" 형식(24시간제) — 결과확인 화면에 그대로 보여줄 용도.
    return f"{ts.hour:02d}:{ts.minute:02d}"


def extract_all_submitters(txt_path: str) -> set:
    """
    날짜·시간대와 상관없이, 업로드한 파일 전체에서 하멈말 제출 메시지를 보낸
    적 있는 모든 사람의 집합을 돌려준다. "이름 고치기" 팝업에서 검색할 전체
    참가자 목록으로만 쓰고, 실제 매칭 판정(그 날짜에 정말 보냈는지)은 여전히
    날짜별로 window로 걸러낸 extract_submitters 결과로만 한다 — 그래야 진짜
    미제출인 날이 과거 이름만 보고 제출로 둔갑하는 일이 없다.
    """
    submitters = set()
    for ts, sender, content in _iter_messages(txt_path):
        if not _is_submission(content):
            continue
        submitters.add(sender)
    return submitters


def detect_candidate_dates(txt_path: str, min_senders: int = 20) -> list:
    """
    대화 파일 전체를 날짜 구분 없이 훑어서, 하루에 "음성메시지"를 보낸 서로 다른
    사람이 min_senders(기본 20)명 이상인 날짜들을 찾아 오름차순으로 반환한다.
    (하멈말이 실제로 진행된 날짜로 추정되는 후보 목록 — 동영상/파일 제출은 세지
    않고 "음성메시지"만 기준으로 삼는다. 실제 제출 인정 기준(extract_submitters)은
    그대로 유지되고, 이 함수는 어떤 날짜를 분석할지 후보만 골라낸다.)
    """
    senders_by_date = {}
    for ts, sender, content in _iter_messages(txt_path):
        if "음성메시지" not in content.strip():
            continue
        date_str = ts.date().isoformat()
        senders_by_date.setdefault(date_str, set()).add(sender)
    return sorted(d for d, senders in senders_by_date.items() if len(senders) >= min_senders)
