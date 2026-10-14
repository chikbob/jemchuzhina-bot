import hashlib
import hmac
import time

ROLE_NAMES = {"owner": "Владелец", "admin": "Администратор", "manager": "Менеджер"}


class Access:
    SESSION_SECONDS = 12 * 60 * 60
    LOCK_SECONDS = 15 * 60
    MAX_FAILURES = 5

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.password_tag = hashlib.sha256(config.admin_password.encode()).hexdigest()
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM settings WHERE key='staff_migrated'"
            ).fetchone():
                c.execute(
                    "INSERT OR IGNORE INTO staff SELECT user_id,'manager' FROM managers"
                )
                c.execute("INSERT INTO settings VALUES ('staff_migrated','1')")
            for uid in config.owners:
                seed_key = f"owner_seeded_{uid}"
                if not c.execute(
                    "SELECT 1 FROM settings WHERE key=?", (seed_key,)
                ).fetchone():
                    c.execute(
                        "INSERT INTO staff VALUES (?,'owner') ON CONFLICT(user_id) DO UPDATE SET role='owner'",
                        (uid,),
                    )
                    c.execute("INSERT INTO settings VALUES (?,'1')", (seed_key,))

    def remember(self, uid, name, username="", seen=False):
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,seen=MAX(users.seen,excluded.seen)",
                (uid, name, username.lower(), int(seen)),
            )

    def role(self, uid):
        rows = self.db.rows("SELECT role FROM staff WHERE user_id=?", (uid,))
        return rows[0]["role"] if rows else None

    def staff_ids(self):
        return {row["user_id"] for row in self.db.rows("SELECT user_id FROM staff")}

    def owner_ids(self):
        return {
            row["user_id"]
            for row in self.db.rows("SELECT user_id FROM staff WHERE role='owner'")
        }

    def authenticated(self, uid):
        role = self.role(uid)
        if role in ("admin", "manager"):
            return True
        if not role:
            return False
        rows = self.db.rows("SELECT * FROM admin_logins WHERE user_id=?", (uid,))
        return bool(
            rows
            and rows[0]["expires"] > time.time()
            and hmac.compare_digest(rows[0]["password_tag"], self.password_tag)
        )

    def login(self, uid, password):
        now = time.time()
        error = None
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            attempt = c.execute(
                "SELECT * FROM login_attempts WHERE user_id=?", (uid,)
            ).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise ValueError(
                    "Слишком много попыток. Повторите вход через 15 минут."
                )
            if not hmac.compare_digest(
                password.encode(), self.config.admin_password.encode()
            ):
                failures = (
                    attempt["failures"]
                    if attempt and not attempt["locked_until"]
                    else 0
                ) + 1
                locked = now + self.LOCK_SECONDS if failures >= self.MAX_FAILURES else 0
                c.execute(
                    "INSERT OR REPLACE INTO login_attempts VALUES (?,?,?)",
                    (uid, failures, locked),
                )
                error = (
                    "Слишком много попыток. Повторите вход через 15 минут."
                    if locked
                    else "Неверный пароль. Попробуйте ещё раз."
                )
            else:
                role = c.execute(
                    "SELECT role FROM staff WHERE user_id=?", (uid,)
                ).fetchone()
                if not role:
                    if c.execute("SELECT 1 FROM staff WHERE role='owner'").fetchone():
                        error = "Сначала владелец должен добавить вас в разделе «Сотрудники»."
                    else:
                        c.execute("INSERT INTO staff VALUES (?,'owner')", (uid,))
                if not error:
                    c.execute("DELETE FROM login_attempts WHERE user_id=?", (uid,))
                    c.execute(
                        "INSERT OR REPLACE INTO admin_logins VALUES (?,?,?)",
                        (uid, now + self.SESSION_SECONDS, self.password_tag),
                    )
        if error:
            raise ValueError(error)
        return self.role(uid)

    def logout(self, uid):
        with self.db.connect() as c:
            c.execute("DELETE FROM admin_logins WHERE user_id=?", (uid,))

    def can_manage(self, actor, target=None, new_role=None):
        if not self.authenticated(actor):
            return False
        role = self.role(actor)
        if role == "owner":
            return True
        return (
            role == "admin"
            and (target is None or self.role(target) in (None, "manager"))
            and new_role in (None, "manager")
        )

    def set_role(self, actor, target, role):
        if role not in (*ROLE_NAMES, None) or not self.can_manage(actor, target, role):
            raise ValueError("Недостаточно прав для изменения этой роли.")
        if role == "owner":
            raise ValueError(
                "Используйте кнопку «Передать владение»: вы останетесь администратором."
            )
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Recheck inside the transaction so another owner cannot revoke the actor mid-change.
            actor_row = c.execute(
                "SELECT role FROM staff WHERE user_id=?", (actor,)
            ).fetchone()
            target_row = c.execute(
                "SELECT role FROM staff WHERE user_id=?", (target,)
            ).fetchone()
            if not actor_row or (
                actor_row["role"] != "owner"
                and not (
                    actor_row["role"] == "admin"
                    and (not target_row or target_row["role"] == "manager")
                    and role in (None, "manager")
                )
            ):
                raise ValueError("Недостаточно прав для изменения этой роли.")
            if (
                target_row
                and target_row["role"] == "owner"
                and role != "owner"
                and c.execute(
                    "SELECT count(*) FROM staff WHERE role='owner'"
                ).fetchone()[0]
                <= 1
            ):
                raise ValueError("Нельзя удалить или понизить последнего владельца.")
            if role:
                c.execute(
                    "INSERT INTO staff VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
                    (target, role),
                )
            else:
                c.execute("DELETE FROM staff WHERE user_id=?", (target,))
            c.execute("DELETE FROM admin_logins WHERE user_id=?", (target,))
            if role is None:
                c.execute(
                    "UPDATE outbox SET delivered=-1,error='Staff access revoked' WHERE chat_id=? AND delivered=0",
                    (target,),
                )

    def transfer_ownership(self, actor, target):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            login = c.execute(
                "SELECT * FROM admin_logins WHERE user_id=?", (actor,)
            ).fetchone()
            source = c.execute(
                "SELECT role FROM staff WHERE user_id=?", (actor,)
            ).fetchone()
            recipient = c.execute(
                "SELECT role FROM staff WHERE user_id=?", (target,)
            ).fetchone()
            if not (
                source
                and source["role"] == "owner"
                and login
                and login["expires"] > time.time()
                and hmac.compare_digest(login["password_tag"], self.password_tag)
            ):
                raise ValueError("Передать владение может только вошедший владелец.")
            if target == actor:
                raise ValueError("Выберите другого сотрудника.")
            if not recipient:
                raise ValueError("Сначала добавьте человека в раздел «Сотрудники».")
            if recipient["role"] == "owner":
                raise ValueError("Этот сотрудник уже владелец.")
            c.execute("UPDATE staff SET role='owner' WHERE user_id=?", (target,))
            c.execute("UPDATE staff SET role='admin' WHERE user_id=?", (actor,))
            # Keep the sender's session: its permissions now follow the administrator role.
            c.execute("DELETE FROM admin_logins WHERE user_id=?", (target,))
            self.db.enqueue(
                c,
                target,
                "Вам передано владение ботом академии «Жемчужина». Ваша роль: Владелец. Войдите заново через /admin с паролем.",
            )
            self.db.enqueue(
                c,
                actor,
                "Владение передано. Ваша роль теперь: Администратор. Вы можете продолжить работу в панели.",
            )

    def staff(self):
        return self.db.rows(
            "SELECT s.*,COALESCE(u.name,CAST(s.user_id AS TEXT)) AS name,COALESCE(u.username,'') AS username FROM staff s LEFT JOIN users u USING(user_id) ORDER BY CASE s.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,name"
        )
