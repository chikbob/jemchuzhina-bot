"""WSGI webhook for hosts without an always-on background worker."""

import asyncio
import atexit
import fcntl
import hmac
import logging
import os
from pathlib import Path

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update
from flask import Flask, redirect, request
from pydantic import ValidationError

from .bot import build_router, deliver, update_publication
from .config import Config
from .db import Database
from .interaction import flush_ui, webhook_ack
from .storage import SQLiteStorage
from .transport import create_bot
from .web_panel import register_panel

log = logging.getLogger(__name__)


def make_dispatcher(db, config):
    dispatcher = Dispatcher(
        storage=SQLiteStorage(db), events_isolation=SimpleEventIsolation()
    )
    dispatcher.include_router(build_router(db, config))
    return dispatcher


class WebRuntime:
    """Reuse the loop, router and HTTP pool in each serialized WSGI worker."""

    def __init__(self, config, db):
        self.config, self.db = config, db
        self.runner = asyncio.Runner()
        self.bot = self.dispatcher = None
        atexit.register(self.close)

    async def process(self, update, config, db):
        if self.bot is None:
            self.bot = create_bot(config)
            self.dispatcher = make_dispatcher(db, config)
        return await process_update(update, config, db, (self.bot, self.dispatcher))

    def close(self):
        if self.bot is not None:
            self.runner.run(self.bot.session.close())
            self.bot = None
        self.runner.close()


async def process_update(update, config, db, resources=None):
    bot, dispatcher = resources or (create_bot(config), make_dispatcher(db, config))
    ack = {}
    token = webhook_ack.set(ack)
    try:
        db.expire_slots()
        if not db.rows(
            "SELECT 1 FROM processed_updates WHERE update_id=?", (update.update_id,)
        ):
            await dispatcher.feed_update(bot, update)
            with db.connect() as c:
                c.execute(
                    "INSERT OR IGNORE INTO processed_updates VALUES (?,?)",
                    (update.update_id, db.now()),
                )
                c.execute(
                    "DELETE FROM processed_updates WHERE processed < datetime('now','-7 days')"
                )
        quick_calendar = update.callback_query and (
            update.callback_query.data or ""
        ).startswith(
            ("calday:", "calmonth:", "calclear", "calnoop", "calpreset:", "calcap:")
        )
        if quick_calendar:
            await flush_ui(bot, db)
        else:
            await deliver(bot, db)
        if (
            not quick_calendar
            and config.channel
            and db.setting("publication_enabled") == "1"
        ):
            username = db.setting("bot_username")
            if not username:
                username = (await bot.get_me()).username
                db.set_setting("bot_username", username)
            await update_publication(bot, db, config, username)
        return ack or {"ok": True}
    finally:
        webhook_ack.reset(token)
        if resources is None:
            await dispatcher.storage.close()
            await dispatcher.fsm.events_isolation.close()
            await bot.session.close()


def create_app(config=None, secret=None, processor=None):
    config = config or Config.load()
    secret = secret or os.getenv("WEBHOOK_SECRET", "")
    if len(secret) < 24:
        raise ValueError("Задайте WEBHOOK_SECRET (не менее 24 символов) в .env")
    db = Database(config.database, config.timezone)
    runtime = None if processor else WebRuntime(config, db)
    process = processor or runtime.process
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    register_panel(app, db, config, secret)

    @app.get("/")
    def landing():
        return redirect("/admin/")

    @app.get("/health")
    def health():
        db.rows("SELECT 1")
        return {"status": "ok", "service": "pearl-booking-bot"}

    @app.post("/telegram/webhook")
    def webhook():
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token.encode(), secret.encode()):
            return {"error": "unauthorized"}, 403
        try:
            update = Update.model_validate(request.get_json(silent=True))
        except ValidationError:
            return {"error": "invalid update"}, 400
        # A file lock also serializes updates across WSGI processes/reloads.
        # No background daemon is needed on the free plan.
        lock_path = Path(config.database).with_suffix(".webhook.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                response = (
                    runtime.runner.run(process(update, config, db))
                    if runtime
                    else asyncio.run(process(update, config, db))
                )
            except Exception as error:  # noqa: BLE001 -- return retryable response without logging secrets
                log.error(
                    "Webhook update %s failed (%s)",
                    update.update_id,
                    type(error).__name__,
                )
                return {"error": "temporary failure"}, 503
        return response or {"ok": True}

    return app
