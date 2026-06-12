import asyncio
import logging
import random
import uuid
import os
from dataclasses import dataclass, field
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated,
)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --------------------------
# Логирование
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------------
# Токен бота (из переменной окружения)
# --------------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не установлена")

# --------------------------
# Оружейная константа
# --------------------------
WEAPON = {
    "name": "Револьвер дуэлянта",
    "accuracy": 20,
    "crit": 5,
    "magazine": 6,
    "range": 7,
}

# --------------------------
# Данные дуэли
# --------------------------
@dataclass
class PlayerState:
    hp: int = 2
    ammo: int = 6
    aim_stacks: int = 0

@dataclass
class DuelSession:
    duel_id: str
    chat_id: int
    challenger_id: int
    target_id: int
    current_turn: int
    distance: int = 10
    players: Dict[int, PlayerState] = field(default_factory=dict)
    timer_task: Optional[asyncio.Task] = None
    current_turn_msg_id: Optional[int] = None
    processing: bool = False  # флаг защиты от параллельных действий

    def opponent(self, user_id: int) -> int:
        return self.challenger_id if user_id == self.target_id else self.target_id

# --------------------------
# Инициализация бота
# --------------------------
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# --------------------------
# Вспомогательные функции
# --------------------------
def calc_hit_chance(duel: DuelSession, shooter_id: int) -> float:
    p = duel.players[shooter_id]
    base = WEAPON["accuracy"]
    aim_bonus = (WEAPON["accuracy"] / 2) * p.aim_stacks
    distance = duel.distance
    range_w = WEAPON["range"]
    if distance < range_w / 2:
        distance_bonus = (WEAPON["accuracy"] / 2) * (range_w / 2 - distance)
    else:
        distance_bonus = 0

    penalty = 10 * (distance - range_w) if distance > range_w else 0
    chance = base + aim_bonus + distance_bonus - penalty
    return max(0.0, min(100.0, chance))

def format_player_state(duel: DuelSession, user_id: int) -> str:
    p = duel.players[user_id]
    ammo_text = f"{p.ammo}/{WEAPON['magazine']}" if p.ammo > 0 else "пуст"
    return f"❤️{p.hp}  🔫{ammo_text}  🎯{p.aim_stacks}"

def build_action_keyboard(duel: DuelSession) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    p = duel.players[duel.current_turn]
    if p.ammo > 0:
        builder.button(text="🔫 Выстрелить", callback_data=f"act_shoot_{duel.duel_id}")
    else:
        builder.button(text="🔄 Перезарядить", callback_data=f"act_reload_{duel.duel_id}")
    builder.button(text="🎯 Прицелиться", callback_data=f"act_aim_{duel.duel_id}")
    builder.button(text="👣 Ближе", callback_data=f"act_closer_{duel.duel_id}")
    builder.button(text="👣 Дальше", callback_data=f"act_farther_{duel.duel_id}")
    builder.adjust(2, 2)
    return builder.as_markup()

async def send_turn_message(duel: DuelSession):
    """Отправляет сообщение с кнопками и запускает таймер хода."""
    if duel.duel_id not in active_duels:
        return
    try:
        user = await bot.get_chat(duel.current_turn)
        name = user.full_name
    except:
        name = "Игрок"

    text = (
        f"⚡ Ход {name}\n"
        f"📏 Дистанция: {duel.distance} шагов\n"
        f"Ваши показатели: {format_player_state(duel, duel.current_turn)}\n"
        f"Противник: {format_player_state(duel, duel.opponent(duel.current_turn))}"
    )

    try:
        sent_msg = await bot.send_message(
            duel.chat_id,
            text,
            reply_markup=build_action_keyboard(duel)
        )
        duel.current_turn_msg_id = sent_msg.message_id
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение хода: {e}")
        # Пытаемся восстановиться – завершаем дуэль с ничьей
        await bot.send_message(duel.chat_id, "⚠️ Техническая ошибка, дуэль прервана.")
        await cleanup_duel(duel.duel_id)
        return

    # Отменяем старый таймер, если есть
    if duel.timer_task:
        duel.timer_task.cancel()
    # Запускаем таймер авто-пропуска хода
    duel.timer_task = asyncio.create_task(auto_skip_turn(duel, duel.current_turn_msg_id))

