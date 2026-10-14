import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Message, Update

from pearl.access import Access
from pearl.bot import build_router, deliver, update_publication
from pearl.db import Database
from pearl.interaction import flush_ui
from pearl.storage import SQLiteStorage


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ in (
            "sendMessage",
            "sendPhoto",
            "editMessageText",
            "editMessageReplyMarkup",
        ):
            return Message.model_validate(
                {
                    "message_id": getattr(method, "message_id", None)
                    or 10000 + len(self.calls),
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": int(method.chat_id), "type": "private"},
                    "text": getattr(method, "text", None),
                },
                context={"bot": bot},
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tz = ZoneInfo("Europe/Moscow")
        self.db = Database(self.tmp.name + "/test.sqlite", self.tz)
        self.config = SimpleNamespace(
            owners=frozenset({999}),
            address="Адрес",
            phone="+79495140330",
            channel="-100123",
            timezone=self.tz,
            admin_password="Test-only-password-123",
        )
        date = (datetime.now(self.tz) + timedelta(days=1)).isoformat()
        self.db.add_slot("haircut", date, 1)
        self.db.add_slot("color", date, 1)
        self.session = FakeSession()
        self.bot = Bot(
            "123456789:abcdefghijklmnopqrstuvwxyz123456789", session=self.session
        )
        self.dp = Dispatcher(
            storage=SQLiteStorage(self.db), events_isolation=SimpleEventIsolation()
        )
        self.dp.include_router(build_router(self.db, self.config))
        self.access = Access(self.db, self.config)
        self.access.login(999, self.config.admin_password)
        self.count = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.tmp.cleanup()

    async def event(
        self, text=None, callback=None, user=100, chat_type="private", extra=None
    ):
        self.count += 1
        u = {"id": user, "is_bot": False, "first_name": "Руслан"}
        anchor = self.db.rows(
            "SELECT message_id FROM ui_anchor WHERE chat_id=?", (user,)
        )
        message_id = (
            anchor[0]["message_id"] if callback and anchor else 20000 + self.count
        )
        msg = {
            "message_id": message_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": user, "type": chat_type},
            "from": u,
            "text": text or "Меню",
        }
        if extra:
            msg.update(extra)
        payload = {"update_id": self.count}
        if callback:
            payload["callback_query"] = {
                "id": str(self.count),
                "from": u,
                "chat_instance": "x",
                "message": msg,
                "data": callback,
            }
        else:
            payload["message"] = msg
        await self.dp.feed_update(self.bot, Update.model_validate(payload))
        await flush_ui(self.bot, self.db)

    async def choose_slot(self, service):
        slot = next(s for s in self.db.slots() if s["service"] == service)
        await self.event(text="/start")
        await self.event(callback="service:" + service)
        await self.event(callback="slot:" + str(slot["id"]))

    async def test_slot_validation_retries_and_cancel_keeps_admin_session(self):
        await self.event(callback="addslot", user=999)
        await self.event(callback="swservice:color", user=999)
        await self.event(text="25:00", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"], "SlotWizard:time"
        )
        await self.event(text="10:00", user=999)
        date = (datetime.now(self.tz) + timedelta(days=3)).strftime("%d.%m.%Y")
        await self.event(text=date, user=999)
        await self.event(text="0", user=999)
        self.assertEqual(
            self.db.rows("SELECT state FROM sessions")[0]["state"],
            "SlotWizard:capacity",
        )
        await self.event(text="2", user=999)
        await self.event(callback="swsave", user=999)
        self.assertEqual(len(self.db.slots("color")), 2)
        await self.event(callback="staffadd", user=999)
        await self.event(text="/cancel", user=999)
        self.assertTrue(self.access.authenticated(999))
        texts = [
            getattr(c, "text", "")
            for c in self.session.calls
            if c.__api_method__ in {"sendMessage", "editMessageText"}
        ]
        self.assertEqual(texts[-1], "Управление академией")

    async def test_slot_card_visibility_deletion_and_ui_cleanup(self):
        slot = self.db.slots()[0]["id"]
        await self.event(callback=f"slotmanage:{slot}", user=999)
        await self.event(callback=f"slotvisible:{slot}:0", user=999)
        self.assertEqual(
            next(s for s in self.db.slots() if s["id"] == slot)["enabled"], 0
        )
        await self.event(callback=f"slotvisible:{slot}:1", user=999)
        await self.event(callback=f"slotdeleteask:{slot}", user=999)
        await self.event(callback=f"slotdelete:{slot}", user=999)
        self.assertFalse(any(s["id"] == slot for s in self.db.slots()))
        self.assertFalse(
            any(c.__api_method__ == "deleteMessage" for c in self.session.calls)
        )
        self.assertEqual(
            len(self.db.rows("SELECT * FROM ui_anchor WHERE chat_id=999")), 1
        )

    async def test_haircut_requires_name_and_valid_phone(self):
        await self.choose_slot("haircut")
        await self.event(text="Короткие")
        await self.event(callback="submit")
        self.assertEqual(self.db.bookings(), [])
        await self.event(text="Руслан")
        await self.event(text="не телефон")
        await self.event(callback="submit")
        self.assertEqual(self.db.bookings(), [])
        await self.event(text="+79491234567")
        await self.event(callback="skip_comment")
        self.assertEqual(self.db.bookings(), [])
        await self.event(callback="submit")
        booking = self.db.bookings()[0]
        self.assertEqual(booking["name"], "Руслан")
        self.assertEqual(booking["phone"], "+79491234567")
        self.assertIn("Короткие", booking["note"])
        self.assertEqual(booking["status"], "confirmed")
        await self.event(callback="submit")
        self.assertEqual(len(self.db.bookings()), 1)
        await deliver(self.bot, self.db)
        self.assertTrue(
            all(x["delivered"] == 1 for x in self.db.rows("SELECT * FROM outbox"))
        )

    async def test_color_collects_length_condition_and_wish(self):
        await self.choose_slot("color")
        for text in [
            "До плеч",
            "Да, окрашены",
            "Ранее осветлённые, сухие",
            "Хочу тёмный оттенок",
        ]:
            await self.event(text=text)
        await self.event(callback="skip_photo")
        await self.event(text="Руслан")
        await self.event(text="89491234567")
        await self.event(callback="skip_comment")
        await self.event(callback="submit")
        booking = self.db.bookings()[0]
        for part in ["До плеч", "сухие", "тёмный оттенок"]:
            self.assertIn(part, booking["note"])
        self.assertEqual(booking["status"], "confirmed")

    async def test_admin_guards_cannot_be_bypassed_by_callback(self):
        before = self.db.slots()[0]["enabled"]
        await self.event(callback="toggle:" + str(self.db.slots()[0]["id"]))
        self.assertEqual(self.db.slots()[0]["enabled"], before)
        await self.event(callback="managers")
        await self.event(text="+ 111")
        self.assertEqual(self.db.rows("SELECT * FROM managers"), [])
        await self.event(callback="publish")
        self.assertNotEqual(self.db.setting("publication_enabled"), "1")

    async def test_owner_can_grant_and_revoke_manager(self):
        await self.event(callback="staffadd", user=999)
        await self.event(
            user=999,
            extra={
                "users_shared": {
                    "request_id": 707,
                    "users": [
                        {
                            "user_id": 100,
                            "first_name": "Наталья",
                            "last_name": "Вознесенская",
                        }
                    ],
                }
            },
        )
        await self.event(callback="staffassign:manager", user=999)
        self.assertEqual(self.access.role(100), "manager")
        await self.event(text="/admin")
        await self.event(text=self.config.admin_password)
        slot_id = self.db.slots()[0]["id"]
        await self.event(callback=f"toggle:{slot_id}")
        self.assertEqual(self.db.slots()[0]["enabled"], 0)
        await self.event(callback="staffremove:100", user=999)
        await self.event(callback=f"toggle:{slot_id}")
        self.assertEqual(self.db.slots()[0]["enabled"], 0)

    async def test_owner_needs_password_and_logout_blocks_old_buttons(self):
        self.access.logout(999)
        slot_id = self.db.slots()[0]["id"]
        await self.event(callback=f"toggle:{slot_id}", user=999)
        self.assertEqual(self.db.slots()[0]["enabled"], 1)
        await self.event(text="/admin", user=999)
        await self.event(text=self.config.admin_password, user=999)
        self.assertTrue(self.access.authenticated(999))
        self.assertTrue(
            any(c.__api_method__ == "deleteMessage" for c in self.session.calls)
        )
        self.assertNotIn(
            self.config.admin_password, str(self.db.rows("SELECT * FROM sessions"))
        )
        await self.event(callback=f"toggle:{slot_id}", user=999)
        self.assertEqual(self.db.slots()[0]["enabled"], 0)
        await self.event(text="/logout", user=999)
        await self.event(callback=f"toggle:{slot_id}", user=999)
        self.assertEqual(self.db.slots()[0]["enabled"], 0)

    async def test_phone_only_contact_cannot_grant_staff_role(self):
        await self.event(callback="staffadd", user=999)
        await self.event(
            user=999,
            extra={
                "contact": {"phone_number": "+79495140330", "first_name": "Наталья"}
            },
        )
        await self.event(callback="staffassign:owner", user=999)
        self.assertEqual(self.access.staff_ids(), {999})

    async def test_logged_out_staff_cannot_cancel_another_client_through_client_button(
        self,
    ):
        slot_id = self.db.slots()[0]["id"]
        booking = self.db.reserve(
            slot_id, 100, "Руслан", "+79491234567", "Короткие", "", {999}, "Адрес"
        )
        self.access.logout(999)
        await self.event(callback=f"cancel:{booking}", user=999)
        self.assertEqual(self.db.bookings()[0]["status"], "confirmed")

    async def test_admin_can_add_manager_but_cannot_promote_itself(self):
        self.access.set_role(999, 100, "admin")
        self.access.login(100, self.config.admin_password)
        await self.event(callback="staffadd")
        await self.event(text="222")
        await self.event(callback="staffassign:manager")
        self.assertEqual(self.access.role(222), "manager")
        await self.event(callback="staffchange:100:owner")
        self.assertEqual(self.access.role(100), "admin")
        await self.event(callback="staffremove:999")
        self.assertEqual(self.access.role(999), "owner")

    async def test_ownership_transfer_requires_confirmation_and_demotes_sender(self):
        self.access.set_role(999, 100, "manager")
        await self.event(callback="staffperson:100", user=999)
        card = self.session.calls[-1]
        self.assertTrue(
            any(
                b.callback_data == "transferask:100"
                for row in card.reply_markup.inline_keyboard
                for b in row
            )
        )
        await self.event(callback="transferconfirm:100", user=999)
        self.assertEqual(self.access.role(999), "owner")
        await self.event(callback="transferask:100", user=999)
        self.assertIn("вы — администратором", self.session.calls[-1].text)
        await self.event(callback="transferconfirm:100", user=999)
        self.assertEqual(self.access.role(100), "owner")
        self.assertEqual(self.access.role(999), "admin")
        self.assertTrue(self.access.authenticated(999))
        await self.event(callback="transferconfirm:100", user=999)
        self.assertEqual(self.access.role(999), "admin")

    async def test_cancelled_transfer_cannot_use_old_confirmation(self):
        self.access.set_role(999, 100, "manager")
        await self.event(callback="transferask:100", user=999)
        await self.event(callback="stafflist:0", user=999)
        await self.event(callback="transferconfirm:100", user=999)
        self.assertEqual(self.access.role(999), "owner")
        self.assertEqual(self.access.role(100), "manager")

    async def test_manager_can_view_roles_but_cannot_change_them(self):
        self.access.set_role(999, 100, "manager")
        self.access.login(100, self.config.admin_password)
        await self.event(callback="stafflist:0")
        buttons = [
            b
            for row in self.session.calls[-1].reply_markup.inline_keyboard
            for b in row
        ]
        self.assertTrue(any("Владелец" in b.text for b in buttons))
        self.assertFalse(any(b.callback_data == "staffadd" for b in buttons))
        await self.event(callback="staffperson:999")
        card = self.session.calls[-1]
        self.assertIn("Роль: Владелец", card.text)
        self.assertEqual(
            [b.callback_data for row in card.reply_markup.inline_keyboard for b in row],
            ["stafflist:0"],
        )
        await self.event(callback="staffchange:999:admin")
        await self.event(callback="transferask:999")
        self.assertEqual(self.access.role(999), "owner")

    async def test_logged_out_person_cannot_view_staff_roles(self):
        await self.event(callback="stafflist:0")
        self.assertEqual(self.session.calls[-1].__api_method__, "answerCallbackQuery")
        await self.event(callback="staffperson:999")
        self.assertEqual(self.session.calls[-1].__api_method__, "answerCallbackQuery")

    async def test_group_messages_ignored(self):
        await self.event(text="/start", chat_type="group")
        self.assertEqual(self.session.calls, [])

    async def test_learning_lead_reaches_administrator(self):
        await self.event(callback="learn")
        await self.event(callback="course:Колористика")
        await self.event(text="Руслан")
        await self.event(text="+79491234567")
        await self.event(callback="skip_comment")
        await self.event(callback="submit")
        self.assertEqual(
            self.db.rows("SELECT * FROM leads")[0]["course"], "Колористика"
        )
        await self.event(callback="lead:working:1", user=999)
        self.assertEqual(self.db.rows("SELECT * FROM leads")[0]["status"], "working")

    async def test_publication_creates_then_edits_without_client_data(self):
        self.db.set_setting("publication_enabled", "1")
        await update_publication(self.bot, self.db, self.config, "test_bot")
        self.assertEqual(self.session.calls[-1].__api_method__, "sendMessage")
        count = len(self.session.calls)
        await update_publication(self.bot, self.db, self.config, "test_bot")
        self.assertEqual(len(self.session.calls), count)
        slot = next(s for s in self.db.slots() if s["service"] == "haircut")
        self.db.reserve(
            slot["id"], 100, "Руслан", "+79491234567", "короткие", "", {999}, "Адрес"
        )
        await update_publication(self.bot, self.db, self.config, "test_bot")
        self.assertEqual(self.session.calls[-1].__api_method__, "editMessageText")
        self.assertNotIn("Руслан", self.session.calls[-1].text)
        self.assertNotIn("Стрижка ·", self.session.calls[-1].text)


if __name__ == "__main__":
    unittest.main()
