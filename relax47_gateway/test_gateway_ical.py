#!/usr/bin/env python3
"""Regression tests for RealtyCalendar iCalendar parsing."""
from __future__ import annotations

import unittest

from gateway import parse_ical_availability


class RealtyCalendarICalTests(unittest.TestCase):
    def test_preserves_named_summary_and_dates(self) -> None:
        payload = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:booking-1\r\n"
            "DTSTART;VALUE=DATE:20261010\r\n"
            "DTEND;VALUE=DATE:20261012\r\n"
            "SUMMARY:Бронь: Иван\\, прямое бронирование\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode("utf-8")
        self.assertEqual(
            parse_ical_availability(payload),
            [{
                "uid": "booking-1",
                "summary": "Бронь: Иван, прямое бронирование",
                "description": "",
                "start_date": "2026-10-10",
                "end_date": "2026-10-12",
            }],
        )

    def test_preserves_labeled_guest_description(self) -> None:
        payload = (
            "BEGIN:VEVENT\r\nUID:rs-1\r\nDTSTART;VALUE=DATE:20261010\r\n"
            "DTEND;VALUE=DATE:20261012\r\nSUMMARY:RS12345\r\n"
            "DESCRIPTION:Источник: RealtyCalendar\\nГость: Семён\\nТелефон: +7...\r\n"
            "END:VEVENT\r\n"
        ).encode("utf-8")
        self.assertEqual(
            parse_ical_availability(payload)[0]["description"],
            "Источник: RealtyCalendar\nГость: Семён\nТелефон: +7...",
        )

    def test_cancelled_event_is_ignored(self) -> None:
        payload = (
            "BEGIN:VEVENT\nUID:cancelled\nDTSTART;VALUE=DATE:20261010\n"
            "DTEND;VALUE=DATE:20261012\nSUMMARY:Иван\nSTATUS:CANCELLED\nEND:VEVENT\n"
        ).encode("utf-8")
        self.assertEqual(parse_ical_availability(payload), [])


if __name__ == "__main__":
    unittest.main()
