"""网页证据的点时时间解析与截止判断。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime

TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class ParsedWebTime:
    value: datetime
    precision: str

    def isoformat(self) -> str:
        return self.value.isoformat()


def parse_web_time(value: str, *, default_tz=TRADING_TZ) -> ParsedWebTime | None:
    """解析常见网页时间；日期值保留 date 精度，避免伪造盘中发布时间。"""
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return ParsedWebTime(parsed.astimezone(default_tz), "second")

    normalized = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .strip()
    )
    iso_candidate = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        precision = "date" if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", normalized) else "second"
        return ParsedWebTime(parsed.astimezone(default_tz), precision)

    match = re.search(
        r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})"
        r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        normalized,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    precision = "date" if hour is None else "second"
    try:
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=default_tz,
        )
    except ValueError:
        return None
    return ParsedWebTime(parsed, precision)


def cutoff_status(published: ParsedWebTime | None, cutoff: datetime) -> str:
    """返回 accepted、unknown、ambiguous 或 future。"""
    if published is None:
        return "unknown"
    normalized_cutoff = cutoff.astimezone(TRADING_TZ)
    if published.precision == "date":
        if published.value.date() == normalized_cutoff.date() and normalized_cutoff.time() < time.max:
            return "ambiguous"
        return "accepted" if published.value.date() <= normalized_cutoff.date() else "future"
    return "accepted" if published.value <= normalized_cutoff else "future"


def require_cutoff(value: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("web_as_of 必须是包含时间和时区的 ISO 8601 时间")
    return parsed.astimezone(TRADING_TZ)
