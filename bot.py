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

    def opponent(self, user_id: int) -> int:
        return self.challenger_id if user_id == self.target_id else self.target_id

active_duels: Dict[str, DuelSession] = {}
occupied: dict[tuple[int, int], str] = {}  # исправленная аннотация

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

def format_duel_state(duel: DuelSession, for_user_id: int) -> str:
    p = duel.players[for_user_id]
    opponent_id = duel.opponent(for_user_id)
    p_opp = duel.players[opponent_id]
    ammo_text = f"{p.ammo}/{WEAPON['magazine']}" if p.ammo > 0 else "пуст"
    lines = [
        f"⚔️ Дуэль {duel.duel_id}",
        f"📏 Дистанция: {duel.distance} шагов",
        "",
        f"🤠 Ваши показатели:",
        f"  ❤️ Стойкость: {p.hp}",
        f"  🔫 Патроны: {ammo_text}",
        f"  🎯 Прицеливание: {p.aim_stacks}",
        "",
        f"👤 Противник:",
        f"  ❤️ Стойкость: {p_opp.hp}",
    ]
    return "\n".join(lines)

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

# --------------------------
# Приветствие при добавлении в группу
# --------------------------
@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    if update.new_chat_member.status == "member" and update.chat.type != "private":
        await bot.send_message(
            update.chat.id,
            "🤠 Бот для дуэлей на Диком Западе готов к работе!\n"
            "Вызовите противника: Дуэль @username"
        )

# --------------------------
# Обработчики команд
# --------------------------
@router.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("🤠 Этот бот для дуэлей в группе. Добавьте его в чат и вызовите на дуэль по @username.")

# Простой отклик на слово "Бот" (только в группах)
@router.message(F.text.lower() == "бот", ~F.from_user.is_bot, F.chat.type.in_({"group", "supergroup"}))
async def bot_echo(message: Message):
    await message.reply("Я")

# Обработчик текстовых «Дуэль да/нет/отмена» (подсказка использовать кнопки)
@router.message(F.text.lower().startswith("дуэль да"),
                F.chat.type.in_({"group", "supergroup"}))
async def duel_yes_text(message: Message):
    await message.reply("Чтобы принять дуэль, нажмите кнопку под вызовом.")

@router.message(F.text.lower().startswith("дуэль нет"),
                F.chat.type.in_({"group", "supergroup"}))
async def duel_no_text(message: Message):
    await message.reply("Чтобы отклонить дуэль, нажмите кнопку под вызовом.")

@router.message(F.text.lower().startswith("дуэль отмена"),
                F.chat.type.in_({"group", "supergroup"}))
async def duel_cancel_text(message: Message):
    await message.reply("Отменить дуэль можно кнопкой или командой /cancel в личных сообщениях (пока недоступно).")

# Основная команда вызова
@router.message(F.text.lower().startswith("дуэль"), F.chat.type.in_({"group", "supergroup"}))
async def duel_command(message: Message):
    try:
        # ----- 1. Обработка ответа на сообщение -----
        if message.reply_to_message:
            target_user = message.reply_to_message.from_user
            if target_user.is_bot:
                await message.reply("Нельзя вызвать бота на дуэль.")
                return
            if target_user.id == message.from_user.id:
                await message.reply("Нельзя вызвать на дуэль самого себя.")
                return
            # проверяем занятость
            key_challenger = (message.chat.id, message.from_user.id)
            key_target = (message.chat.id, target_user.id)
            if key_challenger in occupied or key_target in occupied:
                await message.reply("Один из участников уже участвует в другой дуэли.")
                return

            # создаём дуэль
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
            msg = await message.answer(
                f"🔫 {message.from_user.full_name} вызывает {target_user.full_name} на дуэль!\n"
                f"У вас 60 секунд, чтобы принять.",
                reply_markup=kb
            )

            async def auto_cancel():
                await asyncio.sleep(60)
                if duel_id in active_duels:
                    try:
                        await bot.edit_message_text(
                            chat_id=duel.chat_id,
                            message_id=msg.message_id,
                            text="⏰ Время вышло, вызов отменён."
                        )
                    except Exception as e:
                        logger.warning(f"Ошибка при автоотмене: {e}")
                    finally:
                        await cleanup_duel(duel_id)

            asyncio.create_task(auto_cancel())
            return   # конец обработки reply

        # ----- 2. Вызов по @username (старый код) -----
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "Укажите противника через @username или "
                "ответьте командой «Дуэль» на сообщение игрока."
            )
            return

        target_username = parts[1]
        if not target_username.startswith("@"):
            await message.reply("Используйте @username или ответьте на сообщение игрока.")
            return
        target_username = target_username.lstrip("@")

        try:
            target_user = await bot.get_chat(f"@{target_username}")
        except Exception:
            await message.reply(
                "Не удалось найти игрока с таким username.\n"
                "Попробуйте ответить командой «Дуэль» на сообщение нужного игрока."
            )
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
        msg = await message.answer(
            f"🔫 {message.from_user.full_name} вызывает @{target_username} на дуэль!\n"
            f"У вас 60 секунд, чтобы принять.",
            reply_markup=kb
        )

        async def auto_cancel():
            await asyncio.sleep(60)
            if duel_id in active_duels:
                try:
                    await bot.edit_message_text(
                        chat_id=duel.chat_id,
                        message_id=msg.message_id,
                        text="⏰ Время вышло, вызов отменён."
                    )
                except Exception as e:
                    logger.warning(f"Ошибка при автоотмене: {e}")
                finally:
                    await cleanup_duel(duel_id)

        asyncio.create_task(auto_cancel())

    except Exception as e:
        logger.error(f"Ошибка в duel_command: {e}", exc_info=True)
        await message.reply("Произошла ошибка при создании дуэли. Попробуйте позже.")

