"""Portable, recoverable database maintenance. No credentials are exported to logs."""

import fcntl
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

BUSINESS_TABLES = ("slots", "bookings", "leads")


@contextmanager
def data_lock(db):
    with Path(db.path).with_suffix(".webhook.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def backup_database(db, reason="backup"):
    directory = Path(db.path).resolve().parent / "backups"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    label = (
        reason if reason in ("backup", "before-reset", "before-restore") else "backup"
    )
    name = f"{label}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.sqlite3"
    target = directory / name
    target.touch(mode=0o600, exist_ok=False)
    try:
        with (
            closing(sqlite3.connect(db.path)) as source,
            closing(sqlite3.connect(target)) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Не удалось проверить резервную копию.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _reset_tables(connection):
    # Staff profiles live in users; their names and Telegram usernames are retained.
    for table in ("bookings", "leads", "slots"):
        connection.execute(f"DELETE FROM {table}")
    keep = {"staff", "users", "settings"}
    tables = [
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        if table not in keep and table not in BUSINESS_TABLES:
            connection.execute('DELETE FROM "' + table.replace('"', '""') + '"')
    connection.execute(
        "DELETE FROM users WHERE user_id NOT IN (SELECT user_id FROM staff)"
    )
    # Seed markers prevent an old OWNER_IDS value from reversing ownership transfer.
    seeds = [
        (r["key"], r["value"])
        for r in connection.execute("SELECT key,value FROM settings")
        if r["key"] == "staff_migrated" or r["key"].startswith("owner_seeded_")
    ]
    connection.execute("DELETE FROM settings")
    connection.executemany(
        "INSERT INTO settings VALUES (?,?)",
        seeds
        + [
            ("approval_haircut", "0"),
            ("approval_color", "0"),
            ("schedule_revision", "0"),
            ("reset_cutoff", str(int(time.time()))),
        ],
    )


def reset_data(db):
    """Back up first, then clear demo/business data atomically, preserving staff."""
    with data_lock(db):
        backup = backup_database(db, "before-reset")
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _reset_tables(connection)
    return backup


def restore_business_data(db, backup):
    """Restore business data without reverting current staff roles or replaying notices."""
    source_path = Path(backup).resolve()
    if not source_path.is_file() or source_path == Path(db.path).resolve():
        raise ValueError("Нужен отдельный файл резервной копии.")
    with closing(
        sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    ) as source:
        source.row_factory = sqlite3.Row
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Резервная копия повреждена.")
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(BUSINESS_TABLES) <= tables:
            raise ValueError("Это не резервная копия данных бота.")
        with data_lock(db):
            before = backup_database(db, "before-restore")
            with db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _reset_tables(connection)
                staff = {
                    row[0] for row in connection.execute("SELECT user_id FROM staff")
                }
                for table in ("users", *BUSINESS_TABLES):
                    if table not in tables:
                        continue
                    existing = {
                        row[1]
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    columns = [
                        row[1]
                        for row in source.execute(f"PRAGMA table_info({table})")
                        if row[1] in existing
                    ]
                    for row in source.execute(f"SELECT * FROM {table}"):
                        if table == "users" and row["user_id"] in staff:
                            continue
                        fields = ",".join(
                            '"' + column.replace('"', '""') + '"' for column in columns
                        )
                        connection.execute(
                            f"INSERT INTO {table} ({fields}) VALUES ({','.join('?' for _ in columns)})",
                            [row[column] for column in columns],
                        )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("В копии нарушены связи между окнами и записями.")
    return before
