"""Single source of truth for clinic logistics and booking rules."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ClinicConfig:
    name_ar: str = "عيادات جوثن"
    branch: str = "مدينة نصر"
    address: str = "عيادة 104، 8 ش الدكتور حسن الشريف، مدينة نصر"
    timezone_name: str = "Africa/Cairo"
    opening_time: dt.time = dt.time(12, 0)
    closing_time: dt.time = dt.time(22, 0)
    slot_minutes: int = 30
    closed_weekdays: tuple[int, ...] = (4,)  # Friday; Monday is zero.
    booking_horizon_days: int = 180

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def slots(self) -> tuple[str, ...]:
        cursor = dt.datetime.combine(dt.date(2000, 1, 1), self.opening_time)
        end = dt.datetime.combine(dt.date(2000, 1, 1), self.closing_time)
        values: list[str] = []
        while cursor <= end:
            values.append(cursor.strftime("%I:%M %p").lstrip("0"))
            cursor += dt.timedelta(minutes=self.slot_minutes)
        return tuple(values)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.timezone)


CLINIC = ClinicConfig()


def clinic_prompt(config: ClinicConfig = CLINIC) -> str:
    closed = "الجمعة" if config.closed_weekdays == (4,) else str(config.closed_weekdays)
    return f"""=== بيانات العيادة الرسمية (هي المرجع النهائي) ===
- الفرع: {config.branch}
- العنوان: {config.address}
- المنطقة الزمنية: {config.timezone_name}
- أيام العمل: السبت إلى الخميس؛ الإجازة: {closed}
- المواعيد: {config.slots[0]} إلى {config.slots[-1]}، كل {config.slot_minutes} دقيقة
- لا تطلبي رقم الهاتف أبداً؛ رقم واتساب المريض متاح للنظام تلقائياً.
- اسألي فقط عن بيانات الحجز الناقصة، ولا تعيدي سؤالاً إجابته موجودة في سياق العميل أو المحادثة.
- أدوات الحجز تقبل التاريخ الصريح أو تعبيرات مثل tomorrow وThursday وnext Saturday وبكرة والخميس والسبت الجاي.
"""
