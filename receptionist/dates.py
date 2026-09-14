"""Cairo-aware parsing for appointment dates, including common relative phrases."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_WEEKDAYS = {
    "monday": 0, "mon": 0, "الاثنين": 0, "الإثنين": 0,
    "tuesday": 1, "tue": 1, "الثلاثاء": 1,
    "wednesday": 2, "wed": 2, "الاربعاء": 2, "الأربعاء": 2,
    "thursday": 3, "thu": 3, "الخميس": 3,
    "friday": 4, "fri": 4, "الجمعة": 4, "الجمعه": 4,
    "saturday": 5, "sat": 5, "السبت": 5,
    "sunday": 6, "sun": 6, "الاحد": 6, "الأحد": 6,
}


def _today(now: dt.datetime | dt.date | None, timezone: ZoneInfo) -> dt.date:
    if now is None:
        return dt.datetime.now(timezone).date()
    if isinstance(now, dt.datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone)
        return now.astimezone(timezone).date()
    return now


def parse_date_expression(
    value: str,
    *,
    now: dt.datetime | dt.date | None = None,
    timezone: ZoneInfo = CAIRO,
) -> dt.date:
    raw = (value or "").strip().translate(_ARABIC_DIGITS)
    if not raw:
        raise ValueError("التاريخ مطلوب.")
    normalized = re.sub(r"\s+", " ", raw.lower()).strip()
    today = _today(now, timezone)

    if normalized in {"today", "النهاردة", "النهارده", "اليوم"}:
        return today
    if normalized in {"tomorrow", "tmrw", "بكرة", "بكره", "غداً", "غدا"}:
        return today + dt.timedelta(days=1)
    if normalized in {"day after tomorrow", "بعد بكرة", "بعد بكره"}:
        return today + dt.timedelta(days=2)

    is_next = bool(
        re.search(r"(^next\s+)|(\s+next$)", normalized)
        or any(x in normalized for x in (" الجاي", " القادم", " اللى جاى", " اللي جاي"))
    )
    cleaned = normalized
    for token in ("next ", " next", " الجاي", " القادم", " اللى جاى", " اللي جاي"):
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.strip()
    if cleaned in _WEEKDAYS:
        target = _WEEKDAYS[cleaned]
        delta = (target - today.weekday()) % 7
        if is_next:
            delta = delta or 7
        return today + dt.timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError("صيغة التاريخ غير مفهومة. استخدمي تاريخاً مثل 2026-09-20 أو قولي بكرة/الخميس.")
