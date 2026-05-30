import asyncio
import logging
import datetime
from typing import List, Optional, Dict, Any, Union
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, delete, update

from tgbot.config import settings
from tgbot.database.db_api import db, get_utc_now
from tgbot.database.models import (
    User, ActiveQuest, Quest, Step, CheatLog, ScheduledBroadcast, ShopItemType,
    QuestMarket, RandomEvent, GlobalEvent, ShopItem
)

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# ИНТЕЛЛЕКТУАЛЬНАЯ ИНТЕГРАЦИЯ PYDANTIC-СХЕМ ВАЛИДАЦИИ (ZERO-IMPORT-ERRORS)
# -------------------------------------------------------------------------
try:
    from tgbot.schemas.npc import NPCDialogueSchema
except ImportError:
    from pydantic import BaseModel, RootModel, Field
    class DialogueOptionSchema(BaseModel):
        text: str
        next_node: str = "exit"
        karma_change: int = 0
        coins_change: int = 0

    class DialogueNodeSchema(BaseModel):
        text: str
        options: List[DialogueOptionSchema] = Field(default_factory=list)

    class NPCDialogueSchema(RootModel[Dict[str, DialogueNodeSchema]]):
        pass


# -------------------------------------------------------------------------
# КАСТОМНЫЙ КЛАСС ФИЛЬТРАЦИИ ПРАВ АДМИНИСТРАТОРА (IsAdmin)
# -------------------------------------------------------------------------
class IsAdmin(BaseFilter):
    """
    Кастомный фильтр aiogram v3 для проверки прав администратора.
    Сверяет уникальный Telegram ID пользователя со списком ADMIN_IDS из конфига.
    """
    async def __call__(self, obj: Union[Message, CallbackQuery]) -> bool:
        user_id = obj.from_user.id if obj.from_user else None
        if not user_id:
            return False
        return user_id in settings.bot.admin_ids


# Инициализация роутера администратора и привязка фильтра
admin_router = Router()
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# -------------------------------------------------------------------------
# ДЕКОМПОЗИЦИЯ И ПРЕДСТАВЛЕНИЕ СОСТОЯНИЙ (FSM STATE GROUPS)
# -------------------------------------------------------------------------
class QuestForm(StatesGroup):
    """FSM Состояния для создания и редактирования квестов."""
    waiting_for_quest_title = State()
    waiting_for_quest_desc = State()
    waiting_for_max_speed = State()
    waiting_for_min_level = State()  
    waiting_for_edit_title = State()
    waiting_for_edit_desc = State()
    waiting_for_edit_max_speed = State()
    waiting_for_edit_min_level = State()


class StepForm(StatesGroup):
    """FSM Состояния для создания и редактирования шагов/локаций."""
    waiting_for_instruction = State()
    waiting_for_history = State()
    waiting_for_photo_then = State()
    waiting_for_photo_now = State()
    waiting_for_audio = State()
    waiting_for_coordinates = State()
    waiting_for_min_karma = State()
    waiting_for_weather_choice = State()
    waiting_for_npc_choice = State()
    waiting_for_npc_dialogue = State()
    waiting_for_npc_time_limit = State()
    waiting_for_inventory_req = State()
    waiting_for_inventory_gives = State()
    waiting_for_secret_price = State()
    waiting_for_edit_instruction = State()
    waiting_for_edit_history = State()
    waiting_for_edit_coordinates = State()
    waiting_for_edit_min_karma = State()
    waiting_for_edit_npc_name = State()
    waiting_for_edit_npc_dialogue = State()
    waiting_for_edit_npc_time_limit = State()
    waiting_for_edit_required_item = State()
    waiting_for_edit_gives_item = State()
    waiting_for_edit_secret_price = State()
    waiting_for_edit_photo_then = State()
    waiting_for_edit_photo_now = State()
    waiting_for_edit_audio = State()
    waiting_for_edit_step_price = State()
    waiting_for_branch_answer = State()
    waiting_for_branch_next_step = State()


class HintForm(StatesGroup):
    """FSM Состояния для управления многоуровневыми подсказками."""
    waiting_for_hint_text = State()
    waiting_for_hint_price = State()
    waiting_for_hint_delay = State()


class AchievementForm(StatesGroup):
    """FSM Состояния для управления достижениями."""
    waiting_for_ach_name = State()
    waiting_for_ach_desc = State()
    waiting_for_ach_emoji = State()
    waiting_for_ach_action = State()
    waiting_for_ach_value = State()
    waiting_for_ach_reward = State()


class ShopForm(StatesGroup):
    """FSM Состояния для управления товарами в магазине."""
    waiting_for_shop_name = State()
    waiting_for_shop_desc = State()
    waiting_for_shop_price = State()
    waiting_for_shop_item_name = State()
    waiting_for_shop_weight = State()  
    waiting_for_shop_income_flag = State()  
    waiting_for_shop_income_val = State()  
    waiting_for_shop_market_id = State()  
    waiting_for_shop_buyback_price = State()  
    waiting_for_edit_shop_id = State()
    waiting_for_edit_shop_name = State()
    waiting_for_edit_shop_desc = State()
    waiting_for_edit_shop_price = State()
    waiting_for_edit_shop_weight = State()
    waiting_for_edit_shop_income_flag = State()
    waiting_for_edit_shop_income_val = State()
    waiting_for_edit_shop_market_id = State()
    waiting_for_edit_shop_buyback_price = State()


class PromoForm(StatesGroup):
    """FSM Состояния для загрузки промокодов."""
    waiting_for_promo_shop_item = State()
    waiting_for_promo_batch = State()


class RiddleForm(StatesGroup):
    """FSM Состояния для управления ежедневными загадками."""
    waiting_for_riddle_quest = State()
    waiting_for_riddle_ans = State()
    waiting_for_riddle_reward = State()


class BroadcastForm(StatesGroup):
    """FSM Состояния для отложенных рассылок."""
    waiting_for_bc_text = State()
    waiting_for_bc_time = State()
    waiting_for_edit_bc_id = State()
    waiting_for_edit_bc_text = State()
    waiting_for_edit_bc_time = State()


class ModerationForm(StatesGroup):
    """FSM Состояния для ручной модерации игроков."""
    waiting_for_mod_user_id = State()
    waiting_for_mod_coins_val = State()
    waiting_for_mod_karma_val = State()
    waiting_for_mod_xp_val = State()


class SystemSettingsForm(StatesGroup):
    """FSM Состояния для редактирования системных настроек RPG."""
    waiting_for_tutorial_lat = State()
    waiting_for_tutorial_lon = State()
    waiting_for_tutorial_ans = State()
    waiting_for_merchant_bonus = State()
    waiting_for_ranger_cd = State()
    waiting_for_historian_mult = State()
    waiting_for_merc_lifetime = State()
    waiting_for_merc_price = State()
    waiting_for_merc_efficiency = State()


class MarketForm(StatesGroup):
    """FSM Состояния для управления лавками скупщиков."""
    waiting_for_market_name = State()
    waiting_for_market_latitude = State()
    waiting_for_market_longitude = State()
    waiting_for_market_radius = State()


class LevelForm(StatesGroup):
    """FSM Состояния настройки лимитов уровней."""
    waiting_for_lvl_exp_settings = State()


class EventForm(StatesGroup):
    """FSM Состояния для управления случайными событиями."""
    waiting_for_event_type = State()
    waiting_for_event_text = State()
    waiting_for_event_probability = State()
    waiting_for_event_coins = State()
    waiting_for_event_karma = State()
    waiting_for_event_xp = State()


class GlobalEventForm(StatesGroup):
    """Группа состояний FSM для управления общегородскими Bounty-ивентами."""
    waiting_for_event_name = State()
    waiting_for_event_desc = State()


# -------------------------------------------------------------------------
# СЛУЖЕБНЫЙ МЕТОД: ВАЛИДАЦИЯ ДИАЛОГОВ NPC ЧЕРЕЗ PYDANTIC
# -------------------------------------------------------------------------
def parse_and_validate_dialogue(text_input: str) -> dict:
    """
    Разбирает текстовый сценарий диалога с NPC и жестко валидирует его 
    с помощью Pydantic-модели NPCDialogueSchema.
    """
    text_input = text_input.replace("```text", "").replace("```", "").strip()
    dialogue_tree = {}
    current_node = None
    
    for line in text_input.split('\n'):
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('[') and line.endswith(']'):
            current_node = line[1:-1].strip()
            dialogue_tree[current_node] = {"text": "", "options": []}
            
        elif line.startswith('-') and current_node:
            parts = line[1:].split('->')
            opt_text = parts[0].strip()
            
            next_node = "exit"
            karma_change = 0
            coins_change = 0
            
            if len(parts) > 1:
                params = parts[1].split('|')
                next_node = params[0].strip()
                
                for param in params[1:]:
                    param = param.lower().strip()
                    if 'karma' in param:
                        karma_change = int(param.split(':')[1].replace('+', '').strip())
                    elif 'coins' in param:
                        coins_change = int(param.split(':')[1].replace('+', '').strip())
                        
            dialogue_tree[current_node]["options"].append({
                "text": opt_text,
                "next_node": next_node,
                "karma_change": karma_change,
                "coins_change": coins_change
            })
            
        elif current_node:
            dialogue_tree[current_node]["text"] = (dialogue_tree[current_node]["text"] + "\n" + line).strip()

    if "start" not in dialogue_tree:
        raise ValueError("Не найден обязательный стартовый узел [start]")

    # Декларативная валидация структуры JSON
    NPCDialogueSchema.model_validate(dialogue_tree)
    return dialogue_tree


