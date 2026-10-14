from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .access import ROLE_NAMES
from .ui import keyboard


class StaffFlow(StatesGroup):
    password = State()
    person = State()
    role = State()


async def login_prompt(message, state):
    await state.clear()
    await state.set_state(StaffFlow.password)
    await message.answer(
        "Введите пароль администратора. Отмена: /cancel.",
        reply_markup=ReplyKeyboardRemove(),
    )


def register_staff(router, db, access, admin_menu):
    async def guard(q, target=None, new_role=None):
        if not access.can_manage(q.from_user.id, target, new_role):
            await q.answer("Нет доступа. Для входа: /admin.", show_alert=True)
            return False
        return True

    @router.message(StaffFlow.password, F.text)
    async def password(message: Message, state: FSMContext):
        # Passwords are never saved in FSM data or logs; remove the input from chat if possible.
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        try:
            role = access.login(message.from_user.id, message.text.strip())
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await message.answer(f"Вход выполнен. Ваша роль: {ROLE_NAMES[role]}.")
        await admin_menu(message)

    @router.callback_query(F.data.startswith("stafflist:"))
    async def stafflist(q: CallbackQuery, state: FSMContext):
        if not access.authenticated(q.from_user.id):
            await q.answer(
                "Для просмотра сотрудников войдите: /admin.", show_alert=True
            )
            return
        await q.answer()
        await state.clear()
        offset = max(0, int(q.data.split(":")[1]))
        rows = access.staff()
        items = [
            (
                f"{ROLE_NAMES[s['role']]} · {s['name'][:40]}",
                f"staffperson:{s['user_id']}",
            )
            for s in rows[offset : offset + 8]
        ]
        if offset:
            items.append(("← Назад", f"stafflist:{max(0, offset - 8)}"))
        if offset + 8 < len(rows):
            items.append(("Далее →", f"stafflist:{offset + 8}"))
        if access.can_manage(q.from_user.id):
            items.append(("➕ Добавить сотрудника", "staffadd"))
        items.append(("Управление", "admin"))
        await q.message.answer(
            "Сотрудники академии и их роли. Выберите человека, чтобы открыть карточку.",
            reply_markup=keyboard(items),
        )

    @router.callback_query(F.data == "staffadd")
    async def staffadd(q: CallbackQuery, state: FSMContext):
        if not await guard(q):
            return
        await q.answer()
        await state.clear()
        await state.set_state(StaffFlow.person)
        await q.message.answer(
            "Нажмите «Выбрать человека» и выберите сотрудника в Telegram. "
            "Также можно отправить его контакт с Telegram ID или @username, если он уже запускал бота. "
            "После выбора назначьте роль. Отмена: /cancel.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="👤 Выбрать человека",
                            request_users=KeyboardButtonRequestUsers(
                                request_id=707,
                                user_is_bot=False,
                                max_quantity=1,
                                request_name=True,
                                request_username=True,
                            ),
                        )
                    ]
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @router.message(StaffFlow.person)
    async def person(message: Message, state: FSMContext):
        if not access.can_manage(message.from_user.id):
            await state.clear()
            await message.answer(
                "Войдите заново: /admin.", reply_markup=ReplyKeyboardRemove()
            )
            return
        target = None
        name = ""
        if (
            message.users_shared
            and message.users_shared.request_id == 707
            and len(message.users_shared.users) == 1
        ):
            user = message.users_shared.users[0]
            target = user.user_id
            name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(
                target
            )
            access.remember(target, name, user.username or "")
        elif message.contact and message.contact.user_id:
            target = message.contact.user_id
            name = " ".join(
                x for x in (message.contact.first_name, message.contact.last_name) if x
            )
            existing = db.rows("SELECT username FROM users WHERE user_id=?", (target,))
            access.remember(target, name, existing[0]["username"] if existing else "")
        elif message.text:
            value = message.text.strip()
            if value.startswith("@"):
                rows = db.rows(
                    "SELECT * FROM users WHERE username=? AND seen=1",
                    (value[1:].lower(),),
                )
                if rows:
                    target, name = rows[0]["user_id"], rows[0]["name"]
            elif value.isdigit() and int(value) > 0:
                target = int(value)
                rows = db.rows("SELECT * FROM users WHERE user_id=?", (target,))
                name = rows[0]["name"] if rows else str(target)
        if not target:
            await message.answer(
                "Не удалось определить аккаунт. Нажмите «Выбрать человека». Один номер телефона не позволяет определить Telegram ID."
            )
            return
        if not access.can_manage(message.from_user.id, target):
            await message.answer(
                "Вы можете управлять только менеджерами. Выберите другого человека."
            )
            return
        await state.update_data(staff_target=target, staff_name=name)
        await state.set_state(StaffFlow.role)
        roles = (
            ["manager", "admin"]
            if access.role(message.from_user.id) == "owner"
            else ["manager"]
        )
        await message.answer(
            f"Сотрудник: {name}. Выберите роль.", reply_markup=ReplyKeyboardRemove()
        )
        await message.answer(
            "Менеджер: окна, записи и заявки. Администратор: также управление менеджерами. Владелец: все роли и настройки доступа.",
            reply_markup=keyboard(
                [(ROLE_NAMES[role], f"staffassign:{role}") for role in roles]
                + [("Отмена", "stafflist:0")]
            ),
        )

    @router.callback_query(StaffFlow.role, F.data.startswith("staffassign:"))
    async def staffassign(q: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        role = q.data.split(":")[1]
        target = data.get("staff_target")
        if target is None or not await guard(q, target, role):
            return
        try:
            access.set_role(q.from_user.id, target, role)
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Роль назначена")
        await state.clear()
        await q.message.edit_reply_markup(reply_markup=None)
        with db.connect() as c:
            db.enqueue(
                c,
                target,
                f"Вам назначена роль «{ROLE_NAMES[role]}» в академии «Жемчужина». Для входа: /admin. Пароль не требуется — доступ открывается по вашей роли.",
            )
        await q.message.answer(
            f"{data['staff_name']}: {ROLE_NAMES[role]}. "
            + (
                "Уведомление поставлено в очередь. "
                if db.rows("SELECT 1 FROM users WHERE user_id=? AND seen=1", (target,))
                else "Telegram не позволяет боту написать первым: приглашение появится после запуска бота. "
            )
            + "Отправьте сотруднику ссылку https://t.me/MariupolPearlBookingBot для входа через /admin. Пароль не требуется.",
            reply_markup=keyboard([("Сотрудники", "stafflist:0")]),
        )

    @router.callback_query(F.data.startswith("staffperson:"))
    async def staffperson(q: CallbackQuery):
        target = int(q.data.split(":")[1])
        if not access.authenticated(q.from_user.id):
            await q.answer("Для просмотра ролей войдите: /admin.", show_alert=True)
            return
        staff = next((s for s in access.staff() if s["user_id"] == target), None)
        if not staff:
            await q.answer("Сотрудник больше не имеет доступа.", show_alert=True)
            return
        await q.answer()
        roles = (
            ["manager", "admin"]
            if access.role(q.from_user.id) == "owner"
            else ["manager"]
        )
        items = [
            (f"Назначить: {ROLE_NAMES[role]}", f"staffchange:{target}:{role}")
            for role in roles
            if role != staff["role"] and access.can_manage(q.from_user.id, target, role)
        ]
        if (
            access.role(q.from_user.id) == "owner"
            and target != q.from_user.id
            and staff["role"] != "owner"
        ):
            items.append(("👑 Передать владение", f"transferask:{target}"))
        if access.can_manage(q.from_user.id, target):
            items.append(("Удалить доступ", f"staffremoveask:{target}"))
        items.append(("Сотрудники", "stafflist:0"))
        await q.message.answer(
            f"{staff['name']}"
            + (f"\n@{staff['username']}" if staff["username"] else "")
            + f"\nРоль: {ROLE_NAMES[staff['role']]}",
            reply_markup=keyboard(items),
        )

    @router.callback_query(F.data.startswith("transferask:"))
    async def transferask(q: CallbackQuery, state: FSMContext):
        if (
            not access.authenticated(q.from_user.id)
            or access.role(q.from_user.id) != "owner"
        ):
            await q.answer("Передать владение может только владелец.", show_alert=True)
            return
        target = int(q.data.split(":")[1])
        staff = next((s for s in access.staff() if s["user_id"] == target), None)
        if not staff or target == q.from_user.id or staff["role"] == "owner":
            await q.answer(
                "Выберите другого сотрудника, который ещё не является владельцем.",
                show_alert=True,
            )
            return
        await q.answer()
        await state.clear()
        await state.update_data(ownership_target=target)
        await q.message.answer(
            f"Передать владение сотруднику {staff['name']}?\nОн станет владельцем, а вы — администратором.",
            reply_markup=keyboard(
                [
                    ("Да, передать владение", f"transferconfirm:{target}"),
                    ("Отмена", "stafflist:0"),
                ]
            ),
        )

    @router.callback_query(F.data.startswith("transferconfirm:"))
    async def transferconfirm(q: CallbackQuery, state: FSMContext):
        target = int(q.data.split(":")[1])
        data = await state.get_data()
        if data.get("ownership_target") != target:
            await q.answer(
                "Откройте карточку сотрудника и подтвердите передачу заново.",
                show_alert=True,
            )
            return
        try:
            access.transfer_ownership(q.from_user.id, target)
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await state.clear()
        await q.answer("Владение передано. Вы остались администратором.")
        await q.message.edit_reply_markup(reply_markup=None)
        await admin_menu(q.message)

    @router.callback_query(F.data.startswith("staffchange:"))
    async def staffchange(q: CallbackQuery):
        _, target, role = q.data.split(":")
        target = int(target)
        if not await guard(q, target, role):
            return
        try:
            access.set_role(q.from_user.id, target, role)
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Роль изменена. Для продолжения сотруднику нужно войти заново.")
        await q.message.edit_reply_markup(
            reply_markup=keyboard([("Сотрудники", "stafflist:0")])
        )

    @router.callback_query(F.data.startswith("staffremoveask:"))
    async def staffremoveask(q: CallbackQuery):
        target = int(q.data.split(":")[1])
        if not await guard(q, target):
            return
        await q.answer()
        await q.message.answer(
            "Удалить доступ сотрудника к панели управления?",
            reply_markup=keyboard(
                [
                    ("Да, удалить", f"staffremove:{target}"),
                    ("Оставить доступ", "stafflist:0"),
                ]
            ),
        )

    @router.callback_query(F.data.startswith("staffremove:"))
    async def staffremove(q: CallbackQuery):
        target = int(q.data.split(":")[1])
        if not await guard(q, target):
            return
        try:
            access.set_role(q.from_user.id, target, None)
        except ValueError as error:
            await q.answer(str(error), show_alert=True)
            return
        await q.answer("Доступ удалён")
        await q.message.edit_reply_markup(
            reply_markup=keyboard([("Сотрудники", "stafflist:0")])
        )