async def auto_skip_turn(duel: DuelSession, expected_msg_id: int):
    """Ждёт 120 секунд, и если ход всё ещё у того же игрока – передаёт ход."""
    await asyncio.sleep(120)
    # Проверяем, что дуэль всё ещё активна и с момента запуска таймера ничего не изменилось
    if duel.duel_id not in active_duels:
        return
    if duel.processing:
        # Идёт обработка действия, подождём ещё и проверим позже (однократно)
        await asyncio.sleep(10)
        if duel.duel_id not in active_duels or duel.processing:
            return
    if duel.current_turn_msg_id != expected_msg_id:
        # Уже другой ход, ничего не делаем
        return

    try:
        # Убираем кнопки у сообщения хода
        await bot.edit_message_reply_markup(
            chat_id=duel.chat_id,
            message_id=expected_msg_id,
            reply_markup=None
        )
        user = await bot.get_chat(duel.current_turn)
        name = user.full_name if user else "Игрок"
        await bot.send_message(
            duel.chat_id,
            f"⏰ {name} не сделал ход вовремя. Ход переходит противнику."
        )
    except Exception as e:
        logger.warning(f"Ошибка при пропуске хода: {e}")

    # Передаём ход, если дуэль всё ещё активна
    if duel.duel_id in active_duels:
        duel.current_turn = duel.opponent(duel.current_turn)
        await send_turn_message(duel)

async def cleanup_duel(duel_id: str):
    if duel_id not in active_duels:
        return
    duel = active_duels[duel_id]
    occupied.pop((duel.chat_id, duel.challenger_id), None)
    occupied.pop((duel.chat_id, duel.target_id), None)
    if duel.timer_task:
        duel.timer_task.cancel()
    del active_duels[duel_id]

# --------------------------
# Приветствие при добавлении в группу
# --------------------------
@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    if update.new_chat_member.status == "member" and update.chat.type != "private":
        await bot.send_message(
            update.chat.id,
            "🤠 Бот для дуэлей на Диком Западе готов к работе!\n"
            "Вызовите противника: ответьте на его сообщение командой «Дуэль»."
        )

# --------------------------
# Обработчики команд
# --------------------------
@router.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("🤠 Этот бот для дуэлей в группе. Добавьте его в чат и вызовите на дуэль по @username.")

@router.message(F.text.lower() == "бот", ~F.from_user.is_bot, F.chat.type.in_({"group", "supergroup"}))
async def bot_echo(message: Message):
    await message.reply("Я")

@router.message(F.text.lower().startswith("дуэль да"), F.chat.type.in_({"group", "supergroup"}))
async def duel_yes_text(message: Message):
    await message.reply("Чтобы принять дуэль, нажмите кнопку под вызовом.")

@router.message(F.text.lower().startswith("дуэль нет"), F.chat.type.in_({"group", "supergroup"}))
async def duel_no_text(message: Message):
    await message.reply("Чтобы отклонить дуэль, нажмите кнопку под вызовом.")

@router.message(F.text.lower().startswith("дуэль отмена"), F.chat.type.in_({"group", "supergroup"}))
async def duel_cancel_text(message: Message):
    await message.reply("Отменить дуэль можно кнопкой или командой /cancel в личных сообщениях (пока недоступно).")

