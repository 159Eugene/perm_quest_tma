import datetime
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List, Union
from difflib import SequenceMatcher
from redis.asyncio import Redis

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import select, desc, func, and_ 
from tgbot.database.db_api import db, get_utc_now
from tgbot.database.models import User, Quest, SystemSettings, InventoryItem, ShopItem, ShopItemType, QuestProgress  # <-- Добавлены недостающие модели для инвентаря и магазина
from tgbot.handlers.user_quest import TutorialState, start_tutorial_for_user

logger = logging.getLogger(__name__)
common_router = Router()


class DailyRiddleFSM(StatesGroup):
    """
    Состояния FSM для прохождения ежедневных загадок.
    """
    waiting_for_answer = State()


class MarketTradingFSM(StatesGroup):
    """
    Состояния FSM для торговли в лавках скупщиков.
    """
    waiting_for_gps = State()
    trading_menu = State()


def _match(text_a: str, text_b: str) -> float:
    """Вспомогательная синхронная функция расчета коэффициента подобия строк."""
    return SequenceMatcher(None, text_a, text_b).ratio()


async def calculate_matcher(text_a: str, text_b: str) -> float:
    """
    Неблокирующая функция расчета подобия строк, запускаемая в asyncio.to_thread
    для разгрузки главного цикла событий (Event Loop) от CPU-интенсивных операций.
    """
    return await asyncio.to_thread(_match, text_a, text_b)


# =========================================================================
# ГЛАВНАЯ ТОЧКА ВХОДА И МЕНЮ ИГРЫ (/start)
# =========================================================================

