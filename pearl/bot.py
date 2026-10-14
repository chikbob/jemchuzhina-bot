import asyncio
import json
import logging

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .access import Access
from .booking_manager import register_booking_manager
from .brand import BOT_DESCRIPTION, BOT_NAME, BOT_SHORT_DESCRIPTION
from .calendar_ui import register_calendar
from .config import Config
from .db import SERVICES, STATUS_NAMES, Database
from .interaction import InteractionMiddleware, current_ui, flush_ui
from .slot_wizard import register_slot_wizard
from .staff import login_prompt, register_staff
from .storage import SQLiteStorage
from .transport import create_bot
from .ui import keyboard, phone_number, schedule_text, when

log = logging.getLogger(__name__)


class Flow(StatesGroup):
    length = State()
    quality = State()
    condition = State()
    wish = State()
    photo = State()
    name = State()
    phone = State()
    confirm = State()
    comment = State()


class PrivateOnly(BaseMiddleware):
    def __init__(self, access):
        self.access = access

    async def __call__(self, handler, event, data):
        msg = event.message if isinstance(event, CallbackQuery) else event
        if msg and msg.chat.type == "private":
            user = event.from_user
            if user and not user.is_bot:
                self.access.remember(
                    user.id, user.full_name, user.username or "", seen=True
                )
            return await handler(event, data)


