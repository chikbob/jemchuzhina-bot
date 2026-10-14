import tempfile
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import test_flows

from pearl.calendar_ui import calendar_markup, parse_range, parse_times
from pearl.db import Database


class CalendarTests(unittest.TestCase):
    def test_two_on_two_off_matches_supplied_dates(self):
        selected = parse_range("11.10.2026 - 28.10.2026; 2/2", date(2026, 10, 7))
        self.assertEqual(
            selected,
            [f"2026-10-{day:02}" for day in (11, 12, 15, 16, 19, 20, 23, 24, 27, 28)],
        )
        self.assertEqual(
            len(parse_range("30.10.2026 - 02.11.2026; каждый день", date(2026, 10, 7))),
            4,
        )
        for value in (
            "11.10.2026 - 10.10.2026",
            "01.10.2026 - 12.10.2026",
            "11.10.2026 - 11.10.2027",
            "32.10.2026 - 11.11.2026",
        ):
            with self.assertRaises(ValueError):
                parse_range(value, date(2026, 10, 7))

    def test_calendar_marks_selection_slots_and_bookings(self):
        markup = calendar_markup(
            "2026-10",
            ["2026-10-15"],
            {"2026-10-11": "🟢", "2026-10-12": "👥", "2026-10-16": "🙈"},
            date(2026, 10, 7),
        )
        buttons = {
            b.callback_data: b.text for row in markup.inline_keyboard for b in row
        }
        self.assertEqual(buttons["calday:2026-10-11:1"], "🟢11")
        self.assertEqual(buttons["calday:2026-10-12:1"], "👥12")
        self.assertEqual(buttons["calday:2026-10-15:0"], "✅15")
        self.assertEqual(buttons["calday:2026-10-16:1"], "🙈16")
        self.assertNotIn("calday:2026-10-06", buttons)
        self.assertEqual(
            [b.text for b in markup.inline_keyboard[0]],
            ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        )

    def test_times_validate_capacity_and_duplicates(self):
        self.assertEqual(parse_times("10:00 2, 12:00 2"), [("10:00", 2), ("12:00", 2)])
        for value in (
            "25:00 2",
            "10:00 0",
            "10:00 101",
            "10:00 2, 10:00 1",
            "10:00",
            "10:00, 2",
        ):
            with self.assertRaises(ValueError):
                parse_times(value)

    def test_bulk_creation_is_atomic_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tz = ZoneInfo("Europe/Moscow")
            db = Database(tmp + "/db", tz)
            first = (datetime.now(tz) + timedelta(days=1)).isoformat()
            second = (datetime.now(tz) + timedelta(days=2)).isoformat()
            db.add_slot("haircut", first, 2)
            slot = db.slots()[0]["id"]
            db.reserve(slot, 100, "Клиент", "+79491234567", "", "", {999}, "Адрес")
            db.set_slot_visibility(slot, False)
            self.assertEqual(db.add_slots("haircut", [(first, 1), (second, 3)]), (1, 1))
            original = next(s for s in db.slots() if s["id"] == slot)
            self.assertEqual(
                (original["capacity"], original["enabled"], original["free"]), (2, 0, 1)
            )
            self.assertEqual(db.add_slots("haircut", [(first, 1), (second, 3)]), (0, 2))
            future = (datetime.now(tz) + timedelta(days=3)).isoformat()
            with self.assertRaises(ValueError):
                db.add_slots("haircut", [(future, 1), (second, 0)])
            self.assertEqual(len(db.slots()), 2)


class CalendarFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_flows.FlowTests.asyncSetUp
    asyncTearDown = test_flows.FlowTests.asyncTearDown
    event = test_flows.FlowTests.event

    async def test_calendar_wizard_multi_month_and_confirm_once(self):
        first = (datetime.now(self.tz) + timedelta(days=40)).date()
        second = first + timedelta(days=2)
        await self.event(callback="calendar", user=999)
        await self.event(callback="calservice:haircut", user=999)
        await self.event(callback="calmonth:" + first.strftime("%Y-%m"), user=999)
        await self.event(callback="calday:" + first.isoformat(), user=999)
        await self.event(callback="calday:" + second.isoformat(), user=999)
        await self.event(callback="calday:" + second.isoformat(), user=999)
        await self.event(callback="calday:" + second.isoformat(), user=999)
        await self.event(callback="caltimes", user=999)
        await self.event(text="10:00 2, 12:00 2", user=999)
        self.assertEqual(len(self.db.slots("haircut")), 1)
        await self.event(callback="calsave", user=999)
        await self.event(callback="calsave", user=999)
        self.assertEqual(len(self.db.slots("haircut")), 5)
        self.assertEqual(
            {
                s["capacity"]
                for s in self.db.slots("haircut")
                if s["starts"][:10] in (first.isoformat(), second.isoformat())
            },
            {2},
        )

    async def test_calendar_requires_admin_and_hides_range_mode(self):
        await self.event(callback="calservice:haircut")
        self.assertFalse(self.db.rows("SELECT * FROM sessions"))
        await self.event(callback="calservice:color", user=999)
        markup = self.session.calls[-1].reply_markup
        self.assertFalse(
            any(
                b.callback_data == "calrange"
                for row in markup.inline_keyboard
                for b in row
            )
        )
        await self.event(callback="calrange", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"], "CalendarFlow:days"
        )
        await self.event(callback="calclear", user=999)
        await self.event(callback="caltimes", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"], "CalendarFlow:days"
        )

    async def test_day_time_override_and_capacity_buttons(self):
        dates = [
            (datetime.now(self.tz) + timedelta(days=i)).date().isoformat()
            for i in (10, 11)
        ]
        await self.event(callback="calservice:haircut", user=999)
        for day in dates:
            await self.event(callback=f"calday:{day}:1", user=999)
        await self.event(callback="caltimes", user=999)
        await self.event(callback="calall", user=999)
        await self.event(callback="calpreset:1000", user=999)
        await self.event(callback="calpreset:1200", user=999)
        await self.event(callback="caltimedone", user=999)
        await self.event(callback=f"caltime:{dates[1]}", user=999)
        await self.event(text="13:30 1", user=999)
        await self.event(callback="caltimedone", user=999)
        await self.event(callback="calpreview", user=999)
        await self.event(callback="calsave", user=999)
        added = [
            (s["starts"][:16], s["capacity"])
            for s in self.db.slots("haircut")
            if s["starts"][:10] in dates
        ]
        self.assertEqual(
            added,
            [
                (dates[0] + "T10:00", 2),
                (dates[0] + "T12:00", 2),
                (dates[1] + "T13:30", 1),
            ],
        )

    async def test_date_selection_and_clear_are_idempotent(self):
        await self.event(callback="calservice:haircut", user=999)
        days = [
            (datetime.now(self.tz) + timedelta(days=i)).date().isoformat()
            for i in range(1, 5)
        ]
        for day in days:
            await self.event(callback=f"calday:{day}:1", user=999)
        await self.event(callback=f"calday:{days[-1]}:1", user=999)
        import json

        self.assertEqual(
            len(
                json.loads(self.db.rows("SELECT data FROM sessions")[0]["data"])[
                    "cal_days"
                ]
            ),
            4,
        )
        for _ in range(2):
            await self.event(callback="calclear", user=999)
            self.assertEqual(
                json.loads(self.db.rows("SELECT data FROM sessions")[0]["data"])[
                    "cal_days"
                ],
                [],
            )

    async def test_preview_rejects_day_without_time(self):
        await self.event(callback="calservice:haircut", user=999)
        for i in (10, 11):
            day = (datetime.now(self.tz) + timedelta(days=i)).date().isoformat()
            await self.event(callback=f"calday:{day}:1", user=999)
        await self.event(callback="caltimes", user=999)
        await self.event(callback=f"caltime:{day}", user=999)
        await self.event(callback="calpreset:1200", user=999)
        await self.event(callback="caltimedone", user=999)
        await self.event(callback="calpreview", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"],
            "CalendarFlow:time_menu",
        )
        await self.event(callback="calsave", user=999)
        self.assertEqual(len(self.db.slots("haircut")), 1)