@common_router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    """
    Точка входа в бота. Проверяет регистрацию пользователя, прохождение Квеста №0
    и выводит список доступных квестов с проверкой допуска по минимальному уровню.
    """
    user_data = await db.get_or_create_user(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username
    )
    
    # Если обучение (Квест №0) не пройдено — принудительно отправляем на него
    if not user_data.completed_tutorial:
        await start_tutorial_for_user(message, state)
        return

    quests = await db.get_published_quests()
    
    builder = InlineKeyboardBuilder()
    for q in quests:
        # Проверка уровня игрока для вывода статуса допуска (гейт-контроль #3)
        if user_data.level >= q.min_level_required:
            builder.button(
                text=f"🏃‍♂️ {q.title} [Lvl {q.min_level_required}+]", 
                callback_data=f"start_quest_{q.id}"
            )
        else:
            builder.button(
                text=f"🔒 {q.title} (с {q.min_level_required} уровня)", 
                callback_data=f"quest_locked_level_{q.min_level_required}"
            )
    
    builder.button(text="👤 Мой профиль", callback_data="show_profile")
    builder.button(text="🏪 Торговые лавки скупщиков", callback_data="show_markets")
    builder.button(text="📅 Загадка дня (/daily)", callback_data="start_daily_riddle")
    builder.button(text="🏆 Сезонные рейтинги (/rating)", callback_data="show_seasonal_ratings_0")
    builder.adjust(1)

    text = (
        f"👋 Рады видеть тебя в Перми, {message.from_user.full_name}!\n\n"
        "Добро пожаловать в *Perm Quest Bot* — интерактивные пешие приключения по историческому центру города!\n\n"
        f"🎖 Ваш текущий уровень: *{user_data.level}*\n"
        f"🪙 Баланс: *{user_data.coins} монет*\n\n"
        "👇 Выберите один из доступных квестов или перейдите в меню:"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.callback_query(F.data.startswith("quest_locked_level_"))
async def quest_locked_level_handler(call: CallbackQuery):
    """Оповещает пользователя о блокировке квеста по уровню."""
    req_level = call.data.split("_")[-1]
    await call.answer(f"🔒 Доступно только с {req_level} уровня! Накапливайте XP.", show_alert=True)


# =========================================================================
# ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ И СБОР ПАССИВНОЙ ПРИБЫЛИ (#5)
# =========================================================================

@common_router.message(Command("profile"))
@common_router.callback_query(F.data == "show_profile")
async def user_profile_cmd(event: Message | CallbackQuery, state: FSMContext):
    """Отрисовывает личный кабинет игрока с выводом уровня, опыта, перков и пассивного дохода."""
    user_id = event.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        user_data = await db.get_or_create_user(
            telegram_id=user_id,
            full_name=event.from_user.full_name,
            username=event.from_user.username
        )

    # Принудительное обучение
    if not user_data.completed_tutorial:
        await start_tutorial_for_user(event, state)
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    cfg = await db.get_system_settings()

    balance = user_data.coins
    karma = user_data.karma
    streak = user_data.daily_streak
    xp = user_data.xp
    level = user_data.level
    xp_needed = level * 150
    
    # Безопасный расчет шкалы прогресса опыта
    bar_length = 10
    filled_length = int(round(bar_length * xp / xp_needed)) if xp_needed > 0 else 0
    progress_bar = "█" * filled_length + "░" * (bar_length - filled_length)

    inv = await db.get_user_inventory(user_id)
    ach = await db.get_user_achievements(user_id)

    class_info = {
        "merchant": f"💰 *Купец* (+{cfg.merchant_bonus}% золота за точки)",
        "ranger": f"⏱ *Следопыт* (подсказки раз в {cfg.ranger_cd_minutes} минут)",
        "historian": f"📈 *Историк* (множитель очков x{cfg.historian_mult})"
    }
    class_text = f"🎭 Класс персонажа: {class_info.get(user_data.rpg_class, '❌ _Не выбран_')}"

    text = (
        f"👤 *ПРОФИЛЬ ИГРОКА:* {event.from_user.full_name}\n\n"
        f"🎖 Уровень: *{level}* (XP: `{xp}/{xp_needed}`)\n"
        f"└  `[{progress_bar}]` — {round((xp/xp_needed)*100, 1) if xp_needed > 0 else 0}%\n"
        f"{class_text}\n\n"
        f"🪙 Баланс кошелька: *{balance} монет*\n"
        f"☯️ Карма в диалогах: *{karma}*\n"
        f"🔥 Серия загадок (стрик): *{streak} дней*\n"
        f"🎒 Предметов в рюкзаке: *{len(inv)} шт.*\n"
        f"🏆 Открытых достижений: *{len(ach)} шт.*"
    )

    if user_data.income_buffer > 0:
        text += f"\n\n💰 Накопленный пассивный доход: *{user_data.income_buffer} монет*"
    
    builder = InlineKeyboardBuilder()
    
    # Кнопка сбора прибыли от элитных артефактов (накапливается в буфер раз в час #5)
    if user_data.income_buffer > 0:
        builder.button(text=f"💰 Собрать прибыль (+{user_data.income_buffer} 🪙)", callback_data="claim_passive_income")

    builder.button(text="🎭 Сменить RPG-класс", callback_data="change_class_menu")
    builder.button(text="🎁 Ежедневный подарок за вход", callback_data="claim_daily_gift")
    builder.button(text=f"🧪 Купить эликсир Кармы ({cfg.karma_elixir_price} 🪙)", callback_data="buy_karma_elixir")
    
    builder.button(text="🎒 Мой рюкзак", callback_data="show_inventory")
    builder.button(text="📜 Достижения", callback_data="show_achievements")
    builder.button(text="🏪 Магазин наград", callback_data="show_shop")
    builder.button(text="📖 История прохождений", callback_data="show_history_records")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_start")
    
    builder.adjust(1)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.callback_query(F.data == "claim_passive_income")
async def claim_passive_income_handler(call: CallbackQuery, state: FSMContext):
    """Осуществляет сбор пассивного дохода из буфера игрока на основной баланс."""
    user_id = call.from_user.id
    
    # Кросс-совместимый вызов метода сбора буфера пассивного дохода СУБД
    if hasattr(db, "collect_passive_income_buffer"):
        claimed_amount = await db.collect_passive_income_buffer(user_id)
    else:
        claimed_amount = await db.claim_income_buffer(user_id)
        
    if claimed_amount > 0:
        await call.answer(f"💰 Вы успешно забрали прибыль: +{claimed_amount} монет!", show_alert=True)
    else:
        await call.answer("Буфер пассивного дохода пуст.", show_alert=True)
        
    await user_profile_cmd(call, state)


# =========================================================================
# СМЕНА RPG-КЛАССОВ И СЕЗОННЫЙ КУЛДАУН
# =========================================================================

@common_router.callback_query(F.data == "change_class_menu")
async def change_class_menu_handler(call: CallbackQuery):
    """Отрисовывает меню доступных RPG-классов с описанием перков."""
    cfg = await db.get_system_settings()
    text = (
        "🎭 *СМЕНА ИГРОВОГО КЛАССА*\n\n"
        "Вы можете изменить свой класс на любой другой. Смена класса доступна *раз в 30 дней* (раз в игровой сезон):\n\n"
        f"• *Купец*: +{cfg.merchant_bonus}% монет за прохождение контрольных точек.\n"
        f"• *Следопыт*: Кулдаун бесплатной подсказки снижен до {cfg.ranger_cd_minutes} минут.\n"
        f"• *Историк*: Увеличивает очки рейтинга (score) за все ответы квестов в {cfg.historian_mult} раза."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Стать Купцом", callback_data="set_class_merchant")
    builder.button(text="🏹 Стать Следопытом", callback_data="set_class_ranger")
    builder.button(text="📜 Стать Историком", callback_data="set_class_historian")
    builder.button(text="⬅️ Отмена", callback_data="show_profile")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data.startswith("set_class_"))
async def set_user_class_callback(call: CallbackQuery, state: FSMContext):
    """Присваивает выбранный RPG-класс игроку с проверкой сезонного лимита."""
    selected_class = call.data.replace("set_class_", "")
    user_id = call.from_user.id
    
    success, days_left = await db.update_user_class_with_cooldown(user_id, selected_class, cooldown_days=30)
    
    if not success:
        await call.answer(
            f"❌ Класс уже изменялся недавно!\nВы сможете сменить его снова через {days_left} дн.", 
            show_alert=True
        )
    else:
        class_names = {
            "merchant": "Купец 💰",
            "ranger": "Следопыт 🏹",
            "historian": "Историк 📜"
        }
        await call.answer(f"🎉 Вы успешно переключились на класс: {class_names.get(selected_class)}!", show_alert=True)
        
    await user_profile_cmd(call, state)


# =========================================================================
# ЕЖЕДНЕВНЫЕ ПОДАРКИ ЗА ВХОД
# =========================================================================

@common_router.callback_query(F.data == "claim_daily_gift")
async def claim_daily_gift_handler(call: CallbackQuery, state: FSMContext):
    """Начисляет ежедневный подарок с накопительным стриком входа."""
    user_id = call.from_user.id
    success, coins, streak = await db.claim_daily_gift(user_id)
    
    if not success:
        await call.answer("⏳ Вы уже получили подарок сегодня! Возвращайтесь завтра.", show_alert=True)
    else:
        await db.add_xp(user_id, 30)  # Бонус входа +30 XP
        await call.answer(
            f"🎁 Подарок получен!\n\nНачислено: +{coins} 🪙 и +30 XP!\nТекущая серия входов: {streak} дней подряд!", 
            show_alert=True
        )
    await user_profile_cmd(call, state)


# =========================================================================
# КУПЛЯ-ПРОДАЖА ЭЛИКСИРОВ КАРМЫ
# =========================================================================

@common_router.callback_query(F.data == "buy_karma_elixir")
async def buy_karma_elixir_handler(call: CallbackQuery, state: FSMContext):
    """Покупка эликсира кармы на основе экономических параметров СУБД."""
    user_id = call.from_user.id
    cfg = await db.get_system_settings()

    if not await db.deduct_coins(user_id, cfg.karma_elixir_price):
        await call.answer(f"❌ Недостаточно монет! Эликсир стоит {cfg.karma_elixir_price} 🪙.", show_alert=True)
    else:
        await db.update_karma(user_id, cfg.karma_elixir_effect)
        await call.answer(f"🧪 Вы выпили эликсир Кармы!\n\nВаша репутация поднялась на +{cfg.karma_elixir_effect} Кармы.", show_alert=True)
        
    await user_profile_cmd(call, state)

# =========================================================================
# ПРОСМОТР ИСТОРИИ ПРОХОЖДЕНИЙ КВЕСТОВ И КНОПКА ВОЗВРАТА В МЕНЮ
# =========================================================================

@common_router.callback_query(F.data == "show_history_records")
async def show_history_records_handler(call: CallbackQuery):
    """
    Выводит архив всех завершенных пользователем квестов с подробной
    игровой статистикой (набранные очки, затраченное время, ошибки).
    """
    user_id = call.from_user.id
    
    async with db.session_pool() as session:
        stmt = (
            select(QuestProgress, Quest.title)
            .join(Quest, QuestProgress.quest_id == Quest.id)
            .where(QuestProgress.user_id == user_id)
            .order_by(desc(QuestProgress.completed_at))
        )
        res = await session.execute(stmt)
        records = res.all()

    text = "📖 *ВАША ИСТОРИЯ ПРОХОЖДЕНИЙ КВЕСТОВ ПЕРМИ:*\n\n"
    if not records:
        text += "_Вы пока не завершили ни одного городского квеста._"
    else:
        for idx, (progress, quest_title) in enumerate(records, 1):
            time_formatted = str(datetime.timedelta(seconds=progress.total_time_seconds))
            date_str = progress.completed_at.strftime("%d.%m.%Y в %H:%M")
            text += (
                f"{idx}. Квест: *\"{quest_title}\"* — `[{date_str}]`\n"
                f"   ⏱ Общее время: `{time_formatted}`\n"
                f"   📈 Набранные очки: `{progress.score} очков`\n"
                f"   🚨 Допущено ошибок: `{progress.errors_count}`\n\n"
            )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в профиль", callback_data="show_profile")
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data == "back_to_main_start")
async def back_to_main_start(call: CallbackQuery):
    """
    Возвращает пользователя в главное меню со списком доступных квестов.
    """
    user_data = await db.get_user(call.from_user.id)
    quests = await db.get_published_quests()
    
    builder = InlineKeyboardBuilder()
    for q in quests:
        if user_data and user_data.level >= q.min_level_required:
            builder.button(
                text=f"🏃‍♂️ {q.title} [Lvl {q.min_level_required}+]", 
                callback_data=f"start_quest_{q.id}"
            )
        else:
            builder.button(
                text=f"🔒 {q.title} (с {q.min_level_required} уровня)", 
                callback_data=f"quest_locked_level_{q.min_level_required}"
            )
            
    builder.button(text="👤 Мой профиль", callback_data="show_profile")
    builder.button(text="🏪 Торговые лавки скупщиков", callback_data="show_markets")
    builder.button(text="📅 Загадка дня (/daily)", callback_data="start_daily_riddle")
    builder.button(text="🏆 Сезонные рейтинги (/rating)", callback_data="show_seasonal_ratings_0")
    builder.adjust(1)
    
    text = (
        f"👋 Рады тебя видеть, {call.from_user.full_name}!\n\n"
        "👇 Выберите один из доступных квестов или перейдите в меню:"
    )
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


