"""Admin calendar and bulk schedule creation."""

import calendar
import re
from datetime import date, datetime, timedelta

from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import SERVICES
from .ui import keyboard

MONTHS = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


class CalendarFlow(StatesGroup):
    days = State()
    range = State()
    times = State()
    time_menu = State()
    time_edit = State()
    confirm = State()


def parse_range(value, today):
    match = re.fullmatch(
        r"\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{4})(?:\s*[;,]\s*(2/2|каждый день))?\s*",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(
            "Введите диапазон: 11.10.2026 - 28.10.2026; 2/2 или 11.10.2026 - 28.10.2026; каждый день."
        )
    try:
        start, end = (
            date(*(int(part) for part in x.split(".")[::-1]))
            for x in match.groups()[:2]
        )
    except ValueError:
        raise ValueError("Проверьте даты и введите диапазон ещё раз.") from None
    if start < today or end < start or (end - start).days > 89:
        raise ValueError(
            "Выберите даты от сегодня, не более 90 дней; начало должно быть раньше конца."
        )
    return [
        (start + timedelta(days=i)).isoformat()
        for i in range((end - start).days + 1)
        if match[3] != "2/2" or i % 4 < 2
    ]


def parse_times(value):
    parts = re.split(r"[,;\n]+", value.strip())
    result = {}
    for part in parts:
        match = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s+(\d+)\s*", part)
        if not match:
            raise ValueError(
                "Укажите время и число мест: 10:00 2, 12:00 2. Для каждого времени задайте число мест один раз."
            )
        try:
            hour, minute = map(int, match[1].split(":"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("Invalid time")
            time = f"{hour:02}:{minute:02}"
        except ValueError:
            raise ValueError("Время должно быть от 00:00 до 23:59.") from None
        capacity = int(match[2])
        if not 1 <= capacity <= 100 or time in result:
            raise ValueError("Число мест: от 1 до 100. Время не должно повторяться.")
        result[time] = capacity
    if not 1 <= len(result) <= 8:
        raise ValueError("Можно задать от 1 до 8 времён за один раз.")
    return sorted(result.items())


def calendar_markup(month, selected, marks, today):
    first = date.fromisoformat(month + "-01")
    selected = set(selected)
    rows = [
        [
            InlineKeyboardButton(text=x, callback_data="calnoop")
            for x in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        ]
    ]
    for week in calendar.monthcalendar(first.year, first.month):
        row = []
        for day in week:
            if not day:
                row.append(InlineKeyboardButton(text=" ", callback_data="calnoop"))
                continue
            d = first.replace(day=day)
            iso = d.isoformat()
            label = "✅" if iso in selected else marks.get(iso, "")
            row.append(
                InlineKeyboardButton(
                    text=f"{label}{day}" if d >= today else f"·{day}",
                    callback_data=f"calday:{iso}:{0 if iso in selected else 1}"
                    if d >= today
                    else "calnoop",
                )
            )
        rows.append(row)
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    nxt = (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    rows += [
        [
            InlineKeyboardButton(text="← Месяц", callback_data=f"calmonth:{prev}"),
            InlineKeyboardButton(text="Месяц →", callback_data=f"calmonth:{nxt}"),
        ]
    ]
    for text, data in [
        ("Очистить выбор", "calclear"),
        (f"Задать время · дней: {len(selected)}", "caltimes"),
        ("Управление", "admin"),
    ]:
        rows.append([InlineKeyboardButton(text=text, callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def register_calendar(router, db, config, access):
    async def guard(q):
        if not access.authenticated(q.from_user.id):
            await q.answer("Войдите в управление: /admin.", show_alert=True)
            return False
        return True

    async def show(message, state, edit=False):
        data = await state.get_data()
        today = datetime.now(config.timezone).date()
        selected = data.get("cal_days", [])
        month = data.get("cal_month", today.strftime("%Y-%m"))
        first = date.fromisoformat(month + "-01")
        marks = {}
        for slot in db.slots(data["cal_service"]):
            day = slot["starts"][:10]
            if slot["capacity"] > slot["free"]:
                marks[day] = "👥"
            elif day not in marks:
                marks[day] = "🟢" if slot["enabled"] else "🙈"
        text = (
            f"{SERVICES[data['cal_service']]} · {MONTHS[first.month]} {first.year}\n"
            "Нажимайте на даты: можно выбрать несколько дней, в том числе в разных месяцах. Повторное нажатие снимает выбор.\n"
            "✅ выбрано · 🟢 есть окна · 👥 есть записи клиентов · 🙈 скрытые окна · · прошедшая дата\n"
            f"Выбрано дней: {len(selected)}. Существующие окна при массовом создании пропускаются."
        )
        markup = calendar_markup(month, selected, marks, today)
        if edit:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "calendar")
    async def begin(q, state):
        if not await guard(q):
            return
        await q.answer()
        await state.clear()
        await q.message.answer(
            "Для какой услуги создать окна?",
            reply_markup=keyboard(
                [
                    (title, f"calservice:{service}")
                    for service, title in SERVICES.items()
                ]
                + [("Управление", "admin")]
            ),
        )

    @router.callback_query(F.data.startswith("calservice:"))
    async def service(q, state):
        if not await guard(q):
            return
        value = q.data.split(":")[1]
        if value not in SERVICES:
            return
        await q.answer()
        await state.clear()
        await state.update_data(
            cal_service=value,
            cal_days=[],
            cal_month=datetime.now(config.timezone).strftime("%Y-%m"),
        )
        await state.set_state(CalendarFlow.days)
        await show(q.message, state)

    @router.callback_query(CalendarFlow.days, F.data.startswith("calday:"))
    async def day(q, state):
        if not await guard(q):
            return
        parts = q.data.split(":")
        value = parts[1]
        chosen = date.fromisoformat(value)
        today = datetime.now(config.timezone).date()
        data = await state.get_data()
        days = set(data["cal_days"])
        if chosen < today or chosen > today + timedelta(days=730):
            await q.answer(
                "Выберите дату в пределах ближайших двух лет.", show_alert=True
            )
            return
        add = parts[2] == "1" if len(parts) == 3 else value not in days
        if not add:
            days.discard(value)
        elif value in days or len(days) < 90:
            days.add(value)
        else:
            await q.answer("За один раз можно выбрать до 90 дней.", show_alert=True)
            return
        await state.update_data(cal_days=sorted(days))
        await q.answer()
        await show(q.message, state, edit=True)

    @router.callback_query(CalendarFlow.days, F.data.startswith("calmonth:"))
    async def month(q, state):
        if not await guard(q):
            return
        value = q.data.split(":")[1]
        first = date.fromisoformat(value + "-01")
        today = datetime.now(config.timezone).date()
        if first < today.replace(day=1) or first > today + timedelta(days=730):
            await q.answer(
                "Доступны текущий месяц и ближайшие два года.", show_alert=True
            )
            return
        await state.update_data(cal_month=value)
        await q.answer()
        await show(q.message, state, edit=True)

    @router.callback_query(CalendarFlow.days, F.data == "calclear")
    async def clear(q, state):
        if not await guard(q):
            return
        await state.update_data(cal_days=[])
        await q.answer("Выбор очищен")
        await show(q.message, state, edit=True)

    @router.callback_query(F.data == "calrange")
    async def range_hidden(q):
        await q.answer("Сейчас выбирайте даты кнопками календаря.", show_alert=True)

    @router.message(CalendarFlow.range, F.text)
    async def range_input(m, state):
        if not access.authenticated(m.from_user.id):
            await m.answer("Войдите в управление: /admin.")
            return
        try:
            days = parse_range(m.text, datetime.now(config.timezone).date())
        except ValueError as error:
            await m.answer(
                str(error), reply_markup=keyboard([("Назад к календарю", "calback")])
            )
            return
        await state.update_data(cal_days=days, cal_month=days[0][:7])
        await state.set_state(CalendarFlow.days)
        await show(m, state)

    @router.callback_query(F.data == "calback")
    async def back(q, state):
        if not await guard(q):
            return
        if not (await state.get_data()).get("cal_service"):
            await q.answer(
                "Откройте календарь заново через управление.", show_alert=True
            )
            return
        await q.answer()
        await state.set_state(CalendarFlow.days)
        await show(q.message, state)

    async def show_time_menu(message, state, offset=0):
        data = await state.get_data()
        days = data["cal_days"]
        schedules = data.get("cal_day_times", {})
        items = [("Задать одинаковое время всем дням", "calall")]
        for day in days[offset : offset + 8]:
            times = schedules.get(day, [])
            label = (
                ", ".join(f"{time}×{cap}" for time, cap in times) or "время не задано"
            )
            items.append(
                (
                    f"{date.fromisoformat(day).strftime('%d.%m')} · {label}",
                    f"caltime:{day}",
                )
            )
        if offset:
            items.append(("← Даты", f"caltimepage:{max(0, offset - 8)}"))
        if offset + 8 < len(days):
            items.append(("Даты →", f"caltimepage:{offset + 8}"))
        items += [
            ("Проверить и создать окна", "calpreview"),
            ("Изменить даты", "calback"),
        ]
        await state.set_state(CalendarFlow.time_menu)
        await message.answer(
            "Выберите день и настройте его время. Можно сначала задать одинаковое время всем, затем изменить отдельные дни.\n"
            f"Дней со временем: {sum(bool(schedules.get(day)) for day in days)}/{len(days)}.\n"
            "Также можно отправить общий шаблон текстом: 10:00 2, 12:00 2.",
            reply_markup=keyboard(items),
        )

    async def show_time_editor(message, state):
        data = await state.get_data()
        target = data.get("cal_target", "all")
        times = data.get("cal_edit_times", [])
        capacity = data.get("cal_capacity", 2)
        items = [
            (
                f"{'✅ ' if any(t == time for t, _ in times) else '+ '}{time}",
                f"calpreset:{time.replace(':', '')}",
            )
            for time in ("09:00", "10:00", "11:00", "12:00", "14:00", "16:00")
        ]
        markup = keyboard(items).inline_keyboard
        markup.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if cap == capacity else ''}{cap} мест",
                    callback_data=f"calcap:{cap}",
                )
                for cap in (1, 2, 3, 4)
            ]
        )
        markup += keyboard(
            [
                ("Очистить время", "calemptytimes"),
                ("Готово", "caltimedone"),
                ("Назад к дням", "caltimemenu"),
            ]
        ).inline_keyboard
        title = (
            "Все выбранные дни"
            if target == "all"
            else date.fromisoformat(target).strftime("%d.%m.%Y")
        )
        await state.set_state(CalendarFlow.time_edit)
        await message.answer(
            f"{title}\nОкна: "
            + (", ".join(f"{time} — {cap} мест" for time, cap in times) or "пока нет")
            + "\nВыберите число мест, затем нажмите время. Повторное нажатие убирает время; другое число мест меняет его."
            + "\nДля произвольного времени отправьте: 10:30 2, 13:15 1. Текст заменит время для этого дня (или всех дней). Нажмите «Готово», чтобы применить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=markup),
        )

    async def preview(message, state):
        data = await state.get_data()
        schedules = data.get("cal_day_times", {})
        try:
            if not data.get("cal_days") or any(
                not schedules.get(day) for day in data["cal_days"]
            ):
                raise ValueError("Задайте время для каждого выбранного дня.")
            slots = [
                (
                    datetime.fromisoformat(day + "T" + time)
                    .replace(tzinfo=config.timezone)
                    .isoformat(),
                    cap,
                )
                for day in data["cal_days"]
                for time, cap in schedules[day]
            ]
            if len(slots) > 366:
                raise ValueError(
                    "Не более 366 окон за раз. Выберите меньше дней или времён."
                )
            if any(
                datetime.fromisoformat(starts) <= datetime.now(config.timezone)
                for starts, _ in slots
            ):
                raise ValueError(
                    "Часть выбранных времён уже прошла. Измените время или даты."
                )
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=keyboard(
                    [("Настроить время", "caltimemenu"), ("Изменить даты", "calback")]
                ),
            )
            return
        existing = {
            row["starts"]
            for row in db.rows(
                "SELECT starts FROM slots WHERE service=?", (data["cal_service"],)
            )
        }
        count = sum(starts not in existing for starts, _ in slots)
        await state.update_data(cal_slots=slots)
        await state.set_state(CalendarFlow.confirm)
        lines = [
            f"{date.fromisoformat(day).strftime('%d.%m.%Y')}: "
            + ", ".join(f"{time}×{cap}" for time, cap in schedules[day])
            for day in data["cal_days"][:20]
        ]
        if len(data["cal_days"]) > 20:
            lines.append(
                f"И ещё {len(data['cal_days']) - 20} дней; все даты доступны в настройке времени."
            )
        await message.answer(
            SERVICES[data["cal_service"]]
            + "\n"
            + "\n".join(lines)
            + f"\nВсего дней: {len(data['cal_days'])}. Новых окон: {count}. Совпадений: {len(slots) - count} — будут пропущены.\nСоздать расписание?",
            reply_markup=keyboard(
                [
                    ("Создать окна", "calsave"),
                    ("Изменить время", "caltimemenu"),
                    ("Изменить даты", "calback"),
                    ("Отмена", "admin"),
                ]
            ),
        )

    @router.callback_query(
        F.data.in_({"caltimes", "caltimemenu"}) | F.data.startswith("caltimepage:")
    )
    async def times_menu(q, state):
        if not await guard(q):
            return
        if not (await state.get_data()).get("cal_days"):
            await q.answer("Сначала выберите хотя бы один день.", show_alert=True)
            return
        await q.answer()
        offset = int(q.data.split(":")[1]) if q.data.startswith("caltimepage:") else 0
        await show_time_menu(q.message, state, max(0, offset))

    @router.callback_query(
        CalendarFlow.time_menu, F.data.startswith("caltime:") | (F.data == "calall")
    )
    async def choose_time_day(q, state):
        if not await guard(q):
            return
        data = await state.get_data()
        target = q.data.split(":")[1] if q.data.startswith("caltime:") else "all"
        if target != "all" and target not in data["cal_days"]:
            await q.answer("Этот день больше не выбран.", show_alert=True)
            return
        await q.answer()
        schedules = data.get("cal_day_times", {})
        times = schedules.get(target, [])
        if target == "all" and data["cal_days"]:
            common = schedules.get(data["cal_days"][0], [])
            if all(schedules.get(day, []) == common for day in data["cal_days"]):
                times = common
        await state.update_data(cal_target=target, cal_edit_times=times, cal_capacity=2)
        await show_time_editor(q.message, state)

    @router.callback_query(
        CalendarFlow.time_edit,
        F.data.startswith("calpreset:")
        | F.data.startswith("calcap:")
        | F.data.in_({"calemptytimes", "caltimedone"}),
    )
    async def edit_time(q, state):
        if not await guard(q):
            return
        data = await state.get_data()
        times = dict(data.get("cal_edit_times", []))
        if q.data.startswith("calcap:"):
            cap = int(q.data.split(":")[1])
            if cap not in (1, 2, 3, 4):
                return
            await state.update_data(cal_capacity=cap)
        elif q.data.startswith("calpreset:"):
            raw = q.data.split(":")[1]
            time = raw[:2] + ":" + raw[2:]
            parse_times(f"{time} {data.get('cal_capacity', 2)}")
            cap = data.get("cal_capacity", 2)
            if times.get(time) == cap:
                times.pop(time)
            else:
                if len(times) >= 8 and time not in times:
                    await q.answer("Не более восьми времён в день.", show_alert=True)
                    return
                times[time] = cap
            await state.update_data(cal_edit_times=sorted(times.items()))
        elif q.data == "calemptytimes":
            await state.update_data(cal_edit_times=[])
        else:
            schedules = data.get("cal_day_times", {})
            targets = (
                data["cal_days"]
                if data.get("cal_target") == "all"
                else [data["cal_target"]]
            )
            for day in targets:
                schedules[day] = sorted(times.items())
            await state.update_data(cal_day_times=schedules)
            await q.answer("Время сохранено")
            await show_time_menu(q.message, state)
            return
        await q.answer()
        await show_time_editor(q.message, state)

    @router.message(CalendarFlow.time_edit, F.text)
    async def time_day_input(m, state):
        if not access.authenticated(m.from_user.id):
            await m.answer("Войдите в управление: /admin.")
            return
        try:
            times = parse_times(m.text)
        except ValueError as error:
            await m.answer(
                str(error),
                reply_markup=keyboard(
                    [("Вернуться к настройке времени", "caltimeeditor")]
                ),
            )
            return
        await state.update_data(cal_edit_times=times)
        await show_time_editor(m, state)

    @router.callback_query(CalendarFlow.time_edit, F.data == "caltimeeditor")
    async def restore_editor(q, state):
        if not await guard(q):
            return
        await q.answer()
        await show_time_editor(q.message, state)

    @router.message(CalendarFlow.time_menu, F.text)
    async def times_input(m, state):
        if not access.authenticated(m.from_user.id):
            await m.answer("Войдите в управление: /admin.")
            return
        try:
            times = parse_times(m.text)
        except ValueError as error:
            await m.answer(
                str(error), reply_markup=keyboard([("Вернуться к дням", "caltimemenu")])
            )
            return
        data = await state.get_data()
        await state.update_data(cal_day_times={day: times for day in data["cal_days"]})
        await preview(m, state)

    @router.callback_query(CalendarFlow.time_menu, F.data == "calpreview")
    async def preview_callback(q, state):
        if not await guard(q):
            return
        await q.answer()
        await preview(q.message, state)

    @router.callback_query(CalendarFlow.confirm, F.data == "calsave")
    async def save(q, state):
        if not await guard(q):
            return
        data = await state.get_data()
        try:
            created, skipped = db.add_slots(data["cal_service"], data["cal_slots"])
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await state.clear()
        await q.answer("Расписание сохранено")
        await q.message.answer(
            f"Создано окон: {created}. Уже существовали: {skipped}.",
            reply_markup=keyboard(
                [
                    ("Открыть календарь", "calendar"),
                    ("К окнам", "adminslots:0"),
                    ("Управление", "admin"),
                ]
            ),
        )

    @router.callback_query(F.data == "calnoop")
    async def noop(q):
        await q.answer()
