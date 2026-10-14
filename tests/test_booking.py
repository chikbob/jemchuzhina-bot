import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram.fsm.storage.base import StorageKey

from pearl.db import Database
from pearl.storage import SQLiteStorage
from pearl.ui import parse_slot, phone_number, schedule_text


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tz = ZoneInfo("Europe/Moscow")
        self.db = Database(self.tmp.name + "/test.sqlite", self.tz)
        self.date = (
            (datetime.now(self.tz) + timedelta(days=2))
            .replace(microsecond=0)
            .isoformat()
        )
        self.db.add_slot("haircut", self.date, 1)
        self.slot = self.db.slots()[0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_slot_accepts_commas_semicolons_and_spaces(self):
        date = (datetime.now(self.tz) + timedelta(days=3)).strftime("%d.%m.%Y")
        for sep in (", ", "; ", " "):
            service, starts, capacity = parse_slot(
                sep.join(["окрашивание", date, "10:00", "2"]), self.tz
            )
            self.db.add_slot(service, starts, capacity)
        self.assertEqual(len(self.db.slots("color")), 1)
        for invalid in (
            "окрашивание, завтра, 10:00, 2",
            "окрашивание 16.10.2026 10:00",
            "стрижка, 32.13.2026, 10:00, 2",
        ):
            with self.assertRaises(ValueError) as error:
                parse_slot(invalid, self.tz)
            self.assertNotIn("unpack", str(error.exception))

    def test_hidden_deleted_and_expired_slots_keep_booking_history(self):
        booking = self.reserve()
        self.db.set_slot_visibility(self.slot, False)
        self.assertEqual(self.db.slots(available=True), [])
        self.db.set_slot_visibility(self.slot, True)
        self.db.delete_slot(self.slot)
        self.assertEqual(self.db.slots(), [])
        self.assertEqual(self.db.bookings(history=True)[0]["id"], booking)
        self.db.add_slot("haircut", self.date, 1)
        with self.db.connect() as c:
            c.execute(
                "UPDATE slots SET starts=? WHERE id=?",
                ((datetime.now(self.tz) - timedelta(days=1)).isoformat(), self.slot),
            )
        self.assertEqual(self.db.slots(), [])
        self.assertEqual(self.db.bookings(), [])
        self.assertEqual(self.db.rows("SELECT deleted FROM slots")[0]["deleted"], 1)
        self.db.set_booking_status(booking, "completed", 999, {999})
        self.assertEqual(self.db.bookings(history=True)[0]["status"], "completed")
        with self.assertRaises(ValueError):
            self.db.set_booking_status(booking, "no_show", 100, {999})

    def test_restoring_future_booking_cannot_overbook_slot(self):
        booking = self.reserve()
        self.db.set_booking_status(booking, "rejected", 999, {999})
        self.reserve(user=101)
        with self.assertRaises(ValueError):
            self.db.set_booking_status(booking, "confirmed", 999, {999})

    def reserve(self, user=100):
        return self.db.reserve(
            self.slot,
            user,
            "Руслан",
            "+79491234567",
            "Длина: короткие",
            "",
            {999},
            "Металлургов 145",
        )

    def test_last_place_is_atomic_across_connections(self):
        def book(user):
            try:
                db = Database(self.db.path, self.tz)
                return db.reserve(
                    self.slot,
                    user,
                    "Клиент",
                    "+79491234567",
                    "короткие",
                    "",
                    {999},
                    "Адрес",
                )
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(book, range(10, 18)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(len(self.db.bookings()), 1)
        self.assertEqual(self.db.slots(available=True), [])

    def test_cancel_releases_capacity_and_notifies(self):
        booking = self.reserve()
        self.db.change_booking(booking, "cancel", 100, {999})
        self.assertEqual(self.db.slots(available=True)[0]["free"], 1)
        self.assertEqual(self.db.bookings(), [])
        self.assertEqual(len(self.db.rows("SELECT * FROM outbox")), 4)

    def test_other_client_cannot_cancel(self):
        booking = self.reserve()
        with self.assertRaises(ValueError):
            self.db.change_booking(booking, "cancel", 200, {999})
        self.assertEqual(len(self.db.bookings()), 1)

    def test_duplicate_and_repeated_actions_do_not_change_capacity(self):
        self.db.add_slot("haircut", self.date, 2)
        booking = self.reserve()
        with self.assertRaises(ValueError):
            self.reserve()
        self.db.change_booking(booking, "cancel", 100, {999})
        with self.assertRaises(ValueError):
            self.db.change_booking(booking, "cancel", 100, {999})
        self.assertEqual(self.db.slots()[0]["free"], 2)

    def test_closed_slot_blocks_new_bookings_preserves_existing(self):
        self.db.add_slot("haircut", self.date, 2)
        self.reserve()
        self.db.toggle_slot(self.slot)
        with self.assertRaises(ValueError):
            self.reserve(200)
        self.assertEqual(len(self.db.bookings()), 1)

    def test_capacity_cannot_be_lower_than_bookings(self):
        self.db.add_slot("haircut", self.date, 2)
        self.reserve()
        self.reserve(200)
        with self.assertRaises(ValueError):
            self.db.add_slot("haircut", self.date, 1)

    def test_color_immediately_confirmed_by_default(self):
        self.db.add_slot("color", self.date, 1)
        slot = next(s for s in self.db.slots() if s["service"] == "color")
        self.db.reserve(
            slot["id"],
            100,
            "Руслан",
            "+79491234567",
            "Длина: до плеч\nСостояние: сухие\nПожелания: блонд",
            "photo-id",
            {999},
            "Адрес",
        )
        self.assertEqual(self.db.bookings()[0]["status"], "confirmed")
        self.assertEqual(
            self.db.rows("SELECT * FROM outbox WHERE chat_id=999")[0]["photo"],
            "photo-id",
        )

    def test_optional_approval_holds_capacity_until_rejection(self):
        self.db.set_setting("approval_haircut", "1")
        booking = self.reserve()
        self.assertEqual(self.db.bookings()[0]["status"], "pending")
        self.assertEqual(self.db.slots(available=True), [])
        self.db.change_booking(booking, "reject", 999, {999})
        self.assertEqual(self.db.slots()[0]["free"], 1)

    def test_sessions_and_bookings_survive_restart(self):
        self.reserve()

        async def scenario():
            key = StorageKey(bot_id=1, chat_id=2, user_id=2)
            storage = SQLiteStorage(self.db)
            await storage.set_state(key, "Flow:phone")
            await storage.set_data(key, {"name": "Руслан", "slot_id": self.slot})
            other = Database(self.db.path, self.tz)
            storage = SQLiteStorage(other)
            self.assertEqual(await storage.get_state(key), "Flow:phone")
            self.assertEqual((await storage.get_data(key))["name"], "Руслан")
            self.assertEqual(len(other.bookings()), 1)

        asyncio.run(scenario())

    def test_schedule_contains_only_available_capacity(self):
        config = SimpleNamespace(address="Адрес", phone="Телефон")
        self.assertIn("мест: 1", schedule_text(self.db, config))
        self.reserve()
        self.assertIn("свободных окон нет", schedule_text(self.db, config))
        self.assertNotIn("Руслан", schedule_text(self.db, config))

    def test_past_slots_rejected(self):
        past = (datetime.now(self.tz) - timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            self.db.add_slot("color", past, 1)

    def test_phone_validation(self):
        self.assertEqual(phone_number("8 (949) 123-45-67"), "+79491234567")
        for bad in ("", "не телефон", "12345", "+00000000000"):
            with self.assertRaises(ValueError):
                phone_number(bad)


if __name__ == "__main__":
    unittest.main()


class LegacyMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_old_booking_and_adds_comment_defaults(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/legacy.sqlite"
            with sqlite3.connect(path) as c:
                c.executescript("""
                    CREATE TABLE slots(id INTEGER PRIMARY KEY,service TEXT NOT NULL,starts TEXT NOT NULL,capacity INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,UNIQUE(service,starts));
                    CREATE TABLE bookings(id INTEGER PRIMARY KEY,slot_id INTEGER NOT NULL,user_id INTEGER NOT NULL,name TEXT NOT NULL,phone TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',photo TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,created TEXT NOT NULL);
                    INSERT INTO slots VALUES (1,'haircut','2026-12-20T10:00:00+03:00',2,1);
                    INSERT INTO bookings VALUES (1,1,100,'Тестовый клиент','+79491234567','','','confirmed','2026-10-01T10:00:00+03:00');
                """)
            db = Database(path, ZoneInfo("Europe/Moscow"))
            row = db.bookings(history=True)[0]
            self.assertEqual(
                (row["id"], row["slot_id"], row["name"]), (1, 1, "Тестовый клиент")
            )
            self.assertEqual(
                (row["client_comment"], row["status_comment"], row["archived"]),
                ("", "", 0),
            )
            self.assertEqual(
                Database(path, ZoneInfo("Europe/Moscow")).bookings(history=True)[0][
                    "id"
                ],
                1,
            )
