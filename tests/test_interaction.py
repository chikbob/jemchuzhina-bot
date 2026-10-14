import unittest

import test_flows
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, SendMessage

from pearl.interaction import flush_ui, queue, queue_cleanup


class PersistentScreenTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_flows.FlowTests.asyncSetUp
    asyncTearDown = test_flows.FlowTests.asyncTearDown
    event = test_flows.FlowTests.event

    async def test_start_survives_and_form_edits_same_screen(self):
        await self.event(text="/start")
        anchor = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        await self.event(callback="slot:2")
        await self.event(text="Короткие")
        self.assertEqual(
            self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"], anchor
        )
        deleted = [
            m.message_id for m in self.session.calls if isinstance(m, DeleteMessage)
        ]
        removed = [
            mid
            for m in self.session.calls
            if m.__api_method__ == "deleteMessages"
            for mid in m.message_ids
        ]
        self.assertIn(20001, removed)
        self.assertNotIn(anchor, deleted)
        edits = [m for m in self.session.calls if m.__api_method__ == "editMessageText"]
        self.assertTrue(edits)
        self.assertTrue(all(m.message_id == anchor for m in edits))

    async def test_network_failure_keeps_screen_and_defers_cleanup(self):
        await self.event(text="/start")
        anchor = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        queue(self.db, SendMessage(chat_id=100, text="Следующий вопрос"), ui=2)
        queue(self.db, DeleteMessage(chat_id=100, message_id=55))
        original = self.session.make_request

        async def fail(bot, method, timeout=None):
            raise OSError("Network unavailable")

        self.session.make_request = fail
        with self.assertRaises(OSError):
            await flush_ui(self.bot, self.db)
        self.assertEqual(
            self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"], anchor
        )
        self.assertEqual(
            len(self.db.rows("SELECT * FROM api_outbox WHERE delivered=0")), 2
        )
        self.session.make_request = original
        await flush_ui(self.bot, self.db)
        self.assertEqual(self.session.calls[-1].message_id, 55)
        self.assertEqual(
            self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"], anchor
        )

    async def test_deleted_screen_recreated_before_auxiliary_cleanup(self):
        await self.event(text="/start")
        old = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        with self.db.connect() as c:
            c.execute("INSERT INTO ui_messages VALUES (100, 777)")
        queue(self.db, SendMessage(chat_id=100, text="Восстановленный экран"), ui=2)
        queue_cleanup(self.db, 100)
        original = self.session.make_request

        async def missing(bot, method, timeout=None):
            if method.__api_method__ == "editMessageText":
                raise TelegramBadRequest(
                    method=method, message="message to edit not found"
                )
            return await original(bot, method, timeout)

        self.session.make_request = missing
        before = len(self.session.calls)
        await flush_ui(self.bot, self.db)
        self.assertNotEqual(
            self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"], old
        )
        self.assertEqual(
            [m.__api_method__ for m in self.session.calls[before:]],
            ["sendMessage", "deleteMessages"],
        )

    async def test_legacy_cleanup_cannot_delete_anchor(self):
        await self.event(text="/start")
        anchor = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        queue(self.db, DeleteMessage(chat_id=100, message_id=anchor))
        await flush_ui(self.bot, self.db)
        self.assertFalse(
            any(
                isinstance(m, DeleteMessage) and m.message_id == anchor
                for m in self.session.calls
            )
        )

    async def test_start_cancels_form_pending_replies_and_recreates_menu(self):
        await self.event(text="/start")
        old = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        await self.event(callback="learn")
        queue(self.db, SendMessage(chat_id=100, text="Устаревший вопрос"), ui=2)
        queue(
            self.db,
            SendMessage(chat_id=999, text="Сообщение другому пользователю"),
            ui=2,
        )
        self.session.calls.clear()
        await self.event(text="/start")
        self.assertIsNone(self.db.rows("SELECT state FROM sessions")[0]["state"])
        self.assertNotEqual(
            self.db.rows("SELECT message_id FROM ui_anchor WHERE chat_id=100")[0][
                "message_id"
            ],
            old,
        )
        self.assertFalse(
            any(
                getattr(m, "text", "") == "Устаревший вопрос"
                for m in self.session.calls
            )
        )
        self.assertTrue(
            any(
                getattr(m, "text", "") == "Сообщение другому пользователю"
                for m in self.session.calls
            )
        )
        screens = [
            m
            for m in self.session.calls
            if m.__api_method__ == "sendMessage" and m.chat_id == 100
        ]
        self.assertEqual(len(screens), 1)
        self.assertIn("Чем можем помочь", screens[0].text)

    async def test_clean_removes_recent_messages_but_preserves_anchor_and_bookings(
        self,
    ):
        await self.event(text="/start")
        anchor = self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"]
        await self.event(callback="chatoptions")
        await self.event(callback="chatclean")
        self.assertEqual(
            self.db.rows("SELECT message_id FROM ui_anchor")[0]["message_id"], anchor
        )
        deletes = [
            m for m in self.session.calls if m.__api_method__ == "deleteMessages"
        ]
        self.assertTrue(deletes)
        self.assertTrue(all(anchor not in m.message_ids for m in deletes))
        self.assertTrue(all(len(m.message_ids) <= 100 for m in deletes))
        self.assertEqual(len(self.db.slots()), 2)

    async def test_pending_calendar_edits_are_replaced_not_replayed(self):
        from aiogram.methods import EditMessageText

        queue(self.db, EditMessageText(chat_id=100, message_id=1, text="Четыре дня"))
        queue(self.db, EditMessageText(chat_id=100, message_id=1, text="Выбор очищен"))
        await flush_ui(self.bot, self.db)
        edits = [m for m in self.session.calls if isinstance(m, EditMessageText)]
        self.assertEqual([m.text for m in edits], ["Выбор очищен"])
