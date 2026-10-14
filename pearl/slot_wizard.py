"""One window, one question at a time."""

import calendar
import re
from datetime import date, datetime, timedelta

from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .calendar_ui import MONTHS
from .db import SERVICES
from .ui import keyboard, when


class SlotWizard(StatesGroup):
    service = State()
    time = State()
    date = State()
    capacity = State()
    confirm = State()


def parse_date(value, today):
    parts = re.split(r"[./\-]", value.strip())
    if not 1 <= len(parts) <= 3 or any(not p.isdigit() for p in parts):
        raise ValueError("Выберите дату кнопкой или напишите число, например 15.")
    day = int(parts[0])
    month = int(parts[1]) if len(parts) > 1 else today.month
    year = int(parts[2]) if len(parts) > 2 else today.year
    try:
        result = date(year, month, day)
    except ValueError:
        raise ValueError("Такой даты нет. Выберите другую.") from None
    if result < today or result > today + timedelta(days=730):
        raise ValueError("Выберите дату от сегодня до двух лет вперёд.")
    return result


def date_markup(month, today):
    first = date.fromisoformat(month + "-01")
    rows = [
        [
            InlineKeyboardButton(text=w, callback_data="calnoop")
            for w in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        ]
    ]
    for week in calendar.monthcalendar(first.year, first.month):
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(day)
                    if day and first.replace(day=day) >= today
                    else f"·{day}"
                    if day
                    else " ",
                    callback_data=f"swdate:{first.replace(day=day).isoformat()}"
                    if day and first.replace(day=day) >= today
                    else "calnoop",
                )
                for day in week
            ]
        )
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    nxt = (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    rows.append(
        [
            InlineKeyboardButton(text="← Месяц", callback_data=f"swmonth:{prev}"),
            InlineKeyboardButton(text="Месяц →", callback_data=f"swmonth:{nxt}"),
        ]
    )
    rows += keyboard(
        [("Сегодня", f"swdate:{today.isoformat()}"), ("Отмена", "admin")]
    ).inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows)


