import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession


def create_bot(config):
    proxy = (
        os.getenv("TELEGRAM_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("http_proxy")
        or os.getenv("HTTP_PROXY")
    )
    return Bot(config.token, session=AiohttpSession(proxy=proxy, timeout=20))