# Основной вызов (reply + text_mention + @username)
@router.message(F.text.lower().startswith("дуэль"), F.chat.type.in_({"group", "supergroup"}))
async def duel_command(message: Message):
    try:
        # ----- 1. Ответ на сообщение (reply) -----
        if message.reply_to_message:
            target_user = message.reply_to_message.from_user
            if target_user.is_bot:
                await message.reply("Нельзя вызвать бота на дуэль.")
                return
            if target_user.id == message.from_user.id:
                await message.reply("Нельзя вызвать на дуэль самого себя.")
                return

            key_challenger = (message.chat.id, message.from_user.id)
            key_target = (message.chat.id, target_user.id)
            if key_challenger in occupied or key_target in occupied:
                await message.reply("Один из участников уже участвует в другой дуэли.")
                return

            duel_id = uuid.uuid4().hex[:12]
            duel = DuelSession(
                duel_id=duel_id,
                chat_id=message.chat.id,
                challenger_id=message.from_user.id,
                target_id=target_user.id,
                current_turn=0,
                players={
                    message.from_user.id: PlayerState(),
                    target_user.id: PlayerState(),
                }
            )
            active_duels[duel_id] = duel
            occupied[key_challenger] = duel_id
            occupied[key_target] = duel_id

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{duel_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{duel_id}"),
                ]
            ])
            inv_msg = await message.answer(
                f"🔫 {message.from_user.full_name} вызывает {target_user.full_name} на дуэль!\n"
                f"💬 {target_user.full_name}, нажмите «Принять» или «Отклонить»\n"
                f"На принятие решения у вас есть 5 минут.",
                reply_markup=kb
            )

            async def auto_cancel():
                await asyncio.sleep(300)
                if duel_id in active_duels:
                    try:
                        await inv_msg.edit_text("⏰ Время вышло, вызов отменён.", reply_markup=None)
                    except:
                        pass
                    await cleanup_duel(duel_id)

            asyncio.create_task(auto_cancel())
            return

        # ----- 2. Упоминание с ID (text_mention) -----
        target_user = None
        if message.entities:
            for entity in message.entities:
                if entity.type == "text_mention" and entity.user:
                    target_user = entity.user
                    break

        if target_user:
            if target_user.is_bot:
                await message.reply("Нельзя вызвать бота на дуэль.")
                return
            if target_user.id == message.from_user.id:
                await message.reply("Нельзя вызвать на дуэль самого себя.")
                return

            key_challenger = (message.chat.id, message.from_user.id)
            key_target = (message.chat.id, target_user.id)
            if key_challenger in occupied or key_target in occupied:
                await message.reply("Один из участников уже участвует в другой дуэли.")
                return

            duel_id = uuid.uuid4().hex[:12]
            duel = DuelSession(
                duel_id=duel_id,
                chat_id=message.chat.id,
                challenger_id=message.from_user.id,
                target_id=target_user.id,
                current_turn=0,
                players={
                    message.from_user.id: PlayerState(),
                    target_user.id: PlayerState(),
                }
            )
            active_duels[duel_id] = duel
            occupied[key_challenger] = duel_id
            occupied[key_target] = duel_id

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{duel_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{duel_id}"),
                ]
            ])
            inv_msg = await message.answer(
                f"🔫 {message.from_user.full_name} вызывает {target_user.full_name} на дуэль!\n"
                f"💬 {target_user.full_name}, нажмите «Принять» или «Отклонить»\n"
                f"На принятие решения у вас есть 5 минут.",
                reply_markup=kb
            )

            async def auto_cancel():
                await asyncio.sleep(300)
                if duel_id in active_duels:
                    try:
                        await inv_msg.edit_text("⏰ Время вышло, вызов отменён.", reply_markup=None)
                    except:
                        pass
                    await cleanup_duel(duel_id)

            asyncio.create_task(auto_cancel())
            return

        # ----- 3. Вызов по @username (запасной) -----
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "Чтобы вызвать на дуэль, ответьте на сообщение игрока командой «Дуэль» "
                "или упомяните его через @ (выбрав из списка, чтобы появилось имя с ID)."
            )
            return

        target_username = parts[1]
        if not target_username.startswith("@"):
            await message.reply("Используйте @username или выберите игрока из списка упоминаний.")
            return
        target_username = target_username.lstrip("@")

        try:
            target_chat = await bot.get_chat(f"@{target_username}")
            target_id = target_chat.id
        except Exception:
            await message.reply(
                "Не удалось найти игрока с таким @username. Возможно, он не начинал диалог с ботом.\n"
                "Попробуйте:\n"
                "• Ответьте на его сообщение командой «Дуэль»\n"
                "• Или введите @ и выберите его из всплывающего списка (тогда передастся ID)"
            )
            return

        if target_id == message.from_user.id:
            await message.reply("Нельзя вызвать на дуэль самого себя.")
            return

        key_challenger = (message.chat.id, message.from_user.id)
        key_target = (message.chat.id, target_id)
        if key_challenger in occupied or key_target in occupied:
            await message.reply("Один из участников уже участвует в другой дуэли.")
            return

        duel_id = uuid.uuid4().hex[:12]
        duel = DuelSession(
            duel_id=duel_id,
            chat_id=message.chat.id,
            challenger_id=message.from_user.id,
            target_id=target_id,
            current_turn=0,
            players={
                message.from_user.id: PlayerState(),
                target_id: PlayerState(),
            }
        )
        active_duels[duel_id] = duel
        occupied[key_challenger] = duel_id
        occupied[key_target] = duel_id

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{duel_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{duel_id}"),
            ]
        ])
        inv_msg = await message.answer(
            f"🔫 {message.from_user.full_name} вызывает @{target_username} на дуэль!\n"
            f"💬 @{target_username}, нажмите «Принять» или «Отклонить»\n"
            f"На принятие решения у вас есть 5 минут.",
            reply_markup=kb
        )

        async def auto_cancel():
            await asyncio.sleep(300)
            if duel_id in active_duels:
                try:
                    await inv_msg.edit_text("⏰ Время вышло, вызов отменён.", reply_markup=None)
                except:
                    pass
                await cleanup_duel(duel_id)

        asyncio.create_task(auto_cancel())

    except Exception as e:
        logger.error(f"Ошибка в duel_command: {e}", exc_info=True)
        await message.reply("Произошла ошибка при создании дуэли. Попробуйте позже.")