# =========================================================================
# ПОЛНОЦЕННЫЙ РЮКЗАК, РАСХОДНИКИ И МЕХАНИКА СБРОСА ПРИ ПЕРЕГРУЗЕ (#27)
# =========================================================================

@common_router.message(F.text == "🎒 Мой рюкзак")
@common_router.callback_query(F.data == "show_inventory")
async def inventory_cmd(event: Message | CallbackQuery):
    """
    Открывает интерфейс рюкзака с выводом веса предметов и лимита грузоподъемности.
    Позволяет утилизировать вещи при перегрузе или использовать CONSUMABLE-предметы.
    """
    user_id = event.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    # Загружаем инвентарь игрока
    async with db.session_pool() as session:
        stmt = select(InventoryItem).where(InventoryItem.user_id == user_id).order_by(InventoryItem.id)
        res = await session.execute(stmt)
        items = res.scalars().all()

    curr_weight = await db.get_user_current_weight(user_id)
    max_cap = user_data.max_weight_capacity

    overloaded_warn = ""
    if curr_weight > max_cap:
        overloaded_warn = "🚨 *ВНИМАНИЕ! РЮКЗАК ПЕРЕГРУЖЕН!* Срочно утилизируйте лишний вес, иначе продвижение по точкам будет заблокировано!\n\n"

    text = (
        f"🎒 *ВАШ ИНВЕНТАРЬ (ГРУЗОПОДЪЕМНОСТЬ: {curr_weight}/{max_cap} КГ)*\n\n"
        f"{overloaded_warn}"
        "Найденные реликвии, премиум-билеты и наемники. "
        "Физические артефакты имеют вес, билеты и промокоды — 0 кг.\n\n"
    )
    
    builder = InlineKeyboardBuilder()
    
    if not items:
        text += "_Ваш рюкзак абсолютно пуст. Ищите артефакты на локациях города!_"
    else:
        for idx, item in enumerate(items, 1):
            weight_text = f" ({item.weight} кг)" if item.weight > 0 else " (0 кг)"
            income_text = f" [Доход: +{item.income_per_hour} 🪙/ч]" if item.generates_income else ""
            text += f"{idx}. *{item.item_name}*{weight_text}{income_text}\n"
            
            # Извлекаем характеристики предмета из СУБД для проверки CONSUMABLE типа
            async with db.session_pool() as session:
                shop_item = (await session.execute(
                    select(ShopItem).where(ShopItem.item_name == item.item_name)
                )).scalar_one_or_none()
            
            # Кнопка быстрой активации расходника
            if shop_item and shop_item.item_type == ShopItemType.CONSUMABLE:
                builder.button(text=f"🧪 Активировать {item.item_name[:20]}", callback_data=f"use_consumable_{item.id}")
            
            # Кнопка утилизации / ручного выброса предмета при перегрузе
            builder.button(text=f"🗑 Выбросить {item.item_name[:15]}", callback_data=f"drop_item_profile_{item.id}")

# Умный гейтбэк: если пришли через инлайн-кнопку профиля (CallbackQuery), возвращаем в профиль.
    # Если кликнули текстовую кнопку на карте квеста (Message) — возвращаем в активный квест.
    active = await db.get_active_quest(user_id)
    if active and isinstance(event, Message):
        builder.button(text="⬅️ Вернуться в квест", callback_data="resume_active_quest_from_bag")
    else:
        builder.button(text="⬅️ Назад в профиль", callback_data="show_profile")
    builder.adjust(1)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.callback_query(F.data.startswith("use_consumable_"))
async def use_consumable_callback_handler(call: CallbackQuery):
    """Активирует расходный материал и удаляет его из базы."""
    user_id = call.from_user.id
    item_id = int(call.data.split("_")[-1])

    async with db.session_pool() as session:
        item = await session.get(InventoryItem, item_id)
    
    if not item or item.user_id != user_id:
        await call.answer("Предмет отсутствует в вашем рюкзаке.", show_alert=True)
        return

    success, message = await db.activate_consumable_item(user_id, item.item_name)
    await call.answer(message, show_alert=True)
    await inventory_cmd(call)