def build_router(db, config):
    r = Router()
    access = Access(db, config)
    r.message.outer_middleware(InteractionMiddleware(db))
    r.callback_query.outer_middleware(InteractionMiddleware(db))
    r.message.middleware(PrivateOnly(access))
    r.callback_query.middleware(PrivateOnly(access))

    def admins():
        return access.staff_ids()

    async def guard(q):
        if not access.authenticated(q.from_user.id):
            await q.answer("Войдите в панель по паролю: /admin.", show_alert=True)
            return False
        return True

    async def home(m):
        items = [
            ("✂️ Модель на стрижку", "service:haircut"),
            ("🎨 Модель на окрашивание", "service:color"),
            ("🎓 Обучение", "learn"),
            ("📋 Мои записи", "mine"),
        ]
        if m.chat.id in admins():
            items.append(("⚙️ Управление", "admin"))
        items.append(("⋯ Ещё", "chatoptions"))
        await m.answer(
            "Здравствуйте! Вы в Академии красоты «Жемчужина». Чем можем помочь?",
            reply_markup=keyboard(items),
        )

    async def show_slots(m, service, offset=0):
        slots = db.slots(service, available=True)
        items = [
            (f"{when(s['starts'])} · мест: {s['free']}", f"slot:{s['id']}")
            for s in slots[offset : offset + 8]
        ]
        if offset:
            items.append(("← Раньше", f"page:{service}:{max(0, offset - 8)}"))
        if offset + 8 < len(slots):
            items.append(("Позже →", f"page:{service}:{offset + 8}"))
        items.append(("В главное меню", "home"))
        await m.answer(
            "Выберите время. Место закрепляется после завершения анкеты."
            if slots
            else "Свободных окон пока нет. Вернитесь позже или свяжитесь с академией: "
            + config.phone,
            reply_markup=keyboard(items),
        )

    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        if m.from_user.id in admins():
            with db.connect() as c:
                c.execute(
                    "UPDATE outbox SET delivered=0 WHERE chat_id=? AND delivered=-1 AND error='TelegramForbiddenError'",
                    (m.from_user.id,),
                )
        if db.setting(f"reply_keyboard:{m.chat.id}"):
            await m.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
        await home(m)

    @r.message(Command("clean"))
    async def clean_command(m: Message, state: FSMContext):
        await state.clear()
        # Cleanup is queued after the new screen by the outer middleware.
        data = current_ui.get()
        if data:
            data[2]["recent_cleanup"] = True
        if db.setting(f"reply_keyboard:{m.chat.id}"):
            await m.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
        await home(m)

    @r.callback_query(F.data == "chatoptions")
    async def chat_options(q: CallbackQuery):
        await q.answer()
        await q.message.answer(
            "Очистка удалит до 200 последних сообщений в этом чате. Записи и заявки сохранятся. Для нового начала: /start.",
            reply_markup=keyboard(
                [("🧹 Очистить сообщения", "chatclean"), ("Главное меню", "home")]
            ),
        )

    @r.callback_query(F.data == "chatclean")
    async def clean_callback(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.clear()
        current_ui.get()[2]["recent_cleanup"] = True
        if db.setting(f"reply_keyboard:{q.message.chat.id}"):
            await q.message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
        await home(q.message)

    @r.message(Command("cancel"))
    async def cancel_flow(m: Message, state: FSMContext):
        await state.clear()
        if access.authenticated(m.from_user.id):
            await m.answer(
                "Действие отменено. Вы остаётесь в управлении.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await admin_menu(m)
        else:
            await m.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
            await home(m)

    @r.message(Command("id"))
    async def user_id(m: Message):
        await m.answer(f"Ваш Telegram ID: {m.from_user.id}")

    @r.message(Command("admin"))
    async def admin_command(m: Message, state: FSMContext):
        if access.authenticated(m.from_user.id):
            await state.clear()
            await m.answer("Панель администратора", reply_markup=ReplyKeyboardRemove())
            await admin_menu(m)
        else:
            await login_prompt(m, state)

    @r.message(Command("logout"))
    async def logout(m: Message, state: FSMContext):
        access.logout(m.from_user.id)
        await state.clear()
        await m.answer(
            "Вы вышли из панели управления. Для возврата: /admin."
            + (
                " Доступ по назначенной роли сохраняется."
                if access.role(m.from_user.id) in ("admin", "manager")
                else ""
            ),
            reply_markup=ReplyKeyboardRemove(),
        )

    @r.callback_query(F.data == "home")
    async def menu(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.clear()
        await home(q.message)

    @r.callback_query(F.data.startswith("service:"))
    async def service(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.clear()
        service = q.data.split(":")[1]
        if service in SERVICES:
            await show_slots(q.message, service)

    @r.callback_query(F.data.startswith("page:"))
    async def page(q: CallbackQuery):
        await q.answer()
        _, service, offset = q.data.split(":")
        if service in SERVICES:
            await show_slots(q.message, service, max(0, int(offset)))

    @r.callback_query(F.data.startswith("slot:"))
    async def slot(q: CallbackQuery, state: FSMContext):
        await q.answer()
        slot_id = int(q.data.split(":")[1])
        s = next((s for s in db.slots(available=True) if s["id"] == slot_id), None)
        if not s:
            await q.message.answer("Окно уже недоступно. Выберите другое через /start.")
            return
        await state.clear()
        await state.update_data(
            slot_id=slot_id, service=s["service"], starts=s["starts"]
        )
        await state.set_state(Flow.length)
        await q.message.answer(
            f"{SERVICES[s['service']]} · {when(s['starts'])}\nКакая длина волос сейчас? Например: короткие, до плеч, ниже плеч.\nПрервать анкету: /cancel",
            reply_markup=ReplyKeyboardRemove(),
        )

    @r.callback_query(F.data == "learn")
    async def learn(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.clear()
        await q.message.answer(
            "Какое обучение вас интересует?",
            reply_markup=keyboard(
                [(x, "course:" + x) for x in ("Стрижки", "Колористика", "Причёски")]
                + [("Главное меню", "home")]
            ),
        )

    @r.callback_query(F.data.startswith("course:"))
    async def course(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.clear()
        await state.update_data(course=q.data.split(":", 1)[1])
        await state.set_state(Flow.name)
        await q.message.answer(
            "Как вас зовут? Администратор свяжется с вами и расскажет об обучении."
        )

    async def photo_prompt(m, state):
        await state.set_state(Flow.photo)
        await m.answer(
            "При желании отправьте одно фото волос при дневном свете. Можно пропустить.",
            reply_markup=keyboard([("Пропустить фото", "skip_photo")]),
        )

    @r.message(Flow.length, F.text)
    async def length(m: Message, state: FSMContext):
        if not 2 <= len(m.text.strip()) <= 150:
            await m.answer("Опишите длину волос, до 150 символов.")
            return
        await state.update_data(length=m.text.strip())
        data = await state.get_data()
        if data["service"] == "color":
            await state.set_state(Flow.quality)
            await m.answer(
                "Окрашены волосы или нет?",
                reply_markup=keyboard(
                    [
                        ("Да, окрашены", "colored:yes"),
                        ("Нет, не окрашены", "colored:no"),
                    ]
                ),
            )
        else:
            await state.set_state(Flow.name)
            await m.answer("Как вас зовут?")

    async def ask_condition(message, state, value):
        await state.update_data(colored=value)
        await state.set_state(Flow.condition)
        await message.answer(
            "Какое состояние волос сейчас? Укажите сухость, повреждения, прежнее осветление, если есть.",
            reply_markup=keyboard([("Хорошее, повреждений нет", "condition:good")]),
        )

    @r.message(Flow.quality, F.text)
    async def quality(m: Message, state: FSMContext):
        if not 2 <= len(m.text.strip()) <= 700:
            await m.answer(
                "Ответьте, окрашены волосы или нет. Можно добавить подробности."
            )
            return
        await ask_condition(m, state, m.text.strip())

    @r.callback_query(Flow.quality, F.data.in_({"colored:yes", "colored:no"}))
    async def colored(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await ask_condition(
            q.message, state, "Окрашены" if q.data == "colored:yes" else "Не окрашены"
        )

    async def accept_condition(message, state, value):
        await state.update_data(quality=value)
        await state.set_state(Flow.wish)
        await message.answer(
            "Какое окрашивание хотите? Опишите цвет или технику и желаемый результат."
        )

    @r.message(Flow.condition, F.text)
    async def condition(m: Message, state: FSMContext):
        if not 2 <= len(m.text.strip()) <= 700:
            await m.answer("Опишите состояние волос, от 2 до 700 символов.")
            return
        await accept_condition(m, state, m.text.strip())

    @r.callback_query(Flow.condition, F.data == "condition:good")
    async def condition_good(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await accept_condition(q.message, state, "Хорошее, повреждений нет")

    @r.message(Flow.wish, F.text)
    async def wish(m: Message, state: FSMContext):
        if not 2 <= len(m.text.strip()) <= 700:
            await m.answer("Опишите желаемое окрашивание, от 2 до 700 символов.")
            return
        await state.update_data(wish=m.text.strip())
        await photo_prompt(m, state)

    @r.message(Flow.photo, F.photo)
    async def photo(m: Message, state: FSMContext):
        await state.update_data(photo=m.photo[-1].file_id)
        await state.set_state(Flow.name)
        await m.answer("Как вас зовут?")

    @r.callback_query(Flow.photo, F.data == "skip_photo")
    async def skip_photo(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.update_data(photo="")
        await state.set_state(Flow.name)
        await q.message.answer("Как вас зовут?")

    @r.message(Flow.name, F.text)
    async def name(m: Message, state: FSMContext):
        if not 2 <= len(m.text.strip()) <= 100:
            await m.answer("Введите имя, от 2 до 100 символов.")
            return
        await state.update_data(name=m.text.strip())
        await state.set_state(Flow.phone)
        await m.answer(
            "Оставьте телефон для связи: отправьте свой контакт кнопкой или введите номер. Имя, телефон и сведения о волосах будут доступны администраторам академии для организации записи. Нажимая «Подтвердить», вы соглашаетесь передать эти данные. Отмена: /cancel.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="📱 Поделиться моим номером", request_contact=True
                        )
                    ]
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @r.message(Flow.phone)
    async def phone(m: Message, state: FSMContext):
        if m.contact and m.contact.user_id != m.from_user.id:
            await m.answer("Отправьте свой контакт или введите номер текстом.")
            return
        try:
            phone = phone_number(
                m.contact.phone_number if m.contact else (m.text or "")
            )
        except ValueError as e:
            await m.answer(str(e))
            return
        await state.update_data(phone=phone)
        await state.set_state(Flow.comment)
        await m.answer(
            "Продолжаем запись.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await m.answer(
            "Хотите добавить комментарий к записи? Например, пожелания к мастеру или что важно учесть. Напишите до 700 символов или пропустите.",
            reply_markup=keyboard([("Пропустить", "skip_comment")]),
        )

    async def confirmation(m, state):
        data = await state.get_data()
        await state.set_state(Flow.confirm)
        title = (
            "Обучение: " + data["course"]
            if "course" in data
            else SERVICES[data["service"]] + " · " + when(data["starts"])
        )
        details = [f"Длина: {data['length']}"] if "length" in data else []
        if "colored" in data:
            details.append(f"Волосы: {data['colored']}")
        if "quality" in data:
            details += [f"Состояние: {data['quality']}", f"Пожелания: {data['wish']}"]
        await state.update_data(note="\n".join(details))
        if data.get("client_comment"):
            details.append("Комментарий: " + data["client_comment"])
        await m.answer("Проверьте заявку:", reply_markup=ReplyKeyboardRemove())
        await m.answer(
            "\n".join(
                ["Проверьте заявку:", title, data["name"], data["phone"], *details]
            ),
            reply_markup=keyboard(
                [("✅ Подтвердить", "submit"), ("Заполнить заново", "home")]
            ),
        )

    @r.message(Flow.comment, F.text)
    async def client_comment(m: Message, state: FSMContext):
        value = m.text.strip()
        if not 1 <= len(value) <= 700:
            await m.answer(
                "Комментарий: от 1 до 700 символов. Попробуйте ещё раз или пропустите.",
                reply_markup=keyboard([("Пропустить", "skip_comment")]),
            )
            return
        await state.update_data(client_comment=value)
        await confirmation(m, state)

    @r.callback_query(Flow.comment, F.data == "skip_comment")
    async def skip_comment(q: CallbackQuery, state: FSMContext):
        await q.answer()
        await state.update_data(client_comment="")
        await confirmation(q.message, state)

    @r.callback_query(Flow.confirm, F.data == "submit")
    async def submit(q: CallbackQuery, state: FSMContext):
        await q.answer()
        data = await state.get_data()
        if "course" in data:
            db.add_lead(
                q.from_user.id,
                data["name"],
                data["phone"],
                data["course"],
                admins(),
                data.get("client_comment", ""),
            )
            await q.message.answer(
                "Заявка на обучение передана администратору. С вами свяжутся по указанному номеру."
            )
        else:
            try:
                db.reserve(
                    data["slot_id"],
                    q.from_user.id,
                    data["name"],
                    data["phone"],
                    data["note"],
                    data.get("photo", ""),
                    admins(),
                    config.address,
                    data.get("client_comment", ""),
                )
            except ValueError as e:
                await q.message.answer(str(e))
                await state.clear()
                await show_slots(q.message, data["service"])
                return
            await q.message.answer("Запись оформлена. Сейчас отправлю подтверждение.")
        await state.clear()
        await q.message.edit_reply_markup(reply_markup=None)

    @r.callback_query((F.data == "mine") | F.data.startswith("minepage:"))
    async def mine(q: CallbackQuery):
        await q.answer()
        bookings = db.bookings(q.from_user.id, history=True)
        if not bookings:
            await q.message.answer(
                "У вас пока нет предстоящих записей.",
                reply_markup=keyboard([("Главное меню", "home")]),
            )
        offset = (
            max(0, int(q.data.split(":")[1])) if q.data.startswith("minepage:") else 0
        )
        for b in bookings[offset : offset + 5]:
            await q.message.answer(
                f"№{b['id']} · {SERVICES[b['service']]} · {when(b['starts'])}\nСтатус: {STATUS_NAMES[b['status']]}"
                + (
                    f"\nВаш комментарий: {b['client_comment']}"
                    if b["client_comment"]
                    else ""
                )
                + (
                    f"\nКомментарий академии: {b['status_comment']}"
                    if b["status_comment"]
                    else ""
                ),
                reply_markup=keyboard(
                    (
                        [("Отменить запись", f"cancelask:{b['id']}")]
                        if b["status"] in ("pending", "confirmed")
                        and b["starts"] > db.now()
                        else []
                    )
                    + [("Главное меню", "home")]
                ),
            )

        if bookings:
            items = [("Главное меню", "home")]
            if offset:
                items.insert(0, ("← Назад", f"minepage:{max(0, offset - 5)}"))
            if offset + 5 < len(bookings):
                items.insert(0, ("Далее →", f"minepage:{offset + 5}"))
            await q.message.answer(
                f"Ваши записи: {len(bookings)}. Здесь сохраняется и история.",
                reply_markup=keyboard(items),
            )

    @r.callback_query(F.data.startswith("cancelask:"))
    async def cancelask(q: CallbackQuery):
        await q.answer()
        b_id = int(q.data.split(":")[1])
        if any(b["id"] == b_id for b in db.bookings(q.from_user.id)):
            await q.message.answer(
                "Отменить запись и освободить место?",
                reply_markup=keyboard(
                    [("Да, отменить", f"cancel:{b_id}"), ("Оставить запись", "mine")]
                ),
            )

    @r.callback_query(F.data.startswith("cancel:"))
    async def cancel(q: CallbackQuery):
        try:
            db.change_booking(
                int(q.data.split(":")[1]), "cancel", q.from_user.id, admins()
            )
        except ValueError as e:
            await q.answer(str(e), show_alert=True)
            return
        await q.answer("Запись отменена")
        await q.message.edit_reply_markup(reply_markup=None)

    async def admin_menu(m):
        await m.answer(
            "Управление академией",
            reply_markup=keyboard(
                [
                    ("➕ Создать / изменить окно", "addslot"),
                    ("📆 Календарь / создать много окон", "calendar"),
                    ("🗓 Управление окнами", "adminslots:0"),
                    ("📅 Записи на сегодня", "bookview:today"),
                    ("📋 Записи клиентов", "bookings:0"),
                    ("🎓 Заявки на обучение", "leads:0"),
                    ("📝 Предпросмотр поста", "preview"),
                    ("👥 Сотрудники", "stafflist:0"),
                    ("🔒 Выйти из управления", "logout"),
                    ("Главное меню", "home"),
                ]
            ),
        )

    @r.callback_query(F.data == "admin")
    async def admin(q: CallbackQuery, state: FSMContext):
        await q.answer()
        if access.authenticated(q.from_user.id):
            await state.clear()
            await admin_menu(q.message)
        else:
            await login_prompt(q.message, state)

    @r.callback_query(F.data == "logout")
    async def logout_button(q: CallbackQuery, state: FSMContext):
        await q.answer()
        access.logout(q.from_user.id)
        await state.clear()
        await q.message.answer(
            "Вы вышли из управления. Для возврата: /admin."
            + (
                " Доступ по назначенной роли сохраняется."
                if access.role(q.from_user.id) in ("admin", "manager")
                else ""
            ),
            reply_markup=ReplyKeyboardRemove(),
        )

    @r.callback_query(F.data.startswith("adminslots:"))
    async def adminslots(q: CallbackQuery):
        if not await guard(q):
            return
        await q.answer()
        offset = max(0, int(q.data.split(":")[1]))
        slots = db.slots()
        items = [
            (
                f"{'🟢' if s['enabled'] else '🔴'} {SERVICES[s['service']]} {when(s['starts'])} ({s['free']}/{s['capacity']})",
                f"slotmanage:{s['id']}",
            )
            for s in slots[offset : offset + 8]
        ]
        if offset:
            items.append(("← Назад", f"adminslots:{max(0, offset - 8)}"))
        if offset + 8 < len(slots):
            items.append(("Далее →", f"adminslots:{offset + 8}"))
        items.append(("Управление", "admin"))
        await q.message.answer(
            "Выберите окно для управления. 🟢 — показано клиентам, 🔴 — скрыто."
            if slots
            else "Предстоящих окон нет. Создайте новое окно через управление.",
            reply_markup=keyboard(items),
        )

    async def slot_card(m, slot_id):
        s = next((s for s in db.slots() if s["id"] == slot_id), None)
        if not s:
            await m.answer(
                "Окно удалено или его время прошло.",
                reply_markup=keyboard([("К окнам", "adminslots:0")]),
            )
            return
        await m.answer(
            f"{SERVICES[s['service']]} · {when(s['starts'])}\nСвободно: {s['free']} из {s['capacity']}\n{'Показано клиентам' if s['enabled'] else 'Скрыто от клиентов'}\nСкрытие и удаление окна сохраняют существующие записи в истории.",
            reply_markup=keyboard(
                [
                    (
                        "Скрыть" if s["enabled"] else "Показать",
                        f"slotvisible:{slot_id}:{0 if s['enabled'] else 1}",
                    ),
                    ("Изменить число мест", f"slotcapacity:{slot_id}"),
                    ("Удалить окно", f"slotdeleteask:{slot_id}"),
                    ("К окнам", "adminslots:0"),
                ]
            ),
        )

    @r.callback_query(F.data.startswith("slotmanage:"))
    async def slotmanage(q: CallbackQuery):
        if not await guard(q):
            return
        await q.answer()
        await slot_card(q.message, int(q.data.split(":")[1]))

    @r.callback_query(F.data.startswith("slotvisible:"))
    async def slotvisible(q: CallbackQuery):
        if not await guard(q):
            return
        _, slot_id, visible = q.data.split(":")
        try:
            db.set_slot_visibility(int(slot_id), visible == "1")
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Окно показано" if visible == "1" else "Окно скрыто")
        await slot_card(q.message, int(slot_id))

    @r.callback_query(F.data.startswith("slotdeleteask:"))
    async def slotdeleteask(q: CallbackQuery):
        if not await guard(q):
            return
        slot_id = int(q.data.split(":")[1])
        await q.answer()
        await q.message.answer(
            "Удалить окно из расписания? Уже оформленные записи останутся в истории; отменить их можно отдельно.",
            reply_markup=keyboard(
                [
                    ("Да, удалить", f"slotdelete:{slot_id}"),
                    ("Назад", f"slotmanage:{slot_id}"),
                ]
            ),
        )

    @r.callback_query(F.data.startswith("slotdelete:"))
    async def slotdelete(q: CallbackQuery):
        if not await guard(q):
            return
        db.delete_slot(int(q.data.split(":")[1]))
        await q.answer("Окно удалено")
        await q.message.answer(
            "Окно удалено из расписания. История записей сохранена.",
            reply_markup=keyboard([("К окнам", "adminslots:0")]),
        )

    @r.callback_query(F.data.startswith("toggle:"))
    async def toggle(q: CallbackQuery):
        if not await guard(q):
            return
        db.toggle_slot(int(q.data.split(":")[1]))
        await q.answer("Доступность изменена")
        await q.message.edit_reply_markup(
            reply_markup=keyboard([("Вернуться к окнам", "adminslots:0")])
        )

    @r.callback_query(F.data.startswith("rejectask:"))
    async def rejectask(q: CallbackQuery):
        if not await guard(q):
            return
        await q.answer()
        await q.message.answer(
            "Отменить запись? Клиент получит уведомление.",
            reply_markup=keyboard(
                [
                    ("Да, отменить", "reject:" + q.data.split(":")[1]),
                    ("Вернуться", "admin"),
                ]
            ),
        )

    @r.callback_query(F.data.startswith("leads:"))
    async def leads(q: CallbackQuery):
        if not await guard(q):
            return
        await q.answer()
        offset = max(0, int(q.data.split(":")[1]))
        rows = db.rows("SELECT * FROM leads WHERE status!='done' ORDER BY id")
        if not rows:
            await q.message.answer("Необработанных заявок на обучение нет.")
        for lead in rows[offset : offset + 5]:
            await q.message.answer(
                f"Обучение №{lead['id']} · {lead['course']}\n{lead['name']} · {lead['phone']}\nКомментарий: {lead['client_comment'] or '—'}\nСтатус: {lead['status']}",
                reply_markup=keyboard(
                    [
                        ("Взять в работу", f"lead:working:{lead['id']}"),
                        ("Завершить", f"lead:done:{lead['id']}"),
                    ]
                ),
            )
        items = [("Управление", "admin")]
        if offset:
            items.append(("← Назад", f"leads:{max(0, offset - 5)}"))
        if offset + 5 < len(rows):
            items.append(("Далее →", f"leads:{offset + 5}"))
        await q.message.answer("Навигация", reply_markup=keyboard(items))

    @r.callback_query(F.data.startswith("lead:"))
    async def lead(q: CallbackQuery):
        if not await guard(q):
            return
        _, status, lead_id = q.data.split(":")
        if status not in ("working", "done"):
            return
        with db.connect() as c:
            c.execute("UPDATE leads SET status=? WHERE id=?", (status, int(lead_id)))
        await q.answer("Статус обновлён")
        await q.message.edit_reply_markup(reply_markup=None)

    @r.callback_query(F.data == "preview")
    async def preview(q: CallbackQuery):
        if not await guard(q):
            return
        await q.answer()
        await q.message.answer(
            schedule_text(db, config),
            reply_markup=keyboard(
                [("Опубликовать / обновить пост", "publish"), ("Управление", "admin")]
            ),
        )

    @r.callback_query(F.data == "publish")
    async def publish(q: CallbackQuery):
        if not await guard(q):
            return
        if not config.channel:
            await q.answer("Сначала заполните CHANNEL_ID на сервере.", show_alert=True)
            return
        db.set_setting("publication_enabled", "1")
        db.set_setting("published_revision", "-1")
        await q.answer(
            "Пост поставлен в очередь. Далее расписание обновляется автоматически."
        )

    register_booking_manager(r, db, access)
    register_slot_wizard(r, db, config, access)
    register_staff(r, db, access, admin_menu)
    register_calendar(r, db, config, access)

    @r.callback_query()
    async def stale(q: CallbackQuery):
        await q.answer(
            "Эта кнопка больше не актуальна. Откройте /start.", show_alert=True
        )

    @r.message()
    async def fallback(m: Message, state: FSMContext):
        if await state.get_state():
            await m.answer("Ответьте на предыдущий вопрос. Для начала заново: /cancel.")
        else:
            await home(m)

    return r


async def deliver(bot, db):
    await flush_ui(bot, db)
    for item in db.rows("SELECT * FROM outbox WHERE delivered=0 ORDER BY id LIMIT 20"):
        # No sensitive client data is logged on Telegram API failures.
        try:
            buttons = (
                keyboard(json.loads(item["buttons"]))
                if item["buttons"] != "[]"
                else None
            )
            if item["photo"]:
                photo_message = await bot.send_photo(
                    item["chat_id"], item["photo"], caption="Фото к заявке модели"
                )
                with db.connect() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO ui_messages VALUES (?,?)",
                        (photo_message.chat.id, photo_message.message_id),
                    )
            notification = await bot.send_message(
                item["chat_id"], item["text"], reply_markup=buttons
            )
            with db.connect() as c:
                c.execute(
                    "INSERT OR IGNORE INTO ui_messages VALUES (?,?)",
                    (notification.chat.id, notification.message_id),
                )
                c.execute(
                    "INSERT INTO ui_cursor VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=MAX(message_id,excluded.message_id)",
                    (notification.chat.id, notification.message_id),
                )
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 60))
            break
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            with db.connect() as c:
                c.execute(
                    "UPDATE outbox SET delivered=-1,error=?,attempts=attempts+1 WHERE id=?",
                    (type(e).__name__, item["id"]),
                )
            log.warning(
                "Notification %s could not be delivered (%s)",
                item["id"],
                type(e).__name__,
            )
        except Exception as e:  # noqa: BLE001 -- proxy libraries raise their own transport errors
            log.warning(
                "Notification %s will be retried (%s)", item["id"], type(e).__name__
            )
            break
        else:
            with db.connect() as c:
                c.execute("UPDATE outbox SET delivered=1 WHERE id=?", (item["id"],))


async def update_publication(bot, db, config, username):
    if not config.channel or db.setting("publication_enabled") != "1":
        return
    revision = db.setting("schedule_revision")
    # Time passing can remove past slots even without a booking mutation.
    text = schedule_text(db, config)
    if (
        db.setting("published_revision") == revision
        and db.setting("published_text") == text
    ):
        return
    buttons = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Записаться моделью", url=f"https://t.me/{username}"
                )
            ]
        ]
    )
    chat_id = (
        int(config.channel) if config.channel.lstrip("-").isdigit() else config.channel
    )
    post_id = db.setting("post_id") if db.setting("post_chat") == config.channel else ""
    if post_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=int(post_id), reply_markup=buttons
            )
        except TelegramBadRequest as e:
            if "message to edit not found" in str(e):
                message = await bot.send_message(chat_id, text, reply_markup=buttons)
                db.set_setting("post_id", message.message_id)
            elif "message is not modified" not in str(e):
                raise
    else:
        message = await bot.send_message(chat_id, text, reply_markup=buttons)
        db.set_setting("post_id", message.message_id)
        db.set_setting("post_chat", config.channel)
    db.set_setting("published_text", text)
    db.set_setting("published_revision", revision)


