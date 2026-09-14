"""Reliable, structured booking operations around the existing Apps Script API."""

from __future__ import annotations

import datetime as dt
import logging
import re
import time as time_module
from typing import Callable, Any

import httpx

from .clinic import CLINIC, ClinicConfig
from .dates import parse_date_expression

log = logging.getLogger("jothen.booking")


def result(ok: bool, code: str, message: str, **data: Any) -> dict:
    payload = {"ok": ok, "code": code, "message": message}
    payload.update(data)
    return payload


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("01") and len(digits) == 11:
        digits = "2" + digits
    if digits.startswith("1") and len(digits) == 10:
        digits = "20" + digits
    return digits


def normalize_time(value: str) -> str:
    raw = (value or "").strip().upper().replace(".", "")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return raw


class AppsScriptTemporaryError(RuntimeError):
    pass


class BookingService:
    def __init__(
        self,
        apps_script_url: str,
        *,
        config: ClinicConfig = CLINIC,
        get_func: Callable[[dict], dict] | None = None,
        post_func: Callable[[dict], dict] | None = None,
        now_func: Callable[[], dt.datetime] | None = None,
        on_booked: Callable[[str, str, str], None] | None = None,
        on_audit: Callable[[str, str, str], None] | None = None,
    ):
        self.url = apps_script_url
        self.config = config
        self._get_func = get_func
        self._post_func = post_func
        self._now_func = now_func or config.now
        self._on_booked = on_booked
        self._on_audit = on_audit

    def _request(self, method: str, payload: dict) -> dict:
        callback = self._get_func if method == "GET" else self._post_func
        if callback:
            try:
                data = callback(payload)
                if not isinstance(data, dict):
                    raise AppsScriptTemporaryError("invalid_json")
                return data
            except AppsScriptTemporaryError:
                raise
            except Exception as exc:
                raise AppsScriptTemporaryError("temporarily_unavailable") from exc
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20, connect=10)) as client:
                    response = client.get(self.url, params=payload) if method == "GET" else client.post(self.url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise AppsScriptTemporaryError("invalid_json")
                    return data
            except (httpx.HTTPError, ValueError, AppsScriptTemporaryError) as exc:
                last_error = exc
                log.warning("Apps Script %s attempt %s failed: %s", method, attempt + 1, type(exc).__name__)
                if attempt < 2:
                    time_module.sleep(0.35 * (2 ** attempt))
        raise AppsScriptTemporaryError("temporarily_unavailable") from last_error

    def _now(self) -> dt.datetime:
        now = self._now_func()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.config.timezone)
        return now.astimezone(self.config.timezone)

    def _validate_date(self, value: str, *, reject_past: bool = True) -> dt.date:
        date = parse_date_expression(value, now=self._now(), timezone=self.config.timezone)
        today = self._now().date()
        if reject_past and date < today:
            raise ValueError("لا يمكن اختيار موعد في الماضي.")
        if date > today + dt.timedelta(days=self.config.booking_horizon_days):
            raise ValueError(f"يمكن الحجز خلال {self.config.booking_horizon_days} يوماً فقط.")
        if date.weekday() in self.config.closed_weekdays:
            raise ValueError("الجمعة إجازة ولا يمكن الحجز فيها.")
        return date

    def _validate_slot(self, date: dt.date, value: str) -> str:
        slot = normalize_time(value)
        if slot not in self.config.slots:
            raise ValueError(
                f"الموعد يجب أن يكون بين {self.config.slots[0]} و{self.config.slots[-1]} "
                f"وبفواصل {self.config.slot_minutes} دقيقة."
            )
        parsed_time = dt.datetime.strptime(slot, "%I:%M %p").time()
        candidate = dt.datetime.combine(date, parsed_time, self.config.timezone)
        if candidate <= self._now():
            raise ValueError("لا يمكن اختيار موعد انتهى أو بدأ بالفعل.")
        return slot

    @staticmethod
    def _booked_times(data: dict) -> set[str]:
        booked = data.get("booked", [])
        if booked is None:
            booked = []
        if not isinstance(booked, (list, tuple)):
            raise AppsScriptTemporaryError("invalid_schedule")
        values: set[str] = set()
        for item in booked:
            raw = item.get("time", "") if isinstance(item, dict) else str(item)
            if raw:
                values.add(normalize_time(raw))
        return values

    def schedule(self, date_value: str) -> dict:
        try:
            date = self._validate_date(date_value)
            data = self._request("GET", {"date": date.isoformat()})
            booked = self._booked_times(data)
            available = []
            for slot in self.config.slots:
                try:
                    self._validate_slot(date, slot)
                except ValueError:
                    continue
                if slot not in booked:
                    available.append(slot)
            return result(True, "schedule_loaded", "تم تحميل المواعيد المتاحة.", date=date.isoformat(), booked=sorted(booked), available=available)
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "نظام المواعيد غير متاح مؤقتاً. حاولي مرة أخرى بعد قليل.", retryable=True)

    def appointments(self, phone_number: str) -> dict:
        try:
            phone = normalize_phone(phone_number)
            data = self._request("GET", {"phone": phone})
            appointments = data.get("appointments", []) or []
            return result(True, "appointments_loaded", "تم تحميل حجوزات المريضة.", appointments=appointments, count=len(appointments))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "تعذر قراءة الحجوزات مؤقتاً. حاولي مرة أخرى بعد قليل.", retryable=True)

    def book(self, patient_name: str, phone_number: str, date_value: str, time_value: str, area: str) -> dict:
        name = (patient_name or "").strip()
        area = (area or "").strip()
        phone = normalize_phone(phone_number)
        if not name:
            return result(False, "missing_name", "اسم المريضة مطلوب.")
        if not area:
            return result(False, "missing_area", "منطقة الجلسة مطلوبة.")
        if len(phone) < 10:
            return result(False, "invalid_phone", "رقم واتساب المريضة غير صالح.")
        try:
            date = self._validate_date(date_value)
            slot = self._validate_slot(date, time_value)
            # Mandatory authoritative re-check inside the write operation. A prior
            # Gemini check is never trusted as confirmation of availability.
            availability = self._request("GET", {"date": date.isoformat()})
            if slot in self._booked_times(availability):
                return result(False, "slot_unavailable", "الموعد اتاخد بالفعل. اختاري موعداً آخر.", date=date.isoformat(), time=slot)
            payload = {
                "action": "book", "patient_name": name, "phone_number": phone,
                "branch": self.config.branch, "date": date.isoformat(),
                "time": slot, "area": area,
            }
            data = self._request("POST", payload)
            status = str(data.get("status", "")).strip().lower()
            explicitly_booked = (
                data.get("success") is True
                or data.get("booked") is True
                or status in {"success", "ok", "booked"}
            )
            explicitly_rejected = (
                data.get("success") is False
                or data.get("booked") is False
                or status in {"error", "failed", "rejected"}
            )
            if explicitly_rejected:
                return result(
                    False,
                    "booking_rejected",
                    str(data.get("message") or "تعذر تسجيل الموعد."),
                    retryable=False,
                )
            if not explicitly_booked:
                return result(
                    False,
                    "booking_not_confirmed",
                    "نظام المواعيد لم يؤكد الحجز، لذلك الموعد غير مؤكد. حاولي مرة أخرى بعد قليل.",
                    retryable=True,
                )
            if self._on_booked:
                self._on_booked(phone, name, f"حجز {area} ({date.isoformat()} {slot})")
            if self._on_audit:
                self._on_audit("appointment_booked", phone, f"{date.isoformat()} {slot} - {area}")
            return result(True, "booked", f"تم تأكيد الحجز باسم {name} بفرع {self.config.branch} يوم {date.isoformat()} الساعة {slot} لمنطقة {area}.", appointment={"patient_name": name, "phone_number": phone, "branch": self.config.branch, "date": date.isoformat(), "time": slot, "area": area})
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "نظام الحجز غير متاح مؤقتاً ولم يتم تأكيد الموعد. حاولي مرة أخرى بعد قليل.", retryable=True)

    def cancel(self, phone_number: str, date_value: str) -> dict:
        phone = normalize_phone(phone_number)
        try:
            date = self._validate_date(date_value)
            data = self._request("POST", {"action": "cancel", "phone_number": phone, "date": date.isoformat()})
            if data.get("deleted") or data.get("success") is True:
                if self._on_audit:
                    self._on_audit("appointment_cancelled", phone, date.isoformat())
                return result(True, "cancelled", f"تم إلغاء حجز يوم {date.isoformat()} بنجاح.", date=date.isoformat())
            return result(False, "appointment_not_found", f"لم أجد حجزاً يوم {date.isoformat()} على رقم واتسابك.", date=date.isoformat())
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "تعذر الإلغاء مؤقتاً ولم يتم تغيير الحجز. حاولي مرة أخرى بعد قليل.", retryable=True)

    def reschedule(self, phone_number: str, old_date_value: str, new_date_value: str, new_time_value: str) -> dict:
        phone = normalize_phone(phone_number)
        try:
            old_date = self._validate_date(old_date_value)
            new_date = self._validate_date(new_date_value)
            new_slot = self._validate_slot(new_date, new_time_value)
            availability = self._request("GET", {"date": new_date.isoformat()})
            if new_slot in self._booked_times(availability):
                return result(False, "slot_unavailable", "الموعد الجديد اتاخد بالفعل. اختاري موعداً آخر.", date=new_date.isoformat(), time=new_slot)
            data = self._request("POST", {
                "action": "reschedule", "phone_number": phone,
                "old_date": old_date.isoformat(), "new_date": new_date.isoformat(),
                "new_time": new_slot, "branch": self.config.branch,
            })
            if data.get("status") == "error" or data.get("success") is False:
                return result(False, "reschedule_rejected", str(data.get("message") or "تعذر تغيير الموعد."))
            if data.get("rescheduled") is True:
                if self._on_audit:
                    self._on_audit("appointment_rescheduled", phone, f"{old_date.isoformat()} -> {new_date.isoformat()} {new_slot}")
                return result(True, "rescheduled", f"تم تغيير الحجز إلى {new_date.isoformat()} الساعة {new_slot}.", old_date=old_date.isoformat(), date=new_date.isoformat(), time=new_slot)
            return result(False, "reschedule_not_confirmed", "نظام المواعيد لم يؤكد تغيير الحجز، لذلك الحجز القديم ما زال كما هو.", retryable=True)
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "تعذر تغيير الموعد مؤقتاً، والحجز القديم ما زال كما هو.", retryable=True)
