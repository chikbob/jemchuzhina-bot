import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pearl.access import Access
from pearl.data_tools import reset_data, restore_business_data
from pearl.db import Database


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name + "/db.sqlite", ZoneInfo("Europe/Moscow"))
        self.config = SimpleNamespace(
            owners=frozenset({999}), admin_password="test-owner-password-123"
        )
        access = Access(self.db, self.config)
        access.remember(999, "Владелец", "owner", seen=True)
        access.remember(100, "Клиент", "client", seen=True)
        access.login(999, self.config.admin_password)
        access.set_role(999, 777, "manager")
        access.remember(777, "Менеджер", "manager", seen=True)
        self.staff = access.staff()
        self.db.add_slot(
            "haircut",
            (datetime.now(self.db.timezone) + timedelta(days=2)).isoformat(),
            2,
        )
        self.booking_id = self.db.reserve(
            self.db.slots()[0]["id"],
            100,
            "Клиент",
            "+79491234567",
            "Короткие",
            "",
            {999},
            "Адрес",
            "Комментарий",
        )
        self.db.add_lead(100, "Клиент", "+79491234567", "Стрижки", {999})

    def tearDown(self):
        self.tmp.cleanup()

    def test_reset_is_recoverable_and_preserves_staff_profiles(self):
        self.db.set_setting("publication_enabled", "1")
        backup = reset_data(self.db)
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        for table in (
            "slots",
            "bookings",
            "leads",
            "outbox",
            "api_outbox",
            "sessions",
            "admin_logins",
            "login_attempts",
            "ui_anchor",
            "web_login_attempts",
        ):
            self.assertEqual(
                self.db.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"], 0, table
            )
        self.assertEqual(len(self.db.rows("SELECT * FROM users")), 2)
        self.assertTrue(self.db.setting("reset_cutoff"))
        self.assertEqual(self.db.setting("publication_enabled"), "")
        self.assertEqual(Access(self.db, self.config).staff(), self.staff)
        # Recovery keeps the current manager role, even when it changed after reset.
        with self.db.connect() as c:
            c.execute("UPDATE staff SET role='admin' WHERE user_id=777")
        restore_business_data(self.db, backup)
        self.assertEqual(self.db.bookings()[0]["id"], self.booking_id)
        self.assertEqual(self.db.bookings()[0]["client_comment"], "Комментарий")
        self.assertEqual(Access(self.db, self.config).role(777), "admin")
        self.assertEqual(self.db.rows("SELECT count(*) AS n FROM outbox")[0]["n"], 0)

    def test_reset_does_not_reverse_previous_ownership_transfer(self):
        access = Access(self.db, self.config)
        access.transfer_ownership(999, 777)
        reset_data(self.db)
        restarted = Access(self.db, self.config)
        self.assertEqual(restarted.role(999), "admin")
        self.assertEqual(restarted.role(777), "owner")