# --------------------------
# Обработка принятия/отклонения дуэли
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
            await callback.message.edit_text("❌ Дуэль отклонена.")
            await cleanup_duel(duel_id)
            return

        # Принятие
        duel.current_turn = random.choice([duel.challenger_id, duel.target_id])
        try:
            current_user = await bot.get_chat(duel.current_turn)
            name = current_user.full_name
        except:
            name = "Игрок"
        await callback.message.edit_text(
            f"⚡ Дуэль начинается!\n"
            f"Первый ход: {name}\n"
            f"{format_duel_state(duel, duel.current_turn)}",
            reply_markup=build_action_keyboard(duel)
        )
    except Exception as e:
        logger.error(f"Ошибка в process_invite: {e}", exc_info=True)
        await callback.answer("Ошибка. Попробуйте ещё раз.", show_alert=True)

# --------------------------
# Обработка действий в дуэли
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

        extra_msg = ""

        if action == "shoot":
            if player.ammo <= 0:
                await callback.answer("Нет патронов!", show_alert=True)
                return
            player.ammo -= 1
            chance = calc_hit_chance(duel, callback.from_user.id)
            roll = random.randint(1, 100)
            if roll <= chance:
                crit_roll = random.randint(1, 100)
                if crit_roll <= WEAPON["crit"]:
                    opponent.hp = 0
                    extra_msg = "💥 КРИТИЧЕСКОЕ ПОПАДАНИЕ! Противник повержен."
                else:
                    opponent.hp -= 1
                    extra_msg = "🎯 Попадание! Противник ранен."
                player.aim_stacks = 0
                opponent.aim_stacks = 0
            else:
                extra_msg = "💨 Промах!"
                player.aim_stacks = 0

        elif action == "aim":
            player.aim_stacks += 1
            extra_msg = f"🎯 Прицеливание повышено (стаков: {player.aim_stacks})."

        elif action == "closer":
            duel.distance = max(1, duel.distance - 1)
            extra_msg = f"👣 Вы подошли ближе. Дистанция: {duel.distance}."

        elif action == "farther":
            duel.distance += 1
            if duel.distance > 20:
                extra_msg = "🏃 Вы отошли слишком далеко и позорно сбежали!"
                opponent.hp = -1  # пометка для определения победителя
            else:
                extra_msg = f"👣 Вы отдалились. Дистанция: {duel.distance}."

        elif action == "reload":
            if player.ammo == WEAPON["magazine"]:
                await callback.answer("Магазин уже полон.", show_alert=True)
                return
            player.ammo = WEAPON["magazine"]
            extra_msg = "🔄 Оружие перезаряжено."

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
            extra_msg = "🏳️ Позорное бегство засчитано как поражение."

        if winner_id is not None:
            try:
                winner_name = (await bot.get_chat(winner_id)).full_name
                loser_name = (await bot.get_chat(loser_id)).full_name
            except:
                winner_name = "Игрок"
                loser_name = "Игрок"
            result_text = f"🏆 Победитель: {winner_name}\n💀 Проигравший: {loser_name}\n{extra_msg}"
            await callback.message.edit_text(result_text)
            await cleanup_duel(duel_id)
            return

        # Переход хода
        duel.current_turn = opponent_id
        new_text = format_duel_state(duel, duel.current_turn) + f"\n\n{extra_msg}"
        await callback.message.edit_text(new_text, reply_markup=build_action_keyboard(duel))
        await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка в process_action: {e}", exc_info=True)
        await callback.answer("Произошла ошибка. Попробуйте снова.", show_alert=True)

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
# Запуск
# --------------------------
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