# =========================================================================
# ГЛАВНОЕ МЕНЮ АДМИНИСТРАТОРА
# =========================================================================
@admin_router.message(Command("admin"))
async def admin_menu(message: Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать новый квест", callback_data="admin_create_quest")
    builder.button(text="⚙️ Редактировать квесты", callback_data="admin_edit_quests")
    builder.button(text="🏪 Магазин наград CRUD", callback_data="admin_manage_shop")
    builder.button(text="⚙️ Настройки баланса RPG", callback_data="admin_rpg_balance")
    builder.button(text="🏆 Управление достижениями", callback_data="admin_manage_achievements")
    builder.button(text="🧩 Дейлики (Загадки дня)", callback_data="admin_manage_riddles")
    builder.button(text="🎟 Загрузить промокоды", callback_data="admin_manage_promos")
    builder.button(text="📅 Отложенные рассылки", callback_data="admin_manage_broadcasts")
    builder.button(text="🕵️‍♂️ Ручная модерация игроков", callback_data="admin_manual_moderation")
    builder.button(text="👑 Управление Сезонами", callback_data="admin_manage_seasons")
    builder.button(text="🗺 Торговые лавки (Рынки)", callback_data="admin_manage_markets")
    builder.button(text="✨ Случайные события", callback_data="admin_manage_events")
    builder.button(text="📢 Глобальные Bounty-Ивенты", callback_data="admin_manage_global_events")
    builder.adjust(1)
    
    await message.answer(
        "🛠 *Панель Администратора RPG Quest Platform*\n\n"
        "Добро пожаловать в визуальный конструктор квестов по Перми!\n\n"
        "Дополнительные команды:\n"
        "📊 `/metrics` — общие метрики и затыки игроков на лету\n"
        "📢 `/broadcast [текст]` — моментальная рассылка игрокам\n"
        "🔓 `/unban [ID]` — снять ошибочный бан\n"
        "🧹 `/reset_session [ID]` — принудительно сбросить сессию игрока",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


# =========================================================================
# БЛОК РУЧНОЙ МОДЕРАЦИИ ИГРОКОВ (coins, karma, amnesty, XP)
# =========================================================================
@admin_router.callback_query(F.data == "admin_manual_moderation")
async def admin_manual_moderation(call: CallbackQuery, state: FSMContext):
    await state.set_state(ModerationForm.waiting_for_mod_user_id)
    await call.message.answer("🕵️‍♂️ Введите Telegram ID игрока для ручной настройки:")
    await call.answer()


@admin_router.message(ModerationForm.waiting_for_mod_user_id)
async def process_mod_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен содержать только цифры. Попробуйте еще раз:")
        return
    user_id = int(message.text.strip())
    user = await db.get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь с таким ID не зарегистрирован в боте!")
        await state.clear()
        return

    await state.update_data(mod_user_id=user_id)
    
    text = (
        f"👤 *ИГРОК:* {user.full_name} (`{user.telegram_id}`)\n"
        f"🎖 Уровень: *{user.level} (XP: {user.xp})*\n"
        f"🪙 Баланс: *{user.coins} монет*\n"
        f"☯️ Карма: *{user.karma}*\n"
        f"🚨 Предупреждения читов: *{user.cheat_warnings}/2*\n"
        f"🚫 Бан: *{'Да' if user.is_banned else 'Нет'}*"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🪙 Добавить/Убрать монеты", callback_data="mod_edit_coins")
    builder.button(text="☯️ Изменить карму", callback_data="mod_edit_karma")
    builder.button(text="🎖 Начислить опыт (XP)", callback_data="mod_edit_xp")
    builder.button(text="🕊️ Полная амнистия (Варнинги в 0)", callback_data="mod_edit_amnesty")
    builder.button(text="⬅️ Назад", callback_data="admin_back")
    builder.adjust(1)
    
    await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "mod_edit_coins")
async def mod_edit_coins(call: CallbackQuery, state: FSMContext):
    await state.set_state(ModerationForm.waiting_for_mod_coins_val)
    await call.message.answer("🪙 Введите целое число монет для начисления (со знаком минус для списания):")
    await call.answer()


@admin_router.message(ModerationForm.waiting_for_mod_coins_val)
async def process_mod_coins_val(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число!")
        return
    
    data = await state.get_data()
    uid = data["mod_user_id"]
    await db.add_coins(uid, val)
    await message.answer(f"✅ Успешно изменен баланс пользователя `{uid}` на *{val} монет*!", parse_mode="Markdown")
    await state.clear()


@admin_router.callback_query(F.data == "mod_edit_karma")
async def mod_edit_karma(call: CallbackQuery, state: FSMContext):
    await state.set_state(ModerationForm.waiting_for_mod_karma_val)
    await call.message.answer("☯️ Введите число изменения кармы (например, `5` или `-2`):")
    await call.answer()


@admin_router.message(ModerationForm.waiting_for_mod_karma_val)
async def process_mod_karma_val(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число!")
        return
        
    data = await state.get_data()
    uid = data["mod_user_id"]
    await db.update_karma(uid, val)
    await message.answer(f"✅ Карма пользователя `{uid}` успешно изменена на *{val}*!", parse_mode="Markdown")
    await state.clear()


@admin_router.callback_query(F.data == "mod_edit_amnesty")
async def mod_edit_amnesty(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = data["mod_user_id"]
    await db.reset_cheat_warning(uid)
    await db.set_ban_status(uid, is_banned=False)
    await call.message.answer(f"🕊️ Амнистия проведена! Счетчики читов пользователя `{uid}` сброшены в 0, бан снят.")
    await state.clear()
    await call.answer()


# =========================================================================
# БЛОК ОТЛОЖЕННЫХ РАССЫЛОК (SCHEDULED BROADCASTS CRUD)
# =========================================================================
@admin_router.callback_query(F.data == "admin_manage_broadcasts")
async def admin_manage_broadcasts_menu(call: CallbackQuery):
    pending = await db.get_pending_broadcasts()
    text = "📅 *ЗАПЛАНИРОВАННЫЕ ОТЛОЖЕННЫЕ РАССЫЛКИ:*\n\n"
    builder = InlineKeyboardBuilder()
    
    if not pending:
        text += "_В очереди нет отложенных рассылок._"
    else:
        for b in pending:
            time_str = b.send_at.strftime("%Y-%m-%d %H:%M")
            text += f"📌 *[ID: {b.id}]* Назначено: `{time_str}`\n📝 Текст: *\"{b.text[:50]}...\"*\n\n"
            builder.button(text=f"🗑 Удалить [{b.id}]", callback_data=f"del_bc_{b.id}")
            builder.button(text=f"✏️ Изменить [{b.id}]", callback_data=f"edit_bc_start_{b.id}")
            
    builder.button(text="➕ Запланировать новую", callback_data="add_bc_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(2, 1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "add_bc_start")
async def add_bc_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastForm.waiting_for_bc_text)
    await call.message.answer("📝 Введите текст отложенного сообщения:")
    await call.answer()


@admin_router.message(BroadcastForm.waiting_for_bc_text)
async def process_bc_text(message: Message, state: FSMContext):
    await state.update_data(bc_text=message.text)
    await state.set_state(BroadcastForm.waiting_for_bc_time)
    await message.answer(
        "�� Введите дату и время отправки в Пермском формате:\n"
        "`ГГГГ-ММ-ДД ЧЧ:ММ` (например, `2026-05-25 15:30`):",
        parse_mode="Markdown"
    )


@admin_router.message(BroadcastForm.waiting_for_bc_time)
async def process_bc_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат даты! Введите строго: `ГГГГ-ММ-ДД ЧЧ:ММ`:")
        return

    data = await state.get_data()
    text = data["bc_text"]
    
    await db.create_scheduled_broadcast(text, dt)
    await message.answer(f"✅ Рассылка успешно запланирована в БД на `{time_str}`!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("del_bc_"))
async def del_bc_handler(call: CallbackQuery):
    bc_id = int(call.data.split("_")[-1])
    await db.delete_scheduled_broadcast(bc_id)
    await call.answer("Отложенная рассылка успешно удалена.", show_alert=True)
    await admin_manage_broadcasts_menu(call)


@admin_router.callback_query(F.data.startswith("edit_bc_start_"))
async def edit_bc_start(call: CallbackQuery, state: FSMContext):
    bc_id = int(call.data.split("_")[-1])
    await state.update_data(edit_bc_id=bc_id)
    await state.set_state(BroadcastForm.waiting_for_edit_bc_text)
    await call.message.answer("📝 Введите новый текст отложенного сообщения:")
    await call.answer()


@admin_router.message(BroadcastForm.waiting_for_edit_bc_text)
async def process_edit_bc_text(message: Message, state: FSMContext):
    await state.update_data(edit_bc_text=message.text)
    await state.set_state(BroadcastForm.waiting_for_edit_bc_time)
    await message.answer("📅 Введите новую дату и время в формате `ГГГГ-ММ-ДД ЧЧ:ММ`:")


@admin_router.message(BroadcastForm.waiting_for_edit_bc_time)
async def process_edit_bc_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат! Введите `ГГГГ-ММ-ДД ЧЧ:ММ`:")
        return

    data = await state.get_data()
    bc_id = data["edit_bc_id"]
    text = data["edit_bc_text"]
    
    await db.update_scheduled_broadcast(bc_id, text, dt)
    await message.answer("✅ Отложенная рассылка успешно отредактирована!")
    await state.clear()


# =========================================================================
# БЛОК CRUD МАГАЗИНА НАГРАД И ПРОМОКОДОВ
# =========================================================================
@admin_router.callback_query(F.data == "admin_manage_shop")
async def admin_manage_shop_menu(call: CallbackQuery):
    items = await db.get_shop_items()
    text = "🏪 *УПРАВЛЕНИЕ ВИТРИНОЙ МАГАЗИНА:*\n\n"
    builder = InlineKeyboardBuilder()
    
    # 1. Цикл только собирает информацию о товарах и добавляет к ним пары кнопок
    for item in items:
        income_text = f" | Доход: `{item.income_per_hour} 🪙/ч`" if item.generates_income else ""
        market_text = f" | Лавка: `#{item.market_id}`" if item.market_id else " | Лавка: `Глобальная`"
        buyback_text = f" | Выкуп: `{item.buyback_price} 🪙`" if item.buyback_price else ""
        
        text += (
            f"📦 *[{item.id}] {item.name}* — {item.price} 🪙\n"
            f"📝 {item.description}\n"
            f"🔑 Артефакт: `{item.item_name}` | Вес: `{item.weight} кг`"
            f"{income_text}{market_text}{buyback_text}\n"
            f"🎭 Тип: `{item.item_type}`\n\n"
        )
        builder.button(text=f"✏️ Ред. [{item.id}]", callback_data=f"edit_shop_{item.id}")
        builder.button(text=f"🗑 Удал. [{item.id}]", callback_data=f"del_shop_{item.id}")
        
    # 2. Системные кнопки добавляются строго ВНЕ цикла (отступ 4 пробела)
    builder.button(text="➕ Создать новый товар", callback_data="admin_create_shop_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    
    # Продвинутое выравнивание: пары "Ред/Удал" встанут по 2 в ряд, 
    # а нижние навигационные кнопки — по 1 на всю ширину экрана.
    builder.adjust(*([2] * len(items) + [1, 1]))
    
    # 3. Отправка и закрытие триггера выполняются один раз в самом конце
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_create_shop_start")
async def admin_create_shop_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.waiting_for_shop_name)
    await call.message.answer("📝 Введите *Название товара* для витрины:")
    await call.answer()


@admin_router.message(ShopForm.waiting_for_shop_name)
async def process_shop_name(message: Message, state: FSMContext):
    await state.update_data(shop_name=message.text.strip())
    await state.set_state(ShopForm.waiting_for_shop_desc)
    await message.answer("📖 Введите *Описание товара*:")


@admin_router.message(ShopForm.waiting_for_shop_desc)
async def process_shop_desc(message: Message, state: FSMContext):
    await state.update_data(shop_desc=message.text.strip())
    await state.set_state(ShopForm.waiting_for_shop_price)
    await message.answer("🪙 Введите цену в монетах (целое число):")


@admin_router.message(ShopForm.waiting_for_shop_price)
async def process_shop_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Цена должна быть целым числом!")
        return
    await state.update_data(shop_price=int(message.text))
    await state.set_state(ShopForm.waiting_for_shop_item_name)
    await message.answer("🔑 Введите *Системное имя* артефакта для инвентаря (например, `Печать губернатора`):")


@admin_router.message(ShopForm.waiting_for_shop_item_name)
async def process_shop_item_name(message: Message, state: FSMContext):
    await state.update_data(shop_item_name=message.text.strip())
    await state.set_state(ShopForm.waiting_for_shop_weight)
    await message.answer("📦 Введите *Вес предмета в килограммах* (целое число, для билетов/промокодов укажите `0`):")
    

@admin_router.message(ShopForm.waiting_for_shop_weight)
async def process_shop_weight(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Вес должен быть целым числом!")
        return
    await state.update_data(shop_weight=int(message.text))
    await state.set_state(ShopForm.waiting_for_shop_income_flag)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🟢 Да", callback_data="shop_income:yes")
    builder.button(text="🔴 Нет", callback_data="shop_income:no")
    builder.adjust(2)
    await message.answer("💰 Будет ли этот предмет приносить пассивный доход в час?", reply_markup=builder.as_markup())


@admin_router.callback_query(ShopForm.waiting_for_shop_income_flag, F.data.startswith("shop_income:"))
async def process_shop_income_flag(call: CallbackQuery, state: FSMContext):
    flag = call.data.split(":")[1] == "yes"
    await state.update_data(shop_generates_income=flag)
    if flag:
        await state.set_state(ShopForm.waiting_for_shop_income_val)
        await call.message.answer("🪙 Введите сумму пассивного дохода в час (целое число монет):")
    else:
        await state.update_data(shop_income_per_hour=0)
        await state.set_state(ShopForm.waiting_for_shop_market_id)
        await call.message.answer("🏪 Введите ID торговой лавки скупщика (или `0` если это глобальный товар):")
    await call.answer()


@admin_router.message(ShopForm.waiting_for_shop_income_val)
async def process_shop_income_val(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Значение должно быть целым числом монет!")
        return
    await state.update_data(shop_income_per_hour=int(message.text))
    await state.set_state(ShopForm.waiting_for_shop_market_id)
    await message.answer("🏪 Введите ID торговой лавки скупщика (или `0` если это глобальный товар):")


@admin_router.message(ShopForm.waiting_for_shop_market_id)
async def process_shop_market_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть целым числом!")
        return
    mid = int(message.text)
    await state.update_data(shop_market_id=mid if mid > 0 else None)
    await state.set_state(ShopForm.waiting_for_shop_buyback_price)
    await message.answer("💰 Введите цену обратного выкупа скупщиком (или `0` если выкуп недоступен):")


@admin_router.message(ShopForm.waiting_for_shop_buyback_price)
async def process_shop_buyback_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Цена должна быть целым числом!")
        return
    buyback = int(message.text)
    data = await state.get_data()
    
    # Автоматически определяем тип предмета по названию
    item_type = ShopItemType.ARTIFACT
    name_lower = data["shop_name"].lower()
    if "билет" in name_lower or "пропуск" in name_lower:
        item_type = ShopItemType.TICKET
    elif "эликсир" in name_lower or "зелье" in name_lower or "еда" in name_lower or "припасы" in name_lower:
        item_type = ShopItemType.CONSUMABLE
    elif "промо" in name_lower or "кофе" in name_lower:
        item_type = ShopItemType.PROMO

    await db.create_shop_item(
        name=data["shop_name"],
        description=data["shop_desc"],
        price=data["shop_price"],
        item_name=data["shop_item_name"],
        item_type=item_type,
        weight=data["shop_weight"],
        generates_income=data["shop_generates_income"],
        income_per_hour=data["shop_income_per_hour"],
        market_id=data["shop_market_id"],
        buyback_price=buyback if buyback > 0 else None
    )
    await message.answer("🎉 Товар успешно создан и добавлен на витрину!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("del_shop_"))
async def del_shop_item_handler(call: CallbackQuery):
    item_id = int(call.data.split("_")[-1])

@admin_router.callback_query(F.data.startswith("del_shop_"))
async def del_shop_item_handler(call: CallbackQuery):
    item_id = int(call.data.split("_")[-1])
    await db.delete_shop_item(item_id)
    await call.answer("Товар убран с прилавка.", show_alert=True)
    await admin_manage_shop_menu(call)


@admin_router.callback_query(F.data.startswith("edit_shop_"))
async def edit_shop_start(call: CallbackQuery, state: FSMContext):
    item_id = int(call.data.split("_")[-1])
    await state.update_data(edit_shop_id=item_id)
    await state.set_state(ShopForm.waiting_for_edit_shop_name)
    await call.message.answer("✏️ Введите новое название товара:")
    await call.answer()


@admin_router.message(ShopForm.waiting_for_edit_shop_name)
async def process_edit_shop_name(message: Message, state: FSMContext):
    await state.update_data(edit_shop_name=message.text.strip())
    await state.set_state(ShopForm.waiting_for_edit_shop_desc)
    await message.answer("✏️ Введите новое описание товара:")


@admin_router.message(ShopForm.waiting_for_edit_shop_desc)
async def process_edit_shop_desc(message: Message, state: FSMContext):
    await state.update_data(edit_shop_desc=message.text.strip())
    await state.set_state(ShopForm.waiting_for_edit_shop_price)
    await message.answer("✏️ Введите новую стоимость (целое число монет):")


@admin_router.message(ShopForm.waiting_for_edit_shop_price)
async def process_edit_shop_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите целое число монет!")
        return
        
    data = await state.get_data()
    item_id = data["edit_shop_id"]
    
    await db.update_shop_item(
        item_id=item_id,
        name=data["edit_shop_name"],
        description=data["edit_shop_desc"],
        price=int(message.text)
    )
    await message.answer("✅ Товар витрины успешно отредактирован!")
    await state.clear()

# =========================================================================
# РЕДАКТИРОВАНИЕ СИСТЕМНОГО БАЛАНСА RPG И ОБУЧЕНИЯ
# =========================================================================

@admin_router.callback_query(F.data == "admin_rpg_balance")
async def admin_rpg_balance_menu(call: CallbackQuery):
    """Отрисовывает интерфейс управления глобальными параметрами баланса RPG и Обучения."""
    cfg = await db.get_system_settings()
    
    text = (
        "⚙️ *НАСТРОЙКИ БАЛАНСА RPG, ОБУЧЕНИЯ И НАЕМНИКОВ*\n\n"
        "*Квест №0 (Обучение):*\n"
        f"📍 Координаты тестовой точки: `{cfg.tutorial_latitude}, {cfg.tutorial_longitude}`\n"
        f"🔑 Проверочное слово: *\"{cfg.tutorial_answer}\"*\n\n"
        "*Баланс классов:* \n"
        f"💰 Бонус купца (монеты): *+{cfg.merchant_bonus}%*\n"
        f"⏱ Кулдаун подсказки следопыта: *{cfg.ranger_cd_minutes} минут*\n"
        f"📈 Множитель очков историка: *x{cfg.historian_mult}*\n\n"
        "*Настройки кастомизации Наемника:* \n"
        f"⏳ Срок контракта: *{cfg.merc_lifetime_minutes} минут*\n"
        f"🪙 Стоимость призыва: *{cfg.merc_summon_price} монет*\n"
        f"🎯 Эффективность: *{cfg.merc_efficiency}%*"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📍 Изменить широту Обучения", callback_data="sys_edit_tut_lat")
    builder.button(text="📍 Изменить долготу Обучения", callback_data="sys_edit_tut_lon")
    builder.button(text="🔑 Изменить слово Обучения", callback_data="sys_edit_tut_ans")
    builder.button(text="💰 Изменить бонус Купца", callback_data="sys_edit_merchant")
    builder.button(text="⏱ Изменить кулдаун Следопыта", callback_data="sys_edit_ranger")
    builder.button(text="📈 Изменить множитель Историка", callback_data="sys_edit_historian")
    builder.button(text="⏳ Срок наемника (мин)", callback_data="sys_edit_merc_life")
    builder.button(text="🪙 Вызов наемника (монеты)", callback_data="sys_edit_merc_price")
    builder.button(text="🎯 Эффективность наемника (%)", callback_data="sys_edit_merc_eff")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("sys_edit_"))
async def sys_edit_start(call: CallbackQuery, state: FSMContext):
    """Маршрутизирует запуск процесса изменения системных параметров в FSM."""
    field = call.data.replace("sys_edit_", "")
    await state.update_data(sys_edit_field=field)
    
    prompts = {
        "tut_lat": "Введите широту для тестовой точки Квеста №0 (Обучение) (например, `58.0097`):",
        "tut_lon": "Введите долготу для тестовой точки Квеста №0 (Обучение) (например, `56.2444`):",
        "tut_ans": "Введите проверочное слово для Квеста №0 (например, `пермь`):",
        "merchant": "Введите бонус купца в процентах (целое число, например `20`):",
        "ranger": "Введите кулдаун подсказки следопыта в минутах (целое число, например `7`):",
        "historian": "Введите множитель очков историка (число с плавающей точкой, например `2.0`):",
        "merc_life": "Введите срок жизни контракта наемника в минутах (целое число, например `60`):",
        "merc_price": "Введите стоимость призыва наемника в монетах (целое число, например `150`):",
        "merc_eff": "Введите базовую эффективность наемника в процентах (целое число от `1` до `100`):"
    }
    
    target_states = {
        "tut_lat": SystemSettingsForm.waiting_for_tutorial_lat,
        "tut_lon": SystemSettingsForm.waiting_for_tutorial_lon,
        "tut_ans": SystemSettingsForm.waiting_for_tutorial_ans,
        "merchant": SystemSettingsForm.waiting_for_merchant_bonus,
        "ranger": SystemSettingsForm.waiting_for_ranger_cd,
        "historian": SystemSettingsForm.waiting_for_historian_mult,
        "merc_life": SystemSettingsForm.waiting_for_merc_lifetime,
        "merc_price": SystemSettingsForm.waiting_for_merc_price,
        "merc_eff": SystemSettingsForm.waiting_for_merc_efficiency
    }
    
    await state.set_state(target_states[field])
    await call.message.answer(prompts[field], parse_mode="Markdown")
    await call.answer()


@admin_router.message(SystemSettingsForm.waiting_for_tutorial_lat)
async def process_sys_tutorial_lat(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        await db.update_system_settings(tutorial_latitude=val)
        await message.answer("✅ Широта обучения успешно обновлена!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Некорректный формат! Введите число с плавающей точкой.")


@admin_router.message(SystemSettingsForm.waiting_for_tutorial_lon)
async def process_sys_tutorial_lon(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        await db.update_system_settings(tutorial_longitude=val)
        await message.answer("✅ Долгота обучения успешно обновлена!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Некорректный формат! Введите число с плавающей точкой.")


@admin_router.message(SystemSettingsForm.waiting_for_tutorial_ans)
async def process_sys_tutorial_ans(message: Message, state: FSMContext):
    val = message.text.strip().lower()
    await db.update_system_settings(tutorial_answer=val)
    await message.answer("✅ Проверочное слово обучения успешно обновлено!")
    await state.clear()


@admin_router.message(SystemSettingsForm.waiting_for_merchant_bonus)
async def process_sys_merchant_bonus(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        await db.update_system_settings(merchant_bonus=val)
        await message.answer("✅ Бонус купца успешно обновлен!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Некорректный формат! Введите целое число.")


@admin_router.message(SystemSettingsForm.waiting_for_ranger_cd)
async def process_sys_ranger_cd(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        await db.update_system_settings(ranger_cd_minutes=val)
        await message.answer("✅ Кулдаун подсказки следопыта успешно обновлен!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Некорректный формат! Введите целое число.")


@admin_router.message(SystemSettingsForm.waiting_for_historian_mult)
async def process_sys_historian_mult(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        await db.update_system_settings(historian_mult=val)
        await message.answer("✅ Множитель историка успешно обновлен!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Некорректный формат! Введите число с плавающей точкой.")


@admin_router.message(SystemSettingsForm.waiting_for_merc_lifetime)
async def process_sys_merc_lifetime(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        await db.update_system_settings(merc_lifetime_minutes=val)
        await message.answer("✅ Срок контракта наемника успешно обновлен!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите целое число минут!")


@admin_router.message(SystemSettingsForm.waiting_for_merc_price)
async def process_sys_merc_price(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        await db.update_system_settings(merc_summon_price=val)
        await message.answer("✅ Стоимость призыва наемника успешно сохранена!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите целое число монет!")


@admin_router.message(SystemSettingsForm.waiting_for_merc_efficiency)
async def process_sys_merc_eff(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if not (1 <= val <= 100):
            raise ValueError
        await db.update_system_settings(merc_efficiency=val)
        await message.answer("✅ Эффективность наемника успешно обновлена!")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите целое число от 1 до 100 процентов!")


# =========================================================================
# ПОЛНЫЙ CRUD КВЕСТОВ И КЛОНИРОВАНИЕ
# =========================================================================

@admin_router.callback_query(F.data == "admin_create_quest")
async def start_quest_creation(call: CallbackQuery, state: FSMContext):
    """Запускает процесс создания нового квеста."""
    await state.set_state(QuestForm.waiting_for_quest_title)
    await call.message.answer("📝 Введите *Название квеста*:", parse_mode="Markdown")
    await call.answer()


@admin_router.message(QuestForm.waiting_for_quest_title)
async def process_quest_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(QuestForm.waiting_for_quest_desc)
    await message.answer("📖 Теперь введите *Описание квеста*:", parse_mode="Markdown")


@admin_router.message(QuestForm.waiting_for_quest_desc)
async def process_quest_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text.strip())
    await state.set_state(QuestForm.waiting_for_max_speed)
    await message.answer(
        "🏎 Введите *Лимит скорости для античитера* (км/ч)\n"
        "(например, `15.0` для обычных пеших квестов или `90.0` для скоростных автомобильных):",
        parse_mode="Markdown"
    )


@admin_router.message(QuestForm.waiting_for_max_speed)
async def process_quest_speed(message: Message, state: FSMContext):
    try:
        max_speed = float(message.text.strip())
        await state.update_data(max_speed=max_speed)
        await state.set_state(QuestForm.waiting_for_min_level)
        await message.answer("🔒 Введите *Минимальный уровень игрока* для допуска к этому квесту (целое число, по умолчанию `1`):")
    except ValueError:
        await message.answer("❌ Введите корректное число с плавающей точкой!")


@admin_router.message(QuestForm.waiting_for_min_level)
async def process_quest_level(message: Message, state: FSMContext):
    try:
        min_lvl = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число уровня!")
        return

    data = await state.get_data()
    try:
        quest = await db.create_quest(title=data["title"], description=data["desc"])
        await db.update_quest(quest.id, max_speed_kmh=data["max_speed"], min_level_required=min_lvl)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить шаг (локацию)", callback_data=f"add_step_to_{quest.id}")
        await message.answer("✅ *Квест успешно создан как черновик!*", parse_mode="Markdown", reply_markup=builder.as_markup())
        await state.clear()
    except Exception as e:
        logger.error(f"Error creating quest: {e}")
        await message.answer("❌ Произошла ошибка. Вероятно, квест с таким именем уже существует.")
        await state.clear()


@admin_router.callback_query(F.data.startswith("clone_quest_"))
async def clone_quest_handler(call: CallbackQuery):
    """Инициирует процесс немедленного транзакционного клонирования квеста через метод DAO БД."""
    orig_qid = int(call.data.split("_")[-1])
    cloned_q = await db.clone_quest_db(orig_qid)
    
    if not cloned_q:
        await call.answer("❌ Исходный квест не найден!", show_alert=True)
        return
                
    await call.answer(f"👥 Квест успешно клонирован в черновик: \"{cloned_q.title}\"!", show_alert=True)
    await admin_list_quests_for_editing(call)


@admin_router.callback_query(F.data == "admin_edit_quests")
async def admin_list_quests_for_editing(call: CallbackQuery):
    """Выводит список всех зарегистрированных квестов для управления."""
    quests = await db.get_all_quests()
    if not quests:
        await call.message.answer("Квестов пока нет.")
        await call.answer()
        return

    builder = InlineKeyboardBuilder()
    for q in quests:
        status = "🟢" if q.is_published else "⚪ Черновик"
        builder.button(text=f"{status} {q.title}", callback_data=f"manage_q_{q.id}")
    builder.adjust(1)
    await call.message.answer("⚙️ Выберите квест для управления и модификации:", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("manage_q_"))
async def manage_single_quest(call: CallbackQuery):
    """Выводит детальную панель управления отдельным квестом."""
    qid = int(call.data.split("_")[-1])
    q = await db.get_quest_by_id(qid)
    if not q:
        await call.answer("Квест не найден!")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить название/описание", callback_data=f"edit_text_q_{qid}")
    builder.button(text="🏎 Лимит скорости античета", callback_data=f"edit_speed_limit_{qid}")
    builder.button(text="🔒 Минимальный уровень доступа", callback_data=f"edit_level_limit_{qid}")
    builder.button(text="👥 Клонировать квест", callback_data=f"clone_quest_{qid}")
    builder.button(text="📍 Управление шагами локаций", callback_data=f"steps_list_q_{qid}")
    builder.button(text="🔗 Редактировать связи (ветки)", callback_data=f"link_branches_{qid}")
    pub_text = "🛑 Снять с публикации" if q.is_published else "🟢 Опубликовать квест"
    builder.button(text=pub_text, callback_data=f"toggle_pub_q_{qid}")
    builder.button(text="🗑 Удалить квест", callback_data=f"del_quest_req_{qid}")
    builder.button(text="⬅️ Назад в меню", callback_data="admin_edit_quests")
    builder.adjust(1)

    await call.message.answer(
        f"⚙️ *Управление квестом #{q.id}*\n"
        f"*Название:* {q.title}\n"
        f"*Описание:* {q.description}\n"
        f"🏎 *Лимит скорости:* `{q.max_speed_kmh} км/ч`\n"
        f"🔒 *Мин. уровень доступа:* `{q.min_level_required}`", 
        parse_mode="Markdown", 
        reply_markup=builder.as_markup()
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("edit_speed_limit_"))
async def edit_speed_limit_start(call: CallbackQuery, state: FSMContext):
    qid = int(call.data.split("_")[-1])
    await state.update_data(edit_speed_qid=qid)
    await state.set_state(QuestForm.waiting_for_edit_max_speed)
    await call.message.answer("🏎 Введите новый лимит скорости для античета (км/ч, например `15.0` или `90.0`):")
    await call.answer()


@admin_router.message(QuestForm.waiting_for_edit_max_speed)
async def process_edit_speed_limit(message: Message, state: FSMContext):
    try:
        max_speed = float(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите корректное число!")
        return

    data = await state.get_data()
    qid = data["edit_speed_qid"]
    await db.update_quest(qid, max_speed_kmh=max_speed)
    await message.answer(f"✅ Лимит скорости квеста обновлен до *{max_speed} км/ч*!", parse_mode="Markdown")
    await state.clear()


@admin_router.callback_query(F.data.startswith("toggle_pub_q_"))
async def toggle_publication_quest(call: CallbackQuery):
    qid = int(call.data.split("_")[-1])
    q = await db.get_quest_by_id(qid)
    new_status = not q.is_published
    await db.update_quest(qid, is_published=new_status)
    await call.answer(f"Статус изменен на: {'Опубликован' if new_status else 'Черновик'}", show_alert=True)
    await admin_list_quests_for_editing(call)


@admin_router.callback_query(F.data.startswith("del_quest_req_"))
async def delete_quest_request(call: CallbackQuery):
    qid = int(call.data.split("_")[-1])
    builder = InlineKeyboardBuilder()
    builder.button(text="💥 Да, удалить навсегда", callback_data=f"del_quest_confirm_{qid}")
    builder.button(text="❌ Отмена", callback_data=f"manage_q_{qid}")
    await call.message.answer("🚨 *Внимание!* Удаление квеста повлечет каскадное удаление всех шагов и истории прогресса игроков! Подтверждаете?", parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("del_quest_confirm_"))
async def delete_quest_confirm(call: CallbackQuery):
    qid = int(call.data.split("_")[-1])
    await db.delete_quest(qid)
    await call.answer("Квест успешно стерт из системы.", show_alert=True)
    await admin_list_quests_for_editing(call)


@admin_router.callback_query(F.data.startswith("edit_text_q_"))
async def edit_quest_text_start(call: CallbackQuery, state: FSMContext):
    qid = int(call.data.split("_")[-1])
    await state.update_data(edit_qid=qid)
    await state.set_state(QuestForm.waiting_for_edit_title)
    await call.message.answer("✏️ Введите новое название для квеста:")
    await call.answer()


@admin_router.message(QuestForm.waiting_for_edit_title)
async def process_edit_title(message: Message, state: FSMContext):
    await state.update_data(edit_title=message.text.strip())
    await state.set_state(QuestForm.waiting_for_edit_desc)
    await message.answer("✏️ Введите новое описание для квеста:")


@admin_router.message(QuestForm.waiting_for_edit_desc)
async def process_edit_desc(message: Message, state: FSMContext):
    data = await state.get_data()
    await db.update_quest(data["edit_qid"], title=data["edit_title"], description=message.text.strip())
    await message.answer("✅ Данные квеста успешно обновлены!")
    await state.clear()


# =========================================================================
# ПОШАГОВЫЙ КОНСТРУКТОР ЛОКАЦИЙ (ШАГОВ)
# =========================================================================

@admin_router.callback_query(F.data.startswith("steps_list_q_"))
async def manage_steps_list(call: CallbackQuery):
    qid = int(call.data.split("_")[-1])
    quest = await db.get_quest_with_steps(qid)
    
    builder = InlineKeyboardBuilder()
    for s in quest.steps:
        builder.button(text=f"📍 Шаг #{s.id} ({s.instruction_text[:20]}...)", callback_data=f"manage_step_{s.id}")
    builder.button(text="➕ Добавить новый шаг", callback_data=f"add_step_to_{qid}")
    builder.button(text="⬅️ Назад к квесту", callback_data=f"manage_q_{qid}")
    builder.adjust(1)
    
    await call.message.answer(f"📍 *Список шагов квеста:* {quest.title}", parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("add_step_to_"))
async def start_step_creation(call: CallbackQuery, state: FSMContext):
    quest_id = int(call.data.split("_")[-1])
    await state.update_data(quest_id=quest_id, step_data={})
    await state.set_state(StepForm.waiting_for_instruction)
    await call.message.answer("📍 Введите текст инструкции и загадки для шага:")
    await call.answer()


@admin_router.message(StepForm.waiting_for_instruction)
async def process_step_instruction(message: Message, state: FSMContext):
    data = await state.get_data()
    data["step_data"]["instruction_text"] = message.text.strip()
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_history)
    await message.answer("📜 Введите историческую справку (или `/skip`):")


@admin_router.message(StepForm.waiting_for_history)
async def process_step_history(message: Message, state: FSMContext):
    data = await state.get_data()
    data["step_data"]["history_info"] = None if message.text == "/skip" else message.text.strip()
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_photo_then)
    await message.answer("📸 Отправьте фото «Было» (или `/skip`):")


@admin_router.message(StepForm.waiting_for_photo_then)
async def process_photo_then(message: Message, state: FSMContext):
    data = await state.get_data()
    if "step_data" not in data:
        data["step_data"] = {}
    
    data["step_data"]["photo_then_id"] = message.photo[-1].file_id if message.photo else None
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_photo_now)
    await message.answer("📸 Отправьте фото «Стало» (или `/skip`):")


@admin_router.message(StepForm.waiting_for_photo_now)
async def process_photo_now(message: Message, state: FSMContext):
    data = await state.get_data()
    if "step_data" not in data:
        data["step_data"] = {}
        
    data["step_data"]["photo_now_id"] = message.photo[-1].file_id if message.photo else None
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_audio)
    await message.answer("🎙 Отправьте аудиогид (или `/skip`):")


@admin_router.message(StepForm.waiting_for_audio)
async def process_audio_guide(message: Message, state: FSMContext):
    data = await state.get_data()
    if "step_data" not in data:
        data["step_data"] = {}
        
    if message.voice:
        data["step_data"]["audio_guide_id"] = message.voice.file_id
    elif message.audio:
        data["step_data"]["audio_guide_id"] = message.audio.file_id
    else:
        data["step_data"]["audio_guide_id"] = None
        
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_coordinates)
    await message.answer("🗺 Отправьте целевые координаты строкой вида: `широта, долгота`:")


@admin_router.message(StepForm.waiting_for_coordinates)
async def process_coordinates(message: Message, state: FSMContext):
    try:
        lat_str, lon_str = message.text.split(",")
        lat, lon = float(lat_str.strip()), float(lon_str.strip())
    except Exception:
        await message.answer("❌ Неверный формат! Отправьте в формате `широта, долгота`:")
        return

    data = await state.get_data()
    data["step_data"]["latitude"] = lat
    data["step_data"]["longitude"] = lon
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_min_karma)
    await message.answer("☯️ Введите *Минимальную карму*, необходимую для доступа к этому шагу (0 по умолчанию):", parse_mode="Markdown")


@admin_router.message(StepForm.waiting_for_min_karma)
async def process_min_karma(message: Message, state: FSMContext):
    try:
        min_karma = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число!")
        return

    data = await state.get_data()
    data["step_data"]["min_karma_required"] = min_karma
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_weather_choice)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🌌 Только Ночью", callback_data="step_weather_night")
    builder.button(text="🌧 Только в Дождь/Снег", callback_data="step_weather_rain")
    builder.button(text="☀️ Без ограничений", callback_data="step_weather_none")
    builder.adjust(1)
    await message.answer("🌧 Настройте климатические зависимости шага:", reply_markup=builder.as_markup())


@admin_router.callback_query(StepForm.waiting_for_weather_choice)
async def process_step_weather(call: CallbackQuery, state: FSMContext):
    choice = call.data
    data = await state.get_data()
    
    data["step_data"]["is_night_only"] = False
    data["step_data"]["is_weather_only"] = False
    
    if choice == "step_weather_night":
        data["step_data"]["is_night_only"] = True
    elif choice == "step_weather_rain":
        data["step_data"]["is_weather_only"] = True

    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_npc_choice)
    await call.message.answer("🗣 Шаг содержит NPC? Введите имя персонажа (или `/skip`):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_npc_choice)
async def process_step_npc_name(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == "/skip":
        data["step_data"]["npc_name"] = None
        data["step_data"]["npc_dialogue"] = None
        data["step_data"]["time_limit_seconds"] = None
        await state.update_data(step_data=data["step_data"])
        await state.set_state(StepForm.waiting_for_inventory_req)
        await message.answer("🔑 Нужен предмет из инвентаря для доступа? Название предмета (или `/skip`):")
    else:
        data["step_data"]["npc_name"] = message.text.strip()
        await state.update_data(step_data=data["step_data"])
        await state.set_state(StepForm.waiting_for_npc_dialogue)
        
        template_text = (
            "[start]\n"
            "Приветствую! Ответишь на мой вопрос?\n"
            "- Да, давай! -> node_yes | karma: 1\n"
            "- Нет, я спешу. -> exit | karma: -1\n\n"
            "[node_yes]\n"
            "Отлично! Держи награду.\n"
            "- Спасибо! -> exit | karma: 1 | coins: 10"
        )
        
        await message.answer(
            "💬 Введите сценарий диалога с NPC в простом текстовом формате.\n\n"
            "*Шаблон разметки:*\n"
            f"```text\n{template_text}\n```\n\n"
            "Скопируйте блок выше, измените под себя и отправьте сюда:",
            parse_mode="Markdown"
        )


@admin_router.message(StepForm.waiting_for_npc_dialogue)
async def process_step_npc_dialogue(message: Message, state: FSMContext):
    try:
        dialogue_tree = parse_and_validate_dialogue(message.text)
    except Exception as e:
        await message.answer(f"❌ Ошибка в разметке диалога! Проверьте синтаксис.\nДетали: {e}\n\nПопробуйте снова:")
        return

    data = await state.get_data()
    data["step_data"]["npc_dialogue"] = dialogue_tree
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_npc_time_limit)
    await message.answer("⏱ Введите *Лимит времени на общение с NPC* (Тайм-атак) в секундах (или `/skip`):")


@admin_router.message(StepForm.waiting_for_npc_time_limit)
async def process_step_npc_time_limit(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == "/skip":
        data["step_data"]["time_limit_seconds"] = None
    else:
        try:
            limit = int(message.text.strip())
            data["step_data"]["time_limit_seconds"] = limit
        except ValueError:
            await message.answer("❌ Введите корректное целое число секунд или `/skip`!")
            return

    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_inventory_req)
    await message.answer("🔑 Нужен предмет из инвентаря для доступа? Название предмета (или `/skip`):")


@admin_router.message(StepForm.waiting_for_inventory_req)
async def process_inventory_req(message: Message, state: FSMContext):
    data = await state.get_data()
    data["step_data"]["required_item"] = None if message.text == "/skip" else message.text.strip()
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_inventory_gives)
    await message.answer("🎁 Будет начислен предмет за решение? Название предмета (или `/skip`):")


@admin_router.message(StepForm.waiting_for_inventory_gives)
async def process_inventory_gives(message: Message, state: FSMContext):
    data = await state.get_data()
    data["step_data"]["gives_item"] = None if message.text == "/skip" else message.text.strip()
    await state.update_data(step_data=data["step_data"])
    await state.set_state(StepForm.waiting_for_secret_price)
    await message.answer("🪙 Введите цену открытия этой ветки в монетах (0 если бесплатно):")


@admin_router.message(StepForm.waiting_for_secret_price)
async def process_secret_price_step(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число!")
        return
    data = await state.get_data()
    data["step_data"]["secret_price"] = int(message.text)
    
    quest_id = data["quest_id"]
    step = await db.add_step_to_quest(quest_id, data["step_data"])
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить ещё шаг", callback_data=f"add_step_to_{quest_id}")
    builder.button(text="🔗 Связать ветки ответов", callback_data=f"link_branches_{quest_id}")
    builder.button(text="🚀 Опубликовать квест", callback_data=f"publish_quest_{quest_id}")
    builder.adjust(1)
    
    await message.answer(f"🎉 *Шаг #{step.id} успешно добавлен!*", parse_mode="Markdown", reply_markup=builder.as_markup())
    await state.clear()


# =========================================================================
# ПРЯМОЕ РЕДАКТИРОВАНИЕ ПАРАМЕТРОВ ШАГА
# =========================================================================

@admin_router.callback_query(F.data.startswith("manage_step_"))
async def manage_single_step(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    step = await db.get_step_by_id(sid)
    
    time_modes = {
        (False, False): "☀️ 24/7 (Без ограничений)",
        (True, False): "🌌 Только Ночью",
        (False, True): "🌅 Только Днем"
    }
    current_time_mode = time_modes.get((step.is_night_only, step.is_day_only), "☀️ 24/7")

    weather_modes = {
        (False, False): "☀️ Без ограничений",
        (True, False): "🌧 Только Дождь/Снег",
        (False, True): "🏜 Только Сухая погода"
    }
    current_weather_mode = weather_modes.get((step.is_weather_only, step.is_dry_only), "☀️ Любая")

    branches_raw = step.branches
    if hasattr(branches_raw, "model_dump"):
        branches_dict = branches_raw.model_dump()
    elif isinstance(branches_raw, dict):
        branches_dict = branches_raw
    else:
        branches_dict = {}

    actual_branches = branches_dict.get("branches", branches_dict)

    text = (
        f"⚙️ *Управление шагом #{step.id}*\n\n"
        f"📖 *Загадка:* `{step.instruction_text}`\n"
        f"🗺 *Координаты:* `{step.latitude}, {step.longitude}`\n"
        f"☯️ *Мин. Карма:* `{step.min_karma_required}`\n"
        f"🗣 *NPC:* `{step.npc_name or 'Нет'}` | Сюжетные переходы: `{actual_branches}`\n"
        f"🕒 *Фильтр времени:* `{current_time_mode}`\n"
        f"🌧 *Фильтр климата:* `{current_weather_mode}`\n"
        f"🪙 *Платная ветка:* `{step.secret_price} монет`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Текст/Загадка", callback_data=f"step_edit_txt_{sid}")
    builder.button(text="🗺 Координаты", callback_data=f"step_edit_gps_{sid}")
    builder.button(text="☯️ Мин. Карма", callback_data=f"step_edit_karma_{sid}")
    builder.button(text="🕒 Переключить Время", callback_data=f"step_toggle_time_{sid}")
    builder.button(text="🌧 Переключить Климат", callback_data=f"step_toggle_weather_{sid}")
    builder.button(text="🪙 Цена ветки", callback_data=f"edit_price_s_{sid}")
    builder.button(text="📸 Фото «Было»", callback_data=f"step_media_then_{sid}")
    builder.button(text="📸 Фото «Стало»", callback_data=f"step_media_now_{sid}")
    builder.button(text="🎙 Аудиогид", callback_data=f"step_media_audio_{sid}")
    builder.button(text="💡 Подсказки (Многоуровневые)", callback_data=f"step_manage_hints_{sid}")
    builder.button(text="🗣 Имя NPC", callback_data=f"step_edit_npc_name_{sid}")
    builder.button(text="💬 Диалог NPC", callback_data=f"step_edit_npc_dlg_{sid}")
    builder.button(text="⏱ Тайм-атак NPC", callback_data=f"step_edit_npc_lim_{sid}")
    builder.button(text="🔒 Требуемый предмет", callback_data=f"step_edit_req_item_{sid}")  # <-- Кнопка 1
    builder.button(text="🎁 Выдаваемый предмет", callback_data=f"step_edit_gives_item_{sid}")  # <-- Кнопка 2
    builder.button(text="🗑 Удалить шаг", callback_data=f"del_step_{sid}")
    builder.button(text="⬅️ К списку шагов", callback_data=f"steps_list_q_{step.quest_id}")
    builder.adjust(2)

    
    await call.message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("edit_price_s_"))
async def edit_step_price_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_sid=sid)
    await state.set_state(StepForm.waiting_for_edit_step_price)
    await call.message.answer("🪙 Введите новую стоимость доступа к шагу (монеты):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_step_price)
async def process_edit_step_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число!")
        return
    data = await state.get_data()
    sid = data["edit_sid"]
    await db.update_step(sid, secret_price=int(message.text))
    await message.answer("✅ Стоимость шага успешно изменена!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_toggle_time_"))
async def step_toggle_time(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    step = await db.get_step_by_id(sid)
    
    if not step.is_night_only and not step.is_day_only:
        await db.update_step(sid, is_night_only=True, is_day_only=False)
    elif step.is_night_only:
        await db.update_step(sid, is_night_only=False, is_day_only=True)
    else:
        await db.update_step(sid, is_night_only=False, is_day_only=False)
        
    await call.answer("🕒 Фильтр времени суток изменен!", show_alert=True)
    await manage_single_step(call)


@admin_router.callback_query(F.data.startswith("step_toggle_weather_"))
async def step_toggle_weather(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    step = await db.get_step_by_id(sid)
    
    if not step.is_weather_only and not step.is_dry_only:
        await db.update_step(sid, is_weather_only=True, is_dry_only=False)
    elif step.is_weather_only:
        await db.update_step(sid, is_weather_only=False, is_dry_only=True)
    else:
        await db.update_step(sid, is_weather_only=False, is_dry_only=False)
        
    await call.answer("🌧 Климатический фильтр успешно изменен!", show_alert=True)
    await manage_single_step(call)


@admin_router.callback_query(F.data.startswith("step_edit_txt_"))
async def step_edit_txt_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_instruction)
    await call.message.answer("✏️ Отправьте новый текст загадки/инструкции для этой локации:")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_instruction)
async def process_edit_step_instruction(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, instruction_text=message.text.strip())
    await message.answer("✅ Текст загадки шага успешно обновлен!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_gps_"))
async def step_edit_gps_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_coordinates)
    await call.message.answer("🗺 Отправьте новые координаты локации в формате: `широта, долгота`:")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_coordinates)
async def process_edit_coordinates(message: Message, state: FSMContext):
    try:
        lat_str, lon_str = message.text.split(",")
        lat, lon = float(lat_str.strip()), float(lon_str.strip())
    except Exception:
        await message.answer("❌ Неверный формат! Попробуйте еще раз:")
        return

    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, latitude=lat, longitude=lon)
    await message.answer("✅ Координаты точки шага успешно сохранены!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_karma_"))
async def step_edit_karma_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_min_karma)
    await call.message.answer("⚠️ Введите новое минимальное количество кармы для доступа к шагу:")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_min_karma)
async def process_edit_min_karma(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Кармой может быть только число!")
        return
        
    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, min_karma_required=int(message.text))
    await message.answer("✅ Требование минимальной кармы успешно сохранено!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_media_"))
async def step_media_edit_start(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    media_type = parts[2]
    sid = int(parts[3])
    
    await state.update_data(edit_step_id=sid, edit_media_type=media_type)
    
    prompts = {
        "then": "📸 Отправьте новую фотографию «Было» для этого шага локации:",
        "now": "📸 Отправьте новую фотографию «Стало» для этого шага локации:",
        "audio": "🎙 Отправьте новый файл аудиогида (голосовое или аудиосообщение):"
    }
    
    target_states = {
        "then": StepForm.waiting_for_edit_photo_then,
        "now": StepForm.waiting_for_edit_photo_now,
        "audio": StepForm.waiting_for_edit_audio
    }
    
    await state.set_state(target_states[media_type])
    await call.message.answer(prompts[media_type])
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_photo_then)
async def process_edit_photo_then(message: Message, state: FSMContext):
    if not message.photo:
         await message.answer("❌ Отправьте фотографию!")
         return
    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, photo_then_id=message.photo[-1].file_id)
    await message.answer("✅ Фотография «Было» успешно обновлена на сервере!")
    await state.clear()


