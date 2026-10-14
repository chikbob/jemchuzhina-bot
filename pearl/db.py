import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SERVICES = {"haircut": "Стрижка", "color": "Окрашивание"}
ACTIVE = ("pending", "confirmed")
STATUS_NAMES = {
    "pending": "Ожидает подтверждения",
    "confirmed": "Подтверждена",
    "completed": "Завершена",
    "no_show": "Не пришёл",
    "cancelled": "Отменена клиентом",
    "rejected": "Отменена администратором",
}


class Database:
    def __init__(self, path, timezone):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.timezone = timezone
        with self.connect() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS slots(
                    id INTEGER PRIMARY KEY, service TEXT NOT NULL,
                    starts TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity > 0),
                    enabled INTEGER NOT NULL DEFAULT 1, UNIQUE(service, starts));
                CREATE TABLE IF NOT EXISTS bookings(
                    id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL REFERENCES slots(id),
                    user_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '', photo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, created TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking
                    ON bookings(slot_id,user_id) WHERE status IN ('pending','confirmed');
                CREATE TABLE IF NOT EXISTS leads(
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                    phone TEXT NOT NULL, course TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new');
                CREATE TABLE IF NOT EXISTS managers(user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '', seen INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS staff(
                    user_id INTEGER PRIMARY KEY, role TEXT NOT NULL
                    CHECK(role IN ('owner','admin','manager')));
                CREATE TABLE IF NOT EXISTS admin_logins(
                    user_id INTEGER PRIMARY KEY, expires REAL NOT NULL, password_tag TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts(
                    user_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS processed_updates(
                    update_id INTEGER PRIMARY KEY, processed TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_outbox(id INTEGER PRIMARY KEY, method TEXT NOT NULL,
                    payload TEXT NOT NULL, ui INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ui_cursor(chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ui_disposable(chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS ui_anchor(chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ui_messages(chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                    photo TEXT NOT NULL DEFAULT '', buttons TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    delivered INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO settings VALUES ('approval_haircut', '0');
                INSERT OR IGNORE INTO settings VALUES ('approval_color', '0');
                INSERT OR IGNORE INTO settings VALUES ('schedule_revision', '0');
            """)
            c.execute(
                "UPDATE sessions SET state='CalendarFlow:time_menu' WHERE state='CalendarFlow:times'"
            )
            if "owner_chat" not in {
                r[1] for r in c.execute("PRAGMA table_info(api_outbox)")
            }:
                c.execute("ALTER TABLE api_outbox ADD COLUMN owner_chat INTEGER")
                c.execute(
                    "UPDATE api_outbox SET owner_chat=CAST(json_extract(payload,'$.chat_id') AS INTEGER)"
                )
            for table, columns in {
                "bookings": {
                    "client_comment": "TEXT NOT NULL DEFAULT ''",
                    "status_comment": "TEXT NOT NULL DEFAULT ''",
                    "archived": "INTEGER NOT NULL DEFAULT 0",
                },
                "leads": {"client_comment": "TEXT NOT NULL DEFAULT ''"},
            }.items():
                existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        c.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            c.execute(
                "UPDATE sessions SET state='CalendarFlow:days' WHERE state='CalendarFlow:range'"
            )
            if "deleted" not in {r[1] for r in c.execute("PRAGMA table_info(slots)")}:
                c.execute(
                    "ALTER TABLE slots ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def now(self):
        return datetime.now(self.timezone).isoformat(timespec="seconds")

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def setting(self, key, default=""):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key, value):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    @staticmethod
    def dirty(c):
        c.execute(
            "UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='schedule_revision'"
        )

    def slots(self, service=None, available=False):
        self.expire_slots()
        query = """SELECT s.*, s.capacity - (SELECT count(*) FROM bookings b
            WHERE b.slot_id=s.id AND b.status IN ('pending','confirmed')) AS free
            FROM slots s WHERE starts > ? AND deleted=0"""
        args = [self.now()]
        if service:
            query += " AND service=?"
            args.append(service)
        if available:
            query += " AND enabled=1"
        result = self.rows(query + " ORDER BY starts,service", args)
        return [r for r in result if not available or r["free"] > 0]

    def add_slot(self, service, starts, capacity):
        if service not in SERVICES or capacity < 1 or capacity > 100:
            raise ValueError("Количество мест: от 1 до 100.")
        dt = datetime.fromisoformat(starts)
        if dt.tzinfo is None or dt <= datetime.now(self.timezone):
            raise ValueError("Нужна будущая дата и время.")
        starts = dt.astimezone(self.timezone).isoformat(timespec="seconds")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT id FROM slots WHERE service=? AND starts=?", (service, starts)
            ).fetchone()
            if existing:
                used = c.execute(
                    "SELECT count(*) FROM bookings WHERE slot_id=? AND status IN ('pending','confirmed')",
                    (existing[0],),
                ).fetchone()[0]
                if capacity < used:
                    raise ValueError(
                        "Нельзя уменьшить количество мест ниже числа записей."
                    )
            c.execute(
                "INSERT INTO slots(service,starts,capacity) VALUES (?,?,?) ON CONFLICT(service,starts) DO UPDATE SET capacity=excluded.capacity,enabled=1,deleted=0",
                (service, starts, capacity),
            )
            self.dirty(c)

    def toggle_slot(self, slot_id):
        with self.connect() as c:
            c.execute(
                "UPDATE slots SET enabled=1-enabled WHERE id=? AND deleted=0 AND starts>?",
                (slot_id, self.now()),
            )
            self.dirty(c)

    def add_slots(self, service, slots):
        if service not in SERVICES or not 1 <= len(slots) <= 366:
            raise ValueError("Нужна услуга и от 1 до 366 окон.")
        normalized = {}
        for starts, capacity in slots:
            dt = datetime.fromisoformat(starts)
            if dt.tzinfo is None or dt <= datetime.now(self.timezone):
                raise ValueError(
                    "Время одного из окон уже прошло. Измените даты или время."
                )
            if not isinstance(capacity, int) or not 1 <= capacity <= 100:
                raise ValueError("Число мест: от 1 до 100.")
            normalized[dt.astimezone(self.timezone).isoformat(timespec="seconds")] = (
                capacity
            )
        created = 0
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for starts, capacity in normalized.items():
                created += c.execute(
                    "INSERT OR IGNORE INTO slots(service,starts,capacity) VALUES (?,?,?)",
                    (service, starts, capacity),
                ).rowcount
            if created:
                self.dirty(c)
        return created, len(normalized) - created

    def expire_slots(self):
        with self.connect() as c:
            if c.execute(
                "UPDATE slots SET deleted=1,enabled=0 WHERE starts<=? AND deleted=0",
                (self.now(),),
            ).rowcount:
                self.dirty(c)

    def set_slot_visibility(self, slot_id, visible):
        with self.connect() as c:
            if not c.execute(
                "UPDATE slots SET enabled=? WHERE id=? AND deleted=0 AND starts>?",
                (int(visible), slot_id, self.now()),
            ).rowcount:
                raise ValueError("Окно уже удалено или его время прошло.")
            self.dirty(c)

    def delete_slot(self, slot_id):
        # Keep the schedule reference so bookings and their history are never lost.
        with self.connect() as c:
            c.execute("UPDATE slots SET deleted=1,enabled=0 WHERE id=?", (slot_id,))
            self.dirty(c)

    @staticmethod
    def enqueue(c, user_id, text, photo="", buttons=()):
        c.execute(
            "INSERT INTO outbox(chat_id,text,photo,buttons) VALUES (?,?,?,?)",
            (user_id, text, photo, json.dumps(buttons, ensure_ascii=False)),
        )

    def reserve(
        self,
        slot_id,
        user_id,
        name,
        phone,
        note,
        photo,
        admins,
        address,
        client_comment="",
    ):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            s = c.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not s or s["deleted"] or not s["enabled"] or s["starts"] <= self.now():
                raise ValueError("Это окно уже недоступно. Выберите другое.")
            if c.execute(
                "SELECT 1 FROM bookings WHERE slot_id=? AND user_id=? AND status IN ('pending','confirmed')",
                (slot_id, user_id),
            ).fetchone():
                raise ValueError(
                    "У вас уже есть запись на это время. Откройте «Мои записи»."
                )
            used = c.execute(
                "SELECT count(*) FROM bookings WHERE slot_id=? AND status IN ('pending','confirmed')",
                (slot_id,),
            ).fetchone()[0]
            if used >= s["capacity"]:
                raise ValueError("Последнее место уже заняли. Выберите другое время.")
            approval = (
                c.execute(
                    "SELECT value FROM settings WHERE key=?",
                    ("approval_" + s["service"],),
                ).fetchone()[0]
                == "1"
            )
            status = "pending" if approval else "confirmed"
            b_id = c.execute(
                "INSERT INTO bookings(slot_id,user_id,name,phone,note,photo,status,created,client_comment) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    slot_id,
                    user_id,
                    name,
                    phone,
                    note,
                    photo,
                    status,
                    self.now(),
                    client_comment,
                ),
            ).lastrowid
            title = f"{SERVICES[s['service']]} · {datetime.fromisoformat(s['starts']).strftime('%d.%m.%Y %H:%M')}"
            client = (
                "Заявка принята. Место закреплено до решения администратора."
                if approval
                else "Вы записаны!"
            )
            self.enqueue(
                c,
                user_id,
                f"{client}\n{title}\n{address}\nЗапись №{b_id}. Управление: /start → Мои записи.",
            )
            buttons = (
                [("Подтвердить", f"approve:{b_id}"), ("Отклонить", f"reject:{b_id}")]
                if approval
                else [("Отменить запись", f"reject:{b_id}")]
            )
            for admin in admins:
                self.enqueue(
                    c,
                    admin,
                    f"Запись №{b_id}: {title}\n{name}\n{phone}\n{note}\nКомментарий клиента: {client_comment or '—'}\nСтатус: {'ожидает решения' if approval else 'подтверждена'}",
                    photo,
                    buttons,
                )
            self.dirty(c)
            return b_id

    def bookings(self, user_id=None, history=False):
        q = "SELECT b.*,s.service,s.starts FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE 1=1"
        args = []
        if not history:
            q += " AND b.status IN ('pending','confirmed') AND s.starts>?"
            args.append(self.now())
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        return self.rows(
            q
            + (
                " ORDER BY s.starts DESC,b.id DESC"
                if history
                else " ORDER BY s.starts,b.id"
            ),
            args,
        )

    def booking_list(self, scope="upcoming", search="", sort="date_asc", status=""):
        """Archive is a view: passed dates and final statuses never erase history."""
        rows = self.bookings(history=True)
        now = self.now()
        today = now[:10]
        if scope == "today":
            rows = [r for r in rows if r["starts"][:10] == today]
        elif scope == "upcoming":
            rows = [
                r
                for r in rows
                if r["starts"] > now and r["status"] in ACTIVE and not r["archived"]
            ]
        elif scope == "archive":
            rows = [
                r
                for r in rows
                if r["starts"] <= now or r["status"] not in ACTIVE or r["archived"]
            ]
        elif scope != "all":
            raise ValueError("Неизвестный раздел записей.")
        if status:
            rows = [r for r in rows if r["status"] == status]
        query = search.strip().casefold()
        if query:
            digits = re.sub(r"\D", "", query)
            numeric = query.isdecimal()
            phone_query = len(digits) >= 4 and bool(
                re.fullmatch(r"[+\d\s()\-]+", query)
            )

            def matches(row):
                if numeric and len(query) <= 6 and int(query) == row["id"]:
                    return True
                if phone_query and digits in re.sub(r"\D", "", row["phone"]):
                    return True
                if numeric:
                    return False
                text = " ".join(
                    str(row[k])
                    for k in (
                        "name",
                        "phone",
                        "note",
                        "client_comment",
                        "status_comment",
                        "starts",
                    )
                ).casefold()
                return query in text or query in datetime.fromisoformat(
                    row["starts"]
                ).strftime("%d.%m.%Y")

            rows = [r for r in rows if matches(r)]
        if sort == "name":
            rows.sort(key=lambda r: (r["name"].casefold(), r["starts"], r["id"]))
        elif sort == "status":
            ranks = {value: rank for rank, value in enumerate(STATUS_NAMES)}
            rows.sort(key=lambda r: (ranks.get(r["status"], 99), r["starts"], r["id"]))
        elif sort in ("date_asc", "date_desc"):
            rows.sort(key=lambda r: (r["starts"], r["id"]), reverse=sort == "date_desc")
        else:
            raise ValueError("Неизвестная сортировка.")
        return rows

    def archive_booking(self, booking_id, archived, actor, admins):
        if actor not in admins:
            raise ValueError("Нет доступа.")
        with self.connect() as c:
            if not c.execute(
                "UPDATE bookings SET archived=? WHERE id=?", (int(archived), booking_id)
            ).rowcount:
                raise ValueError("Запись не найдена.")

    def set_booking_status(self, booking_id, status, actor, admins, comment=None):
        if actor not in admins or status not in STATUS_NAMES:
            raise ValueError("Нет доступа или неверный статус.")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            b = c.execute(
                "SELECT b.*,s.starts,s.capacity,s.enabled,s.deleted FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?",
                (booking_id,),
            ).fetchone()
            if not b:
                raise ValueError("Запись не найдена.")
            if (
                status in ACTIVE
                and b["status"] not in ACTIVE
                and b["starts"] > self.now()
            ):
                used = c.execute(
                    "SELECT count(*) FROM bookings WHERE slot_id=? AND status IN ('pending','confirmed')",
                    (b["slot_id"],),
                ).fetchone()[0]
                duplicate = c.execute(
                    "SELECT 1 FROM bookings WHERE slot_id=? AND user_id=? AND id!=? AND status IN ('pending','confirmed')",
                    (b["slot_id"], b["user_id"], booking_id),
                ).fetchone()
                if (
                    b["deleted"]
                    or not b["enabled"]
                    or used >= b["capacity"]
                    or duplicate
                ):
                    raise ValueError(
                        "Нельзя восстановить запись: окно недоступно, занято или есть другая запись клиента."
                    )
            if comment is not None and len(comment) > 700:
                raise ValueError("Комментарий: не более 700 символов.")
            if b["status"] == status and (
                comment is None or comment == b["status_comment"]
            ):
                return
            message = comment if comment is not None else ""
            c.execute(
                "UPDATE bookings SET status=?,status_comment=? WHERE id=?",
                (status, message, booking_id),
            )
            self.enqueue(
                c,
                b["user_id"],
                f"Статус записи №{booking_id}: {STATUS_NAMES[status]}."
                + (f"\nКомментарий академии: {message}" if message else ""),
            )
            self.dirty(c)

    def change_booking(self, booking_id, action, actor, admins):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            b = c.execute(
                "SELECT b.*,s.service,s.starts FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?",
                (booking_id,),
            ).fetchone()
            if not b or (
                (action == "cancel" and b["user_id"] != actor)
                or (action != "cancel" and actor not in admins)
            ):
                raise ValueError("Запись не найдена или нет доступа.")
            if b["status"] not in ACTIVE or (
                action == "approve" and b["status"] != "pending"
            ):
                raise ValueError("Эта запись уже обработана.")
            status = {
                "approve": "confirmed",
                "reject": "rejected",
                "cancel": "cancelled",
            }[action]
            c.execute(
                "UPDATE bookings SET status=?,status_comment='' WHERE id=?",
                (status, booking_id),
            )
            title = f"{SERVICES[b['service']]} · {datetime.fromisoformat(b['starts']).strftime('%d.%m.%Y %H:%M')}"
            label = {
                "confirmed": "подтверждена",
                "rejected": "отменена администратором. Свяжитесь с академией, чтобы подобрать другой вариант",
                "cancelled": "отменена вами",
            }[status]
            self.enqueue(c, b["user_id"], f"Запись №{booking_id} {label}.\n{title}")
            if action == "cancel":
                for admin in admins:
                    self.enqueue(
                        c,
                        admin,
                        f"Клиент отменил запись №{booking_id}.\n{title}\n{b['name']}",
                    )
            self.dirty(c)

    def add_lead(self, user_id, name, phone, course, admins, client_comment=""):
        with self.connect() as c:
            lead_id = c.execute(
                "INSERT INTO leads(user_id,name,phone,course,client_comment) VALUES (?,?,?,?,?)",
                (user_id, name, phone, course, client_comment),
            ).lastrowid
            for admin in admins:
                self.enqueue(
                    c,
                    admin,
                    f"Заявка на обучение №{lead_id}\n{course}\n{name}\n{phone}\nКомментарий: {client_comment or '—'}",
                    buttons=[
                        ("Взять в работу", f"lead:working:{lead_id}"),
                        ("Завершить", f"lead:done:{lead_id}"),
                    ],
                )
            return lead_id
