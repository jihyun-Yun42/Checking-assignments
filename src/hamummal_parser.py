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
