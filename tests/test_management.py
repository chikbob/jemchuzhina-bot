import json
import unittest
from datetime import datetime, timedelta

import test_flows


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_flows.FlowTests.asyncSetUp
    asyncTearDown = test_flows.FlowTests.asyncTearDown
    event = test_flows.FlowTests.event
    choose_slot = test_flows.FlowTests.choose_slot

    def reserve(self, name="Руслан", user=100, starts=None):
        if starts:
            self.db.add_slot("haircut", starts, 2)
            slot = next(
                s
                for s in self.db.slots()
                if s["starts"]
                == datetime.fromisoformat(starts).isoformat(timespec="seconds")
            )
        else:
            slot = self.db.slots("haircut")[0]
        return self.db.reserve(
            slot["id"],
            user,
            name,
            "+79491234567",
            "Короткие",
            "",
            {999},
            "Адрес",
            "Учтите пожелания",
        )

    async def test_client_coloring_buttons_and_optional_comment_persist(self):
        await self.choose_slot("color")
        await self.event(text="Ниже плеч")
        await self.event(callback="colored:no")
        await self.event(callback="condition:good")
        await self.event(text="Зелёный цвет")
        await self.event(callback="skip_photo")
        await self.event(text="Руслан")
        await self.event(text="+79491234567")
        await self.event(text="Не укорачивать длину")
        await self.event(callback="submit")
        booking = self.db.bookings()[0]
        self.assertIn("Не окрашены", booking["note"])
        self.assertEqual(booking["client_comment"], "Не укорачивать длину")
        await self.event(callback="bookingcard:" + str(booking["id"]), user=999)
        self.assertIn("Не укорачивать длину", self.session.calls[-1].text)

    async def test_status_comment_notifies_client_and_appears_in_own_history(self):
        booking_id = self.reserve()
        await self.event(callback=f"bookingstatusset:{booking_id}:completed", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["status"], "confirmed")
        await self.event(text="Спасибо, будем рады видеть вас снова!", user=999)
        booking = self.db.bookings(history=True)[0]
        self.assertEqual(booking["status"], "completed")
        self.assertIn("рады", booking["status_comment"])
        self.assertTrue(
            any(
                "Комментарий академии: Спасибо" in r["text"]
                for r in self.db.rows("SELECT * FROM outbox")
            )
        )
        await self.event(callback="mine")
        texts = [getattr(m, "text", "") or "" for m in self.session.calls]
        self.assertTrue(any("Комментарий академии: Спасибо" in text for text in texts))

    async def test_cancel_status_comment_abandons_pending_change(self):
        booking_id = self.reserve()
        await self.event(callback=f"bookingstatusset:{booking_id}:rejected", user=999)
        await self.event(callback=f"bookingcard:{booking_id}", user=999)
        await self.event(text="Не должно отменить запись", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["status"], "confirmed")

    async def test_status_comment_without_text_and_permission_check(self):
        booking_id = self.reserve()
        await self.event(callback=f"bookingstatusset:{booking_id}:rejected")
        await self.event(text="Неавторизованный комментарий")
        self.assertEqual(self.db.bookings(history=True)[0]["status"], "confirmed")
        await self.event(callback=f"bookingstatusset:{booking_id}:rejected", user=999)
        await self.event(callback="statuscomment:skip", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["status"], "rejected")
        self.assertEqual(len(self.db.booking_list(scope="archive")), 1)

    async def test_assigned_staff_open_panel_without_password_and_revocation_is_immediate(
        self,
    ):
        for role in ("manager", "admin"):
            self.access.set_role(999, 100, role)
            self.access.logout(100)
            await self.event(text="/admin")
            self.assertTrue(self.access.authenticated(100))
            self.assertIsNone(self.db.rows("SELECT state FROM sessions")[0]["state"])
            await self.event(callback="bookview:today")
            self.assertIn("Сегодня", self.session.calls[-1].text)
        self.access.set_role(999, 100, None)
        self.assertFalse(self.access.authenticated(100))
        count = len(self.session.calls)
        await self.event(callback="bookview:today")
        self.assertFalse(
            any(
                "Сегодня · найдено" in (getattr(m, "text", "") or "")
                for m in self.session.calls[count:]
            )
        )

    async def test_today_archive_search_and_sort(self):
        now = datetime.now(self.tz)
        today = now + timedelta(minutes=5)
        if today.date() != now.date():
            self.skipTest("Less than five minutes remain today")
        first = self.reserve("Яна", 101, today.isoformat())
        second = self.reserve("алексей", 102, (now + timedelta(days=2)).isoformat())
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="today")], [first]
        )
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="all", sort="name")],
            [second, first],
        )
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="all", search="АЛЕКСЕЙ")],
            [second],
        )
        self.assertEqual(
            len(self.db.booking_list(scope="all", search="+7 (949) 123-45-67")), 2
        )
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="all", search=str(first))],
            [first],
        )
        self.db.archive_booking(second, True, 999, {999})
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="archive")], [second]
        )
        self.assertEqual(len(self.db.bookings()), 2)
        self.assertEqual(
            [r["id"] for r in self.db.booking_list(scope="upcoming")], [first]
        )
        await self.event(callback="bookview:all", user=999)
        await self.event(callback="booksearch", user=999)
        await self.event(text="АЛЕКСЕЙ", user=999)
        self.assertIn(
            "найдено: 1",
            next(
                m.text for m in reversed(self.session.calls) if getattr(m, "text", None)
            ),
        )
        await self.event(callback="booksortset:name", user=999)
        state_data = json.loads(self.db.rows("SELECT data FROM sessions")[0]["data"])
        self.assertEqual(state_data["booking_view"]["sort"], "name")
        await self.event(callback="bookfilterset:completed", user=999)
        self.assertIn("найдено: 0", self.session.calls[-1].text)
        await self.event(callback="bookreset", user=999)
        self.assertIn("найдено: 2", self.session.calls[-1].text)

    async def test_passed_bookings_are_archived_without_removing_records(self):
        booking_id = self.reserve()
        with self.db.connect() as c:
            c.execute(
                "UPDATE slots SET starts=? WHERE id=(SELECT slot_id FROM bookings WHERE id=?)",
                ((datetime.now(self.tz) - timedelta(days=1)).isoformat(), booking_id),
            )
        self.db.expire_slots()
        self.assertEqual(self.db.booking_list(scope="upcoming"), [])
        self.assertEqual(self.db.booking_list(scope="archive")[0]["id"], booking_id)
        self.assertEqual(len(self.db.bookings(history=True)), 1)

    async def test_single_slot_wizard_buttons_and_duplicate_submission(self):
        target = (datetime.now(self.tz) + timedelta(days=4)).date().isoformat()
        await self.event(callback="addslot", user=999)
        await self.event(callback="swservice:color", user=999)
        await self.event(callback="swtime:1200", user=999)
        await self.event(callback=f"swdate:{target}", user=999)
        await self.event(callback="swcap:3", user=999)
        await self.event(callback="swsave", user=999)
        await self.event(callback="swsave", user=999)
        slots = [s for s in self.db.slots("color") if s["starts"][:10] == target]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["capacity"], 3)

    async def test_booking_archive_is_guarded_and_capacity_is_kept(self):
        booking_id = self.reserve()
        await self.event(callback=f"bookarchive:{booking_id}:1")
        self.assertEqual(self.db.bookings(history=True)[0]["archived"], 0)
        await self.event(callback=f"bookarchive:{booking_id}:1", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["archived"], 1)
        self.assertEqual(self.db.slots("haircut")[0]["free"], 0)
        await self.event(callback=f"bookarchive:{booking_id}:0", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["archived"], 0)

    async def test_capacity_change_cannot_displace_existing_booking(self):
        booking_id = self.reserve()
        booking = self.db.bookings(history=True)[0]
        await self.event(callback=f"slotcapacity:{booking['slot_id']}", user=999)
        await self.event(text="0", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"],
            "SlotWizard:capacity",
        )
        await self.event(text="2", user=999)
        await self.event(callback="swsave", user=999)
        self.assertEqual(self.db.bookings(history=True)[0]["id"], booking_id)
        self.assertEqual(self.db.slots("haircut")[0]["free"], 1)