@common_router.callback_query(F.data.startswith("drop_item_profile_"))
async def drop_item_profile_handler(call: CallbackQuery):
    """Выбрасывает вещь из профиля с предупреждением игрока о возможной сюжетной потере."""
    user_id = call.from_user.id
    item_id = int(call.data.split("_")[-1])

    async with db.session_pool() as session:
        item = await session.get(InventoryItem, item_id)
        
    if not item or item.user_id != user_id:
        await call.answer("Предмет не найден.", show_alert=True)
        return

    item_name = item.item_name
    await db.discard_inventory_item(user_id, item_name)
    await call.answer(
        f"⚠️ Предмет выброшен.\n\nВнимание: некоторые предметы могут понадобиться для прохождения будущих квестов!", 
        show_alert=True
    )
    await inventory_cmd(call)


# =========================================================================
# ГЕОГРАФИЧЕСКИЕ ТОРГОВЫЕ ЛАВКИ СКУПЩИКОВ В ПЕРМИ (#13)
# =========================================================================

def local_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Вычисляет расстояние в метрах между двумя точками."""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


import math # Добавлен импорт библиотеки для математического вычисления

@common_router.callback_query(F.data == "show_markets")
async def show_markets_handler(call: CallbackQuery):
    """Выводит список созданных администратором рынков и лавок скупщиков на карте Перми."""
    markets = await db.get_all_markets()
    text = "🏪 *СПИСОК ДОСТУПНЫХ ЛАВОК СКУПЩИКОВ В ПЕРМИ:*\n\n"
    builder = InlineKeyboardBuilder()
    
    if not markets:
        text += "_На данный момент в Перми нет активных лавок скупщиков._"
    else:
        for m in markets:
            text += f"📍 *{m.name}* (Вход в радиусе: `{int(m.radius)}м`)\n"
            builder.button(text=f"🚪 Войти в лавку \"{m.name[:20]}\"", callback_data=f"select_market_{m.id}")
            
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_start")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data.startswith("select_market_"))
async def select_market_handler(call: CallbackQuery, state: FSMContext):
    """Инициирует FSM-стейт ввода GPS-позиции для допуска на скупку."""
    market_id = int(call.data.split("_")[-1])
    await state.update_data(trading_market_id=market_id)
    await state.set_state(MarketTradingFSM.waiting_for_gps)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить GPS лавки", request_location=True)],
            [KeyboardButton(text="❌ Выход из лавки")]
        ],
        resize_keyboard=True
    )
    await call.message.answer(
        "📍 Для входа в лавку скупщика подтвердите свое присутствие на месте. "
        "Пожалуйста, отправьте свои текущие GPS-координаты с телефона:",
        reply_markup=kb
    )
    await call.answer()


@common_router.message(MarketTradingFSM.waiting_for_gps, F.text == "❌ Выход из лавки")
@common_router.message(MarketTradingFSM.trading_menu, F.text == "❌ Выход из лавки")
async def exit_market_text(message: Message, state: FSMContext):
    """Выход из лавки по текстовой кнопке с перенаправлением в список лавок."""
    await state.clear()
    await message.answer("👋 Вы покинули лавку скупщика.", reply_markup=ReplyKeyboardRemove())
    
    markets = await db.get_all_markets()
    text = "🏪 *СПИСОК ДОСТУПНЫХ ЛАВОК СКУПЩИКОВ В ПЕРМИ:*\n\n"
    builder = InlineKeyboardBuilder()
    if not markets:
        text += "_На данный момент в Перми нет активных лавок скупщиков._"
    else:
        for m in markets:
            text += f"📍 *{m.name}* (Вход в радиусе: `{int(m.radius)}м`)\n"
            builder.button(text=f"🚪 Войти в лавку \"{m.name[:20]}\"", callback_data=f"select_market_{m.id}")
            
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_start")
    builder.adjust(1)
    await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.callback_query(F.data == "exit_market")
async def exit_market_callback(call: CallbackQuery, state: FSMContext):
    """Выход из скупки через инлайн-кнопку с бесшовным возвратом к списку лавок."""
    await state.clear()
    
    markets = await db.get_all_markets()
    text = "🏪 *СПИСОК ДОСТУПНЫХ ЛАВОК СКУПЩИКОВ В ПЕРМИ:*\n\n"
    builder = InlineKeyboardBuilder()
    if not markets:
        text += "_На данный момент в Перми нет активных лавок скупщиков._"
    else:
        for m in markets:
            text += f"📍 *{m.name}* (Вход в радиусе: `{int(m.radius)}м`)\n"
            builder.button(text=f"🚪 Войти в лавку \"{m.name[:20]}\"", callback_data=f"select_market_{m.id}")
            
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_start")
    builder.adjust(1)
    
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer("👋 Вы покинули лавку скупщика.")

@common_router.edited_message(MarketTradingFSM.waiting_for_gps, F.location)
async def market_live_location_handler(message: Message, state: FSMContext):
    """Автоматически впускает в лавку, если у игрока включена трансляция геопозиции (Live Location)."""
    data = await state.get_data()
    market_id = data.get("trading_market_id")
    if not market_id:
        return
        
    market = await db.get_market_by_id(market_id)
    if not market:
        return

    dist = local_haversine_distance(message.location.latitude, message.location.longitude, market.latitude, market.longitude)
    
    if dist <= market.radius:
        await state.set_state(MarketTradingFSM.trading_menu)
        await show_trading_menu(message, market_id, state)


@common_router.message(MarketTradingFSM.waiting_for_gps, F.location)
async def market_gps_handler(message: Message, state: FSMContext):
    """Верифицирует геопозицию игрока и допускает в меню торговли при нахождении в радиусе."""
    data = await state.get_data()
    market_id = data.get("trading_market_id")
    market = await db.get_market_by_id(market_id)
    if not market:
        await message.answer("Лавка не найдена.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    dist = local_haversine_distance(message.location.latitude, message.location.longitude, market.latitude, market.longitude)
    
    if dist > market.radius:
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📍 Отправить GPS лавки", request_location=True)],
                [KeyboardButton(text="❌ Выход из лавки")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            f"❌ *Вы слишком далеко от лавки скупщика!*\n\n"
            f"Расстояние до неё: *{int(dist)} метров* при радиусе входа *{int(market.radius)} метров*.\n"
            f"Подойдите ближе к локации \"{market.name}\" и попробуйте снова!",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    await state.set_state(MarketTradingFSM.trading_menu)
    await show_trading_menu(message, market_id, state)


async def show_trading_menu(message: Message, market_id: int, state: FSMContext):
    """Отрисовывает интерфейс торговли в скупке с предварительным уничтожением Reply-кнопок."""
    market = await db.get_market_by_id(market_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Продать вещи из рюкзака", callback_data=f"market_sell_list_{market_id}")
    builder.button(text="📦 Купить редкие артефакты", callback_data=f"market_buy_list_{market_id}")
    builder.button(text="❌ Выйти из лавки", callback_data="exit_market")
    builder.adjust(1)
    
    await message.answer("🚪 Входим в торговую лавку...", reply_markup=ReplyKeyboardRemove())
    
    await message.answer(
        f"🏪 *ДОБРО ПОЖАЛОВАТЬ В ЛАВКУ: {market.name}!*\\n\\n"
        f"Скупщик прищуривается и раскладывает свои весы.\\n"
        f"Вы можете выгодно сдать старые артефакты из рюкзака "
        f"или прикупить уникальные вещи, недоступные в глобальном магазине!",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )



@common_router.callback_query(F.data.startswith("market_back_"))
async def market_back(call: CallbackQuery, state: FSMContext):
    """Возврат в стартовое меню торговли."""
    market_id = int(call.data.split("_")[-1])
    await show_trading_menu(call.message, market_id, state)
    await call.answer()


@common_router.callback_query(F.data.startswith("market_sell_list_"))
async def market_sell_list(call: CallbackQuery):
    """Формирует список вещей игрока, доступных для обратного выкупа лавкой."""
    market_id = int(call.data.split("_")[-1])
    user_id = call.from_user.id
    inventory = await db.get_user_inventory(user_id)
    
    text = "💰 *ПРОДАЖА СТАРЫХ ВЕЩЕЙ СКУПЩИКУ:*\n\n"
    builder = InlineKeyboardBuilder()
    
    sellable_count = 0
    for item_name in inventory:
        async with db.session_pool() as session:
            shop_item = (await session.execute(
                select(ShopItem).where(ShopItem.item_name == item_name)
            )).scalar_one_or_none()
            
        if shop_item:
            # Скупщик выкупает по buyback_price или по дефолтной цене в 50% от номинала
            price = shop_item.buyback_price if shop_item.buyback_price else int(shop_item.price * 0.5)
            text += f"▪️ *{item_name}* — Выкуп за: *{price} 🪙*\n"
            builder.button(text=f"Сдать {item_name[:20]} ({price} 🪙)", callback_data=f"market_sell_act_{market_id}_{item_name}")
            sellable_count += 1

    if sellable_count == 0:
        text += "_У вас нет предметов, подходящих для скупки (или ваш рюкзак пуст)._"
        
    builder.button(text="⬅️ Назад в меню лавки", callback_data=f"market_back_{market_id}")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data.startswith("market_sell_act_"))
async def market_sell_act(call: CallbackQuery):
    """Списание предмета при продаже и начисление золота."""
    parts = call.data.split("_")
    market_id = int(parts[3])
    item_name = "_".join(parts[4:])
    user_id = call.from_user.id
    
    success, price, msg = await db.sell_user_item_to_market(user_id, item_name, market_id)
    if success:
        await call.answer(f"✅ Успешно сдано скупщику за {price} 🪙!", show_alert=True)
    else:
        await call.answer(f"❌ Ошибка: {msg}", show_alert=True)
        
    await market_sell_list(call)


@common_router.callback_query(F.data.startswith("market_buy_list_"))
async def market_buy_list(call: CallbackQuery):
    """Отображает ассортимент редких артефактов, закрепленных за конкретной лавкой."""
    market_id = int(call.data.split("_")[-1])
    items = await db.get_market_items(market_id)
    
    text = "📦 *РЕДКИЕ ТУРИСТИЧЕСКИЕ ТОВАРЫ ЭТОЙ ЛАВКИ:*\n\n"
    builder = InlineKeyboardBuilder()
    
    if not items:
        text += "_В лавке временно закончился уникальный ассортимент товаров._"
    else:
        for item in items:
            weight_text = f" | Вес: `{item.weight} кг`" if item.weight > 0 else ""
            income_text = f" | Пассивный доход: `+{item.income_per_hour} 🪙/ч`" if item.generates_income else ""
            
            text += f"▪️ *{item.name}* — Цена: *{item.price} 🪙*{weight_text}{income_text}\n📝 {item.description}\n\n"
            builder.button(text=f"Купить {item.name[:20]} ({item.price} 🪙)", callback_data=f"market_buy_act_{market_id}_{item.id}")
            
    builder.button(text="⬅️ Назад в меню лавки", callback_data=f"market_back_{market_id}")
    builder.adjust(1)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data.startswith("market_buy_act_"))
async def market_buy_act(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Покупка артефакта у скупщика с проверкой весовых ограничений и транзакцией."""
    parts = call.data.split("_")
    market_id = int(parts[3])
    shop_item_id = int(parts[4])
    user_id = call.from_user.id
    
    shop_item = await db.get_shop_item_by_id(shop_item_id)
    if not shop_item:
        await call.answer("Товар исчез из лавки!", show_alert=True)
        return

    user = await db.get_user(user_id)
    if user.coins < shop_item.price:
        await call.answer("❌ Недостаточно монет для совершения покупки!", show_alert=True)
        return

    already_has = await db.check_item_in_inventory(user_id, shop_item.item_name)
    if already_has:
        await call.answer("❌ У вас уже есть этот артефакт в рюкзаке!", show_alert=True)
        return

    # Проверка грузоподъемности рюкзака (Overweight - #27)
    overloaded, curr, max_cap = await db.is_inventory_overloaded(user_id, shop_item.weight)
    if overloaded:
        await state.update_data(pending_item_name=shop_item.item_name)
        inv_items = await db.get_user_inventory(user_id)
        
        builder = InlineKeyboardBuilder()
        for item in inv_items:
            builder.button(text=f"🗑 Выбросить {item}", callback_data=f"drop_item_{item}")
        builder.button(text="🔄 Попробовать снова", callback_data="retry_add_pending_item")
        builder.adjust(1)
        
        await bot.send_message(
            user_id,
            f"🚨 *РЮКЗАК ПЕРЕГРУЖЕН!*\n\n"
            f"Вы пытаетесь купить предмет: *{shop_item.name}* (Вес: {shop_item.weight} кг).\n"
            f"Текущая грузоподъемность: *{curr}/{max_cap} кг*.\n\n"
            f"Пожалуйста, освободите рюкзак, выбросив ненужные вещи:",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await call.answer()
        return

    # Покупка
    success_payment = await db.deduct_coins(user_id, shop_item.price)
    if not success_payment:
        await call.answer("❌ Недостаточно монет!", show_alert=True)
        return

    await db.add_item_to_inventory(user_id, shop_item.item_name)
    await call.answer(f"🎉 Вы успешно купили: {shop_item.name}!", show_alert=True)
    await market_buy_list(call)


# =========================================================================
# КАТАЛОГ ТРОФЕЕВ, ДОСТИЖЕНИЙ И ВНУТРИИГРОВОЙ МАРКЕТ
# =========================================================================

@common_router.message(Command("achievements"))
@common_router.callback_query(F.data == "show_achievements")
async def list_user_achievements_cmd(event: Message | CallbackQuery):
    """Выводит все доступные в игре достижения и статус их разблокировки."""
    user_id = event.from_user.id
    all_ach = await db.get_all_achievements()
    user_ach = await db.get_user_achievements(user_id)
    user_ach_ids = {a.id for a in user_ach}

    text = "🏆 *ДОСТИЖЕНИЯ И ТРОФЕИ ПЛАТФОРМЫ:*\n\n"
    for ach in all_ach:
        status = "✅ Открыто" if ach.id in user_ach_ids else "🔒 Заблокировано"
        text += f"{ach.badge_emoji} *{ach.name}* — _{status}_\n📜 {ach.description}\n🎁 Бонус: {ach.reward_coins} 🪙\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в профиль", callback_data="show_profile")

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.message(Command("shop"))
@common_router.callback_query(F.data == "show_shop")
async def shop_show_catalog_cmd(event: Message | CallbackQuery):
    """Отрисовывает витрину предметов и промокодов глобального магазина наград."""
    items = await db.get_shop_items()
    builder = InlineKeyboardBuilder()
    
    text = (
        "🏪 *ГЛОБАЛЬНЫЙ КУПЕЧЕСКИЙ МАГАЗИН ПЕРМИ*\n\n"
        "Артефакты дают бонусы к прохождению. "
        "Промокоды на подарки в Перми можно обменять у партнеров, а билеты открывают новые треки!\n\n"
    )
    
    global_items = [i for i in items if i.market_id is None]
    
    if not global_items:
        text += "_Глобальный ассортимент товаров временно пуст._"
    else:
        for item in global_items:
            stock = await db.get_promo_codes_count(item.id) if item.item_type == ShopItemType.PROMO else None
            stock_text = f" | На складе: *{stock} шт.*" if stock is not None else ""
            weight_text = f" | Вес: `{item.weight} кг`" if item.weight > 0 else ""
            income_text = f" | Доход: `+{item.income_per_hour} 🪙/ч`" if item.generates_income else ""
            
            text += (
                f"📦 *[{item.id}] {item.name}* — {item.price} 🪙{stock_text}{weight_text}{income_text}\n"
                f"    └ Тип: `{item.item_type}`\n📝 {item.description}\n\n"
            )
            builder.button(text=f"Купить {item.name[:22]} ({item.price} 🪙)", callback_data=f"buy_shop_item_{item.id}")
        
    builder.button(text="⬅️ Назад в профиль", callback_data="show_profile")
    builder.adjust(1)
    
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@common_router.callback_query(F.data.startswith("buy_shop_item_"))
async def process_shop_purchase_callback(call: CallbackQuery, state: FSMContext, bot: Bot):
    """
    Покупка артефакта, промокода или квест-билета со строгим row locking СУБД 
    FOR UPDATE и весовым контролем перегруза.
    """
    item_id = int(call.data.split("_")[-1])
    user_id = call.from_user.id
    
    shop_item = await db.get_shop_item_by_id(item_id)
    if not shop_item:
        await call.answer("Товар исчез с прилавка!", show_alert=True)
        return

    if shop_item.item_type != ShopItemType.PROMO:
        already_has = await db.check_item_in_inventory(user_id, shop_item.item_name)
        if already_has:
            await call.answer("❌ Этот артефакт/билет уже лежит у вас в рюкзаке!", show_alert=True)
            return
        
        # Проверка лимитов перегруза
        overloaded, curr, max_cap = await db.is_inventory_overloaded(user_id, shop_item.weight)
        if overloaded:
            await state.update_data(pending_item_name=shop_item.item_name)
            inv_items = await db.get_user_inventory(user_id)
            
            builder = InlineKeyboardBuilder()
            for item in inv_items:
                builder.button(text=f"🗑 Выбросить {item}", callback_data=f"drop_item_{item}")
            builder.button(text="🔄 Попробовать снова", callback_data="retry_add_pending_item")
            builder.adjust(1)
            
            await bot.send_message(
                user_id,
                f"🚨 *РЮКЗАК ПЕРЕГРУЖЕН!*\n\n"
                f"Вы пытаетесь купить предмет: *{shop_item.name}* (Вес: {shop_item.weight} кг).\n"
                f"Текущая грузоподъемность: *{curr}/{max_cap} кг*.\n\n"
                f"Пожалуйста, освободите рюкзак, выбросив ненужные вещи:",
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            await call.answer()
            return

        success_payment = await db.deduct_coins(user_id, shop_item.price)
        if not success_payment:
            await call.answer("❌ Недостаточно монет для совершения покупки!", show_alert=True)
            return

        await db.add_item_to_inventory(user_id, shop_item.item_name)
        await call.answer(f"🎉 Вы успешно приобрели: {shop_item.name}!", show_alert=True)
    else:
        res = await db.buy_promo_item(user_id, item_id)
        if res == "insufficient_coins":
            await call.answer("❌ Недостаточно монет для покупки промокода!", show_alert=True)
        elif res == "no_stock":
            await call.answer("❌ Товар закончился в наличии на нашем сервере!", show_alert=True)
        elif res is not None:
            await call.message.answer(
                f"🎉 *УСПЕШНАЯ ПОКУПКА РЕАЛЬНОГО ПРИЗА!*\n\n"
                f"Вы приобрели: *{shop_item.name}*\n"
                f"🔑 Ваш промокод: `{res}`\n\n"
                f"Промокод также сохранен в вашем рюкзаке (инвентаре). Предъявите его на месте получения награды!",
                parse_mode="Markdown"
            )
            await call.answer()
        else:
            await call.answer("Произошла неизвестная ошибка при покупке.", show_alert=True)
            
    await shop_show_catalog_cmd(call)


# =========================================================================
# ЕЖЕДНЕВНЫЕ ЗАГАДКИ (ДЕЙЛИКИ) - СИСТЕМА ДИНАМИЧЕСКИХ СТРИКОВ
# =========================================================================

@common_router.message(Command("daily"))
@common_router.callback_query(F.data == "start_daily_riddle")
async def start_daily_riddle_handler(event: Message | CallbackQuery, state: FSMContext):
    """Случайным образом извлекает ежедневное испытание для игрока."""
    user_id = event.from_user.id
    user_data = await db.get_user(user_id)
    
    if user_data and not user_data.completed_tutorial:
        await start_tutorial_for_user(event, state)
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    if user_data and user_data.last_daily_at:
        now = get_utc_now()
        last_daily = user_data.last_daily_at
        if (now - last_daily).total_seconds() < 24 * 3600:
            msg = "⏳ Вы уже проходили ежедневное испытание сегодня. Возвращайтесь завтра!"
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return

    riddle = await db.get_random_daily_riddle()
    if not riddle:
        msg = "🧩 На сегодня нет доступных ежедневных загадок. Загляните позже!"
        if isinstance(event, CallbackQuery):
            await event.answer(msg, show_alert=True)
        else:
            await event.answer(msg)
        return

    await state.set_state(DailyRiddleFSM.waiting_for_answer)
    await state.update_data(riddle_id=riddle.id)

    text = (
        f"📅 *ЕЖЕДНЕВНОЕ RPG-ИСПЫТАНИЕ*\n\n"
        f"Загадка: *{riddle.question}*\n\n"
        f"🎁 Базовая награда: *{riddle.reward_coins} монет* и *+50 XP*!\n"
        f"🔥 Множитель за стрик входов активируется автоматически!\n\n"
        f"✍️ Напишите ваш текстовый ответ ниже:"
    )

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown")


@common_router.message(DailyRiddleFSM.waiting_for_answer)
async def process_daily_answer(message: Message, state: FSMContext):
    """Сверяет ответ загадки с нечетким SequenceMatcher-поиском в потоке."""
    data = await state.get_data()
    riddle_id = data["riddle_id"]
    riddle = await db.get_daily_riddle_by_id(riddle_id)
    
    user_text = message.text.strip().lower()
    correct_ans = riddle.correct_answer.strip().lower()

    ratio = await calculate_matcher(user_text, correct_ans)
    
    if ratio >= 0.85:
        streak, coins_granted = await db.process_daily_streak_logic(message.from_user.id, riddle.reward_coins)
        
        await message.answer(
            f"🎉 *Абсолютно верно!* (Сходство ответов: {round(ratio*100, 1)}%)\n\n"
            f"🔥 Серия ежедневных входов: *{streak} дней подряд.*\n"
            f"🪙 Вы получили с учетом множителя: *+{coins_granted} монет!*\n"
            f"🎖 Начислено: *+50 XP*!",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"❌ Увы, ответ неверный. Попробуйте в следующий раз!\n"
            f"(Правильный ответ скрыт для предотвращения спойлеров)."
        )
    
    await state.clear()


# =========================================================================
# РЕЙТИНГИ С ПОСТРАНИЧНОЙ ПАГИНАЦИЕЙ, ТИТУЛАМИ И REDIS-КЭШЕМ
# =========================================================================

@common_router.message(Command("rating"))
async def process_rating_menu_command(message: Message):
    """Выводит интерфейс таблиц лидеров различных игровых сезонов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Ежемесячный топ", callback_data="rating_view_month_0")
    builder.button(text="⏳ Ежегодный топ", callback_data="rating_view_year_0")
    builder.button(text="👑 Глобальный Топ-10", callback_data="rating_view_global_0")
    builder.button(text="⬅️ Назад", callback_data="back_to_main_start")
    builder.adjust(1)
    
    await message.answer(
        "🏆 *ТАБЛИЦА ЛИДЕРОВ КВЕСТОВ ПЕРМИ*\n\n"
        "Выберите временной период для генерации рейтингов текущего сезона:",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@common_router.callback_query(F.data.startswith("show_seasonal_ratings_"))
async def seasonal_ratings_menu_callback(call: CallbackQuery):
    """Инлайн-меню выбора категорий рейтингов."""
    text = (
        "🏆 *ТАБЛИЦА ЛИДЕРОВ КВЕСТОВ ПЕРМИ*\n\n"
        "Выберите временной период для генерации рейтингов текущего сезона:"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Ежемесячный топ", callback_data="rating_view_month_0")
    builder.button(text="⏳ Ежегодный топ", callback_data="rating_view_year_0")
    builder.button(text="👑 Глобальный Топ-10", callback_data="rating_view_global_0")
    builder.button(text="⬅️ Назад", callback_data="back_to_main_start")
    builder.adjust(1)

    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.callback_query(F.data.startswith("rating_view_"))
async def rating_view_callback(call: CallbackQuery, redis: Optional[Redis] = None):
    """
    Выводит пагинируемую таблицу лидеров и позицию самого игрока.
    Реализует кэширование в Redis (TTL 30 минут) для разгрузки СУБД.
    """
    parts = call.data.split("_")
    period = parts[2]
    offset = int(parts[3])
    limit = 10
    user_id = call.from_user.id
    
    leaders = None
    cache_key = f"leaderboard:{period}:{offset}"

    if redis:
        try:
            cached_data = await redis.get(cache_key)
            if cached_data:
                leaders = json.loads(cached_data.decode('utf-8'))
                logger.info(f"Рейтинг [{period}:{offset}] извлечен из кэша Redis.")
        except Exception as e:
            logger.error(f"Ошибка при чтении рейтинга из Redis: {e}")

    if not leaders:
        if period == "global":
            leaders = await db.get_leaderboard(limit=limit, offset=offset)
            title = "👑 *ГЛОБАЛЬНЫЙ ТОП ИГРОКОВ (ВСЕ ВРЕМЯ)*"
        else:
            leaders = await db.get_seasonal_leaderboard(period=period, limit=limit, offset=offset)
            title_period = "ТЕКУЩИЙ МЕСЯЦ" if period == "month" else "ТЕКУЩИЙ ГОД"
            title = f"📅 *СЕЗОННЫЙ ТОП ИГРОКОВ ({title_period})*"

        if redis and leaders:
            try:
                await redis.setex(cache_key, 1800, json.dumps(leaders))
            except Exception as e:
                logger.error(f"Ошибка при записи рейтинга в Redis: {e}")
    else:
        if period == "global":
            title = "👑 *ГЛОБАЛЬНЫЙ ТОП ИГРОКОВ (ВСЕ ВРЕМЯ)*"
        else:
            title_period = "ТЕКУЩИЙ МЕСЯЦ" if period == "month" else "ТЕКУЩИЙ ГОД"
            title = f"📅 *СЕЗОННЫЙ ТОП ИГРОКОВ ({title_period})*"

    user_rank_text = ""
    async with db.session_pool() as session:
        subq = (
            select(
                QuestProgress.user_id,
                func.sum(QuestProgress.score).label("total_score")
            )
            .group_by(QuestProgress.user_id)
            .subquery()
        )
        
        stmt_rank = (
            select(func.count(subq.c.user_id))
            .where(subq.c.total_score > (
                select(func.coalesce(func.sum(QuestProgress.score), 0))
                .where(QuestProgress.user_id == user_id)
                .scalar_subquery()
            ))
        )
        rank_res = await session.execute(stmt_rank, execution_options={"read_only": True})
        user_rank = rank_res.scalar() + 1
        
        stmt_score = select(func.sum(QuestProgress.score)).where(QuestProgress.user_id == user_id)
        user_score = (await session.execute(stmt_score, execution_options={"read_only": True})).scalar() or 0
        user_rank_text = f"📍 Ваша позиция в рейтинге: *#{user_rank}* (Очки: `{user_score}`)\n"

    if not leaders and offset == 0:
        text = f"{title}\n\n🏆 В этом сезоне пока нет завершенных прохождений.\n\n{user_rank_text}"
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ К выбору топа", callback_data="show_seasonal_ratings_0")
        builder.adjust(1)
    else:
        text = f"{title}\n\n"
        for idx, l in enumerate(leaders, 1):
            global_rank = offset + idx
            username_val = f" (@{l['username']})" if l['username'] else ""
            time_formatted = str(datetime.timedelta(seconds=l['total_time']))
            text += f"{global_rank}. *{l['full_name']}*{username_val} — {l['total_score']} очков ({time_formatted})\n     └ 🎭 {l['title']} | Lvl: `{l['level']}` | 🏆 {l['achievements_count']} дост.\n\n"

        text += f"---------------------------------\n{user_rank_text}"

        builder = InlineKeyboardBuilder()
        row_sizes = []
        
        # Если это не первая страница, добавляем кнопку "Назад"
        if offset > 0 and len(leaders) == limit:
            builder.button(text="⬅️ Назад", callback_data=f"rating_view_{period}_{offset - limit}")
            builder.button(text="➡️ Вперед", callback_data=f"rating_view_{period}_{offset + limit}")
            row_sizes.append(2) # Две кнопки в ряд
        elif offset > 0:
            builder.button(text="⬅️ Назад", callback_data=f"rating_view_{period}_{offset - limit}")
            row_sizes.append(1) # Одна кнопка в ряд
        elif len(leaders) == limit:
            builder.button(text="➡️ Вперед", callback_data=f"rating_view_{period}_{offset + limit}")
            row_sizes.append(1) # Одна кнопка в ряд
            
        # Кнопка возврата всегда идёт на отдельной строке снизу
        builder.button(text="⬅️ К выбору топа", callback_data="show_seasonal_ratings_0")
        row_sizes.append(1)
        
        # Динамически выстраиваем красивую сетку
        builder.adjust(*row_sizes)

    # Инструменты вывода выравниваются на уровень тела функции (4 пробела)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@common_router.message(Command("top"))
async def leaderboard_cmd(message: Message, redis: Optional[Redis] = None):
    """Текстовая команда для быстрого вывода Топ-10 глобальных игроков."""
    leaders = None
    cache_key = "leaderboard:global:0"

    if redis:
        try:
            cached_data = await redis.get(cache_key)
            if cached_data:
                leaders = json.loads(cached_data.decode('utf-8'))
        except Exception as e:
            logger.error(f"Ошибка при чтении топ-10 из Redis: {e}")

    if not leaders:
        leaders = await db.get_leaderboard(limit=10, offset=0)
        if redis and leaders:
            try:
                await redis.setex(cache_key, 1800, json.dumps(leaders))
            except Exception as e:
                logger.error(f"Ошибка записи топ-10 в Redis: {e}")

    if not leaders:
        await message.answer("🏆 Таблица лидеров пока пуста.")
        return

    text = "🏆 *ГЛОБАЛЬНЫЙ РЕЙТИНГ ИГРОКОВ ПЕРМИ*\n\n"
    for idx, l in enumerate(leaders, 1):
        username_val = f" (@{l['username']})" if l['username'] else ""
        time_formatted = str(datetime.timedelta(seconds=l['total_time']))
        text += f"{idx}. *{l['full_name']}*{username_val} — {l['total_score']} очков ({time_formatted})\n     └ 🎭 {l['title']} | Lvl: `{l['level']}` | 🏆 {l['achievements_count']} дост.\n\n"

    await message.answer(text, parse_mode="Markdown")


@common_router.message(Command("balance"))
async def balance_cmd(message: Message):
    """Выводит текущий баланс монет кошелька пользователя."""
    balance = await db.get_user_balance(message.from_user.id)
    await message.answer(f"🪙 Ваш текущий баланс: *{balance} монет*", parse_mode="Markdown")


@common_router.message(Command("help"))
async def help_cmd(message: Message):
    """Справочная информация по игровому процессу на платформе."""
    text = (
        "❓ *СПРАВОЧНИК ПО ИГРОВОМУ ПРОЦЕССУ*\n\n"
        "1. Для игры требуется включенный GPS в телефоне.\n"
        "2. Добравшись до нужной точки, отправьте геопозицию через кнопку «📍 Я на месте».\n"
        "3. После сверки геопозиции отправьте ответ текстом на загадку.\n"
        "4. Поддерживается умный парсер ответов — опечатки и падежи прощаются!\n"
        "5. Некоторые шаги доступны только НОЧЬЮ или в ДОЖДЬ/СНЕГ!\n"
        "6. На локациях вас могут встретить интерактивные NPC с диалогами и выбором вариантов.\n"
        "7. Выберите RPG-класс в своем профиле (/profile) для активации бонусов монет, подсказок или рейтинга!\n"
        "8. Следите за весом вашего рюкзака: перегруз лимита грузоподъемности не позволит сдавать шаги квестов!"
    )
    await message.answer(text, parse_mode="Markdown")