# --------------------------
# Принятие/отклонение
# --------------------------
@router.callback_query(F.data.startswith("accept_") | F.data.startswith("decline_"))
async def process_invite(callback: CallbackQuery):
    try:
        data = callback.data
        duel_id = data.split("_", 1)[1]

        if duel_id not in active_duels:
            await callback.answer("Дуэль уже неактивна.", show_alert=True)
            return

        duel = active_duels[duel_id]

        if callback.from_user.id != duel.target_id:
            await callback.answer("Только вызванный игрок может принять или отклонить.", show_alert=True)
            return

        if data.startswith("decline_"):
            await callback.message.edit_text("❌ Дуэль отклонена.", reply_markup=None)
            await cleanup_duel(duel_id)
            return

        await callback.message.edit_text(
            f"✅ {callback.from_user.full_name} принял вызов {(await bot.get_chat(duel.challenger_id)).full_name} на дуэль",
            reply_markup=None
        )

        duel.current_turn = random.choice([duel.challenger_id, duel.target_id])
        try:
            first_user = await bot.get_chat(duel.current_turn)
            first_name = first_user.full_name
        except:
            first_name = "Игрок"

        await bot.send_message(
            duel.chat_id,
            f"Право первого выстрела предоставляется {first_name}"
        )
        await send_turn_message(duel)

    except Exception as e:
        logger.error(f"Ошибка в process_invite: {e}", exc_info=True)
        await callback.answer("Ошибка. Попробуйте ещё раз.", show_alert=True)