def register_slot_wizard(router, db, config, access):
    async def allowed(event):
        uid = event.from_user.id
        if access.authenticated(uid):
            return True
        if hasattr(event, "data"):
            await event.answer("Нет доступа. Войдите через /admin.", show_alert=True)
        else:
            await event.answer("Нет доступа. Войдите через /admin.")
        return False

    async def ask_date(message, state, month=None, hint=""):
        today = datetime.now(config.timezone).date()
        month = month or today.strftime("%Y-%m")
        await state.set_state(SlotWizard.date)
        first = date.fromisoformat(month + "-01")
        await message.answer(
            hint
            + f"На какое число? {MONTHS[first.month]} {first.year}.\nВыберите дату в календаре или напишите число. Для другого месяца можно написать 15.11.",
            reply_markup=date_markup(month, today),
        )

    async def ask_capacity(message, state, day):
        data = await state.get_data()
        starts = datetime.fromisoformat(
            day.isoformat() + "T" + data["sw_time"]
        ).replace(tzinfo=config.timezone)
        if starts <= datetime.now(config.timezone):
            await ask_date(
                message, state, hint="Это время уже прошло. Выберите другую дату.\n"
            )
            return
        await state.update_data(sw_starts=starts.isoformat())
        await state.set_state(SlotWizard.capacity)
        await message.answer(
            f"{SERVICES[data['sw_service']]} · {when(starts.isoformat())}\nСколько записей можно принять на это время? Напишите число от 1 до 100.",
            reply_markup=keyboard(
                [(str(n), f"swcap:{n}") for n in (1, 2, 3, 4)] + [("Отмена", "admin")]
            ),
        )

    async def review(message, state, value):
        if not value.strip().isdigit() or not 1 <= int(value) <= 100:
            await message.answer(
                "Количество записей: целое число от 1 до 100. Попробуйте ещё раз."
            )
            return
        data = await state.get_data()
        await state.update_data(sw_capacity=int(value))
        await state.set_state(SlotWizard.confirm)
        await message.answer(
            f"{SERVICES[data['sw_service']]}\n{when(data['sw_starts'])}\nКоличество записей: {int(value)}.\nСохранить окно? Если такое время уже есть, изменится его число мест; действующие записи сохранятся.",
            reply_markup=keyboard(
                [
                    ("Сохранить", "swsave"),
                    ("Заполнить заново", "addslot"),
                    ("Отмена", "admin"),
                ]
            ),
        )

    @router.callback_query(F.data == "addslot")
    async def begin(q, state):
        if not await allowed(q):
            return
        await q.answer()
        await state.clear()
        await state.set_state(SlotWizard.service)
        await q.message.answer(
            "Для какой услуги создать окно?",
            reply_markup=keyboard(
                [(title, f"swservice:{key}") for key, title in SERVICES.items()]
                + [("Отмена", "admin")]
            ),
        )

    @router.callback_query(SlotWizard.service, F.data.startswith("swservice:"))
    async def service(q, state):
        if not await allowed(q):
            return
        value = q.data.split(":")[1]
        if value not in SERVICES:
            return
        await q.answer()
        await state.update_data(sw_service=value)
        await state.set_state(SlotWizard.time)
        await q.message.answer(
            "Во сколько? Напишите время, например 10:00, или выберите кнопку.",
            reply_markup=keyboard(
                [
                    (time, f"swtime:{time.replace(':', '')}")
                    for time in ("10:00", "12:00", "14:00", "16:00")
                ]
                + [("Отмена", "admin")]
            ),
        )

    async def accept_time(message, state, value):
        match = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*", value)
        if not match or int(match[1]) > 23:
            await message.answer("Введите время от 00:00 до 23:59, например 10:30.")
            return
        await state.update_data(sw_time=f"{int(match[1]):02}:{match[2]}")
        await ask_date(message, state)

    @router.message(SlotWizard.time, F.text)
    async def time_text(m, state):
        if await allowed(m):
            await accept_time(m, state, m.text)

    @router.callback_query(SlotWizard.time, F.data.startswith("swtime:"))
    async def time_button(q, state):
        if not await allowed(q):
            return
        await q.answer()
        raw = q.data.split(":")[1]
        await accept_time(q.message, state, raw[:2] + ":" + raw[2:])

    @router.callback_query(SlotWizard.date, F.data.startswith("swmonth:"))
    async def month(q, state):
        if not await allowed(q):
            return
        try:
            value = q.data.split(":")[1]
            first = date.fromisoformat(value + "-01")
            today = datetime.now(config.timezone).date()
            if first < today.replace(day=1) or first > today + timedelta(days=730):
                raise ValueError
        except ValueError:
            await q.answer(
                "Выберите текущий месяц или ближайшие два года.", show_alert=True
            )
            return
        await q.answer()
        await ask_date(q.message, state, value)

    @router.message(SlotWizard.date, F.text)
    async def date_text(m, state):
        if not await allowed(m):
            return
        try:
            day = parse_date(m.text, datetime.now(config.timezone).date())
        except ValueError as error:
            await m.answer(str(error))
            return
        await ask_capacity(m, state, day)

    @router.callback_query(SlotWizard.date, F.data.startswith("swdate:"))
    async def date_button(q, state):
        if not await allowed(q):
            return
        try:
            day = parse_date(
                date.fromisoformat(q.data.split(":")[1]).strftime("%d.%m.%Y"),
                datetime.now(config.timezone).date(),
            )
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer()
        await ask_capacity(q.message, state, day)

    @router.message(SlotWizard.capacity, F.text)
    async def capacity_text(m, state):
        if await allowed(m):
            await review(m, state, m.text)

    @router.callback_query(SlotWizard.capacity, F.data.startswith("swcap:"))
    async def capacity_button(q, state):
        if not await allowed(q):
            return
        await q.answer()
        await review(q.message, state, q.data.split(":")[1])

    @router.callback_query(F.data.startswith("slotcapacity:"))
    async def change_capacity(q, state):
        if not await allowed(q):
            return
        rows = db.rows(
            "SELECT * FROM slots WHERE id=? AND deleted=0 AND starts>?",
            (int(q.data.split(":")[1]), db.now()),
        )
        if not rows:
            await q.answer("Окно недоступно.", show_alert=True)
            return
        await q.answer()
        slot = rows[0]
        await state.clear()
        await state.update_data(
            sw_service=slot["service"],
            sw_time=slot["starts"][11:16],
            sw_starts=slot["starts"],
        )
        await state.set_state(SlotWizard.capacity)
        await q.message.answer(
            f"{SERVICES[slot['service']]} · {when(slot['starts'])}\nСейчас мест: {slot['capacity']}. Сколько записей можно принять? Напишите число от 1 до 100.",
            reply_markup=keyboard(
                [(str(n), f"swcap:{n}") for n in (1, 2, 3, 4)] + [("Отмена", "admin")]
            ),
        )

    @router.callback_query(SlotWizard.confirm, F.data == "swsave")
    async def save(q, state):
        if not await allowed(q):
            return
        data = await state.get_data()
        try:
            db.add_slot(data["sw_service"], data["sw_starts"], data["sw_capacity"])
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Окно сохранено")
        await state.clear()
        await q.message.answer(
            "Окно сохранено.",
            reply_markup=keyboard(
                [
                    ("Создать ещё", "addslot"),
                    ("К окнам", "adminslots:0"),
                    ("Управление", "admin"),
                ]
            ),
        )
