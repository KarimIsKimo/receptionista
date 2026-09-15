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
    def _schedule_entries(data: dict) -> list[dict]:
        booked = data.get("booked", [])
        if booked is None:
            booked = []
        if not isinstance(booked, (list, tuple)):
            raise AppsScriptTemporaryError("invalid_schedule")
        entries: list[dict] = []
        for item in booked:
            if isinstance(item, dict):
                raw = next(
                    (
                        item.get(key)
                        for key in ("time", "appointment_time", "Time", "الوقت")
                        if item.get(key)
                    ),
                    None,
                )
                if not isinstance(raw, str) or not raw.strip():
                    raise AppsScriptTemporaryError("invalid_schedule_item")
                entries.append(
                    {
                        "raw": item,
                        "time": normalize_time(raw),
                        "phone": normalize_phone(
                            str(item.get("phone") or item.get("phone_number") or "")
                        ),
                        "appointment_id": str(
                            item.get("appointment_id")
                            or item.get("booking_id")
                            or item.get("id")
                            or item.get("row_id")
                            or ""
                        ).strip(),
                    }
                )
            elif isinstance(item, str) and item.strip():
                entries.append(
                    {
                        "raw": item,
                        "time": normalize_time(item),
                        "phone": "",
                        "appointment_id": "",
                    }
                )
            else:
                raise AppsScriptTemporaryError("invalid_schedule_item")
        return entries

    @classmethod
    def _booked_times(cls, data: dict) -> set[str]:
        return {entry["time"] for entry in cls._schedule_entries(data)}

    @staticmethod
    def _entry_matches_target(entry: dict, phone: str, target: dict) -> bool:
        if entry.get("phone") != phone:
            return False
        target_id = target.get("appointment_id")
        target_time = target.get("time") or target.get("old_time")
        if target_id and entry.get("appointment_id") != target_id:
            return False
        if target_time and entry.get("time") != target_time:
            return False
        return True

    @staticmethod
    def _response_target_matches(data: dict, target: dict) -> bool:
        """Validate identifiers when an upgraded Apps Script echoes them."""
        echoed = data.get("target") if isinstance(data.get("target"), dict) else data
        expected_id = target.get("appointment_id")
        returned_id = next(
            (
                echoed.get(key)
                for key in ("appointment_id", "booking_id", "id", "row_id")
                if echoed.get(key) is not None
            ),
            None,
        )
        if expected_id and returned_id is not None and str(returned_id).strip() != str(expected_id).strip():
            return False
        expected_time = target.get("time") or target.get("old_time")
        returned_time = next(
            (
                echoed.get(key)
                for key in ("time", "old_time", "cancelled_time")
                if echoed.get(key) is not None
            ),
            None,
        )
        if expected_time and returned_time is not None and normalize_time(str(returned_time)) != expected_time:
            return False
        return True

    def _resolve_existing_target(
        self,
        *,
        date: dt.date,
        phone: str,
        target: dict,
    ) -> tuple[dict | None, dict | None]:
        schedule = self._request("GET", {"date": date.isoformat()})
        entries = self._schedule_entries(schedule)
        patient_entries = [entry for entry in entries if entry.get("phone") == phone]
        if (
            target.get("appointment_id")
            and patient_entries
            and not any(entry.get("appointment_id") for entry in patient_entries)
        ):
            return None, result(
                False,
                "exact_target_not_supported",
                "نسخة Apps Script الحالية لا ترجع رقم حجز ثابتاً. لم يتم تغيير أي موعد.",
                retryable=False,
                deployment_required=True,
            )
        matches = [
            entry
            for entry in patient_entries
            if self._entry_matches_target(entry, phone, target)
        ]
        if len(patient_entries) > 1:
            return None, result(
                False,
                "exact_target_not_supported",
                "يوجد أكثر من موعد لنفس المريضة في هذا اليوم، ونسخة Apps Script الحالية لا تضمن استهداف الوقت الصحيح. لم يتم تغيير أي موعد.",
                retryable=False,
                deployment_required=True,
            )
        if len(matches) != 1:
            return None, result(
                False,
                "appointment_not_found",
                "لم أجد موعداً واحداً يطابق التاريخ والوقت/رقم الحجز المطلوب.",
                retryable=False,
            )
        return matches[0], None

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

    def _target_fields(
        self,
        *,
        time_value: str | None,
        appointment_id: str | None,
        time_key: str,
    ) -> dict:
        target_id = str(appointment_id or "").strip()
        target_time = normalize_time(time_value or "")
        if not target_id and not target_time:
            raise ValueError("يجب تحديد وقت الموعد أو رقم الحجز لتجنب تعديل موعد آخر بالخطأ.")
        if target_time and target_time not in self.config.slots:
            raise ValueError("وقت الموعد المطلوب غير صالح.")
        fields: dict[str, str] = {}
        if target_id:
            fields["appointment_id"] = target_id
        if target_time:
            fields[time_key] = target_time
        return fields

    def cancel(
        self,
        phone_number: str,
        date_value: str,
        time_value: str | None = None,
        appointment_id: str | None = None,
    ) -> dict:
        phone = normalize_phone(phone_number)
        try:
            date = self._validate_date(date_value)
            target = self._target_fields(
                time_value=time_value,
                appointment_id=appointment_id,
                time_key="time",
            )
            _, target_error = self._resolve_existing_target(
                date=date,
                phone=phone,
                target=target,
            )
            if target_error:
                return target_error
            data = self._request(
                "POST",
                {
                    "action": "cancel",
                    "phone_number": phone,
                    "date": date.isoformat(),
                    **target,
                },
            )
            status = str(data.get("status", "")).strip().lower()
            if (
                data.get("deleted") is True
                or data.get("cancelled") is True
                or data.get("success") is True
                or status == "cancelled"
            ):
                if not self._response_target_matches(data, target):
                    return result(
                        False,
                        "cancellation_target_mismatch",
                        "رد نظام المواعيد لا يطابق الموعد المطلوب، لذلك لا يمكن تأكيد الإلغاء.",
                        retryable=False,
                    )
                try:
                    after = self._request("GET", {"date": date.isoformat()})
                    still_present = any(
                        self._entry_matches_target(entry, phone, target)
                        for entry in self._schedule_entries(after)
                    )
                except AppsScriptTemporaryError:
                    return result(
                        False,
                        "cancellation_verification_unavailable",
                        "تم إرسال الإلغاء لكن تعذر التحقق من النتيجة. حالة الموعد غير معروفة حتى التحديث.",
                        retryable=True,
                        state="unknown",
                    )
                if still_present:
                    return result(
                        False,
                        "cancellation_not_verified",
                        "نظام المواعيد ما زال يعرض الموعد، لذلك لم يتم تأكيد الإلغاء.",
                        retryable=True,
                    )
                if self._on_audit:
                    self._on_audit("appointment_cancelled", phone, f"{date.isoformat()} {target}")
                return result(
                    True,
                    "cancelled",
                    f"تم إلغاء حجز يوم {date.isoformat()} بنجاح.",
                    date=date.isoformat(),
                    **target,
                )
            if (
                data.get("deleted") is False
                or data.get("cancelled") is False
                or data.get("success") is False
                or status in {"error", "failed", "not_found"}
            ):
                return result(False, "appointment_not_found", f"لم أجد حجزاً يوم {date.isoformat()} على رقم واتسابك.", date=date.isoformat())
            return result(
                False,
                "cancellation_not_confirmed",
                "نظام المواعيد لم يؤكد الإلغاء، لذلك الموعد ما زال قائماً.",
                retryable=True,
                date=date.isoformat(),
                **target,
            )
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "تعذر الإلغاء مؤقتاً ولم يتم تغيير الحجز. حاولي مرة أخرى بعد قليل.", retryable=True)

    def reschedule(
        self,
        phone_number: str,
        old_date_value: str,
        new_date_value: str,
        new_time_value: str,
        old_time_value: str | None = None,
        appointment_id: str | None = None,
    ) -> dict:
        phone = normalize_phone(phone_number)
        try:
            old_date = self._validate_date(old_date_value)
            new_date = self._validate_date(new_date_value)
            target = self._target_fields(
                time_value=old_time_value,
                appointment_id=appointment_id,
                time_key="old_time",
            )
            _, target_error = self._resolve_existing_target(
                date=old_date,
                phone=phone,
                target=target,
            )
            if target_error:
                return target_error
            new_slot = self._validate_slot(new_date, new_time_value)
            if (
                old_date == new_date
                and target.get("old_time") == new_slot
            ):
                raise ValueError("الموعد الجديد يطابق الموعد الحالي.")
            availability = self._request("GET", {"date": new_date.isoformat()})
            if new_slot in self._booked_times(availability):
                return result(False, "slot_unavailable", "الموعد الجديد اتاخد بالفعل. اختاري موعداً آخر.", date=new_date.isoformat(), time=new_slot)
            data = self._request("POST", {
                "action": "reschedule", "phone_number": phone,
                "old_date": old_date.isoformat(), "new_date": new_date.isoformat(),
                "new_time": new_slot, "branch": self.config.branch,
                **target,
            })
            if data.get("status") == "error" or data.get("success") is False:
                return result(False, "reschedule_rejected", str(data.get("message") or "تعذر تغيير الموعد."))
            if data.get("rescheduled") is True:
                if not self._response_target_matches(data, target):
                    return result(
                        False,
                        "reschedule_target_mismatch",
                        "رد نظام المواعيد لا يطابق الموعد القديم المطلوب، لذلك لا يمكن تأكيد التغيير.",
                        retryable=False,
                    )
                try:
                    old_after = self._request("GET", {"date": old_date.isoformat()})
                    new_after = old_after if new_date == old_date else self._request(
                        "GET", {"date": new_date.isoformat()}
                    )
                    old_still_present = any(
                        self._entry_matches_target(entry, phone, target)
                        for entry in self._schedule_entries(old_after)
                    )
                    new_present = any(
                        entry.get("phone") == phone and entry.get("time") == new_slot
                        for entry in self._schedule_entries(new_after)
                    )
                except AppsScriptTemporaryError:
                    return result(
                        False,
                        "reschedule_verification_unavailable",
                        "تم إرسال تغيير الموعد لكن تعذر التحقق من النتيجة. حالة الحجز غير معروفة حتى التحديث.",
                        retryable=True,
                        state="unknown",
                    )
                if old_still_present or not new_present:
                    return result(
                        False,
                        "reschedule_not_verified",
                        "لم يؤكد جدول المواعيد إزالة الموعد القديم وإضافة الجديد.",
                        retryable=True,
                    )
                if self._on_audit:
                    self._on_audit("appointment_rescheduled", phone, f"{old_date.isoformat()} -> {new_date.isoformat()} {new_slot}")
                return result(
                    True,
                    "rescheduled",
                    f"تم تغيير الحجز إلى {new_date.isoformat()} الساعة {new_slot}.",
                    old_date=old_date.isoformat(),
                    date=new_date.isoformat(),
                    time=new_slot,
                    **target,
                )
            return result(False, "reschedule_not_confirmed", "نظام المواعيد لم يؤكد تغيير الحجز، لذلك الحجز القديم ما زال كما هو.", retryable=True)
        except ValueError as exc:
            return result(False, "invalid_appointment", str(exc))
        except AppsScriptTemporaryError:
            return result(False, "booking_service_unavailable", "تعذر تغيير الموعد مؤقتاً، والحجز القديم ما زال كما هو.", retryable=True)
