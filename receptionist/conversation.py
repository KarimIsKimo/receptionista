"""Deterministic conversation helpers for booking drafts and message batching."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .clinic import CLINIC, ClinicConfig
from .dates import parse_date_expression


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_BOOK_WORDS = ("احجز", "حجز", "موعد", "book", "appointment")
_CANCEL_WORDS = ("الغي", "إلغاء", "الغاء", "cancel")
_RESCHEDULE_WORDS = (
    "تغيير الموعد",
    "غير الموعد",
    "اغير الموعد",
    "أغير الموعد",
    "reschedule",
)
_ABANDON_WORDS = (
    "خلاص مش عايز",
    "خلاص مش عايزة",
    "مش هاحجز",
    "مش هحجز",
    "سيبك",
    "never mind",
    "nevermind",
    "forget it",
)
_DATE_PHRASES = (
    "day after tomorrow",
    "بعد بكرة",
    "بعد بكره",
    "next saturday",
    "next sunday",
    "next monday",
    "next tuesday",
    "next wednesday",
    "next thursday",
    "السبت الجاي",
    "الأحد الجاي",
    "الاحد الجاي",
    "الاثنين الجاي",
    "الإثنين الجاي",
    "الثلاثاء الجاي",
    "الأربعاء الجاي",
    "الاربعاء الجاي",
    "الخميس الجاي",
    "tomorrow",
    "today",
    "بكرة",
    "بكره",
    "النهاردة",
    "النهارده",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "الاثنين",
    "الإثنين",
    "الثلاثاء",
    "الأربعاء",
    "الاربعاء",
    "الخميس",
    "الجمعة",
    "الجمعه",
    "السبت",
    "الأحد",
    "الاحد",
)
_SERVICES = (
    ("جسم كامل", "جسم كامل"),
    ("full body", "جسم كامل"),
    ("اندر ارم", "أندر آرم"),
    ("أندر آرم", "أندر آرم"),
    ("underarm", "أندر آرم"),
    ("بيكيني", "بيكيني"),
    ("bikini", "بيكيني"),
    ("ذقن", "ذقن"),
    ("beard", "ذقن"),
    ("وجه", "وجه"),
    ("face", "وجه"),
    ("رقبة", "رقبة"),
    ("neck", "رقبة"),
    ("بوکسر", "بوكسر"),
    ("بوكسر", "بوكسر"),
)
_TRIVIAL_PHRASES = {
    "hi",
    "hello",
    "hey",
    "هاي",
    "هلا",
    "اهلا",
    "أهلا",
    "السلام عليكم",
    "وعليكم السلام",
    "ok",
    "okay",
    "تمام",
    "ماشي",
    "شكرا",
    "شكراً",
    "thanks",
    "thank you",
    "حاضر",
}


def combine_inbound_messages(messages: list[dict[str, Any]]) -> str:
    """Combine an ordered burst without discarding any non-empty fragment."""
    return "\n".join(
        str(message.get("content") or "").strip()
        for message in messages
        if str(message.get("content") or "").strip()
    )


def should_extract_memory(message: str) -> bool:
    """Skip only clearly transient inputs; meaningful human-takeover turns remain eligible."""
    text = re.sub(r"\s+", " ", (message or "").strip()).strip(".!،,؟?").lower()
    if not text:
        return False
    if text in _TRIVIAL_PHRASES:
        return False
    # No letters or digits means emoji/punctuation-only.
    if not re.search(r"[A-Za-z0-9\u0600-\u06FF]", text):
        return False
    # A bare time fragment belongs in the booking draft, not long-term memory.
    if re.fullmatch(r"(?:الساعة\s*)?(?:بعد\s*)?[0-9٠-٩]{1,2}(?::[0-9٠-٩]{2})?\s*(?:am|pm|ص|م)?", text):
        return False
    return True


def _find_intent(text: str, current: dict[str, Any]) -> str:
    if any(word in text for word in _RESCHEDULE_WORDS):
        return "reschedule"
    if any(word in text for word in _CANCEL_WORDS):
        return "cancel"
    if any(word in text for word in _BOOK_WORDS):
        return "book"
    return str(current.get("intent") or "")


def _find_name(message: str) -> str:
    for pattern in (
        r"(?:اسمي|الاسم)\s*[:：]?\s*([\u0600-\u06FF ]{2,60})",
        r"(?:my name is|name is)\s+([A-Za-z ]{2,60})",
    ):
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            candidate = re.split(r"[\n،,.!?]", match.group(1))[0].strip()
            candidate = re.split(
                r"\s+(?:وعايز(?:ة)?|عايز(?:ة)?|احجز|أحجز|حجز|موعد|ليزر|بكرة|بكره|tomorrow|book)\b",
                candidate,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
            return candidate
    return ""


def _find_service(text: str) -> str:
    for token, canonical in _SERVICES:
        if token.lower() in text:
            return canonical
    return ""


def _find_date(text: str, now: dt.datetime, config: ClinicConfig) -> str:
    normalized = text.translate(_ARABIC_DIGITS).lower()
    absolute = re.search(r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b", normalized)
    candidates = [absolute.group(0)] if absolute else []
    candidates.extend(phrase for phrase in _DATE_PHRASES if phrase in normalized)
    for candidate in candidates:
        try:
            return parse_date_expression(
                candidate,
                now=now,
                timezone=config.timezone,
            ).isoformat()
        except ValueError:
            continue
    return ""


def _find_time(text: str, *, active_booking: bool, config: ClinicConfig) -> str:
    normalized = text.translate(_ARABIC_DIGITS).lower().replace("صباحاً", "am").replace("مساءً", "pm")
    patterns = [
        r"(?:الساعة|الساعه|بعد|at)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|ص|م)?",
        r"\b(\d{1,2}):(\d{2})\s*(am|pm|ص|م)?\b",
    ]
    if active_booking:
        patterns.append(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|ص|م)?\s*$")
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        suffix = (match.group(3) or "").lower()
        if minute not in {0, 30}:
            continue
        if suffix in {"pm", "م"} and hour < 12:
            hour += 12
        elif suffix in {"am", "ص"} and hour == 12:
            hour = 0
        elif not suffix and 1 <= hour <= 10:
            # The clinic only operates from noon; a bare 1–10 is therefore PM.
            hour += 12
        if hour > 23:
            continue
        value = dt.datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")
        if value in config.slots:
            return value
    return ""


def _stage(draft: dict[str, Any]) -> str:
    intent = draft.get("intent")
    if intent == "cancel":
        return "cancelling"
    if intent == "reschedule":
        if not draft.get("requested_date"):
            return "need_new_date"
        if not draft.get("requested_time"):
            return "need_new_time"
        return "rescheduling"
    if intent != "book":
        return "idle"
    for field, stage in (
        ("patient_name", "need_name"),
        ("service_area", "need_service"),
        ("requested_date", "need_date"),
        ("requested_time", "need_time"),
    ):
        if not draft.get(field):
            return stage
    return "ready_to_book"


def evolve_booking_draft(
    current: dict[str, Any] | None,
    message: str,
    profile: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
    config: ClinicConfig = CLINIC,
) -> tuple[dict[str, str] | None, str]:
    """Return (draft, action), where action is upsert, clear, or none."""
    text = re.sub(r"\s+", " ", (message or "").strip())
    lowered = text.lower()
    if any(phrase in lowered for phrase in _ABANDON_WORDS):
        return None, "clear"

    draft: dict[str, Any] = dict(current or {})
    intent = _find_intent(lowered, draft)
    if not intent:
        return None, "none"
    if current and current.get("intent") and current.get("intent") != intent:
        # Do not leak a previous booking's date/time into a newly stated
        # cancellation or reschedule flow.
        draft = {
            "patient_name": current.get("patient_name") or "",
            "service_area": current.get("service_area") or "",
        }
    draft["intent"] = intent
    profile = profile or {}
    if not draft.get("patient_name"):
        draft["patient_name"] = (profile.get("name") or _find_name(text)).strip()
    service = _find_service(lowered)
    if service:
        draft["service_area"] = service
    now = now or config.now()
    date = _find_date(lowered, now, config)
    if date:
        draft["requested_date"] = date
    time = _find_time(lowered, active_booking=bool(current or intent), config=config)
    if time:
        draft["requested_time"] = time
    draft["stage"] = _stage(draft)
    return {
        key: str(draft.get(key) or "")
        for key in (
            "intent",
            "patient_name",
            "service_area",
            "requested_date",
            "requested_time",
            "stage",
        )
    }, "upsert"
