import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from test_flows import FakeSession

from pearl.db import Database
from pearl.web import create_app


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            database=self.tmp.name + "/db.sqlite",
            timezone=ZoneInfo("Europe/Moscow"),
            owners=frozenset({999}),
            admin_password="test-password-123",
            channel="",
            address="Адрес",
            phone="+79495140330",
        )
        self.secret = "test-webhook-secret-123456789"
        self.events = []

        async def processor(update, config, db):
            self.events.append(update.update_id)

        self.app = create_app(self.config, self.secret, processor)
        self.client = self.app.test_client()
        self.payload = {
            "update_id": 123,
            "message": {
                "message_id": 1,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100, "is_bot": False, "first_name": "Руслан"},
                "text": "/start",
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, payload=None, secret=None):
        return self.client.post(
            "/telegram/webhook",
            json=payload or self.payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret or self.secret},
        )

    def test_color_webhook_retries_do_not_advance_form_twice(self):
        db = Database(self.config.database, self.config.timezone)
        db.add_slot(
            "color",
            (datetime.now(self.config.timezone) + timedelta(days=1)).isoformat(),
            2,
        )
        session = FakeSession()
        original = session.make_request
        fail_next = False

        async def flaky(bot, method, timeout=None):
            nonlocal fail_next
            if fail_next and method.__api_method__ in {
                "sendMessage",
                "editMessageText",
            }:
                fail_next = False
                raise OSError("proxy failed")
            if method.__api_method__ == "answerCallbackQuery":
                raise TelegramBadRequest(method=method, message="query is too old")
            return await original(bot, method, timeout)

        session.make_request = flaky

        def factory(config):
            return Bot("123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session)

        self.client = create_app(self.config, self.secret).test_client()

        def event(text=None, callback=None):
            self.payload["update_id"] += 1
            msg = {**self.payload["message"], "text": text or "menu"}
            if callback:
                anchor = db.rows(
                    "SELECT message_id FROM ui_anchor WHERE chat_id=?",
                    (msg["chat"]["id"],),
                )
                if anchor:
                    msg["message_id"] = anchor[0]["message_id"]
                return {
                    "update_id": self.payload["update_id"],
                    "callback_query": {
                        "id": str(self.payload["update_id"]),
                        "from": msg["from"],
                        "chat_instance": "x",
                        "message": msg,
                        "data": callback,
                    },
                }
            return {"update_id": self.payload["update_id"], "message": msg}

        with patch("pearl.web.create_bot", factory):
            self.assertEqual(self.post(event(callback="slot:1")).status_code, 200)
            self.assertEqual(self.post(event(text="Короткие 10 см")).status_code, 200)
            self.assertEqual(self.post(event(callback="colored:no")).status_code, 200)
            quality = event(text="Повреждений нет")
            fail_next = True
            with self.assertLogs("pearl.web", level="ERROR"):
                self.assertEqual(self.post(quality).status_code, 503)
            self.assertEqual(
                db.rows("SELECT state FROM sessions")[0]["state"], "Flow:wish"
            )
            self.assertEqual(self.post(quality).status_code, 200)
            self.assertEqual(
                db.rows("SELECT state FROM sessions")[0]["state"], "Flow:wish"
            )
            self.assertEqual(
                self.post(event(text="Зелёный цвет, технику не знаю")).status_code, 200
            )
            self.assertEqual(
                db.rows("SELECT state FROM sessions")[0]["state"], "Flow:photo"
            )
            self.assertEqual(self.post(event(callback="skip_photo")).status_code, 200)
            self.assertEqual(self.post(event(text="Руслан")).status_code, 200)
            self.assertEqual(self.post(event(text="+79491234567")).status_code, 200)
            self.assertEqual(self.post(event(callback="skip_comment")).status_code, 200)
            submit = event(callback="submit")
            self.assertEqual(self.post(submit).status_code, 200)
            self.assertEqual(self.post(submit).status_code, 200)
        self.assertEqual(len(db.bookings()), 1)
        self.assertIn("Зелёный цвет", db.bookings()[0]["note"])
        self.assertFalse(db.rows("SELECT * FROM api_outbox WHERE delivered=0"))

    def test_health_is_public_but_webhook_requires_correct_secret(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/").location, "/admin/")
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(
            self.client.post("/telegram/webhook", json=self.payload).status_code, 403
        )
        self.assertEqual(self.post(secret="wrong").status_code, 403)
        self.assertEqual(self.events, [])
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.events, [123])

    def test_invalid_update_rejected_before_handler(self):
        self.assertEqual(self.post(payload={"hello": "world"}).status_code, 400)
        self.assertEqual(self.events, [])

    def test_internal_errors_return_retryable_status(self):
        async def failing(*args):
            raise OSError("secret details must not be exposed")

        client = create_app(self.config, self.secret, failing).test_client()
        with self.assertLogs("pearl.web", level="ERROR") as logs:
            response = client.post(
                "/telegram/webhook",
                json=self.payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret details", response.text)
        self.assertNotIn("secret details", "".join(logs.output))

    def test_actual_router_processes_each_update_once(self):
        session = FakeSession()

        def bot_factory(config):
            return Bot("123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session)

        client = create_app(self.config, self.secret).test_client()
        with patch("pearl.web.create_bot", bot_factory):
            for _ in range(2):
                response = client.post(
                    "/telegram/webhook",
                    json=self.payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
                )
                self.assertEqual(response.status_code, 200)
        calls = [c for c in session.calls if c.__api_method__ == "sendMessage"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            len(
                Database(self.config.database, self.config.timezone).rows(
                    "SELECT * FROM processed_updates"
                )
            ),
            1,
        )

    def test_sessions_survive_separate_wsgi_requests(self):
        session = FakeSession()

        def bot_factory(config):
            return Bot("123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session)

        client = create_app(self.config, self.secret).test_client()
        with patch("pearl.web.create_bot", bot_factory):
            self.payload["message"]["text"] = "/admin"
            self.payload["message"]["from"]["id"] = 999
            self.payload["message"]["chat"]["id"] = 999
            response = client.post(
                "/telegram/webhook",
                json=self.payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
            )
            self.assertEqual(response.status_code, 200)
            self.payload["update_id"] = 124
            self.payload["message"]["text"] = self.config.admin_password
            response = client.post(
                "/telegram/webhook",
                json=self.payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
            )
            self.assertEqual(response.status_code, 200)
        db = Database(self.config.database, self.config.timezone)
        self.assertEqual(db.rows("SELECT user_id FROM admin_logins")[0]["user_id"], 999)

    def test_calendar_reuses_connection_and_answers_callback_in_webhook(self):
        session = FakeSession()
        db = Database(self.config.database, self.config.timezone)
        from pearl.access import Access

        Access(db, self.config).login(999, self.config.admin_password)
        self.client = create_app(self.config, self.secret).test_client()

        def callback(data):
            self.payload["update_id"] += 1
            msg = {
                **self.payload["message"],
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "is_bot": False, "first_name": "Admin"},
            }
            anchor = db.rows("SELECT message_id FROM ui_anchor WHERE chat_id=999")
            if anchor:
                msg["message_id"] = anchor[0]["message_id"]
            return {
                "update_id": self.payload["update_id"],
                "callback_query": {
                    "id": str(self.payload["update_id"]),
                    "from": msg["from"],
                    "chat_instance": "x",
                    "message": msg,
                    "data": data,
                },
            }

        with patch(
            "pearl.web.create_bot",
            return_value=Bot(
                "123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session
            ),
        ) as factory:
            self.assertEqual(self.post(callback("calservice:haircut")).status_code, 200)
            before = len(session.calls)
            day = (
                (datetime.now(self.config.timezone) + timedelta(days=10))
                .date()
                .isoformat()
            )
            response = self.post(callback(f"calday:{day}:1"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["method"], "answerCallbackQuery")
            self.assertEqual(
                [m.__api_method__ for m in session.calls[before:]], ["editMessageText"]
            )
            response = self.post(callback("calclear"))
            self.assertEqual(response.json["text"], "Выбор очищен")
            self.assertEqual(factory.call_count, 1)

    def test_deleted_callback_message_is_acknowledged_without_restarting_form(self):
        client = create_app(self.config, self.secret).test_client()
        message = {**self.payload["message"], "date": 0}
        update = {
            "update_id": 124,
            "callback_query": {
                "id": "expired-callback",
                "from": message["from"],
                "chat_instance": "x",
                "message": message,
                "data": "slot:1",
            },
        }
        session = FakeSession()
        with patch(
            "pearl.web.create_bot",
            return_value=Bot(
                "123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session
            ),
        ):
            response = client.post(
                "/telegram/webhook",
                json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["method"], "answerCallbackQuery")
        self.assertEqual(session.calls, [])
        self.assertEqual(
            Database(self.config.database, self.config.timezone).rows(
                "SELECT * FROM sessions"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
