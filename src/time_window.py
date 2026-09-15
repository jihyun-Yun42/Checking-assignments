from datetime import datetime, timedelta, date as date_cls
import pytz

from . import config

TZ = pytz.timezone(config.TIMEZONE)


def now_kst() -> datetime:
    return datetime.now(TZ)


def dashboard_check_date(now: datetime = None) -> date_cls:
    now = now or now_kst()
    d = now.date()
    if now.hour < 7:
        d -= timedelta(days=1)
    return d


def format_kst_timestamp(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    else:
        dt = dt.astimezone(TZ)
    ampm = "오전" if dt.hour < 12 else "오후"
    hour12 = dt.hour % 12 or 12
    return f"{dt.month}월 {dt.day}일 {ampm} {hour12}:{dt.minute:02d} 기준"
