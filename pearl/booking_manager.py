"""Staff booking inbox, history and client-visible status comments."""

from aiogram import F
from aiogram.fsm.state import State, StatesGroup

from .db import SERVICES, STATUS_NAMES
from .ui import keyboard, when

SCOPES = {
    "today": "Сегодня",
    "upcoming": "Предстоящие",
    "archive": "Архив",
    "all": "Все записи",
}
SORTS = {
    "date_asc": "Сначала ближайшие",
    "date_desc": "Сначала поздние",
    "name": "По имени",
    "status": "По статусу",
}


class BookingFlow(StatesGroup):
    search = State()
    status_comment = State()


def register_booking_manager(router, db, access):
    async def allowed(event):
        if access.authenticated(event.from_user.id):
            return True
        if hasattr(event, "data"):
            await event.answer("Нет доступа. Войдите через /admin.", show_alert=True)
        else:
            await event.answer("Нет доступа. Войдите через /admin.")
        return False

    async def render(message, state, offset=0):
        data = await state.get_data()
        view = {
            "scope": "upcoming",
            "search": "",
            "sort": "date_asc",
            "status": "",
            **data.get("booking_view", {}),
        }
        await state.set_state(None)
        await state.update_data(booking_view=view)
        rows = db.booking_list(**view)
        offset = max(0, min(offset, max(0, ((len(rows) - 1) // 8) * 8)))
        items = [
            (
                f"№{b['id']} · {when(b['starts'])} · {b['name'][:28]} · {STATUS_NAMES[b['status']]}",
                f"bookingcard:{b['id']}",
            )
            for b in rows[offset : offset + 8]
        ]
        if offset:
            items.append(("← Назад", f"bookings:{max(0, offset - 8)}"))
        if offset + 8 < len(rows):
            items.append(("Далее →", f"bookings:{offset + 8}"))
        items += [
            ("📅 Сегодня", "bookview:today"),
            ("Предстоящие", "bookview:upcoming"),
            ("📦 Архив", "bookview:archive"),
            ("Все записи", "bookview:all"),
            ("🔎 Поиск", "booksearch"),
            ("Сортировка", "booksort"),
            ("Фильтр по статусу", "bookfilter"),
        ]
        if view["search"] or view["status"]:
            items.append(("Сбросить поиск и фильтр", "bookreset"))
        items.append(("Управление", "admin"))
        text = f"{SCOPES[view['scope']]} · найдено: {len(rows)}\nСортировка: {SORTS[view['sort']]}"
        if view["search"]:
            text += f"\nПоиск: {view['search']}"
        if view["status"]:
            text += f"\nСтатус: {STATUS_NAMES[view['status']]}"
        text += (
            "\nВыберите запись для просмотра."
            if rows
            else "\nЗаписей по этим условиям нет."
        )
        await message.answer(text, reply_markup=keyboard(items))

    async def card(message, booking_id):
        rows = db.rows(
            "SELECT b.*,s.service,s.starts FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?",
            (booking_id,),
        )
        if not rows:
            await message.answer(
                "Запись не найдена.",
                reply_markup=keyboard([("К записям", "bookings:0")]),
            )
            return
        b = rows[0]
        if b["photo"]:
            await message.answer_photo(
                b["photo"], caption=f"Фото к записи №{booking_id}"
            )
        text = f"№{booking_id} · {SERVICES[b['service']]} · {when(b['starts'])}\n{b['name']}\n{b['phone']}\n{b['note']}\nКомментарий клиента: {b['client_comment'] or '—'}\nСтатус: {STATUS_NAMES[b['status']]}\nКомментарий академии для клиента: {b['status_comment'] or '—'}"
        items = [
            ("Изменить статус", f"bookingstatus:{booking_id}"),
            ("Комментарий для клиента", f"bookingcomment:{booking_id}"),
        ]
        if b["status"] in ("pending", "confirmed"):
            items.append(("Отменить", f"rejectask:{booking_id}"))
        if b["status"] == "pending":
            items.insert(0, ("Подтвердить", f"approve:{booking_id}"))
        items += [
            (
                "Вернуть из ручного архива" if b["archived"] else "Убрать в архив",
                f"bookarchive:{booking_id}:{0 if b['archived'] else 1}",
            ),
            ("К записям", "bookings:0"),
        ]
        await message.answer(text, reply_markup=keyboard(items))

    @router.callback_query(
        F.data.startswith("bookings:")
        | F.data.startswith("bookview:")
        | (F.data == "bookreset")
    )
    async def list_bookings(q, state):
        if not await allowed(q):
            return
        await q.answer()
        data = await state.get_data()
        view = data.get("booking_view", {})
        offset = 0
        if q.data.startswith("bookview:"):
            scope = q.data.split(":")[1]
            if scope not in SCOPES:
                return
            view["scope"] = scope
        elif q.data == "bookreset":
            view.update(search="", status="")
        else:
            offset = max(0, int(q.data.split(":")[1]))
        await state.update_data(booking_view=view)
        await render(q.message, state, offset)

    @router.callback_query(F.data.startswith("bookingcard:"))
    async def show_card(q, state):
        if not await allowed(q):
            return
        await state.set_state(None)
        await q.answer()
        await card(q.message, int(q.data.split(":")[1]))

    @router.callback_query(F.data == "booksearch")
    async def search_prompt(q, state):
        if not await allowed(q):
            return
        await q.answer()
        await state.set_state(BookingFlow.search)
        await q.message.answer(
            "Введите имя, телефон, номер записи или дату. Поиск выполняется в текущем разделе.",
            reply_markup=keyboard([("К записям", "bookings:0")]),
        )

    @router.message(BookingFlow.search, F.text)
    async def search_input(m, state):
        if not await allowed(m):
            return
        query = m.text.strip()
        if not 1 <= len(query) <= 100:
            await m.answer("Введите от 1 до 100 символов.")
            return
        data = await state.get_data()
        view = data.get("booking_view", {})
        view["search"] = query.lstrip("#№").strip()
        await state.update_data(booking_view=view)
        await render(m, state)

    @router.callback_query(F.data == "booksort")
    async def sort_prompt(q):
        if not await allowed(q):
            return
        await q.answer()
        await q.message.answer(
            "Как отсортировать записи?",
            reply_markup=keyboard(
                [(name, f"booksortset:{key}") for key, name in SORTS.items()]
                + [("К записям", "bookings:0")]
            ),
        )

    @router.callback_query(
        F.data.startswith("booksortset:") | F.data.startswith("bookfilterset:")
    )
    async def set_view(q, state):
        if not await allowed(q):
            return
        key, value = q.data.split(":", 1)
        if key == "booksortset" and value not in SORTS:
            return
        if key == "bookfilterset" and value and value not in STATUS_NAMES:
            return
        await q.answer()
        data = await state.get_data()
        view = data.get("booking_view", {})
        view["sort" if key == "booksortset" else "status"] = value
        await state.update_data(booking_view=view)
        await render(q.message, state)

    @router.callback_query(F.data == "bookfilter")
    async def filter_prompt(q):
        if not await allowed(q):
            return
        await q.answer()
        await q.message.answer(
            "Какие статусы показывать?",
            reply_markup=keyboard(
                [("Любой статус", "bookfilterset:")]
                + [(name, f"bookfilterset:{key}") for key, name in STATUS_NAMES.items()]
                + [("К записям", "bookings:0")]
            ),
        )

    @router.callback_query(F.data.startswith("bookarchive:"))
    async def archive(q):
        if not await allowed(q):
            return
        _, booking_id, flag = q.data.split(":")
        try:
            db.archive_booking(
                int(booking_id), flag == "1", q.from_user.id, access.staff_ids()
            )
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Раздел изменён. Статус и место сохранены.")
        await card(q.message, int(booking_id))

    @router.callback_query(F.data.startswith("bookingstatus:"))
    async def choose_status(q):
        if not await allowed(q):
            return
        booking_id = int(q.data.split(":")[1])
        rows = db.rows("SELECT * FROM bookings WHERE id=?", (booking_id,))
        if not rows:
            await q.answer("Запись не найдена.", show_alert=True)
            return
        await q.answer()
        await q.message.answer(
            f"Запись №{booking_id}. Текущий статус: {STATUS_NAMES[rows[0]['status']]}.\nВыберите новый статус, затем добавьте комментарий для клиента.",
            reply_markup=keyboard(
                [
                    (name, f"bookingstatusset:{booking_id}:{key}")
                    for key, name in STATUS_NAMES.items()
                    if key != rows[0]["status"]
                ]
                + [("Назад", f"bookingcard:{booking_id}")]
            ),
        )

    @router.callback_query(
        F.data.startswith("bookingstatusset:")
        | F.data.startswith("bookingcomment:")
        | F.data.startswith("approve:")
        | F.data.startswith("reject:")
    )
    async def comment_prompt(q, state):
        if not await allowed(q):
            return
        parts = q.data.split(":")
        booking_id = int(parts[1])
        rows = db.rows("SELECT status FROM bookings WHERE id=?", (booking_id,))
        status = parts[2] if len(parts) == 3 else rows[0]["status"] if rows else ""
        if parts[0] in ("approve", "reject"):
            status = "confirmed" if parts[0] == "approve" else "rejected"
        if not rows or status not in STATUS_NAMES:
            await q.answer("Запись или статус не найдены.", show_alert=True)
            return
        await q.answer()
        await state.update_data(status_booking=booking_id, status_target=status)
        await state.set_state(BookingFlow.status_comment)
        await q.message.answer(
            f"Статус: {STATUS_NAMES[status]}.\nНапишите комментарий для клиента (до 700 символов). Он появится в «Мои записи» и придёт уведомлением. Можно сохранить без комментария.",
            reply_markup=keyboard(
                [
                    ("Без комментария", "statuscomment:skip"),
                    ("Отмена", f"bookingcard:{booking_id}"),
                ]
            ),
        )

    async def apply_comment(message, state, actor, text):
        data = await state.get_data()
        if len(text) > 700:
            await message.answer("Комментарий слишком длинный. Не более 700 символов.")
            return
        booking_id = data["status_booking"]
        try:
            db.set_booking_status(
                booking_id,
                data["status_target"],
                actor,
                access.staff_ids(),
                comment=text,
            )
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=keyboard([("Назад", f"bookingcard:{booking_id}")]),
            )
            return
        await state.set_state(None)
        await state.set_data({"booking_view": data.get("booking_view", {})})
        await card(message, booking_id)

    @router.message(BookingFlow.status_comment, F.text)
    async def comment_text(m, state):
        if await allowed(m):
            await apply_comment(m, state, m.from_user.id, m.text.strip())

    @router.callback_query(BookingFlow.status_comment, F.data == "statuscomment:skip")
    async def skip_comment(q, state):
        if not await allowed(q):
            return
        await q.answer()
        await apply_comment(q.message, state, q.from_user.id, "")
