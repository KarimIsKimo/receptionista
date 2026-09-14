import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from receptionist.dates import parse_date_expression


class DateParsingTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 14, 18, 0, tzinfo=ZoneInfo("Africa/Cairo"))  # Monday

    def test_tomorrow_in_cairo(self):
        self.assertEqual(parse_date_expression("tomorrow", now=self.now), dt.date(2026, 9, 15))
        self.assertEqual(parse_date_expression("بكرة", now=self.now), dt.date(2026, 9, 15))

    def test_plain_weekday_is_nearest_occurrence(self):
        self.assertEqual(parse_date_expression("Thursday", now=self.now), dt.date(2026, 9, 17))
        self.assertEqual(parse_date_expression("الخميس", now=self.now), dt.date(2026, 9, 17))

    def test_next_weekday_means_next_occurrence(self):
        self.assertEqual(parse_date_expression("next Saturday", now=self.now), dt.date(2026, 9, 19))
        self.assertEqual(parse_date_expression("السبت الجاي", now=self.now), dt.date(2026, 9, 19))

    def test_arabic_digits(self):
        self.assertEqual(parse_date_expression("٢٠٢٦-٠٩-٢٠", now=self.now), dt.date(2026, 9, 20))


if __name__ == "__main__":
    unittest.main()
