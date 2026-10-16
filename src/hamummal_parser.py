import re
from datetime import datetime

import pytz

TZ = pytz.timezone("Asia/Seoul")

LINE_PATTERN = re.compile(
    r"^(?P<date>\d{4}년 \d{1,2}월 \d{1,2}일) (?P<ampm>오전|오후) (?P<hour>\d{1,2}):(?P<minute>\d{2}), "
    r"(?P<sender>[^:]+) : (?P<content>.*)$"
)

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


def extract_submitters(txt_path: str, window_start: datetime, window_end: datetime) -> set:
    submitters = set()
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = LINE_PATTERN.match(line)
            if not m:
                continue
            content = m.group("content")
            if not _is_submission(content):
                continue
            ts = _to_datetime(m.group("date"), m.group("ampm"), m.group("hour"), m.group("minute"))
            if window_start <= ts <= window_end:
                submitters.add(m.group("sender").strip())
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
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = LINE_PATTERN.match(line)
            if not m:
                continue
            content = m.group("content").strip()
            if "음성메시지" not in content:
                continue
            ts = _to_datetime(m.group("date"), m.group("ampm"), m.group("hour"), m.group("minute"))
            date_str = ts.date().isoformat()
            senders_by_date.setdefault(date_str, set()).add(m.group("sender").strip())
    return sorted(d for d, senders in senders_by_date.items() if len(senders) >= min_senders)