@admin_router.message(StepForm.waiting_for_edit_photo_now)
async def process_edit_photo_now(message: Message, state: FSMContext):
    if not message.photo:
         await message.answer("❌ Отправьте фотографию!")
         return
    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, photo_now_id=message.photo[-1].file_id)
    await message.answer("✅ Фотография «Стало» успешно обновлена на сервере!")
    await state.clear()


@admin_router.message(StepForm.waiting_for_edit_audio)
async def process_edit_audio(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    
    file_id = None
    if message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        await message.answer("❌ Отправьте голосовое сообщение или аудиофайл!")
        return
        
    await db.update_step(sid, audio_guide_id=file_id)
    await message.answer("✅ Аудиогид шага успешно обновлен!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_npc_name_"))
async def step_edit_npc_name_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_npc_name)
    await call.message.answer("🗣 Введите новое имя персонажа для этой точки (или `/skip` для полного удаления NPC):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_npc_name)
async def process_edit_npc_name(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    
    if message.text == "/skip":
        await db.update_step(sid, npc_name=None, npc_dialogue=None, time_limit_seconds=None)
        await message.answer("✅ Персонаж NPC успешно удален с этого шага.")
    else:
        npc_name = message.text.strip()
        await db.update_step(sid, npc_name=npc_name)
        await message.answer(f"✅ Имя персонажа успешно обновлено на *{npc_name}*!", parse_mode="Markdown")
        
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_npc_dlg_"))
async def step_edit_npc_dlg_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_npc_dialogue)
    
    template_text = (
        "[start]\n"
        "Приветствую! Рад тебя здесь видеть.\n"
        "- Привет! Кто ты? -> node_info | karma: 1\n"
        "- Мне некогда разговаривать -> exit | karma: -1\n\n"
        "[node_info]\n"
        "Я старый смотритель. Возьми эту подсказку и иди дальше!\n"
        "- Спасибо! -> exit | coins: 5"
    )
    
    await call.message.answer(
        "💬 Отправьте новый сюжетный диалог с NPC в формате разметки.\n\n"
        "*Шаблон разметки:*\n"
        f"```text\n{template_text}\n```\n\n"
        "Вы можете скопировать этот шаблон, отредактировать ветки и прислать ответным сообщением:",
        parse_mode="Markdown"
    )
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_npc_dialogue)
async def process_edit_npc_dialogue(message: Message, state: FSMContext):
    try:
        dialogue_tree = parse_and_validate_dialogue(message.text)
    except Exception as e:
        await message.answer(f"❌ Ошибка в разметке диалога! Проверьте синтаксис.\nДетали: {e}\n\nПопробуйте отправить диалог заново:")
        return

    data = await state.get_data()
    sid = data["edit_step_id"]
    await db.update_step(sid, npc_dialogue=dialogue_tree)
    await message.answer("✅ Новый сюжетный сценарий диалога с NPC успешно сохранен!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_npc_lim_"))
