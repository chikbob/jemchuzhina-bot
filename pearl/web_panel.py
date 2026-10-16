"""Private server-rendered browser for academy records, with safe exports."""

import csv
import hashlib
import hmac
import io
import secrets
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .access import ROLE_NAMES
from .booking_manager import SCOPES, SORTS
from .data_tools import backup_database
from .db import SERVICES, STATUS_NAMES
from .panel_auth import panel_password
from .ui import when

LEAD_STATUSES = {"new": "Новая", "working": "В работе", "done": "Завершена"}


def register_panel(app, db, config, secret):
    password = panel_password(config, secret)
    app.secret_key = hmac.new(
        secret.encode(), ("panel-session:" + password).encode(), hashlib.sha256
    ).digest()
    app.config.update(
        SESSION_COOKIE_NAME="pearl_owner",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=getattr(config, "web_cookie_secure", True),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    )
    panel = Blueprint("panel", __name__, url_prefix="/admin")

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def csrf_check():
        if not session.get("csrf") or not hmac.compare_digest(
            session["csrf"], request.form.get("csrf", "")
        ):
            abort(400, "Обновите страницу и попробуйте ещё раз.")

    def protected(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if not session.get("owner_panel"):
                return redirect(url_for("panel.login"))
            return handler(*args, **kwargs)

        return wrapped

    @panel.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return response

    def export_url(dataset):
        args = {
            key: value
            for key, value in request.args.items()
            if key in ("scope", "sort", "status", "q", "service")
        }
        return url_for("panel.export_csv", dataset=dataset, **args)

    @panel.context_processor
    def helpers():
        return {
            "csrf_token": csrf_token,
            "export_url": export_url,
            "services": SERVICES,
            "status_names": STATUS_NAMES,
            "role_names": ROLE_NAMES,
            "lead_statuses": LEAD_STATUSES,
            "when": when,
            "academy_channel": "https://t.me/Mariupol_pearlacademj",
        }

    @panel.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        status_code = 200
        if request.method == "POST":
            csrf_check()
            key = hmac.new(
                secret.encode(),
                (request.remote_addr or "unknown").encode(),
                hashlib.sha256,
            ).hexdigest()
            now = time.time()
            with db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM web_login_attempts WHERE client=?", (key,)
                ).fetchone()
                if row and row["locked_until"] > now:
                    error, status_code = (
                        "Слишком много попыток. Повторите через 15 минут.",
                        429,
                    )
                elif hmac.compare_digest(
                    request.form.get("password", "").encode(), password.encode()
                ):
                    connection.execute(
                        "DELETE FROM web_login_attempts WHERE client=?", (key,)
                    )
                    session.clear()
                    session.permanent = True
                    session["owner_panel"] = True
                    session["csrf"] = secrets.token_urlsafe(32)
                    return redirect(url_for("panel.dashboard"))
                else:
                    failures = (
                        row["failures"] if row and not row["locked_until"] else 0
                    ) + 1
                    connection.execute(
                        "INSERT OR REPLACE INTO web_login_attempts VALUES (?,?,?)",
                        (key, failures, now + 900 if failures >= 5 else 0),
                    )
                    error, status_code = "Неверный пароль.", 401
        return render_template("panel/login.html", error=error), status_code

    @panel.post("/logout")
    @protected
    def logout():
        csrf_check()
        session.clear()
        return redirect(url_for("panel.login"))

    @panel.get("/")
    @protected
    def dashboard():
        today = db.booking_list(scope="today")
        stats = [
            ("Сегодня", len(today), "panel.bookings", "today"),
            ("Предстоящие", len(db.booking_list()), "panel.bookings", "upcoming"),
            (
                "Новые заявки",
                db.rows("SELECT count(*) AS n FROM leads WHERE status='new'")[0]["n"],
                "panel.leads",
                "",
            ),
            (
                "Сотрудники",
                db.rows("SELECT count(*) AS n FROM staff")[0]["n"],
                "panel.staff",
                "",
            ),
        ]
        return render_template(
            "panel/dashboard.html", active="dashboard", stats=stats, bookings=today[:8]
        )

    def booking_rows():
        scope = request.args.get("scope", "upcoming")
        sort = request.args.get("sort", "date_asc")
        status = request.args.get("status", "")
        search = request.args.get("q", "").strip()[:100]
        if (
            scope not in SCOPES
            or sort not in SORTS
            or (status and status not in STATUS_NAMES)
        ):
            abort(400)
        return db.booking_list(scope=scope, sort=sort, status=status, search=search)

    def paginate(rows):
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            abort(400)
        pages = max(1, (len(rows) + 24) // 25)
        page = min(page, pages)

        def page_url(number):
            args = {
                key: value
                for key, value in request.args.items()
                if key in ("scope", "sort", "status", "q", "service")
            }
            args["page"] = number
            return url_for(request.endpoint, **args)

        return {
            "rows": rows[(page - 1) * 25 : page * 25],
            "page": page,
            "pages": pages,
            "total": len(rows),
            "page_url": page_url,
        }

    @panel.get("/bookings")
    @protected
    def bookings():
        return render_template(
            "panel/bookings.html",
            active="bookings",
            scopes=SCOPES,
            sorts=SORTS,
            **paginate(booking_rows()),
        )

    @panel.get("/bookings/<int:booking_id>")
    @protected
    def booking(booking_id):
        rows = db.rows(
            "SELECT b.*,s.starts,s.service FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?",
            (booking_id,),
        )
        if not rows:
            abort(404)
        return render_template("panel/booking.html", active="bookings", booking=rows[0])

    def slot_rows():
        rows = db.rows(
            "SELECT s.*,s.capacity-(SELECT count(*) FROM bookings b WHERE b.slot_id=s.id AND b.status IN ('pending','confirmed')) AS free FROM slots s ORDER BY starts DESC,id DESC"
        )
        scope = request.args.get("scope", "upcoming")
        service = request.args.get("service", "")
        if (
            scope not in ("upcoming", "archive", "all")
            or service
            and service not in SERVICES
        ):
            abort(400)
        if scope == "upcoming":
            rows = [r for r in rows if r["starts"] > db.now() and not r["deleted"]]
        if scope == "archive":
            rows = [r for r in rows if r["starts"] <= db.now() or r["deleted"]]
        if service:
            rows = [r for r in rows if r["service"] == service]
        return rows

    @panel.get("/slots")
    @protected
    def slots():
        return render_template(
            "panel/slots.html", active="slots", now=db.now(), **paginate(slot_rows())
        )

    def lead_rows():
        status = request.args.get("status", "")
        if status and status not in LEAD_STATUSES:
            abort(400)
        query = request.args.get("q", "").strip().casefold()[:100]
        rows = db.rows("SELECT * FROM leads ORDER BY id DESC")
        if status:
            rows = [r for r in rows if r["status"] == status]
        if query:
            rows = [
                r
                for r in rows
                if query
                in " ".join(
                    str(r[k])
                    for k in ("id", "name", "phone", "course", "client_comment")
                ).casefold()
            ]
        return rows

    @panel.get("/leads")
    @protected
    def leads():
        return render_template(
            "panel/leads.html", active="leads", **paginate(lead_rows())
        )

    @panel.get("/staff")
    @protected
    def staff():
        rows = db.rows(
            "SELECT s.*,COALESCE(u.name,CAST(s.user_id AS TEXT)) AS name,COALESCE(u.username,'') AS username FROM staff s LEFT JOIN users u USING(user_id) ORDER BY CASE s.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,name"
        )
        return render_template("panel/staff.html", active="staff", rows=rows)

    @panel.get("/exports/<dataset>.csv")
    @protected
    def export_csv(dataset):
        if dataset == "bookings":
            rows = booking_rows()
            columns = {
                "id": "Номер",
                "starts": "Дата и время",
                "service": "Услуга",
                "name": "Клиент",
                "phone": "Телефон",
                "note": "Анкета",
                "client_comment": "Комментарий клиента",
                "status": "Статус",
                "status_comment": "Комментарий академии",
                "archived": "Ручной архив",
            }
        elif dataset == "slots":
            rows = slot_rows()
            columns = {
                "id": "Номер",
                "service": "Услуга",
                "starts": "Дата и время",
                "capacity": "Мест",
                "free": "Свободно",
                "enabled": "Показано",
                "deleted": "Удалено",
            }
        elif dataset == "leads":
            rows = lead_rows()
            columns = {
                "id": "Номер",
                "name": "Имя",
                "phone": "Телефон",
                "course": "Обучение",
                "status": "Статус",
                "client_comment": "Комментарий клиента",
            }
        else:
            abort(404)
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(columns.values())
        for row in rows:
            values = []
            for key in columns:
                value = str(row.get(key, ""))
                if key == "service":
                    value = SERVICES.get(value, value)
                if key == "status":
                    value = STATUS_NAMES.get(value, LEAD_STATUSES.get(value, value))
                if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
                    ("\t", "\r", "\n")
                ):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="pearl-{dataset}.csv"'
            },
        )

    @panel.get("/backups")
    @protected
    def backups():
        directory = Path(db.path).resolve().parent / "backups"
        files = (
            sorted(
                directory.glob("*.sqlite3"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if directory.exists()
            else []
        )
        rows = [
            {"name": p.name, "size": round(p.stat().st_size / 1024, 1)} for p in files
        ]
        return render_template("panel/backups.html", active="backups", backups=rows)

    @panel.post("/backups/create")
    @protected
    def create_backup():
        csrf_check()
        backup = backup_database(db)
        return send_file(backup, as_attachment=True, download_name=backup.name)

    @panel.get("/backups/<name>")
    @protected
    def download_backup(name):
        directory = Path(db.path).resolve().parent / "backups"
        path = (directory / name).resolve()
        if path.parent != directory or path.suffix != ".sqlite3" or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    app.register_blueprint(panel)
