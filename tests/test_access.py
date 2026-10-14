import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pearl.access import Access
from pearl.db import Database


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name + "/db.sqlite", ZoneInfo("Europe/Moscow"))
        self.config = SimpleNamespace(
            owners=frozenset(), admin_password="Test-password-123456"
        )
        self.access = Access(self.db, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bootstrap_claim_is_atomic_and_password_does_not_grant_future_strangers(
        self,
    ):
        def claim(uid):
            try:
                return self.access.login(uid, self.config.admin_password)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [1, 2]))
        self.assertEqual(results.count("owner"), 1)
        self.assertEqual(len(self.access.owner_ids()), 1)
        with self.assertRaises(ValueError):
            self.access.login(3, self.config.admin_password)
        self.assertIsNone(self.access.role(3))

    def test_lockout_survives_restart_and_expiry_allows_login(self):
        for _ in range(5):
            with self.assertRaises(ValueError):
                self.access.login(1, "wrong")
        access = Access(self.db, self.config)
        with self.assertRaises(ValueError):
            access.login(1, self.config.admin_password)
        with self.db.connect() as c:
            c.execute("UPDATE login_attempts SET locked_until=1 WHERE user_id=1")
        self.assertEqual(access.login(1, self.config.admin_password), "owner")

    def test_expiry_and_password_change_invalidate_sessions(self):
        self.access.login(1, self.config.admin_password)
        self.assertTrue(self.access.authenticated(1))
        rotated = Access(
            self.db,
            SimpleNamespace(
                owners=frozenset(), admin_password="Another-password-123456"
            ),
        )
        self.assertFalse(rotated.authenticated(1))
        with self.db.connect() as c:
            c.execute("UPDATE admin_logins SET expires=0")
        self.assertFalse(self.access.authenticated(1))

    def test_last_owner_protected_and_revoked_users_cannot_rejoin_with_password(self):
        self.access.login(1, self.config.admin_password)
        with self.assertRaises(ValueError):
            self.access.set_role(1, 1, None)
        with self.assertRaises(ValueError):
            self.access.set_role(1, 1, "manager")
        self.access.set_role(1, 2, "admin")
        self.access.login(2, self.config.admin_password)
        with self.db.connect() as c:
            self.db.enqueue(c, 2, "Private appointment")
        self.access.set_role(1, 2, None)
        self.assertFalse(self.access.authenticated(2))
        self.assertEqual(
            self.db.rows("SELECT delivered FROM outbox")[0]["delivered"], -1
        )
        with self.assertRaises(ValueError):
            self.access.login(2, self.config.admin_password)

    def test_manager_cannot_add_staff_even_with_valid_session(self):
        self.access.login(1, self.config.admin_password)
        self.access.set_role(1, 2, "manager")
        self.access.login(2, self.config.admin_password)
        with self.assertRaises(ValueError):
            self.access.set_role(2, 3, "manager")

    def test_legacy_manager_migrates_once_without_resurrection(self):
        with self.db.connect() as c:
            c.execute("DELETE FROM settings WHERE key='staff_migrated'")
            c.execute("INSERT INTO managers VALUES (2)")
        access = Access(self.db, self.config)
        access.login(1, self.config.admin_password)
        self.assertEqual(access.role(2), "manager")
        access.set_role(1, 2, None)
        self.assertIsNone(Access(self.db, self.config).role(2))

    def test_transfer_promotes_recipient_and_keeps_sender_logged_in_as_admin(self):
        self.access.login(1, self.config.admin_password)
        self.access.set_role(1, 2, "manager")
        self.access.login(2, self.config.admin_password)
        self.access.transfer_ownership(1, 2)
        self.assertEqual(self.access.role(2), "owner")
        self.assertEqual(self.access.role(1), "admin")
        self.assertEqual(self.access.owner_ids(), {2})
        self.assertTrue(self.access.authenticated(1))
        self.assertFalse(self.access.authenticated(2))
        self.assertFalse(self.access.can_manage(1, 2))
        self.assertEqual(len(self.db.rows("SELECT * FROM outbox")), 2)

    def test_transfer_of_preconfigured_owner_survives_restart(self):
        config = SimpleNamespace(
            owners=frozenset({1}), admin_password=self.config.admin_password
        )
        access = Access(self.db, config)
        access.login(1, config.admin_password)
        access.set_role(1, 2, "admin")
        access.transfer_ownership(1, 2)
        restarted = Access(self.db, config)
        self.assertEqual(restarted.role(1), "admin")
        self.assertEqual(restarted.role(2), "owner")

    def test_transfer_rejects_self_missing_target_and_logged_out_owner(self):
        self.access.login(1, self.config.admin_password)
        for target in (1, 2):
            with self.assertRaises(ValueError):
                self.access.transfer_ownership(1, target)
        self.access.set_role(1, 2, "manager")
        self.access.logout(1)
        with self.assertRaises(ValueError):
            self.access.transfer_ownership(1, 2)
        self.assertEqual(self.access.owner_ids(), {1})
        self.assertEqual(self.access.role(2), "manager")

    def test_concurrent_transfer_happens_once(self):
        self.access.login(1, self.config.admin_password)
        for uid in (2, 3):
            self.access.set_role(1, uid, "admin")

        def transfer(uid):
            try:
                self.access.transfer_ownership(1, uid)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(transfer, (2, 3)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.access.owner_ids()), 1)
        self.assertEqual(self.access.role(1), "admin")


if __name__ == "__main__":
    unittest.main()
