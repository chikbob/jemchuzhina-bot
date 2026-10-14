"""Run on the host after /health responds over HTTPS. Never prints bot secrets."""

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pearl.brand import BOT_DESCRIPTION, BOT_NAME, BOT_SHORT_DESCRIPTION
from pearl.config import Config
from pearl.db import Database
from pearl.transport import create_bot


async def main():
    config = Config.load()
    base = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
    secret = os.getenv("WEBHOOK_SECRET", "")
    if (
        urlparse(base).scheme != "https"
        or not urlparse(base).hostname
        or len(secret) < 24
    ):
        raise ValueError(
            "Заполните WEBHOOK_BASE_URL (https://...) и WEBHOOK_SECRET в .env"
        )
    bot = create_bot(config)
    try:
        me = await bot.get_me()
        await bot.set_my_name(BOT_NAME)
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        await bot.set_my_commands(
            [
                {"command": "start", "description": "Главное меню"},
                {"command": "cancel", "description": "Начать заново"},
                {"command": "admin", "description": "Панель сотрудников"},
                {"command": "logout", "description": "Выйти из управления"},
                {"command": "id", "description": "Мой Telegram ID"},
            ]
        )
        db = Database(config.database, config.timezone)
        db.set_setting("bot_username", me.username)
        await bot.set_webhook(
            base + "/telegram/webhook",
            secret_token=secret,
            max_connections=1,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        print(
            {
                "bot": "https://t.me/" + me.username,
                "webhook": info.url,
                "pending_updates": info.pending_update_count,
                "last_error": bool(info.last_error_message),
            }
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 -- API exception text can contain credentials
        print("Webhook setup failed:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
