import json

from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteStorage(BaseStorage):
    def __init__(self, db):
        self.db = db

    @staticmethod
    def key(key: StorageKey):
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id}:{key.destiny}"

    async def set_state(self, key, state=None):
        state = state.state if hasattr(state, "state") else state
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?,?,'{}') ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                (self.key(key), state),
            )

    async def get_state(self, key):
        rows = self.db.rows("SELECT state FROM sessions WHERE key=?", (self.key(key),))
        return rows[0]["state"] if rows else None

    async def set_data(self, key, data):
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?,NULL,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (self.key(key), json.dumps(data, ensure_ascii=False)),
            )

    async def get_data(self, key):
        rows = self.db.rows("SELECT data FROM sessions WHERE key=?", (self.key(key),))
        return json.loads(rows[0]["data"]) if rows else {}

    async def close(self):
        pass
