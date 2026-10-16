import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pearl.access import Access
from pearl.db import Database
from pearl.web import create_app


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            database=self.tmp.name + "/db.sqlite",
            timezone=ZoneInfo("Europe/Moscow"),
            owners=frozenset({999}),
            admin_password="Telegram-only-password-123",
            web_panel_password="Web-only-password-123456",
            channel="",
            address="Адрес",
            phone="+79491234567",
        )
        self.app = create_app(self.config, "webhook-secret-for-tests-12345678")
        self.client = self.app.test_client()
        self.db = Database(self.config.database, self.config.timezone)
        access = Access(self.db, self.config)
        access.remember(999, "Владелец", "owner", seen=True)
        starts = (datetime.now(self.config.timezone) + timedelta(days=1)).isoformat()
        self.db.add_slot("haircut", starts, 2)
        self.booking_id = self.db.reserve(
            self.db.slots()[0]["id"],
            100,
            "<script>alert(1)</script>",
            "+79491234567",
            "Короткие",
            "",
            {999},
            "Адрес",
            client_comment='=HYPERLINK("bad")',
        )
        self.db.add_lead(
            101,
            "Тестовый клиент",
            "+79491234567",
            "Колористика",
            {999},
            "Вечерняя группа",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path, **kwargs):
        return self.client.get(path, base_url="https://localhost", **kwargs)

    def post(self, path, **kwargs):
        return self.client.post(path, base_url="https://localhost", **kwargs)

    def token(self):
        response = self.get("/admin/login")
        return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]

    def login(self):
        return self.post(
            "/admin/login",
            data={"csrf": self.token(), "password": self.config.web_panel_password},
            follow_redirects=True,
        )

    def test_all_data_routes_and_exports_require_authentication(self):
        for path in (
            "/admin/",
            "/admin/bookings",
            "/admin/bookings/1",
            "/admin/leads",
            "/admin/slots",
            "/admin/staff",
            "/admin/backups",
            "/admin/exports/bookings.csv",
            "/admin/backups/backup.sqlite3",
        ):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/admin/login"))
                self.assertNotIn("+79491234567", response.text)
                self.assertIn("no-store", response.headers["Cache-Control"])

    def test_login_renders_every_page_and_escapes_customer_text(self):
        self.assertEqual(self.login().status_code, 200)
        for path in (
            "/admin/",
            "/admin/bookings?scope=all",
            "/admin/slots",
            "/admin/leads",
            "/admin/staff",
            "/admin/backups",
            f"/admin/bookings/{self.booking_id}",
        ):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Жемчужина", response.text)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn(
            "&lt;script&gt;", self.get(f"/admin/bookings/{self.booking_id}").text
        )
        for path in ("/static/brand/logo.png", "/static/panel/style.css"):
            with self.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_csrf_and_separate_password_and_logout(self):
        self.assertEqual(
            self.post(
                "/admin/login", data={"password": self.config.web_panel_password}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(
                "/admin/login",
                data={"csrf": self.token(), "password": self.config.admin_password},
            ).status_code,
            401,
        )
        response = self.post(
            "/admin/login",
            data={"csrf": self.token(), "password": self.config.web_panel_password},
        )
        self.assertEqual(response.status_code, 302)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(self.post("/admin/backups/create").status_code, 400)
        self.assertEqual(self.post("/admin/logout").status_code, 400)
        with self.client.session_transaction(base_url="https://localhost") as session:
            csrf = session["csrf"]
        self.assertEqual(
            self.post("/admin/logout", data={"csrf": csrf}).status_code, 302
        )
        self.assertEqual(self.get("/admin/bookings").status_code, 302)

    def test_login_attempts_are_rate_limited(self):
        for _ in range(5):
            self.assertEqual(
                self.post(
                    "/admin/login", data={"csrf": self.token(), "password": "wrong"}
                ).status_code,
                401,
            )
        self.assertEqual(
            self.post(
                "/admin/login",
                data={"csrf": self.token(), "password": self.config.web_panel_password},
            ).status_code,
            429,
        )

    def test_filters_csv_and_unknown_inputs(self):
        self.login()
        response = self.get("/admin/bookings?scope=all&q=nomatch")
        self.assertNotIn("Открыть ↗", response.text)
        response = self.get("/admin/exports/bookings.csv?scope=all")
        self.assertEqual(response.status_code, 200)
        self.assertIn("'=HYPERLINK", response.text)
        self.assertIn("'+79491234567", response.text)
        self.assertNotIn(self.config.web_panel_password, response.text)
        self.assertEqual(self.get("/admin/bookings?scope=invalid").status_code, 400)
        self.assertEqual(self.get("/admin/bookings?sort=DROP%20TABLE").status_code, 400)
        self.assertEqual(self.get("/admin/bookings?page=not-a-number").status_code, 400)
        self.assertEqual(
            self.get("/admin/bookings?endpoint=bad&dataset=bad").status_code, 200
        )
        self.assertEqual(self.get("/admin/exports/settings.csv").status_code, 404)
        self.assertEqual(self.get("/admin/exports/leads.csv").status_code, 200)
        self.assertEqual(self.get("/admin/exports/slots.csv").status_code, 200)

    def test_backup_download_is_valid_and_protected_from_path_traversal(self):
        self.login()
        with self.client.session_transaction(base_url="https://localhost") as session:
            csrf = session["csrf"]
        response = self.post("/admin/backups/create", data={"csrf": csrf})
        self.assertEqual(response.status_code, 200)
        response.close()
        backups = list(
            (Path(self.config.database).parent / "backups").glob("*.sqlite3")
        )
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM bookings").fetchone()[0], 1
            )
        with self.get("/admin/backups/" + backups[0].name) as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get("/admin/backups/..%2F.env").status_code, 404)