# --------------------------
# Действия в дуэли
# --------------------------
@router.callback_query(F.data.startswith("act_"))
async def process_action(callback: CallbackQuery):
    try:
        data = callback.data
        _, action, duel_id = data.split("_", 2)

        if duel_id not in active_duels:
            await callback.answer("Дуэль завершена.", show_alert=True)
            return

        duel = active_duels[duel_id]

        if callback.from_user.id != duel.current_turn:
            await callback.answer("Сейчас не ваш ход.", show_alert=True)
            return

        player = duel.players[callback.from_user.id]
        opponent_id = duel.opponent(callback.from_user.id)
        opponent = duel.players[opponent_id]

        # Сохраняем текущее сообщение хода на случай ошибки
        saved_msg_id = duel.current_turn_msg_id
        saved_turn = duel.current_turn

        # Отменяем таймер текущего хода
        if duel.timer_task:
            duel.timer_task.cancel()

        # Убираем кнопки у текущего сообщения хода
        try:
            await bot.edit_message_reply_markup(
                chat_id=duel.chat_id,
                message_id=duel.current_turn_msg_id,
                reply_markup=None
            )
        except:
            pass

        extra_msg = ""
        current_name = callback.from_user.full_name
        try:
            opponent_name = (await bot.get_chat(opponent_id)).full_name
        except:
            opponent_name = "Игрок"

        if action == "shoot":
            if player.ammo <= 0:
                await callback.answer("Нет патронов!", show_alert=True)
                # Восстанавливаем кнопки, потому что ход не завершён
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=duel.chat_id,
                        message_id=duel.current_turn_msg_id,
                        reply_markup=build_action_keyboard(duel)
                    )
                except:
                    pass
                return
            player.ammo -= 1
            chance = calc_hit_chance(duel, callback.from_user.id)
            roll = random.randint(1, 100)
            if roll <= chance:
                crit_roll = random.randint(1, 100)
                if crit_roll <= WEAPON["crit"]:
                    opponent.hp = 0
                    extra_msg = f"💥 КРИТИЧЕСКОЕ ПОПАДАНИЕ! {current_name} убивает {opponent_name} наповал!"
                else:
                    opponent.hp -= 1
                    extra_msg = f"🔫🤯 Попадание! {current_name} ранит {opponent_name}."
                player.aim_stacks = 0
                opponent.aim_stacks = 0
            else:
                extra_msg = f"💨 Промах! {current_name} стреляет мимо."
                player.aim_stacks = 0

        elif action == "aim":
            player.aim_stacks += 1
            extra_msg = f"🥽 {current_name} прицеливается получше (×{player.aim_stacks})"

        elif action == "closer":
            duel.distance = max(1, duel.distance - 1)
            extra_msg = f"👣 {current_name} подходит ближе. Дистанция: {duel.distance}"

        elif action == "farther":
            duel.distance += 1
            if duel.distance > 20:
                extra_msg = f"🏃 {current_name} отходит слишком далеко и позорно сбегает!"
                opponent.hp = -1
            else:
                extra_msg = f"👣 {current_name} отдаляется. Дистанция: {duel.distance}"

        elif action == "reload":
            if player.ammo == WEAPON["magazine"]:
                await callback.answer("Магазин уже полон.", show_alert=True)
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=duel.chat_id,
                        message_id=duel.current_turn_msg_id,
                        reply_markup=build_action_keyboard(duel)
                    )
                except:
                    pass
                return
            player.ammo = WEAPON["magazine"]
            extra_msg = f"🔄 {current_name} перезаряжает оружие"

        # Отправляем результат действия
        try:
            await bot.send_message(duel.chat_id, extra_msg)
        except Exception as e:
            logger.error(f"Ошибка при отправке результата: {e}")
            # Пытаемся восстановить кнопки
            try:
                await bot.edit_message_reply_markup(
                    chat_id=duel.chat_id,
                    message_id=saved_msg_id,
                    reply_markup=build_action_keyboard(duel)
                )
            except:
                pass
            await callback.answer("Ошибка отправки сообщения. Попробуйте ещё раз.", show_alert=True)
            return

        # Проверка завершения
        winner_id = None
        loser_id = None
        if opponent.hp <= 0:
            winner_id = callback.from_user.id
            loser_id = opponent_id
        elif player.hp <= 0:
            winner_id = opponent_id
            loser_id = callback.from_user.id
        elif duel.distance > 20:
            winner_id = opponent_id
            loser_id = callback.from_user.id

        if winner_id is not None:
            try:
                winner_name = (await bot.get_chat(winner_id)).full_name
            except:
                winner_name = "Игрок"
            try:
                loser_name = (await bot.get_chat(loser_id)).full_name
            except:
                loser_name = "Игрок"
            await bot.send_message(
                duel.chat_id,
                f"🏆 Победитель: {winner_name}\n💀 Проигравший: {loser_name}"
            )
            await cleanup_duel(duel_id)
            return

        # Переход хода
        duel.current_turn = opponent_id
        try:
            await bot.send_message(
                duel.chat_id,
                f"Теперь черёд {opponent_name} делать выстрел"
            )
            await send_turn_message(duel)
        except Exception as e:
            logger.error(f"Ошибка при переходе хода: {e}")
            # Критично – пытаемся восстановить кнопки у предыдущего игрока
            duel.current_turn = saved_turn   # откатываем ход, чтобы кнопки были актуальны
            try:
                await bot.edit_message_reply_markup(
                    chat_id=duel.chat_id,
                    message_id=saved_msg_id,
                    reply_markup=build_action_keyboard(duel)
                )
            except:
                pass
            await callback.answer("Ошибка при передаче хода. Попробуйте снова.", show_alert=True)
            return

    except Exception as e:
        logger.error(f"Непредвиденная ошибка в process_action: {e}", exc_info=True)
        # Пытаемся восстановить кнопки, если это возможно
        if 'duel' in locals() and 'saved_msg_id' in locals():
            try:
                await bot.edit_message_reply_markup(
                    chat_id=duel.chat_id,
                    message_id=saved_msg_id,
                    reply_markup=build_action_keyboard(duel)
                )
            except:
                pass
        await callback.answer("Произошла ошибка. Попробуйте снова.", show_alert=True)


# --------------------------
# Запуск
# --------------------------
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
