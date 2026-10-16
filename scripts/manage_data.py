"""Maintenance commands with automatic backup and a supported restore path."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pearl.config import Config
from pearl.data_tools import backup_database, reset_data, restore_business_data
from pearl.db import Database
from pearl.panel_auth import panel_password


def main():
    parser = argparse.ArgumentParser(description="Копии, очистка и перенос данных бота")
    parser.add_argument(
        "command", choices=("backup", "reset", "restore", "web-access", "counts")
    )
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    config = Config.load()
    db = Database(config.database, config.timezone)
    if args.command == "web-access":
        secret = os.getenv("WEBHOOK_SECRET", "")
        if len(secret) < 24:
            raise ValueError("Заполните WEBHOOK_SECRET в .env.")
        target = Path(config.database).resolve().parent / "web-access.txt"
        descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                "Панель: "
                + os.getenv("WEBHOOK_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
                + "/admin/\nПароль: "
                + panel_password(config, secret)
                + "\n"
            )
        target.chmod(0o600)
        print("Данные входа сохранены в приватном файле: " + str(target))
    elif args.command == "backup":
        print("Резервная копия: " + str(backup_database(db)))
    elif args.command == "reset":
        if args.confirm != "RESET":
            parser.error(
                "Для очистки укажите --confirm RESET. Перед очисткой создаётся копия; сотрудники сохраняются."
            )
        print("Очищено. Копия для отката: " + str(reset_data(db)))
    elif args.command == "restore":
        if args.confirm != "RESTORE" or not args.backup:
            parser.error("Нужны --backup путь_к_копии и --confirm RESTORE.")
        print(
            "Данные восстановлены. Текущие сотрудники сохранены. Копия до восстановления: "
            + str(restore_business_data(db, args.backup))
        )
    else:
        print(
            json.dumps(
                {
                    table: db.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]
                    for table in ("staff", "slots", "bookings", "leads")
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
