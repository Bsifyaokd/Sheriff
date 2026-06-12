import os
import asyncio
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Токен берётся из переменной окружения Render
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не установлен BOT_TOKEN!")

# Оружие (базовое)
WEAPON = {
    "name": "Револьвер дуэлянта",
    "accuracy": 20,
    "crit": 5,
    "magazine": 6,
    "range": 7,
}

# Структуры данных
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
occupied: Dict[(int, int), str] = {}

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# --- Вспомогательные функции ---
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
    if distance > range_w:
        penalty = 10 * (distance - range_w)
    else:
        penalty = 0
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

async def cleanup_duel(duel_id: str):
    if duel_id not in active_duels:
        return
    duel = active_duels[duel_id]
    key_challenger = (duel.chat_id, duel.challenger_id)
    key_target = (duel.chat_id, duel.target_id)
    occupied.pop(key_challenger, None)
    occupied.pop(key_target, None)
    if duel.timer_task:
        duel.timer_task.cancel()
    del active_duels[duel_id]

# --- Команды ---
@router.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("🤠 Бот для дуэлей в группе. Добавьте в чат и вызовите: Дуэль @username")

@router.message(F.text.lower() == "бот")
async def bot_echo(message: Message):
    if message.chat.type != "private":
        await message.reply("Я")

@router.message(F.text.lower().startswith("дуэль"))
async def duel_command(message: Message):
    if message.chat.type == "private":
        await message.answer("Дуэли проходят только в группах.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Укажите противника: Дуэль @username")
        return
    target_username = parts[1]
    if not target_username.startswith("@"):
        await message.reply("Используйте @username.")
        return
    target_username = target_username.lstrip("@")
    try:
        target_user = await bot.get_chat(f"@{target_username}")
    except Exception:
        await message.reply("Не удалось найти игрока с таким username. Убедитесь, что он есть в Telegram.")
        return
    if target_user.id == message.from_user.id:
        await message.reply("Нельзя вызвать на дуэль самого себя.")
        return
    key_challenger = (message.chat.id, message.from_user.id)
    key_target = (message.chat.id, target_user.id)
    if key_challenger in occupied or key_target in occupied:
        await message.reply("Один из участников уже участвует в другой дуэли.")
        return
    duel_id = f"{message.chat.id}_{message.from_user.id}_{target_user.id}_{random.randint(1000,9999)}"
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
            await bot.edit_message_text(
                chat_id=duel.chat_id,
                message_id=msg.message_id,
                text="⏰ Время вышло, вызов отменён."
            )
            await cleanup_duel(duel_id)
    asyncio.create_task(auto_cancel())

@router.callback_query(F.data.startswith("accept_") | F.data.startswith("decline_"))
async def process_invite(callback: CallbackQuery):
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
    await callback.message.edit_text(
        f"⚡ Дуэль начинается!\n"
        f"Первый ход: { (await bot.get_chat(duel.current_turn)).full_name }\n"
        f"{format_duel_state(duel, duel.current_turn)}",
        reply_markup=build_action_keyboard(duel)
    )

@router.callback_query(F.data.startswith("act_"))
async def process_action(callback: CallbackQuery):
    _, action, duel_id = callback.data.split("_", 2)
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
        duel.distance -= 1
        if duel.distance < 1:
            duel.distance = 1
        extra_msg = f"👣 Вы подошли ближе. Дистанция: {duel.distance}."
    elif action == "farther":
        duel.distance += 1
        if duel.distance > 20:
            extra_msg = "🏃 Вы отошли слишком далеко и позорно сбежали!"
            opponent.hp = -1  # пометка побега
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
        winner_name = (await bot.get_chat(winner_id)).full_name
        loser_name = (await bot.get_chat(loser_id)).full_name
        await callback.message.edit_text(f"🏆 Победитель: {winner_name}\n💀 Проигравший: {loser_name}\n{extra_msg}")
        await cleanup_duel(duel_id)
        return

    # Переход хода
    duel.current_turn = opponent_id
    new_text = format_duel_state(duel, duel.current_turn) + f"\n\n{extra_msg}"
    try:
        await callback.message.edit_text(new_text, reply_markup=build_action_keyboard(duel))
    except Exception:
        pass
    await callback.answer()

# --- Запуск ---
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
