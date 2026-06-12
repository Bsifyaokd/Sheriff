import asyncio
import os
import random
from uuid import uuid4

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# ---------- Глобальное состояние ----------
active_duels = {}       # duel_id -> DuelState
occupied = {}           # (chat_id, user_id) -> duel_id

# ---------- Инициализация ----------
bot = Bot(
    token=os.getenv("BOT_TOKEN"),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ---------- Вспомогательные классы и функции ----------
class DuelState:
    def __init__(self, duel_id, chat_id, initiator, opponent):
        self.duel_id = duel_id
        self.chat_id = chat_id
        self.initiator = initiator      # (id, name)
        self.opponent = opponent        # (id, name)
        self.initiator_hp = 2
        self.opponent_hp = 2
        self.initiator_ammo = 6
        self.opponent_ammo = 6
        self.initiator_aim = 0
        self.opponent_aim = 0
        self.distance = 10
        self.current_turn = None        # будет задан после определения первого хода
        self.accepted = False
        self.finished = False
        self.processing = False
        self.turn_msg_id = None
        self.timer_task = None

    def get_attacker_state(self, user_id):
        """Возвращает ammo, aim и hp атакующего + имя"""
        if user_id == self.initiator[0]:
            return self.initiator_ammo, self.initiator_aim, self.initiator_hp, self.initiator[1]
        else:
            return self.opponent_ammo, self.opponent_aim, self.opponent_hp, self.opponent[1]

    def get_defender_state(self, user_id):
        """Возвращает hp и имя защитника (противника)"""
        if user_id == self.initiator[0]:
            return self.opponent_hp, self.opponent[1]
        else:
            return self.initiator_hp, self.initiator[1]

    def set_attacker_state(self, user_id, ammo=None, aim=None, hp=None):
        if user_id == self.initiator[0]:
            if ammo is not None:
                self.initiator_ammo = ammo
            if aim is not None:
                self.initiator_aim = aim
            if hp is not None:
                self.initiator_hp = hp
        else:
            if ammo is not None:
                self.opponent_ammo = ammo
            if aim is not None:
                self.opponent_aim = aim
            if hp is not None:
                self.opponent_hp = hp

    def opponent_id(self, user_id):
        return self.initiator[0] if user_id == self.opponent[0] else self.opponent[0]

    def opponent_name(self, user_id):
        return self.initiator[1] if user_id == self.opponent[0] else self.opponent[1]

    def is_initiator(self, user_id):
        return user_id == self.initiator[0]


def calc_hit_chance(accuracy, aim_stacks, distance, weapon_range=7):
    """Формула шанса попадания (0–100)."""
    chance = accuracy + (accuracy / 2) * aim_stacks
    # Бонус сближения: +15, если дистанция меньше половины дальнобойности (<4)
    if distance < weapon_range / 2:
        chance += 15
    # Штраф за превышение дальности
    if distance > weapon_range:
        chance -= 10 * (distance - weapon_range)
    return max(0, min(100, int(chance)))


def generate_keyboard(duel_id, ammo):
    """Кнопки действий для хода. Если патронов 0 — вместо 'Выстрелить' 'Перезарядить'."""
    if ammo > 0:
        shoot_btn = InlineKeyboardButton(
            text="🔫 Выстрелить", callback_data=f"act_shoot_{duel_id}"
        )
    else:
        shoot_btn = InlineKeyboardButton(
            text="🔄 Перезарядить", callback_data=f"act_reload_{duel_id}"
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [shoot_btn],
            [
                InlineKeyboardButton(text="🎯 Прицелиться", callback_data=f"act_aim_{duel_id}"),
            ],
            [
                InlineKeyboardButton(text="👣 Ближе", callback_data=f"act_closer_{duel_id}"),
                InlineKeyboardButton(text="👣 Дальше", callback_data=f"act_farther_{duel_id}"),
            ],
        ]
    )
    return keyboard


async def send_system_message(chat_id, text, reply_to=None):
    """Безопасная отправка сообщения."""
    try:
        return await bot.send_message(chat_id, text, reply_to_message_id=reply_to)
    except TelegramAPIError as e:
        print(f"Ошибка отправки сообщения: {e}")
        return None


