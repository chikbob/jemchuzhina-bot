import re
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import SERVICES


def keyboard(items):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d)] for t, d in items
        ]
    )


def when(value):
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")


def parse_slot(value, timezone):
    match = re.fullmatch(
        r"\s*(стрижка|окрашивание)\s*(?:[;,]\s*|\s+)(\d{1,2}\.\d{1,2}\.\d{4})\s*(?:[;,]\s*|\s+)(\d{1,2}:\d{2})\s*(?:[;,]\s*|\s+)(\d+)\s*",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(
            "Данные неверны. Введите услугу, дату, время и число мест ещё раз. Например: окрашивание, 16.10.2026, 10:00, 2. Можно разделять запятыми, точками с запятой или пробелами."
        )
    service, date, time, capacity = match.groups()
    try:
        starts = datetime.strptime(date + " " + time, "%d.%m.%Y %H:%M").replace(
            tzinfo=timezone
        )
    except ValueError:
        raise ValueError(
            "Данные неверны: проверьте дату и время. Введите окно ещё раз."
        ) from None
    return (
        {"стрижка": "haircut", "окрашивание": "color"}[service.lower()],
        starts.isoformat(),
        int(capacity),
    )


def phone_number(value):
    digits = re.sub(r"[\s()\-]", "", value)
    if re.fullmatch(r"8\d{10}", digits):
        digits = "+7" + digits[1:]
    if re.fullmatch(r"7\d{10}", digits):
        digits = "+" + digits
    if not re.fullmatch(r"\+[1-9]\d{9,14}", digits):
        raise ValueError(
            "Введите телефон в международном формате, например +79491234567."
        )
    return digits


def schedule_text(db, config):
    lines = [
        "💎 Академия красоты «Жемчужина»",
        "Приглашаем моделей на стрижку и окрашивание!",
        "",
        "Свободные окна:",
    ]
    slots = db.slots(available=True)
    if not slots:
        lines.append("Сейчас свободных окон нет. Новые даты появятся здесь.")
    for s in slots:
        line = f"• {SERVICES[s['service']]} · {when(s['starts'])} — мест: {s['free']}"
        if len("\n".join(lines)) + len(line) > 3200:
            lines.append("Остальные даты доступны в боте.")
            break
        lines.append(line)
    lines += [
        "",
        f"📍 {config.address}",
        f"📞 {config.phone}",
        "Условия и длительность процедуры уточняйте в академии.",
        "Для записи нажмите кнопку ниже.",
    ]
    return "\n".join(lines)
