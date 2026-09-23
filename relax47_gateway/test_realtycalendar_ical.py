import unittest

from gateway import parse_ical_availability


class RealtyCalendarICalTests(unittest.TestCase):
    def test_extracts_only_dates_and_uid(self):
        payload = b"""BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:abc-1\r
DTSTART;VALUE=DATE:20261001\r
DTEND;VALUE=DATE:20261004\r
SUMMARY:Private guest name\r
DESCRIPTION:Private phone\r
END:VEVENT\r
END:VCALENDAR\r
"""
        self.assertEqual(parse_ical_availability(payload), [{
            "uid": "abc-1", "start_date": "2026-10-01", "end_date": "2026-10-04"
        }])

    def test_skips_cancelled_and_invalid_ranges(self):
        payload = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cancelled
DTSTART:20261001T120000Z
DTEND:20261003T100000Z
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:empty
DTSTART;VALUE=DATE:20261001
DTEND;VALUE=DATE:20261001
END:VEVENT
END:VCALENDAR
"""
        self.assertEqual(parse_ical_availability(payload), [])

    def test_unfolds_lines(self):
        payload = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:long-
 value
DTSTART;VALUE=DATE:20261230
DTEND;VALUE=DATE:20270103
END:VEVENT
END:VCALENDAR
"""
        self.assertEqual(parse_ical_availability(payload)[0]["uid"], "long-value")


if __name__ == "__main__":
    unittest.main()
