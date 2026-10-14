import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    owners: frozenset[int]
    channel: str
    database: str
    timezone: ZoneInfo
    address: str
    phone: str
    admin_password: str

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        owners = frozenset(
            int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()
        )
        password = os.getenv("ADMIN_PASSWORD", "").strip()
        if not token:
            raise ValueError("Заполните BOT_TOKEN в .env")
        if len(password) < 12:
            raise ValueError("Задайте ADMIN_PASSWORD в .env (не менее 12 символов)")
        return cls(
            token,
            owners,
            os.getenv("CHANNEL_ID", "").strip(),
            os.getenv("DATABASE_PATH", "data/pearl.sqlite3"),
            ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
            os.getenv("SALON_ADDRESS", "Мариуполь, ул. Металлургов, 145"),
            os.getenv("SALON_PHONE", "+79495140330"),
            password,
        )
