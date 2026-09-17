"""Deterministic conversation helpers for booking drafts and message batching."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .clinic import CLINIC, ClinicConfig
from .dates import parse_date_expression


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_BOOK_PATTERNS = (
    r"(?:^|\s)(?:احجز|أحجز|احجزي|أحجزي)(?:\s|$)",
    r"(?:عايز|عايزة|محتاج|محتاجة|ممكن)\s+(?:احجز|أحجز|حجز\s+جديد)",
    r"\b(?:book|make)\s+(?:a\s+)?(?:new\s+)?appointment\b",
)
_CANCEL_WORDS = (
    "ألغي",
    "الغي",
    "ألغى",
    "الغى",
    "إلغاء",
    "الغاء",
    "cancel",
)
_RESCHEDULE_PATTERNS = (
    r"(?:[اأ]?غير|تغيير)\s+(?:الموعد|موعدي|الحجز|حجزي)",
    r"(?:عايز|عايزة|محتاج|محتاجة)\s+(?:[اأ]?غير|تغيير)",
    r"\b(?:reschedule|change\s+(?:my\s+)?appointment)\b",
)
_EXISTING_APPOINTMENT_PATTERNS = (
    r"(?:^|\s)عندي\s+(?:حجز|موعد)(?:\s|$)",
    r"(?:^|\s)(?:حجزي|موعدي)(?:\s|$|[؟?])",
    r"(?:الحجز|الموعد)\s+بتاعي",
    r"(?:^|\s)(?:انا|أنا)?\s*حاجز(?:ة)?(?:\s|$)",
    r"ممكن\s+(?:أ?عرف|اعرف)\s+(?:الحجز|الموعد)\s+بتاعي",
    r"\bmy\s+appointment\b",
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
_AREA_PATTERNS = (
    ("جسم كامل", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:جسم\s+كامل|full\s+body)(?![\w\u0600-\u06FF])"),
    ("بيكيني", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:بيكيني|بكيني|bikini)(?![\w\u0600-\u06FF])"),
    ("أندر آرم", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:[اأإ]ندر\s*[اآ]?رم|under\s*arm|underarm)(?![\w\u0600-\u06FF])"),
    ("لاين", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:لاين|line)(?![\w\u0600-\u06FF])"),
    ("وجه", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:وجه|face)(?![\w\u0600-\u06FF])"),
    ("رقبة", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:رقب[هة]|neck)(?![\w\u0600-\u06FF])"),
    ("صدر", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:صدر|chest)(?![\w\u0600-\u06FF])"),
    ("ظهر", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:ظهر|back)(?![\w\u0600-\u06FF])"),
    ("رجلين", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:رجلين|ساقين|legs?)(?![\w\u0600-\u06FF])"),
    ("إيدين", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:[اأإ]يدين|ذراعين|arms?)(?![\w\u0600-\u06FF])"),
    ("ذقن", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:ذقن|beard)(?![\w\u0600-\u06FF])"),
    ("بوكسر", r"(?<![\w\u0600-\u06FF])(?:و\s*)?(?:بو[كک]سر|boxer)(?![\w\u0600-\u06FF])"),
)
_AREA_NEGATION_PATTERN = re.compile(
    r"(?:^|\s)(?:بدون|من\s+غير|ما\s*عدا|except|without)(?:\s|$)",
    re.IGNORECASE,
)
_DRAFT_CORRECTION_PATTERN = re.compile(
    r"(?:^|\s)(?:قصدي|بدل|غيّر|غيري|خليها|خليه|change|instead)(?:\s|$)",
    re.IGNORECASE,
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

_STANDALONE_NAME_STOPWORDS = _TRIVIAL_PHRASES | {
    "مساء",
    "الخير",
    "صباح",
    "انا",
    "أنا",
    "عايز",
    "عايزة",
    "محتاج",
    "محتاجة",
    "ممكن",
    "حجز",
    "موعد",
    "ليزر",
    "بكرة",
    "بكره",
    "النهاردة",
    "النهارده",
    "شكرا",
    "شكراً",
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
    if re.fullmatch(
        r"(?:(?:الساعة|الساعه|بعد)\s*)?[0-9٠-٩]{1,2}"
        r"(?::[0-9٠-٩]{2}|\s*و\s*(?:نص|نصف|ربع))?\s*(?:am|pm|ص|م)?",
        text,
    ):
        return False
    return True


def _find_intent(text: str, current: dict[str, Any]) -> str:
    return _find_explicit_intent(text) or str(current.get("intent") or "")


def _find_explicit_intent(text: str) -> str:
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _RESCHEDULE_PATTERNS):
        return "reschedule"
    if any(word in text for word in _CANCEL_WORDS):
        return "cancel"
    if any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _EXISTING_APPOINTMENT_PATTERNS
    ):
        return "check_appointment"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _BOOK_PATTERNS):
        return "book"
    return ""


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


def _find_standalone_name(message: str) -> str:
    """Accept only a short, name-shaped reply while explicitly waiting for a name."""
    candidate = re.sub(r"\s+", " ", (message or "").strip()).strip(".،,!?؟")
    if not re.fullmatch(
        r"(?:[A-Za-z]{2,}|[\u0600-\u06FF]{2,})(?:\s+(?:[A-Za-z]{2,}|[\u0600-\u06FF]{2,})){1,3}",
        candidate,
    ):
        return ""
    words = {word.lower() for word in candidate.split()}
    if words & {word.lower() for word in _STANDALONE_NAME_STOPWORDS}:
        return ""
    if any(word in candidate.lower() for word in _CANCEL_WORDS):
        return ""
    if any(re.search(pattern, candidate, re.IGNORECASE) for pattern in (*_BOOK_PATTERNS, *_RESCHEDULE_PATTERNS)):
        return ""
    if _find_service(candidate.lower()):
        return ""
    return candidate


def _find_service(text: str) -> str:
    """Return an area only when every coordinated part can be represented."""
    matches: list[tuple[int, int, str]] = []
    for canonical, pattern in _AREA_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matches.append((match.start(), match.end(), canonical))
    if not matches or _AREA_NEGATION_PATTERN.search(text):
        return ""

    spans = [(start, end) for start, end, _ in matches]

    def begins_recognized_area(position: int) -> bool:
        while position < len(text) and text[position].isspace():
            position += 1
        return any(start <= position < end for start, end in spans)

    # A connector leading to anything outside the vocabulary means the patient
    # requested more than the canonical result could retain. Returning blank is
    # safer than silently booking only the recognized subset.
    first_area_start = min(start for start, _, _ in matches)
    for connector in re.finditer(r"\+|\band\b|(?:^|\s)و(?=\s*[\w\u0600-\u06FF])", text, re.IGNORECASE):
        if connector.start() < first_area_start:
            continue
        connector_end = connector.end()
        if connector.group(0).lstrip().startswith("و"):
            connector_end = connector.start() + len(connector.group(0))
        if not begins_recognized_area(connector_end):
            return ""

    ordered: list[str] = []
    for _, _, canonical in sorted(matches):
        if canonical not in ordered:
            ordered.append(canonical)
    return " + ".join(ordered)


def _find_date(text: str, now: dt.datetime, config: ClinicConfig) -> str:
    normalized = text.translate(_ARABIC_DIGITS).lower()
    candidates: list[str] = []
    occupied: list[tuple[int, int]] = []
    for absolute in re.finditer(
        r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
        normalized,
    ):
        candidates.append(absolute.group(0))
        occupied.append(absolute.span())
    # Longest phrases win so "بعد بكرة" is not also parsed as "بكرة".
    for phrase in sorted(_DATE_PHRASES, key=len, reverse=True):
        for match in re.finditer(re.escape(phrase), normalized):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            candidates.append(phrase)
            occupied.append(match.span())
    parsed: set[str] = set()
    for candidate in candidates:
        try:
            parsed.add(
                parse_date_expression(
                    candidate,
                    now=now,
                    timezone=config.timezone,
                ).isoformat()
            )
        except ValueError:
            continue
    return next(iter(parsed)) if len(parsed) == 1 else ""


def _mentions_date(text: str) -> bool:
    normalized = text.translate(_ARABIC_DIGITS).lower()
    if re.search(
        r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
        normalized,
    ):
        return True
    return any(phrase in normalized for phrase in _DATE_PHRASES)


def _mentions_time(text: str, *, active_booking: bool) -> bool:
    normalized = text.translate(_ARABIC_DIGITS).lower().strip()
    if re.search(r"(?:الساعة|الساعه|بعد|\bat\b)\s*\d", normalized):
        return True
    if re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", normalized):
        return True
    if not active_booking:
        return False
    if re.fullmatch(
        r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        normalized,
    ):
        return False
    return bool(re.fullmatch(r"\d{1,2}(?!\d).*", normalized))


def _find_time(text: str, *, active_booking: bool, config: ClinicConfig) -> str:
    normalized = (
        text.translate(_ARABIC_DIGITS)
        .lower()
        .replace("صباحاً", "am")
        .replace("صباحا", "am")
        .replace("مساءً", "pm")
        .replace("مساء", "pm")
    )
    if re.search(
        r"\d{1,2}(?::\d{2})?\s*(?:أو|او|ولا|or)\s*\d{1,2}(?::\d{2})?",
        normalized,
        re.IGNORECASE,
    ):
        return ""
    patterns: list[tuple[str, bool]] = [
        (
            r"(?:الساعة|الساعه|بعد|at)\s*(\d{1,2})(?:(?::(\d{2}))|(?:\s*(و\s*(?:نص|نصف))))?\s*(am|pm|ص|م)?(?![\d:]|\s*و)",
            True,
        ),
        (
            r"(?<![\d:])(\d{1,2}):(\d{2})\s*(am|pm|ص|م)?(?![\d:]|\s*و)",
            False,
        ),
    ]
    if active_booking:
        patterns.append(
            (
                r"^\s*(\d{1,2})(?:(?::(\d{2}))|(?:\s*(و\s*(?:نص|نصف))))?\s*(am|pm|ص|م)?\s*$",
                True,
            )
        )
    unsafe_before = r"(?:حوالي|تقريباً|تقريبا|تقريبًا)\s*$"
    unsafe_after = (
        r"^\s*(?:و|إلا|الا|أو|او|ولا|or\b|حوالي|تقريباً|تقريبا|تقريبًا|"
        r"لحد|لغاية|إلى|الى|لل?ساعة|[\d:])"
    )
    for pattern, has_half_group in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if not match:
            continue
        if re.search(unsafe_before, normalized[: match.start()], re.IGNORECASE):
            continue
        if re.match(unsafe_after, normalized[match.end() :], re.IGNORECASE):
            continue
        hour = int(match.group(1))
        minute = 30 if has_half_group and match.group(3) else int(match.group(2) or 0)
        suffix_group = 4 if has_half_group else 3
        suffix = (match.group(suffix_group) or "").lower()
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
    if intent == "check_appointment":
        return "checking_appointment"
    if intent == "cancel":
        return "cancelling"
    if intent == "reschedule":
        # Date/time roles are deliberately left to Gemini unless old vs new is
        # unambiguous. The current deterministic parser does not guess them.
        return "reschedule_unclassified"
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
    explicit_intent = _find_explicit_intent(lowered)
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
        explicit_name = _find_name(text)
        standalone_name = ""
        if current and current.get("stage") == "need_name":
            standalone_name = _find_standalone_name(text)
        draft["patient_name"] = (
            profile.get("name") or explicit_name or standalone_name
        ).strip()
    service = _find_service(lowered)
    correction = bool(_DRAFT_CORRECTION_PATTERN.search(lowered))
    new_booking_request = explicit_intent == "book"
    if service and (
        not draft.get("service_area")
        or (current and current.get("stage") == "need_service")
        or new_booking_request
        or correction
    ):
        draft["service_area"] = service
    if intent in {"check_appointment", "reschedule"}:
        # A reschedule sentence may contain both current and desired date/time;
        # an appointment query may mention an existing date. Neither belongs in
        # the new-booking requested_* fields.
        draft["requested_date"] = ""
        draft["requested_time"] = ""
        if intent == "check_appointment":
            draft["service_area"] = ""
    else:
        now = now or config.now()
        date = _find_date(lowered, now, config)
        may_change_date = (
            not draft.get("requested_date")
            or (current and current.get("stage") == "need_date")
            or new_booking_request
            or correction
        )
        if date and may_change_date:
            draft["requested_date"] = date
        elif _mentions_date(lowered) and may_change_date:
            draft["requested_date"] = ""
        active_booking = bool(current or intent)
        time = _find_time(lowered, active_booking=active_booking, config=config)
        may_change_time = (
            not draft.get("requested_time")
            or (current and current.get("stage") == "need_time")
            or new_booking_request
            or correction
        )
        if time and may_change_time:
            draft["requested_time"] = time
        elif _mentions_time(lowered, active_booking=active_booking) and may_change_time:
            draft["requested_time"] = ""
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