async def worker(bot, db, config, username):
    failures = 0
    while True:
        try:
            db.expire_slots()
            await deliver(bot, db)
            await update_publication(bot, db, config, username)
            failures = 0
        except Exception as e:  # noqa: BLE001 -- proxy libraries raise their own transport errors
            failures += 1
            log.warning("Schedule publication failed (%s)", type(e).__name__)
            if failures == 1:
                with db.connect() as c:
                    for owner in (
                        row["user_id"]
                        for row in db.rows(
                            "SELECT user_id FROM staff WHERE role='owner'"
                        )
                    ):
                        db.enqueue(
                            c,
                            owner,
                            "Не удалось обновить расписание. Проверьте CHANNEL_ID, наличие поста и права бота на публикацию и редактирование. После исправления бот повторит попытку.",
                        )
        await asyncio.sleep(min(60, 5 * max(1, failures)))


async def run():
    config = Config.load()
    db = Database(config.database, config.timezone)
    bot = create_bot(config)
    dp = Dispatcher(storage=SQLiteStorage(db), events_isolation=SimpleEventIsolation())
    dp.include_router(build_router(db, config))
    task = None
    try:
        me = await bot.get_me()
        await bot.set_my_name(BOT_NAME)
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        await bot.set_my_commands(
            [
                {"command": "start", "description": "Главное меню"},
                {"command": "cancel", "description": "Начать заново"},
                {"command": "id", "description": "Мой Telegram ID"},
                {"command": "admin", "description": "Для администратора"},
                {"command": "logout", "description": "Выйти из панели управления"},
            ]
        )
        task = asyncio.create_task(worker(bot, db, config, me.username))
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await bot.session.close()