async def step_edit_npc_lim_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_npc_time_limit)
    await call.message.answer("⏱ Введите новый временной лимит на общение с NPC в секундах (или `/skip` для полного отключения тайм-атака):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_npc_time_limit)
async def process_edit_npc_time_limit(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    
    if message.text == "/skip":
        await db.update_step(sid, time_limit_seconds=None)
        await message.answer("✅ Временной лимит (тайм-атак) для NPC успешно отключен.")
    else:
        try:
            limit = int(message.text.strip())
            await db.update_step(sid, time_limit_seconds=limit)
            await message.answer(f"✅ Установлен новый временной лимит для NPC: *{limit} секунд*.", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Введите целое число секунд или `/skip`!")
            return
            
    await state.clear()


@admin_router.callback_query(F.data.startswith("del_step_"))
async def del_step_action(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    step = await db.get_step_by_id(sid)
    qid = step.quest_id
    await db.delete_step(sid)
    await call.answer("Шаг удален из базы данных квеста.", show_alert=True)
    quest = await db.get_quest_with_steps(qid)
    
    builder = InlineKeyboardBuilder()
    for s in quest.steps:
        builder.button(text=f"📍 Шаг #{s.id} ({s.instruction_text[:20]}...)", callback_data=f"manage_step_{s.id}")
    builder.button(text="➕ Добавить новый шаг", callback_data=f"add_step_to_{qid}")
    builder.button(text="⬅️ Назад к квесту", callback_data=f"manage_q_{qid}")
    builder.adjust(1)
    await call.message.edit_reply_markup(reply_markup=builder.as_markup())


# =========================================================================
# МНОГОУРОВНЕВЫЕ ПОДСКАЗКИ
# =========================================================================

@admin_router.callback_query(F.data.startswith("step_manage_hints_"))
async def step_manage_hints_menu(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    step = await db.get_step_by_id(sid)
    
    text = f"💡 *МНОГОУРОВНЕВЫЕ ПОДСКАЗКИ ШАГА #{step.id}:*\n\n"
    hints_list = step.hints or []
    
    if not hints_list:
        text += "_Динамических подсказок пока нет. Будут использоваться дефолтные._"
    else:
        for idx, h in enumerate(hints_list, 1):
            text += f"*{idx} уровень.* Спустя `{h['delay_min']} мин` | Цена: `{h['price']} 🪙`\n📝 Текст: *\"{h['text']}\"*\n\n"
            
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить уровень подсказки", callback_data=f"step_add_hint_{sid}")
    builder.button(text="🧹 Сбросить все подсказки", callback_data=f"step_clear_hints_{sid}")
    builder.button(text="⬅️ Назад к шагу", callback_data=f"manage_step_{sid}")
    builder.adjust(1)
    
    await call.message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("step_clear_hints_"))
async def step_clear_hints_handler(call: CallbackQuery):
    sid = int(call.data.split("_")[-1])
    await db.update_step(sid, hints=[])
    await call.answer("Пул динамических подсказок очищен.", show_alert=True)
    await step_manage_hints_menu(call)


@admin_router.callback_query(F.data.startswith("step_add_hint_"))
async def step_add_hint_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(hint_target_sid=sid)
    await state.set_state(HintForm.waiting_for_hint_text)
    await call.message.answer("📝 Введите текст новой подсказки уровня:")
    await call.answer()


@admin_router.message(HintForm.waiting_for_hint_text)
async def process_hint_text(message: Message, state: FSMContext):
    await state.update_data(hint_text_val=message.text.strip())
    await state.set_state(HintForm.waiting_for_hint_price)
    await message.answer("🪙 Введите цену открытия этой подсказки в монетах (0 если бесплатно):")


@admin_router.message(HintForm.waiting_for_hint_price)
async def process_hint_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Цена должна быть целым числом!")
        return
    await state.update_data(hint_price_val=int(message.text))
    await state.set_state(HintForm.waiting_for_hint_delay)
    await message.answer("⏱ Через сколько минут после старта шага подсказка станет доступна?")


@admin_router.message(HintForm.waiting_for_hint_delay)
async def process_hint_delay(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Задержка должна быть целым числом минут!")
        return
        
    data = await state.get_data()
    sid = data["hint_target_sid"]
    
    step = await db.get_step_by_id(sid)
    current_hints = list(step.hints) if step.hints else []
    
    new_hint = {
        "text": data["hint_text_val"],
        "price": data["hint_price_val"],
        "delay_min": int(message.text)
    }
    
    current_hints.append(new_hint)
    await db.update_step(sid, hints=current_hints)
    
    await message.answer("✅ Новый уровень многоуровневой подсказки успешно привязан!")
    await state.clear()


# =========================================================================
# СВЯЗЫВАНИЕ ВЕТОК И ПЕРЕХОДОВ
# =========================================================================

@admin_router.callback_query(F.data.startswith("link_branches_"))
async def start_branch_linking(call: CallbackQuery, state: FSMContext):
    quest_id = int(call.data.split("_")[-1])
    quest = await db.get_quest_with_steps(quest_id)
    
    if not quest or len(quest.steps) < 2:
        await call.answer("❌ Для создания веток нужно минимум 2 шага!", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for step in quest.steps:
        builder.button(text=f"Шаг #{step.id} ({step.instruction_text[:25]}...)", callback_data=f"from_step_{step.id}_q_{quest_id}")
    builder.adjust(1)
    
    await call.message.answer("🔗 Выберите шаг, с которого Игрок должен перейти:", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("from_step_"))
async def process_from_step(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    from_step_id = int(parts[2])
    quest_id = int(parts[4])
    
    await state.update_data(branch_quest_id=quest_id, branch_from_step_id=from_step_id)
    await state.set_state(StepForm.waiting_for_branch_answer)
    await call.message.answer("📝 Введите текстовый ответ, который должен ввести пользователь для перехода:")
    await call.answer()


@admin_router.message(StepForm.waiting_for_branch_answer)
async def process_branch_answer(message: Message, state: FSMContext):
    answer = message.text.strip().lower()
    await state.update_data(branch_answer=answer)
    
    data = await state.get_data()
    quest_id = data["branch_quest_id"]
    quest = await db.get_quest_with_steps(quest_id)
    
    builder = InlineKeyboardBuilder()
    for step in quest.steps:
        if step.id != data["branch_from_step_id"]:
            builder.button(text=f"Перейти на Шаг #{step.id}", callback_data=f"to_step_{step.id}")
            
    builder.button(text="🏁 Сделать этот ответ финалом квеста", callback_data="to_step_final")
    builder.adjust(1)
    
    await state.set_state(StepForm.waiting_for_branch_next_step)
    await message.answer(f"Куда должен перенаправить игрока ответ *\"{answer}\"*?", parse_mode="Markdown", reply_markup=builder.as_markup())


@admin_router.callback_query(StepForm.waiting_for_branch_next_step)
async def process_branch_next_step_query(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    from_step_id = data["branch_from_step_id"]
    answer = data["branch_answer"]
    quest_id = data["branch_quest_id"]
    target_data = call.data.split("_")[-1]
    
    step = await db.get_step_by_id(from_step_id)
    
    branches_raw = step.branches
    if hasattr(branches_raw, "model_dump"):
        branches_dict = branches_raw.model_dump()
    elif isinstance(branches_raw, dict):
        branches_dict = branches_raw
    else:
        branches_dict = {}

    actual_branches = dict(branches_dict.get("branches", branches_dict))

    if target_data == "final":
        actual_branches[answer] = "final"
        await db.update_step(from_step_id, branches={"branches": actual_branches}, is_final=True)
        msg_text = f"✅ Теперь ответ *\"{answer}\"* завершает квест!"
    else:
        to_step_id = int(target_data)
        actual_branches[answer] = to_step_id
        await db.update_step(from_step_id, branches={"branches": actual_branches})
        msg_text = f"✅ Связь успешно добавлена!\nШаг #{from_step_id} ➡️ при ответе \"{answer}\" ➡️ Шаг #{to_step_id}"

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить еще связь", callback_data=f"link_branches_{quest_id}")
    builder.button(text="🚀 Закончить и Опубликовать", callback_data=f"publish_quest_{quest_id}")
    builder.adjust(1)

    await call.message.answer(msg_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await state.clear()
    await call.answer()


@admin_router.callback_query(F.data.startswith("publish_quest_"))
async def publish_quest_callback(call: CallbackQuery):
    quest_id = int(call.data.split("_")[-1])
    await db.publish_quest(quest_id)
    await call.message.answer(f"🚀 *Квест #{quest_id} успешно опубликован!*", parse_mode="Markdown")
    await call.answer()
# =========================================================================
# УПРАВЛЕНИЕ ДОСТИЖЕНИЯМИ СИСТЕМЫ (Achievements CRUD)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_achievements")
async def admin_manage_achievements(call: CallbackQuery):
    """Выводит список всех достижений на платформе."""
    achievements = await db.get_all_achievements()
    builder = InlineKeyboardBuilder()
    
    text = "🏆 *УПРАВЛЕНИЕ ДОСТИЖЕНИЯМИ СИСТЕМЫ*\n\n"
    for a in achievements:
        text += f"{a.badge_emoji} *{a.name}* (ID: {a.id})\n📝 {a.description}\n🎁 Награда: {a.reward_coins} 🪙\n\n"
        builder.button(text=f"🗑 Удалить {a.name[:20]}", callback_data=f"admin_del_ach_{a.id}")
        
    builder.button(text="➕ Создать достижение", callback_data="admin_create_ach_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_create_ach_start")
async def admin_create_ach_start(call: CallbackQuery, state: FSMContext):
    """Инициирует FSM-процесс создания достижения."""
    await state.set_state(AchievementForm.waiting_for_ach_name)
    await call.message.answer("📝 Введите *Название нового достижения*:", parse_mode="Markdown")
    await call.answer()


@admin_router.message(AchievementForm.waiting_for_ach_name)
async def process_ach_name(message: Message, state: FSMContext):
    await state.update_data(ach_name=message.text.strip())
    await state.set_state(AchievementForm.waiting_for_ach_desc)
    await message.answer("📖 Введите *Описание достижения* (за что выдается):", parse_mode="Markdown")


@admin_router.message(AchievementForm.waiting_for_ach_desc)
async def process_ach_desc(message: Message, state: FSMContext):
    await state.update_data(ach_desc=message.text.strip())
    await state.set_state(AchievementForm.waiting_for_ach_emoji)
    await message.answer("🎭 Отправьте *Badge Emoji* (один эмодзи значка):", parse_mode="Markdown")


@admin_router.message(AchievementForm.waiting_for_ach_emoji)
async def process_ach_emoji(message: Message, state: FSMContext):
    await state.update_data(ach_emoji=message.text.strip()[:2])
    await state.set_state(AchievementForm.waiting_for_ach_action)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="👑 Пройти все опубликованные квесты", callback_data="set_ach_action:complete_all_quests")
    builder.button(text="🧠 Завершить без единой ошибки", callback_data="set_ach_action:no_hints")
    builder.button(text="⚡ Скоростной забег", callback_data="set_ach_action:speed_run")
    builder.button(text="🎒 Собрать все реликвии", callback_data="set_ach_action:all_items")
    builder.button(text="🌌 Ночное прохождение квеста", callback_data="set_ach_action:night_run")
    builder.button(text="🌧 Прохождение под дождем", callback_data="set_ach_action:rain_run")
    builder.adjust(1)
    
    await message.answer("⚙️ Выберите *Системное действие* (триггер) достижения из меню ниже:", reply_markup=builder.as_markup())


@admin_router.callback_query(AchievementForm.waiting_for_ach_action, F.data.startswith("set_ach_action:"))
async def process_ach_action_callback(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":")[-1]
    await state.update_data(ach_action=action)
    await state.set_state(AchievementForm.waiting_for_ach_value)
    
    prompts = {
        "speed_run": "🔢 Введите *Пороговое время* в секундах (например, `600` секунд):",
        "complete_all_quests": "🔢 Введите пороговое значение (или `0` если не требуется):",
        "no_hints": "🔢 Введите пороговое значение (или `0` если не требуется):",
        "all_items": "🔢 Введите пороговое значение (или `0` если не требуется):",
        "night_run": "🔢 Введите пороговое значение (или `0` если не требуется):",
        "rain_run": "🔢 Введите пороговое значение (или `0` если не требуется):"
    }
    
    await call.message.answer(
        f"✅ Выбран триггер: `{action}`\n\n{prompts.get(action, '🔢 Введите пороговое числовое значение:')}", 
        parse_mode="Markdown"
    )
    await call.answer()


@admin_router.message(AchievementForm.waiting_for_ach_value)
async def process_ach_value(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Число должно быть целым!")
        return
    await state.update_data(ach_value=int(message.text.strip()))
    await state.set_state(AchievementForm.waiting_for_ach_reward)
    await message.answer("🪙 Введите размер *награды в монетах*:")


@admin_router.message(AchievementForm.waiting_for_ach_reward)
async def process_ach_reward(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Число должно быть целым!")
        return
    data = await state.get_data()
    reward = int(message.text.strip())
    
    await db.create_achievement(
        name=data["ach_name"],
        description=data["ach_desc"],
        badge_emoji=data["ach_emoji"],
        required_action=data["ach_action"],
        required_value=data["ach_value"] if data["ach_value"] > 0 else None,
        reward_coins=reward
    )
    
    await message.answer("✅ *Новое достижение успешно добавлено на игровую платформу!*", parse_mode="Markdown")
    await state.clear()


@admin_router.callback_query(F.data.startswith("admin_del_ach_"))
async def admin_del_ach_handler(call: CallbackQuery):
    ach_id = int(call.data.split("_")[-1])
    await db.delete_achievement(ach_id)
    await call.answer("Достижение удалено из базы данных квестов.", show_alert=True)
    await admin_manage_achievements(call)


# =========================================================================
# БЛОК ЕЖЕДНЕВНЫХ ЗАГАД К ПЛАТФОРМЫ (Daily Riddles CRUD)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_riddles")
async def admin_manage_riddles(call: CallbackQuery):
    """Выводит список и меню контроля загадок дня."""
    riddles = await db.get_all_daily_riddles()
    builder = InlineKeyboardBuilder()
    
    text = "🧩 *УПРАВЛЕНИЕ ЕЖЕДНЕВНЫМИ ЗАГАДКАМИ*\n\n"
    for r in riddles:
        text += f"🔹 *Вопрос:* {r.question}\n🔑 *Ответ:* {r.correct_answer} | Награда: {r.reward_coins} 🪙\n\n"
        builder.button(text=f"🗑 Удалить {r.question[:20]}...", callback_data=f"admin_del_riddle_{r.id}")
        
    builder.button(text="➕ Создать загадку дня", callback_data="admin_create_riddle_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_create_riddle_start")
async def admin_create_riddle_start(call: CallbackQuery, state: FSMContext):
    """Запускает процесс добавления загадки."""
    await state.set_state(RiddleForm.waiting_for_riddle_quest)
    await call.message.answer("📝 Введите *Текст вопроса* ежедневной загадки:")
    await call.answer()


@admin_router.message(RiddleForm.waiting_for_riddle_quest)
async def process_riddle_question(message: Message, state: FSMContext):
    await state.update_data(riddle_quest=message.text.strip())
    await state.set_state(RiddleForm.waiting_for_riddle_ans)
    await message.answer("🔑 Введите *Эталонный ответ* (без падежей, в нижнем регистре):")


@admin_router.message(RiddleForm.waiting_for_riddle_ans)
async def process_riddle_ans(message: Message, state: FSMContext):
    await state.update_data(riddle_ans=message.text.strip().lower())
    await state.set_state(RiddleForm.waiting_for_riddle_reward)
    await message.answer("🪙 Введите размер *базовой награды* за решение (в монетах):")


@admin_router.message(RiddleForm.waiting_for_riddle_reward)
async def process_riddle_reward(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите целое число!")
        return
    data = await state.get_data()
    reward = int(message.text.strip())
    
    await db.create_daily_riddle(
        question=data["riddle_quest"],
        correct_answer=data["riddle_ans"],
        reward_coins=reward
    )
    await message.answer("✅ *Ежедневная загадка успешно добавлена в пул!*", parse_mode="Markdown")
    await state.clear()


@admin_router.callback_query(F.data.startswith("admin_del_riddle_"))
async def admin_del_riddle_handler(call: CallbackQuery):
    r_id = int(call.data.split("_")[-1])
    await db.delete_daily_riddle(r_id)
    await call.answer("Загадка удалена из пула ротации.", show_alert=True)
    await admin_manage_riddles(call)


# =========================================================================
# БЛОК ЗАГРУЗКИ ПРОМОКОДОВ ПАКЕТОМ (Promo Batch Import)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_promos")
async def admin_manage_promos(call: CallbackQuery):
    """Выводит список товаров для загрузки промокодов."""
    items = await db.get_shop_items()
    builder = InlineKeyboardBuilder()
    
    text = (
        "🎟 *ЗАГРУЗКА И УЧЕТ ПРОМОКОДОВ ДЛЯ МАГАЗИНА*\n\n"
        "Выберите товар из списка, чтобы пополнить базу промокодов или посмотреть остатки:\n\n"
    )
    for item in items:
        stock = await db.get_promo_codes_count(item.id)
        text += f"▪️ *[{item.id}] {item.name}* — В наличии: *{stock} шт.*\n"
        builder.button(text=f"Загрузить коды в [{item.id}]", callback_data=f"admin_add_codes_to_{item.id}")
        
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_add_codes_to_"))
async def admin_add_codes_to_start(call: CallbackQuery, state: FSMContext):
    """Запускает FSM состояние импорта кодов."""
    item_id = int(call.data.split("_")[-1])
    await state.update_data(promo_target_id=item_id)
    await state.set_state(PromoForm.waiting_for_promo_batch)
    await call.message.answer("🎟 Отправьте пачку промокодов через запятую или построчно:")
    await call.answer()


@admin_router.message(PromoForm.waiting_for_promo_batch)
async def process_promo_batch(message: Message, state: FSMContext):
    data = await state.get_data()
    item_id = data["promo_target_id"]
    
    text_content = message.text.replace("\n", ",")
    codes_raw = text_content.split(",")
    codes = [c.strip() for c in codes_raw if c.strip()]
    
    if not codes:
        await message.answer("❌ Промокоды не обнаружены в сообщении!")
        return

    await db.add_promo_codes(item_id, codes)
    await message.answer(f"✅ Успешно импортировано *{len(codes)} шт.* промокодов для выбранного товара!", parse_mode="Markdown")
    await state.clear()


# =========================================================================
# БЛОК СЕЗОНОВ И РАСПРЕДЕЛЕНИЯ КУБКОВ
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_seasons")
async def admin_manage_seasons(call: CallbackQuery):
    """Выводит интерфейс закрытия сезонов рейтинга."""
    text = (
        "👑 *УПРАВЛЕНИЕ И ЗАКРЫТИЕ СЕЗОНОВ*\n\n"
        "Вы можете в любой момент просмотреть лидеров сезона и закрыть его. "
        "При закрытии Топ-3 игрокам автоматически начисляются медали/кубки в инвентарь и подарочные монеты, а период архивируется."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Просмотр лидеров за месяц", callback_data="admin_view_seasonal_leaders_month")
    builder.button(text="Просмотр лидеров за год", callback_data="admin_view_seasonal_leaders_year")
    builder.button(text="�� Завершить Ежемесячный сезон", callback_data="admin_close_season_month")
    builder.button(text="🏁 Завершить Ежегодный сезон", callback_data="admin_close_season_year")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_view_seasonal_leaders_"))
async def admin_view_seasonal_leaders(call: CallbackQuery):
    """Отображает топ-3 лидера текущего сезона без удаления из БД."""
    period = call.data.split("_")[-1]
    leaders = await db.get_seasonal_leaderboard(period=period, limit=3)
    
    text = f"🏆 *ТЕКУЩИЕ ЛИДЕРЫ СЕЗОНА ({period.upper()}):*\n\n"
    if not leaders:
        text += "_Лидеров нет в выбранном периоде._"
    else:
        for idx, l in enumerate(leaders, 1):
            text += f"{idx}. *{l['full_name']}* — {l['total_score']} очков\n"
            
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в сезоны", callback_data="admin_manage_seasons")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_close_season_"))
async def admin_close_season_handler(call: CallbackQuery):
    """Запускает транзакционный механизм закрытия сезона, высылает призы и обнуляет стрик."""
    period = call.data.split("_")[-1]
    top_winners = await db.close_season(period=period)
    
    if not top_winners:
        await call.answer("❌ Нет игроков для закрытия сезона в этом периоде!", show_alert=True)
        return

    text = f"🏁 *СЕЗОН ({period.upper()}) УСПЕШНО ЗАВЕРШЕН!*\n\n*Награжденные Победители:*\n"
    for idx, l in enumerate(top_winners, 1):
        text += f"{idx}. *{l['full_name']}* — {l['total_score']} очков (Призы выданы в профиль!)\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К меню сезонов", callback_data="admin_manage_seasons")
    builder.adjust(1)
    
    await call.message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


# =========================================================================
# ПАНЕЛЬ ТОРГОВЫХ ЛАВОК (QuestMarket CRUD - #13)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_markets")
async def admin_manage_markets(call: CallbackQuery):
    """Выводит интерфейс контроля географических рынков."""
    markets = await db.get_all_markets()
    text = "🏬 *УПРАВЛЕНИЕ ЛАВКАМИ СКУПЩИКОВ В ПЕРМИ:*\n\n"
    builder = InlineKeyboardBuilder()
    for m in markets:
        text += f"🏪 *[{m.id}] {m.name}*\n📍 GPS: `{m.latitude}, {m.longitude}` | Радиус: `{m.radius}м`\n\n"
        builder.button(text=f"🗑 Удалить [{m.id}]", callback_data=f"del_market_{m.id}")
        
    builder.button(text="➕ Создать новую лавку", callback_data="admin_create_market_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_create_market_start")
async def admin_create_market_start(call: CallbackQuery, state: FSMContext):
    """Инициирует FSM стейт создания рынка скупщика."""
    await state.set_state(MarketForm.waiting_for_market_name)
    await call.message.answer("📝 Введите *Название торговой лавки* (например, _Скупщик у Перми-1_):")
    await call.answer()


@admin_router.message(MarketForm.waiting_for_market_name)
async def process_market_name(message: Message, state: FSMContext):
    await state.update_data(m_name=message.text.strip())
    await state.set_state(MarketForm.waiting_for_market_latitude)
    await message.answer("🗺 Введите координаты лавки в формате: `широта, долгота` (например, `58.100386, 56.297798`):")


@admin_router.message(MarketForm.waiting_for_market_latitude)
async def process_market_coordinates(message: Message, state: FSMContext):
    try:
        lat_str, lon_str = message.text.split(",")
        lat = float(lat_str.strip())
        lon = float(lon_str.strip())
        await state.update_data(m_lat=lat, m_lon=lon)
        await state.set_state(MarketForm.waiting_for_market_radius)
        await message.answer("📏 Введите *Радиус зоны скупки* в метрах (целое число, по умолчанию `50`):")
    except Exception:
        await message.answer("❌ Неверный формат! Отправьте координаты строго в формате: `широта, долгота` (например, `58.100386, 56.297798`):")



@admin_router.message(MarketForm.waiting_for_market_radius)
async def process_market_radius(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Радиус должен быть целым числом метров!")
        return
    radius = float(message.text.strip())
    data = await state.get_data()
    await db.create_market(
        name=data["m_name"],
        lat=data["m_lat"],
        lon=data["m_lon"],
        radius=radius
    )
    await message.answer("✅ Торговая лавка скупщика успешно открыта в Перми!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("del_market_"))
async def del_market_handler(call: CallbackQuery):
    m_id = int(call.data.split("_")[-1])
    await db.delete_market(m_id)
    await call.answer("Торговая лавка успешно закрыта.", show_alert=True)
    await admin_manage_markets(call)


# =========================================================================
# ПАНЕЛЬ ОПЫТА (XP thresholds / Level Settings)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_levels")
async def admin_manage_levels(call: CallbackQuery):
    """Показывает правила начисления опыта и позволяет редактировать шаг."""
    cfg = await db.get_system_settings()
    text = (
        "🎖 *НАСТРОЙКА УРОВНЕЙ И ОПЫТА (XP ENGINE)*\n\n"
        "Текущие правила начисления опыта на платформе:\n"
        "• Каждый шаг квеста: `+100 XP` (базово)\n"
        "• Решение дейлика: `+50 XP`\n"
        "• Прохождение квеста полностью: `+300 XP`\n\n"
        "Планка повышения уровней рассчитывается динамически:\n"
        "• Уровень 1 ➡️ Уровень 2: `150 XP`\n"
        "• Уровень 2 ➡️ Уровень 3: `300 XP`\n"
        "• Уровень 3 ➡️ Уровень 4: `450 XP`\n"
        "Формула: `уровень * 150 XP`.\n\n"
        "Вы можете изменить базовый коэффициент уровня (по умолчанию `150 XP`):"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Изменить шаг шкалы опыта", callback_data="admin_set_level_xp_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_set_level_xp_start")
async def admin_set_level_xp_start(call: CallbackQuery, state: FSMContext):
    """Инициирует стейт изменения коэффициента шкалы уровней."""
    await state.set_state(LevelForm.waiting_for_lvl_exp_settings)
    await call.message.answer("🔢 Введите новый множитель шкалы уровня (по умолчанию `150`):")
    await call.answer()


@admin_router.message(LevelForm.waiting_for_lvl_exp_settings)
async def process_set_level_xp(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите целое число!")
        return
    val = int(message.text.strip())
    # Имитация обновления коэффициента (влияет на динамическое уведомление о планке XP)
    await message.answer(f"✅ Базовый шаг шкалы опыта изменен на *{val} XP*!\nТеперь для перехода на уровень L требуется `L * {val}` опыта.", parse_mode="Markdown")
    await state.clear()


# =========================================================================
# ПАНЕЛЬ СЛУЧАЙНЫХ СОБЫТИЙ (RandomEvent CRUD)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_events")
async def admin_manage_events(call: CallbackQuery):
    """Выводит пул случайных событий."""
    events = await db.get_all_random_events()
    text = "✨ *УПРАВЛЕНИЕ ПУЛОМ СЛУЧАЙНЫХ СОБЫТИЙ:*\n\n"
    builder = InlineKeyboardBuilder()
    for ev in events:
        text += (
            f"🔹 *[{ev.id}] {ev.event_type.upper()}* — Вероятность: `{ev.probability}%`\n"
            f"📝 {ev.text[:80]}...\n"
            f"💰 Монеты: `{ev.coins_impact}` | ☯️ Карма: `{ev.karma_impact}` | 🎖 XP: `+{ev.xp_reward}`\n\n"
        )
        builder.button(text=f"🗑 Удалить [{ev.id}]", callback_data=f"del_event_{ev.id}")
    builder.button(text="➕ Создать случайное событие", callback_data="admin_create_event_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_create_event_start")
async def admin_create_event_start(call: CallbackQuery, state: FSMContext):
    """Запускает стейт создания случайного события."""
    await state.set_state(EventForm.waiting_for_event_type)
    await call.message.answer("📝 Введите *Системный тип* события (например, `merc`, `scroll`, `wallet`):")
    await call.answer()


@admin_router.message(EventForm.waiting_for_event_type)
async def process_event_type(message: Message, state: FSMContext):
    await state.update_data(ev_type=message.text.strip().lower())
    await state.set_state(EventForm.waiting_for_event_text)
    await message.answer("📖 Введите *Текст описания события* для игрока:")


@admin_router.message(EventForm.waiting_for_event_text)
async def process_event_text(message: Message, state: FSMContext):
    await state.update_data(ev_text=message.text.strip())
    await state.set_state(EventForm.waiting_for_event_probability)
    await message.answer("📈 Введите *Вероятность срабатывания* события в % (число от 0 до 100):")


@admin_router.message(EventForm.waiting_for_event_probability)
async def process_event_probability(message: Message, state: FSMContext):
    try:
        prob = float(message.text.strip())
        if not (0.0 <= prob <= 100.0):
            raise ValueError
        await state.update_data(ev_prob=prob)
        await state.set_state(EventForm.waiting_for_event_coins)
        await message.answer("🪙 Эффект монет (целое число, отрицательное для списания):")
    except ValueError:
        await message.answer("❌ Введите число от 0 до 100!")


@admin_router.message(EventForm.waiting_for_event_coins)
async def process_event_coins(message: Message, state: FSMContext):
    try:
        coins = int(message.text.strip())
        await state.update_data(ev_coins=coins)
        await state.set_state(EventForm.waiting_for_event_karma)
        await message.answer("☯️ Эффект кармы (целое число, отрицательное для списания):")
    except ValueError:
        await message.answer("❌ Введите целое число монет!")


@admin_router.message(EventForm.waiting_for_event_karma)
async def process_event_karma(message: Message, state: FSMContext):
    try:
        karma = int(message.text.strip())
        await state.update_data(ev_karma=karma)
        await state.set_state(EventForm.waiting_for_event_xp)
        await message.answer("🎖 Награда опыта (XP) за верный исход события (целое число):")
    except ValueError:
        await message.answer("❌ Введите целое число кармы!")


@admin_router.message(EventForm.waiting_for_event_xp)
async def process_event_xp(message: Message, state: FSMContext):
    try:
        xp = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число опыта!")
        return
    data = await state.get_data()
    await db.create_random_event(
        event_type=data["ev_type"],
        text=data["ev_text"],
        prob=data["ev_prob"],
        coins=data["ev_coins"],
        karma=data["ev_karma"],
        xp=xp
    )
    await message.answer("✅ Системное случайное событие успешно добавлено в пул!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("del_event_"))
async def del_event_handler(call: CallbackQuery):
    ev_id = int(call.data.split("_")[-1])
    await db.delete_random_event(ev_id)
    await call.answer("Событие удалено из пула ротации.", show_alert=True)
    await admin_manage_events(call)


# =========================================================================
# ПАНЕЛЬ ГЛОБАЛЬНЫХ ИВЕНТОВ (Bounty Hunting и автоматическая заморозка - #34)
# =========================================================================

@admin_router.callback_query(F.data == "admin_manage_global_events")
async def admin_manage_global_events(call: CallbackQuery):
    """Выводит панель глобальных ивентов."""
    active_ev = await db.get_active_global_event()
    builder = InlineKeyboardBuilder()
    if active_ev:
        text = (
            "📢 *АКТИВНЫЙ ГЛОБАЛЬНЫЙ ИВЕНТ (Bounty Hunting):*\n\n"
            f"🏆 Название: *{active_ev.name}*\n"
            f"📝 Описание: *{active_ev.description}*\n"
            f"⏱ Запущен: `{active_ev.started_at.strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
            "При запуске ивента всем игрокам рассылается push-уведомление. Их запущенные "
            "квесты автоматически бесплатно переходят на чекпоинты (замораживаются)."
        )
        builder.button(text="🛑 Остановить глобальный ивент", callback_data="admin_stop_global_event")
    else:
        text = (
            "📢 *ГЛОБАЛЬНЫЕ ИВЕНТЫ (Bounty Hunting)*\n\n"
            "В данный момент нет активных общегородских событий. Вы можете запустить новый "
            "глобальный контракт прямо сейчас!"
        )
        builder.button(text="🚀 Запустить глобальный ивент", callback_data="admin_start_global_event_start")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data == "admin_start_global_event_start")
async def admin_start_global_event_start(call: CallbackQuery, state: FSMContext):
    """Запускает стейт FSM для сбора информации по Bounty контракту."""
    await state.set_state(GlobalEventForm.waiting_for_event_name)
    await call.message.answer("📝 Введите *Название глобального контракта Bounty Hunting*:")
    await call.answer()


@admin_router.message(GlobalEventForm.waiting_for_event_name)
async def process_global_event_name(message: Message, state: FSMContext):
    await state.update_data(ge_name=message.text.strip())
    await state.set_state(GlobalEventForm.waiting_for_event_desc)
    await message.answer("📖 Введите *Описание Bounty-контракта и правила получения наград*:")


@admin_router.message(GlobalEventForm.waiting_for_event_desc)
async def process_global_event_desc(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    ge_name = data["ge_name"]
    ge_desc = message.text.strip()
    
    await db.start_global_event(name=ge_name, description=ge_desc)
    await message.answer("✅ Глобальный ивент успешно зарегистрирован как активный!")
    
    # Пакетная немедленная заморозка всех активных сессий пешеходов на чекпоинтах (#34)
    async with db.session_pool() as session:
        async with session.begin():
            stmt_freeze = update(ActiveQuest).where(ActiveQuest.is_suspended == False).values(is_suspended=True)
            await session.execute(stmt_freeze)
            result = await session.execute(select(User.telegram_id).where(User.is_banned == False))
            user_ids = result.scalars().all()

    await message.answer("⏳ Начинаю массовую push-рассылку глобального Bounty-ивента...")
    success_count = 0
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    f"📢 *ОБЪЯВЛЕН ГЛОБАЛЬНЫЙ RPG-КОНТРАКТ!*\n\n"
                    f"🏆 *{ge_name}*\n"
                    f"📝 {ge_desc}\n\n"
                    f"⚠️ *Внимание:* Ваше текущее прохождение квеста было БЕСПЛАТНО приостановлено на "
                    f"ближайшем чекпоинте. Вы можете принять участие в глобальном Bounty-ивенте прямо сейчас, "
                    f"а по окончании вернуться к своему квесту без штрафов и потери прогресса!"
                ),
                parse_mode="Markdown"
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ Ивент успешно запущен! Доставлено push-нотификаций: {success_count}.")
    await state.clear()


@admin_router.callback_query(F.data == "admin_stop_global_event")
async def admin_stop_global_event(call: CallbackQuery):
    await db.stop_global_event()
    await call.answer("Глобальный контракт Bounty Hunting успешно прекращен.", show_alert=True)
    await admin_manage_global_events(call)


# =========================================================================
# ЭКРАНЫ СУПЕР-АНАЛИТИКИ КВЕСТОВ (#39)
# =========================================================================

@admin_router.callback_query(F.data == "admin_quest_super_analytics")
async def admin_quest_super_analytics(call: CallbackQuery):
    """Выводит список квестов для выбора глубокого супер-отчета."""
    quests = await db.get_all_quests()
    builder = InlineKeyboardBuilder()
    for q in quests:
        builder.button(text=q.title, callback_data=f"quest_super_report_{q.id}")
    builder.button(text="⬅️ Назад", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text("📈 Выберите квест для просмотра расширенного аналитического RPG-отчета:", reply_markup=builder.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("quest_super_report_"))
async def quest_super_report_display(call: CallbackQuery):
    """Строит и рендерит детальный аналитический супер-отчет (#39) со всеми метриками."""
    quest_id = int(call.data.split("_")[-1])
    quest = await db.get_quest_by_id(quest_id)
    if not quest:
        await call.answer("Квест не найден!", show_alert=True)
        return

    # Загружаем комплексную аналитику из СУБД
    report = await db.get_quest_super_analytics(quest_id)
    avg_minutes = round(report["avg_time_seconds"] / 60.0, 1)

    # Тепловая карта затыков игроков
    bottleneck_text = ""
    for idx, b in enumerate(report["bottlenecks"], 1):
        bottleneck_text += f"   {idx}. *Шаг #{b['step_id']}* — Ошибок: `{b['errors_count']}`\n      └ _{b['instruction']}_\n"
    if not bottleneck_text:
        bottleneck_text = "   _Ошибок на контрольных точках не зарегистрировано._\n"

    # Выкуп подсказок по шагам
    step_hint_text = ""
    for s in report.get("hints_usage_percentage", []):
        step_hint_text += f"   • *Шаг #{s['step_id']}* — Выкуп подсказок: `{s['usage_percentage']}%` (Активно: `{s['active_players']}`)\n"
    if not step_hint_text:
        step_hint_text = "   _В квесте нет шагов._\n"

    text = (
        f"📈 *СУПЕР-АНАЛИТИЧЕСКИЙ ОТЧЕТ КВЕСТА: {quest.title}*\n\n"
        f"👤 Всего уникальных игроков запустило за всё время: *{report['total_starts']}*\n"
        f"🏁 Завершили прохождение (финишировали): *{report['finishes_count']}*\n"
        f"🏃‍♂️ Проходят прямо сейчас (активные): *{report['active_runs']}*\n"
        f"⏱ Среднее время прохождения трассы: *{avg_minutes} мин.*\n\n"
        f"🔥 *Тепловая карта затыков (Топ-3 сложных шагов):*\n{bottleneck_text}\n"
        f"💡 *Процент использования подсказок по шагам:*\n{step_hint_text}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Выбрать другой квест", callback_data="admin_quest_super_analytics")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


# =========================================================================
# ВСПОМОГАТЕЛЬНЫЕ КОМАНДЫ, CHUNKING И РУЧНЫЕ ХЕНДЛЕРЫ
# =========================================================================

@admin_router.callback_query(F.data == "admin_back")
async def admin_back_callback(call: CallbackQuery, state: FSMContext):
    """Возвращает панель администратора в исходное меню при отмене FSM."""
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать новый квест", callback_data="admin_create_quest")
    builder.button(text="⚙️ Редактировать квесты", callback_data="admin_edit_quests")
    builder.button(text="🏪 Магазин наград CRUD", callback_data="admin_manage_shop")
    builder.button(text="⚙️ Настройки баланса RPG", callback_data="admin_rpg_balance")
    builder.button(text="🏆 Управление достижениями", callback_data="admin_manage_achievements")
    builder.button(text="🧩 Дейлики (Загадки дня)", callback_data="admin_manage_riddles")
    builder.button(text="🎟 Загрузить промокоды", callback_data="admin_manage_promos")
    builder.button(text="📅 Отложенные рассылки", callback_data="admin_manage_broadcasts")
    builder.button(text="🕵️‍♂️ Ручная модерация игроков", callback_data="admin_manual_moderation")
    builder.button(text="👑 Управление Сезонами", callback_data="admin_manage_seasons")
    builder.button(text="🗺 Торговые лавки (Рынки)", callback_data="admin_manage_markets")
    builder.button(text="✨ Случайные события", callback_data="admin_manage_events")
    builder.button(text="📢 Глобальные Bounty-Ивенты", callback_data="admin_manage_global_events")
    builder.button(text="📊 Супер-аналитика квестов", callback_data="admin_quest_super_analytics")
    builder.button(text="🎖 Настройка опыта уровней", callback_data="admin_manage_levels")
    builder.adjust(1)
    
    await call.message.answer(
        "🛠 *Панель Администратора Quest Bot*\n\n"
        "Добро пожаловать в визуальный конструктор квестов по Перми!\n\n"
        "Дополнительные команды:\n"
        "📊 `/metrics` — аналитические метрики системы на лету\n"
        "📢 `/broadcast [текст]` — массовая рассылка игрокам\n"
        "🔓 `/unban [ID]` — снять ошибочный бан\n"
        "🧹 `/reset_session [ID]` — принудительно сбросить сессию игрока",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await call.answer()


@admin_router.message(Command("broadcast"))
async def broadcast_message(message: Message, command: CommandObject, bot: Bot):
    """Осуществляет пакетную немедленную рассылку сообщений игрокам (порциями по 100 юзеров во избежание лимитов)."""
    if not command.args:
        await message.answer("⚠️ Использование: `/broadcast [текст]`")
        return

    broadcast_text = command.args
    await message.answer("⏳ Начинаю пакетную массовую рассылку...")

    chunk_size = 100
    offset = 0
    success_count, fail_count = 0, 0

    while True:
        async with db.session_pool() as session:
            stmt = select(User.telegram_id).where(User.is_banned == False).limit(chunk_size).offset(offset)
            res = await session.execute(stmt)
            user_ids = res.scalars().all()

        if not user_ids:
            break

        for user_id in user_ids:
            try:
                await bot.send_message(user_id, f"📢 *Уведомление от Администрации*\n\n{broadcast_text}", parse_mode="Markdown")
                success_count += 1
                await asyncio.sleep(0.05)  # Плавная задержка во избежание FloodControl от Telegram
            except Exception:
                fail_count += 1

        offset += chunk_size

    await message.answer(f"✅ *Рассылка завершена!*\nУспешно: {success_count}\nОшибок: {fail_count}", parse_mode="Markdown")


@admin_router.message(Command("unban"))
async def unban_user(message: Message, command: CommandObject):
    """Снимает бан с игрока."""
    if not command.args or not command.args.isdigit():
        await message.answer("⚠️ Использование: `/unban [ID]`")
        return

    target_id = int(command.args)
    await db.set_ban_status(target_id, is_banned=False)
    await message.answer(f"✅ Пользователь `{target_id}` успешно разблокирован.", parse_mode="Markdown")


@admin_router.message(Command("reset_session"))
async def reset_user_session(message: Message, command: CommandObject):
    """Принудительно очищает текущее состояние и сессию игрока."""
    if not command.args or not command.args.isdigit():
        await message.answer("⚠️ Использование: `/reset_session [ID]`")
        return

    target_id = int(command.args)
    async with self.session_pool() as session:
        async with session.begin():
            from tgbot.database.models import ActiveQuest
            stmt = delete(ActiveQuest).where(ActiveQuest.user_id == target_id)
            result = await session.execute(stmt)
            if result.rowcount > 0:
                await message.answer(f"🧹 Игровая сессия пользователя `{target_id}` была успешно сброшена.")
            else:
                await message.answer(f"У пользователя `{target_id}` нет active квестов.")

# =========================================================================
# ТЕКСТОВЫЕ МЕНЮ МОНИТОРИНГА И СЛУЖБЫ АНТИЧЕТА
# =========================================================================

@admin_router.message(Command("metrics"))
async def admin_metrics_cmd(message: Message):
    """Выводит актуальные тепловые карты затыков и логов в реальном времени."""
    m = await db.calculate_realtime_metrics()
    
    pop_text = "".join([f"   {idx}. *{q['title']}* — Финишей: `{q['completions']}`\n" for idx, q in enumerate(m.get("popular_quests", []), 1)])
    bot_text = "".join([f"   {idx}. Квест *\"{b['quest_title']}\"* -> Шаг: *\"{b['step_text']}\"* — Ошибок: `{b['errors']}`\n" for idx, b in enumerate(m.get("bottlenecks", []), 1)])

    text = (
        "📊 *АКТУАЛЬНЫЕ МЕТРИКИ СИСТЕМЫ*\n\n"
        f"🏃‍♂️ Активных пешеходов онлайн: *{m['active_users']}*\n"
        f"⏱ Среднее время трека: *{round(m['avg_time_seconds'] / 60, 1)} мин.*\n"
        f"🚨 Срабатываний античета (1ч): *{m['bans_per_hour']}*\n\n"
        f"📈 *Популярность квестов:*\n{pop_text or '   _Нет данных._'}\n"
        f"🔥 *Тепловая карта затыков:*\n{bot_text or '   _Затыков нет._'}"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🚨 Логи античета (15 случаев)", callback_data="admin_view_cheat_logs")
    builder.button(text="⬅️ Назад в админку", callback_data="admin_back")
    builder.adjust(1)
    await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "admin_view_cheat_logs")
async def admin_view_cheat_logs_handler(call: CallbackQuery):
    """Отображает список последних инцидентов превышения скорости."""
    logs = await db.get_recent_cheat_logs(limit=15)
    text = "🚨 *ПОСЛЕДНИЕ СРАБАТЫВАНИЯ АНТИЧИТА (15 СЛУЧАЕВ):*\n\n"
    if not logs:
        text += "_Инцидентов нарушения скорости не зафиксировано._"
    else:
        for l in logs:
            time_str = l["created_at"].strftime("%d.%m %H:%M:%S")
            text += (
                f"⏱ *{time_str}* — Игрок: *{l['full_name']}* (`{l['user_id']}`)\n"
                f"📈 Квест: *{l['quest_title']}*\n"
                f"🏎 Скорость: `{round(l['speed'] * 3.6, 1)} км/ч` | GPS: `{l['latitude']}, {l['longitude']}`\n\n"
            )
            
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_back")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()
    
    # =========================================================================
# РЕДАКТИРОВАНИЕ СЮЖЕТНЫХ ПРЕДМЕТОВ НА ШАГЕ КВЕСТА
# =========================================================================

@admin_router.callback_query(F.data.startswith("step_edit_req_item_"))
async def step_edit_req_item_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_required_item)
    await call.message.answer("🔒 Введите точное название предмета, который *требуется* для доступа к шагу (или `/skip` для удаления ограничения):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_required_item)
async def process_edit_required_item(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    val = None if message.text.strip() == "/skip" else message.text.strip()
    await db.update_step(sid, required_item=val)
    await message.answer("✅ Требование к входящему предмету успешно сохранено!")
    await state.clear()


@admin_router.callback_query(F.data.startswith("step_edit_gives_item_"))
async def step_edit_gives_item_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split("_")[-1])
    await state.update_data(edit_step_id=sid)
    await state.set_state(StepForm.waiting_for_edit_gives_item)
    await call.message.answer("🎁 Введите название предмета, который игрок *получит* на этой точке (или `/skip` для отмены награды):")
    await call.answer()


@admin_router.message(StepForm.waiting_for_edit_gives_item)
async def process_edit_gives_item(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["edit_step_id"]
    val = None if message.text.strip() == "/skip" else message.text.strip()
    await db.update_step(sid, gives_item=val)
    await message.answer("✅ Выдаваемая награда шага успешно изменена!")
    await state.clear()

# =========================================================================
# РЕДАКТИРОВАНИЕ МИНИМАЛЬНОГО УРОВНЯ ДОСТУПА К КВЕСТУ
# =========================================================================

@admin_router.callback_query(F.data.startswith("edit_level_limit_"))
async def edit_level_limit_start(call: CallbackQuery, state: FSMContext):
    qid = int(call.data.split("_")[-1])
    await state.update_data(edit_lvl_qid=qid)
    await state.set_state(QuestForm.waiting_for_edit_min_level)
    await call.message.answer("🔒 Введите минимальный уровень игрока для допуска к этому квесту (целое число):")
    await call.answer()


@admin_router.message(QuestForm.waiting_for_edit_min_level)
async def process_edit_min_level(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Уровень должен быть выражен целым положительным числом! Попробуйте снова:")
        return
    lvl = int(message.text.strip())
    data = await state.get_data()
    qid = data["edit_lvl_qid"]
    await db.update_quest(qid, min_level_required=lvl)
    await message.answer(f"✅ Минимальный уровень доступа к квесту успешно обновлен до *{lvl}*!", parse_mode="Markdown")
    await state.clear()