async def edit_message_safe(chat_id, message_id, text, reply_markup=None):
    """Безопасное редактирование."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except TelegramAPIError as e:
        print(f"Ошибка редактирования сообщения: {e}")


async def finish_duel(duel: DuelState, winner_id=None, loser_id=None, reason=""):
    """Корректное завершение дуэли."""
    if duel.finished:
        return
    duel.finished = True

    # Отмена таймера
    if duel.timer_task and not duel.timer_task.done():
        duel.timer_task.cancel()

    # Сообщение о результате
    if winner_id and loser_id:
        winner_name = duel.initiator[1] if duel.initiator[0] == winner_id else duel.opponent[1]
        loser_name = duel.initiator[1] if duel.initiator[0] == loser_id else duel.opponent[1]
        text = f"🏆 Победитель: {winner_name}\n💀 Проигравший: {loser_name}"
        if reason:
            text += f"\n{reason}"
        await send_system_message(duel.chat_id, text)

    # Очистка occupied
    for uid in [duel.initiator[0], duel.opponent[0]]:
        occupied.pop((duel.chat_id, uid), None)

    # Удаление дуэли из активных
    active_duels.pop(duel.duel_id, None)


async def switch_turn(duel: DuelState, next_player_id):
    """Передаём ход следующему игроку."""
    if duel.finished:
        return

    # Убираем кнопки у предыдущего сообщения хода
    if duel.turn_msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=duel.chat_id,
                message_id=duel.turn_msg_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            pass

    duel.current_turn = next_player_id
    attacker_name = duel.initiator[1] if next_player_id == duel.initiator[0] else duel.opponent[1]
    opponent_id = duel.opponent_id(next_player_id)
    opponent_name = duel.opponent_name(next_player_id)

    # Получаем состояние атакующего и защитника
    if next_player_id == duel.initiator[0]:
        ammo = duel.initiator_ammo
        aim = duel.initiator_aim
        hp = duel.initiator_hp
        opp_hp = duel.opponent_hp
        opp_ammo = duel.opponent_ammo
        opp_aim = duel.opponent_aim
    else:
        ammo = duel.opponent_ammo
        aim = duel.opponent_aim
        hp = duel.opponent_hp
        opp_hp = duel.initiator_hp
        opp_ammo = duel.initiator_ammo
        opp_aim = duel.initiator_aim

    text = (
        f"⚡ Ход {attacker_name}\n"
        f"📏 Дистанция: {duel.distance} шагов\n"
        f"Ваши показатели: ❤️{hp}  🔫{ammo}/6  🎯{20 + (20//2)*aim}\n"
        f"Противник: ❤️{opp_hp}  🔫{opp_ammo}/6  🎯{20 + (20//2)*opp_aim}"
    )

    markup = generate_keyboard(duel.duel_id, ammo)

    msg = await send_system_message(duel.chat_id, text)
    if not msg:
        # Фатальная ошибка — завершаем дуэль
        await finish_duel(duel, reason="⚠️ Не удалось отправить сообщение хода. Дуэль прервана.")
        return

    duel.turn_msg_id = msg.message_id

    # Запуск таймера хода (120 секунд)
    duel.timer_task = asyncio.create_task(turn_timer(duel.duel_id, msg.message_id))


async def turn_timer(duel_id: str, msg_id: int):
    """Таймер хода: если за 120 сек не было действий, ход пропускается."""
    await asyncio.sleep(120)
    duel = active_duels.get(duel_id)
    if not duel or duel.finished:
        return
    # Проверяем, не устарело ли сообщение (не сменился ли ход)
    if duel.turn_msg_id != msg_id or duel.processing:
        return
    # Пропуск хода
    current_id = duel.current_turn
    current_name = duel.initiator[1] if current_id == duel.initiator[0] else duel.opponent[1]
    await send_system_message(duel.chat_id, f"⏰ {current_name} не сделал ход вовремя. Ход переходит противнику.")
    await switch_turn(duel, duel.opponent_id(current_id))


async def execute_action(duel: DuelState, user_id: int, action: str):
    """Обработка действия игрока."""
    if duel.finished or user_id != duel.current_turn:
        return

    # Блокировка повторных действий
    if duel.processing:
        return
    duel.processing = True

    try:
        # Отмена таймера
        if duel.timer_task and not duel.timer_task.done():
            duel.timer_task.cancel()

        attacker_name = duel.initiator[1] if user_id == duel.initiator[0] else duel.opponent[1]
        opponent_id = duel.opponent_id(user_id)
        opponent_name = duel.opponent_name(user_id)

        # Получаем текущие параметры атакующего
        ammo, aim, hp, _ = duel.get_attacker_state(user_id)

        if action == "shoot":
            if ammo <= 0:
                # Игнорируем, кнопка должна быть неактивна
                duel.processing = False
                return

            ammo -= 1
            chance = calc_hit_chance(20, aim, duel.distance)
            hit = random.random() < (chance / 100)
            crit = hit and (random.random() < 0.05)

            # Сброс прицеливания у атакующего после выстрела
            duel.set_attacker_state(user_id, ammo=ammo, aim=0)

            if crit:
                # Критическое попадание — мгновенная победа
                await send_system_message(duel.chat_id,
                    f"💥 КРИТИЧЕСКОЕ ПОПАДАНИЕ! {attacker_name} сражает {opponent_name} наповал!")
                await finish_duel(duel, winner_id=user_id, loser_id=opponent_id)
                return
            elif hit:
                # Обычное попадание
                opp_hp, _ = duel.get_defender_state(user_id)
                opp_hp -= 1
                duel.set_attacker_state(opponent_id, hp=opp_hp, aim=0)  # сброс прицеливания у противника
                await send_system_message(duel.chat_id,
                    f"🔫🤯 Попадание! {attacker_name} ранит {opponent_name}.")
                if opp_hp <= 0:
                    await finish_duel(duel, winner_id=user_id, loser_id=opponent_id)
                    return
            else:
                await send_system_message(duel.chat_id, f"💨 Промах! {attacker_name} стреляет мимо.")

        elif action == "aim":
            aim += 1
            duel.set_attacker_state(user_id, aim=aim)
            await send_system_message(duel.chat_id, f"🥽 {attacker_name} прицеливается получше (×{aim})")

        elif action == "closer":
            if duel.distance <= 1:
                # Минимальная дистанция уже, ничего не делаем
                await send_system_message(duel.chat_id, f"👣 {attacker_name} и так вплотную.")
            else:
                duel.distance -= 1
                await send_system_message(duel.chat_id,
                    f"👣 {attacker_name} подходит ближе. Дистанция: {duel.distance}")

        elif action == "farther":
            duel.distance += 1
            if duel.distance > 20:
                await send_system_message(duel.chat_id,
                    f"🏃 {attacker_name} отходит слишком далеко и позорно сбегает!")
                await finish_duel(duel, winner_id=opponent_id, loser_id=user_id, reason="Позорное бегство")
                return
            await send_system_message(duel.chat_id,
                f"👣 {attacker_name} отдаляется. Дистанция: {duel.distance}")

        elif action == "reload":
            if ammo != 0:
                duel.processing = False
                return  # нельзя перезаряжать с патронами
            duel.set_attacker_state(user_id, ammo=6)
            await send_system_message(duel.chat_id, f"🔄 {attacker_name} перезаряжает оружие")

        # Переход хода
        await send_system_message(duel.chat_id, f"Теперь черёд {opponent_name} делать выстрел")
        await switch_turn(duel, opponent_id)

    except Exception as e:
        print(f"Ошибка выполнения действия: {e}")
        await send_system_message(duel.chat_id, "⚠️ Произошла ошибка, дуэль прервана.")
        await finish_duel(duel)
    finally:
        if not duel.finished:
            duel.processing = False


# ---------- Обработчики команд ----------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    if message.chat.type == "private":
        await message.answer(
            "🤠 Добро пожаловать в «Новый шериф»!\n\n"
            "Я — автоматический рефери дуэлей на Диком Западе.\n"
            "Чтобы вызвать на дуэль в группе, ответьте на сообщение соперника командой «Дуэль».\n"
            "Удачи!"
        )
    else:
        # В группах не реагируем на /start
        pass


@dp.message(F.text.lower() == "я")
async def test_response(message: Message):
    # Проверка: должен быть ответ на любое сообщение бота
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        await message.answer("Я")
    else:
        # Если просто сообщение "Я", не отвечаем
        pass


@dp.message(F.text.lower().startswith("дуэль"))
async def duel_challenge(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("Вызывать на дуэль можно только в групповом чате.")
        return

    caller = (message.from_user.id, message.from_user.full_name)
    target = None

    # 1. Ответ на сообщение
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
        if target_id == bot.id:
            await message.reply("🤠 Со мной дуэль? Я только рефери, стреляю только метафорически.")
            return
        if target_id == caller[0]:
            await message.reply("Нельзя вызвать на дуэль самого себя!")
            return
        target = (target_id, target_name)
    else:
        # 2. Поиск упоминания
        entities = message.entities or []
        mentioned_user = None
        for ent in entities:
            if ent.type == "text_mention":
                mentioned_user = (ent.user.id, ent.user.full_name)
                break
            elif ent.type == "mention":
                # username упоминание
                username = message.text[ent.offset:ent.offset+ent.length].lstrip("@")
                try:
                    chat_member = await bot.get_chat_member(message.chat.id, f"@{username}")
                    user = chat_member.user
                    mentioned_user = (user.id, user.full_name)
                    break
                except TelegramAPIError:
                    # Не найдём
                    pass
        if not mentioned_user:
            # 3. Запасной вариант: Дуэль @username (без entity, если текст введён вручную)
            parts = message.text.strip().split()
            if len(parts) > 1 and parts[1].startswith("@"):
                username = parts[1].lstrip("@")
                try:
                    chat_member = await bot.get_chat_member(message.chat.id, f"@{username}")
                    user = chat_member.user
                    mentioned_user = (user.id, user.full_name)
                except TelegramAPIError:
                    await message.reply(
                        "Не удалось найти игрока. Убедитесь, что он запускал бота, или используйте "
                        "ответ на сообщение / упоминание через @."
                    )
                    return
            else:
                await message.reply(
                    "Используйте:\n"
                    "- Ответьте на сообщение игрока командой «Дуэль»\n"
                    "- Или упомяните его через @\n"
                    "- Или напишите «Дуэль @username»"
                )
                return
        target = mentioned_user

    if target[0] == caller[0]:
        await message.reply("Нельзя вызвать на дуэль самого себя!")
        return

    # Проверка занятости
    if (message.chat.id, caller[0]) in occupied or (message.chat.id, target[0]) in occupied:
        await message.reply("Один из участников уже участвует в другой дуэли. Дождитесь завершения.")
        return

    # Создание дуэли
    duel_id = uuid4().hex[:12]
    duel = DuelState(duel_id, message.chat.id, caller, target)
    active_duels[duel_id] = duel
    occupied[(message.chat.id, caller[0])] = duel_id
    occupied[(message.chat.id, target[0])] = duel_id

    # Сообщение вызова
    text = (
        f"🔫 {caller[1]} вызывает {target[1]} на дуэль!\n"
        f"💬 {target[1]}, нажмите «Принять» или «Отклонить»\n"
        "На принятие решения у вас есть 5 минут."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{duel_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{duel_id}"),
        ]
    ])
    msg = await send_system_message(message.chat.id, text)
    if not msg:
        await finish_duel(duel)
        return

    # Таймер на 5 минут автоотмены
    async def auto_decline():
        await asyncio.sleep(300)
        d = active_duels.get(duel_id)
        if d and not d.accepted and not d.finished:
            await edit_message_safe(d.chat_id, msg.message_id, "⏰ Время вышло, вызов отменён.")
            await finish_duel(d)

    asyncio.create_task(auto_decline())


# ---------- Обработчики инлайн кнопок ----------
@dp.callback_query(F.data.startswith("accept_"))
async def accept_duel(call: CallbackQuery):
    duel_id = call.data[7:]
    duel = active_duels.get(duel_id)
    if not duel or duel.finished:
        await call.answer("Дуэль уже неактивна.", show_alert=False)
        return
    if call.from_user.id != duel.opponent[0]:
        await call.answer("Только вызванный игрок может принять дуэль.", show_alert=True)
        return
    if duel.accepted:
        await call.answer("Дуэль уже принята.")
        return

    duel.accepted = True
    # Редактируем сообщение вызова, убираем кнопки
    await edit_message_safe(duel.chat_id, call.message.message_id,
                            f"✅ {duel.opponent[1]} принял вызов {duel.initiator[1]} на дуэль", reply_markup=None)
    await call.answer()

    # Определяем первого ходящего
    first = random.choice([duel.initiator[0], duel.opponent[0]])
    first_name = duel.initiator[1] if first == duel.initiator[0] else duel.opponent[1]
    await send_system_message(duel.chat_id, f"Право первого выстрела предоставляется {first_name}")
    await switch_turn(duel, first)


@dp.callback_query(F.data.startswith("decline_"))
async def decline_duel(call: CallbackQuery):
    duel_id = call.data[8:]
    duel = active_duels.get(duel_id)
    if not duel or duel.finished:
        await call.answer("Дуэль уже неактивна.")
        return
    if call.from_user.id != duel.opponent[0]:
        await call.answer("Только вызванный игрок может отклонить дуэль.", show_alert=True)
        return

    await edit_message_safe(duel.chat_id, call.message.message_id, "❌ Дуэль отклонена.", reply_markup=None)
    await call.answer()
    await finish_duel(duel)


@dp.callback_query(F.data.startswith("act_"))
async def handle_action(call: CallbackQuery):
    parts = call.data.split("_")
    # act_shoot_<duel_id> / act_aim_<duel_id> / act_closer_<duel_id> / act_farther_<duel_id> / act_reload_<duel_id>
    if len(parts) != 3:
        return
    action = parts[1]
    duel_id = parts[2]
    duel = active_duels.get(duel_id)
    if not duel or duel.finished:
        await call.answer("Дуэль завершена.")
        return
    if call.from_user.id != duel.current_turn:
        await call.answer("Сейчас не ваш ход.", show_alert=True)
        return

    await call.answer()
    # Запускаем выполнение действия (уже с блокировкой)
    await execute_action(duel, call.from_user.id, action)


# ---------- Запуск ----------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
