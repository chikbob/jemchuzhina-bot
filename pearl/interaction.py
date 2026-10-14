"""Durable UI responses: transport retries must never repeat a form transition."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

from aiogram import BaseMiddleware, methods
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessage
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

log = logging.getLogger(__name__)
current_ui = ContextVar("current_ui", default=None)
webhook_ack = ContextVar("webhook_ack", default=None)


class UIRequestMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        ctx = current_ui.get()
        if ctx is None:
            return await make_request(bot, method)
        db, chat_id, batch = ctx
        name = method.__api_method__
        if name not in {
            "sendMessage",
            "sendPhoto",
            "editMessageText",
            "editMessageReplyMarkup",
            "deleteMessage",
            "answerCallbackQuery",
        }:
            return await make_request(bot, method)
        if name == "answerCallbackQuery" and webhook_ack.get() is not None:
            webhook_ack.get().update(
                json.loads(method.model_dump_json(exclude_none=True))
            )
            webhook_ack.get()["method"] = name
            return True
        if name in {"sendMessage", "sendPhoto"} and method.chat_id == chat_id:
            batch["screens"].append(method)
        else:
            queue(db, method)
        if name in {"sendMessage", "sendPhoto", "editMessageText"}:
            return Message.model_validate(
                {
                    "message_id": getattr(method, "message_id", None) or 1,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": int(method.chat_id), "type": "private"},
                    "text": getattr(method, "text", None),
                },
                context={"bot": bot},
            )
        return True


def queue(db, method, ui=False):
    defaults = {key for key, value in method if isinstance(value, Default)}
    with db.connect() as c:
        chat_id = getattr(method, "chat_id", None)
        if isinstance(method, methods.EditMessageText):
            c.execute(
                "UPDATE api_outbox SET delivered=-1 WHERE delivered=0 AND method='EditMessageText' AND owner_chat=?",
                (chat_id,),
            )
        c.execute(
            "INSERT INTO api_outbox(method,payload,ui,owner_chat) VALUES (?,?,?,?)",
            (
                type(method).__name__,
                method.model_dump_json(exclude_none=True, exclude=defaults),
                int(ui),
                chat_id,
            ),
        )


class InteractionMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event, data):
        msg = event.message if isinstance(event, CallbackQuery) else event
        if not msg or msg.chat.type != "private":
            return await handler(event, data)
        bot = data["bot"]
        if not getattr(bot.session, "_pearl_ui", False):
            bot.session.middleware(UIRequestMiddleware())
            bot.session._pearl_ui = True
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO ui_cursor VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=MAX(message_id,excluded.message_id)",
                (msg.chat.id, msg.message_id),
            )
        restarting = isinstance(event, Message) and (msg.text or "").split()[0:1] in (
            ["/start"],
            ["/clean"],
        )
        if restarting:
            reset_screen(self.db, msg.chat.id)
        if isinstance(event, CallbackQuery):
            anchors = self.db.rows(
                "SELECT message_id FROM ui_anchor WHERE chat_id=?", (msg.chat.id,)
            )
            if (
                anchors
                and msg.message_id != anchors[0]["message_id"]
                and not self.db.rows(
                    "SELECT 1 FROM ui_messages WHERE chat_id=? AND message_id=?",
                    (msg.chat.id, msg.message_id),
                )
            ):
                ack = webhook_ack.get()
                if ack is not None:
                    ack.update(
                        method="answerCallbackQuery",
                        callback_query_id=event.id,
                        text="Этот экран устарел. Откройте /start.",
                    )
                else:
                    await event.answer("Этот экран устарел. Откройте /start.")
                return
        token = current_ui.set((self.db, msg.chat.id, {"screens": []}))
        try:
            result = await handler(event, data)
            screens = current_ui.get()[2]["screens"]
            candidates = [
                i
                for i, screen in enumerate(screens)
                if screen.__api_method__ == "sendMessage"
                and (
                    screen.reply_markup is None
                    or type(screen.reply_markup).__name__ == "InlineKeyboardMarkup"
                    or (
                        isinstance(screen.reply_markup, ReplyKeyboardRemove)
                        and i == len(screens) - 1
                    )
                )
            ]
            anchor_index = candidates[-1] if candidates else None
            for i, screen in enumerate(screens):
                # Remove keyboard carriers only after the actual screen is ready.
                disposable = isinstance(
                    getattr(screen, "reply_markup", None), ReplyKeyboardRemove
                )
                if (
                    disposable
                    and i != len(screens) - 1
                    and not self.db.setting(f"reply_keyboard:{msg.chat.id}")
                ):
                    continue
                if (
                    disposable
                    and i == anchor_index
                    and not self.db.setting(f"reply_keyboard:{msg.chat.id}")
                ):
                    screen = screen.model_copy(update={"reply_markup": None})
                queue(
                    self.db,
                    screen,
                    ui=2
                    if i == anchor_index
                    else 3
                    if disposable and anchor_index is not None
                    else 1,
                )
            if screens:
                queue_cleanup(
                    self.db,
                    msg.chat.id,
                    [msg.message_id] if isinstance(event, Message) else None,
                )
            if current_ui.get()[2].get("recent_cleanup"):
                queue_recent_cleanup(self.db, msg.chat.id)
            if isinstance(event, Message) and not screens:
                queue(
                    self.db,
                    DeleteMessage(chat_id=msg.chat.id, message_id=msg.message_id),
                )
            return result
        finally:
            current_ui.reset(token)


def reset_screen(db, chat_id):
    """Cancel pending UI from abandoned forms; keep the visible screen until replacement."""
    with db.connect() as c:
        c.execute(
            "UPDATE api_outbox SET delivered=-1 WHERE delivered=0 AND owner_chat=?",
            (chat_id,),
        )
        c.execute(
            "INSERT OR IGNORE INTO ui_messages SELECT chat_id,message_id FROM ui_anchor WHERE chat_id=?",
            (chat_id,),
        )
        c.execute("DELETE FROM ui_anchor WHERE chat_id=?", (chat_id,))


def queue_recent_cleanup(db, chat_id):
    rows = db.rows("SELECT message_id FROM ui_cursor WHERE chat_id=?", (chat_id,))
    if rows:
        newest = rows[0]["message_id"]
        queue_cleanup(db, chat_id, list(range(max(1, newest - 199), newest + 1)))


def queue_cleanup(db, chat_id, recent=None):
    ids = [
        r["message_id"]
        for r in db.rows(
            "SELECT message_id FROM ui_messages WHERE chat_id=?", (chat_id,)
        )
    ]
    with db.connect() as c:
        c.execute(
            "INSERT INTO api_outbox(method,payload,owner_chat) VALUES (?,?,?)",
            (
                "CleanupUI",
                json.dumps({"chat_id": chat_id, "ids": ids + (recent or [])}),
                chat_id,
            ),
        )


async def show_anchor(bot, db, method):
    rows = db.rows(
        "SELECT message_id FROM ui_anchor WHERE chat_id=?", (method.chat_id,)
    )
    if rows and not isinstance(method.reply_markup, ReplyKeyboardRemove):
        values = method.model_dump(exclude_none=True)
        edit = methods.EditMessageText.model_validate(
            {
                **{
                    k: v
                    for k, v in values.items()
                    if k in methods.EditMessageText.model_fields
                },
                "message_id": rows[0]["message_id"],
            }
        )
        try:
            return await bot(edit)
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return None
            if not any(
                part in error.message.lower()
                for part in (
                    "message to edit not found",
                    "message can't be edited",
                    "there is no text",
                    "message_id_invalid",
                    "message identifier is not specified",
                    "message not found",
                )
            ):
                raise
    result = await bot(method)
    with db.connect() as c:
        if rows:
            c.execute(
                "INSERT OR IGNORE INTO ui_disposable VALUES (?,?)",
                (method.chat_id, rows[0]["message_id"]),
            )
        c.execute(
            "INSERT OR REPLACE INTO ui_anchor VALUES (?,?)",
            (result.chat.id, result.message_id),
        )
    return result


async def flush_ui(bot, db):
    """Failed network calls stay queued; the previous screen remains visible."""
    for row in db.rows(
        "SELECT * FROM api_outbox WHERE delivered=0 ORDER BY id LIMIT 100"
    ):
        payload = json.loads(row["payload"])
        if row["method"] == "CleanupUI":
            chat_id = payload["chat_id"]
            ids = set(payload["ids"]) | {
                r["message_id"]
                for r in db.rows(
                    "SELECT message_id FROM ui_disposable WHERE chat_id=?", (chat_id,)
                )
            }
            anchors = db.rows(
                "SELECT message_id FROM ui_anchor WHERE chat_id=?", (chat_id,)
            )
            ids -= {r["message_id"] for r in anchors}
            for offset in range(0, len(ids), 100):
                group = sorted(ids)[offset : offset + 100]
                try:
                    deletion = methods.DeleteMessages(
                        chat_id=chat_id, message_ids=group
                    )
                    ack = webhook_ack.get()
                    if ack is not None and not ack:
                        ack.update(
                            method="deleteMessages", chat_id=chat_id, message_ids=group
                        )
                    else:
                        await bot(deletion)
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
                with db.connect() as c:
                    c.executemany(
                        "DELETE FROM ui_messages WHERE chat_id=? AND message_id=?",
                        [(chat_id, mid) for mid in group],
                    )
                    c.executemany(
                        "DELETE FROM ui_disposable WHERE chat_id=? AND message_id=?",
                        [(chat_id, mid) for mid in group],
                    )
            with db.connect() as c:
                c.execute("UPDATE api_outbox SET delivered=1 WHERE id=?", (row["id"],))
            continue
        method = getattr(methods, row["method"]).model_validate(payload)
        if row["method"] == "DeleteMessage":
            anchor = db.rows(
                "SELECT message_id FROM ui_anchor WHERE chat_id=?", (method.chat_id,)
            )
            if anchor and anchor[0]["message_id"] == method.message_id:
                with db.connect() as c:
                    c.execute(
                        "UPDATE api_outbox SET delivered=-1 WHERE id=?", (row["id"],)
                    )
                continue
        try:
            result = (
                await show_anchor(bot, db, method)
                if row["ui"] == 2
                else await bot(method)
            )
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            log.warning("UI request %s skipped (%s)", row["id"], type(error).__name__)
            with db.connect() as c:
                c.execute("UPDATE api_outbox SET delivered=-1 WHERE id=?", (row["id"],))
        else:
            with db.connect() as c:
                c.execute("UPDATE api_outbox SET delivered=1 WHERE id=?", (row["id"],))
                if isinstance(result, Message):
                    c.execute(
                        "INSERT INTO ui_cursor VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=MAX(message_id,excluded.message_id)",
                        (result.chat.id, result.message_id),
                    )
                if isinstance(method, methods.SendMessage):
                    markup = method.reply_markup
                    if type(markup).__name__ == "ReplyKeyboardMarkup":
                        c.execute(
                            "INSERT OR REPLACE INTO settings VALUES (?,?)",
                            (f"reply_keyboard:{method.chat_id}", "1"),
                        )
                    elif isinstance(markup, ReplyKeyboardRemove):
                        c.execute(
                            "INSERT OR REPLACE INTO settings VALUES (?,?)",
                            (f"reply_keyboard:{method.chat_id}", ""),
                        )
                if row["ui"] in (1, 3) and isinstance(result, Message):
                    if row["ui"] == 3:
                        c.execute(
                            "INSERT OR IGNORE INTO ui_disposable VALUES (?,?)",
                            (result.chat.id, result.message_id),
                        )
                    c.execute(
                        "INSERT OR IGNORE INTO ui_messages VALUES (?,?)",
                        (result.chat.id, result.message_id),
                    )
    with db.connect() as c:
        c.execute(
            "DELETE FROM api_outbox WHERE delivered!=0 AND id < (SELECT COALESCE(MAX(id),0)-500 FROM api_outbox)"
        )
