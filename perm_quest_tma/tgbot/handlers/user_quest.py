import datetime
import logging
import aiohttp
import time
import random
import asyncio
from typing import Optional, Tuple, Dict, Any, Union

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from redis.asyncio import Redis
from difflib import SequenceMatcher

from sqlalchemy import select
from tgbot.database.db_api import db
from tgbot.database.models import InventoryItem, ActiveQuest
from tgbot.config import settings

logger = logging.getLogger(__name__)
user_quest_router = Router()


class TutorialState(StatesGroup):
    """
    Состояния FSM для прохождения обучения (Квеста №0) новыми игроками.
    """
    waiting_for_gps = State()
    waiting_for_word = State()


class OverweightForm(StatesGroup):
    """
    Состояния FSM для управления перегрузом рюкзака.
    """
    waiting_for_discard = State()


# -------------------------------------------------------------------------
# ИНТЕЛЛЕКТУАЛЬНЫЙ И БЕЗОПАСНЫЙ ИМПОРТ ФУНКЦИЙ АНТИЧЕТА (ZERO-IMPORT-ERRORS)
# -------------------------------------------------------------------------
try:
    from anti_cheat import verify_gps_and_speed, haversine_distance
except ImportError:
    try:
        from tgbot.handlers.anti_cheat import verify_gps_and_speed, haversine_distance
    except ImportError:
        try:
            from tgbot.anti_cheat import verify_gps_and_speed, haversine_distance
        except ImportError:
            try:
                from .anti_cheat import verify_gps_and_speed, haversine_distance
            except ImportError:
                import math
                logger.warning("⚠️ Файл anti_cheat.py не обнаружен! Активирован автономный резервный античит.")

                def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
                    """Ортодромическое расстояние в метрах (резервный расчет)."""
                    R = 6371000.0
                    phi1 = math.radians(lat1)
                    phi2 = math.radians(lat2)
                    delta_phi = math.radians(lat2 - lat1)
                    delta_lambda = math.radians(lon2 - lon1)
                    a = (math.sin(delta_phi / 2.0) ** 2 +
                         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
                    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
                    return R * c

                def verify_gps_and_speed(
                    current_lat: float,
                    current_lon: float,
                    target_lat: float,
                    target_lon: float,
                    prev_lat: Optional[float],
                    prev_lon: Optional[float],
                    prev_time: Optional[datetime.datetime],
                    max_distance_error: float = 30.0,
                    max_speed_mps: float = 15.0
                ) -> Tuple[bool, str]:
                    """Резервная проверка координат и детекция читов по скорости."""
                    distance_to_target = haversine_distance(current_lat, current_lon, target_lat, target_lon)
                    if distance_to_target > max_distance_error:
                        return False, (
                            f"📍 Вы еще не дошли до цели. Текущее расстояние до точки: {int(distance_to_target)} метров. "
                            f"Подойдите ближе (требуется радиус до {int(max_distance_error)}м) и попробуйте снова!"
                        )
                    if prev_lat is not None and prev_lon is not None and prev_time is not None:
                        now = datetime.datetime.now(datetime.timezone.utc)
                        if prev_time.tzinfo is None:
                            prev_time = prev_time.replace(tzinfo=datetime.timezone.utc)
                        time_diff = (now - prev_time).total_seconds()
                        if time_diff < 1.0:
                            return True, "Успешно! Вы прибыли на контрольную точку (проверка скорости пропущена)."
                        distance_from_prev = haversine_distance(prev_lat, prev_lon, current_lat, current_lon)
                        calculated_speed = distance_from_prev / time_diff
                        if calculated_speed > max_speed_mps:
                            speed_kmh = calculated_speed * 3.6
                            limit_kmh = max_speed_mps * 3.6
                            return False, (
                                f"🚨 *Обнаружена аномалия GPS!*\n\n"
                                f"Система зафиксировала нереалистичную скорость перемещения: "
                                f"*{round(speed_kmh, 1)} км/ч* при лимите для этого квеста *{round(limit_kmh, 1)} км/ч*.\n"
                                f"Использование Fake GPS или высокоскоростного транспорта запрещено правилами текущего квеста!"
                            )
                    return True, "Успешно! Вы прибыли на контрольную точку."


# Внутренний кэш для Live Location радара (throttle-фильтр)
radar_cache = {}


def get_location_keyboard() -> ReplyKeyboardMarkup:
    """
    Возвращает клавиатуру управления квестом на местности с кнопкой быстрого инвентаря.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Я на месте (Отправить GPS)", request_location=True)],
            [KeyboardButton(text="📡 Включить радар")],
            [KeyboardButton(text="🎒 Мой рюкзак"), KeyboardButton(text="❄️ Заморозить квест (Пауза)")],
            [KeyboardButton(text="🛑 Выйти")]
        ],
        resize_keyboard=True
    )


def calculate_matcher(text_a: str, text_b: str) -> float:
    """
    Вспомогательная функция для CPU-интенсивного расчета подобия строк,
    запускаемая в отдельном потоке.
    """
    return SequenceMatcher(None, text_a, text_b).ratio()


async def check_weather_and_time(step, redis: Optional[Redis] = None) -> tuple[bool, str, bool, bool]:
    """
    Проверяет климатические и временные условия для прохождения шага квеста.
    Реализует кэширование погоды в Redis (TTL 10 минут) для экономии лимитов API.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    perm_hour = (now_utc.hour + 5) % 24  # Перевод на Пермское время UTC+5
    is_night = perm_hour >= 22 or perm_hour < 6
    is_day = 6 <= perm_hour < 22
    is_rain = False

    api_key = settings.bot.weather_api_key.get_secret_value() if settings.bot.weather_api_key else ""
    if api_key:
        cached_val = None
        if redis:
            try:
                cached_val = await redis.get("weather:Perm:is_rain")
            except Exception as e:
                logger.error(f"Ошибка чтения кэша погоды из Redis: {e}")

        if cached_val is not None:
            is_rain = (cached_val.decode('utf-8') == "true")
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.openweathermap.org/data/2.5/weather?q=Perm&appid={api_key}", 
                        timeout=5
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            weather_main = [w["main"].lower() for w in data.get("weather", [])]
                            is_rain = any(x in weather_main for x in ["rain", "snow", "drizzle", "thunderstorm"])
                            
                            if redis:
                                try:
                                    # Кэшируем статус осадков на 10 минут (600 секунд)
                                    await redis.setex("weather:Perm:is_rain", 600, "true" if is_rain else "false")
                                except Exception as e:
                                    logger.error(f"Ошибка записи кэша погоды в Redis: {e}")
            except Exception as e:
                logger.error(f"Weather API error: {e}")
                is_rain = datetime.datetime.now().minute % 2 == 1
    else:
        is_rain = datetime.datetime.now().minute % 2 == 1

    if step.is_night_only and not is_night: 
        return False, "🌌 Это сюжетное событие происходит только ночью (с 22:00 до 06:00)!", is_night, is_rain
    if hasattr(step, "is_day_only") and step.is_day_only and not is_day:
        return False, "🌅 Это сюжетное событие происходит только в светлое время суток (с 06:00 до 22:00)!", is_night, is_rain
    if step.is_weather_only and not is_rain: 
        return False, "🌧 Призрак появляется только во время пермского дождя/снега!", is_night, is_rain
    if hasattr(step, "is_dry_only") and step.is_dry_only and is_rain:
        return False, "☀️ Это задание требует сухой, ясной погоды!", is_night, is_rain
        
    return True, "", is_night, is_rain


async def refresh_step_ui(bot: Bot, user_id: int, step, active, hint_text: str = "", photo_mode: str = "then", radar_text: str = "") -> None:
    """
    Обновляет интерфейс шага квеста в одном сообщении.
    Мягко глушит исключения TelegramBadRequest "message is not modified".
    """
    user = await db.get_user(user_id)
    if not user:
        return
        
# 1. Восстанавливаем данные радара (если радар запущен и есть кэш геопозиции)
    if not radar_text: 
        user_cache = radar_cache.get(user_id)
        if user_cache:
            dist = user_cache["last_dist"]
            if dist > 1000:
                status = "🧊 Леденящий холод (более 1 км)"
            elif dist > 300:
                status = "❄️ Холодно (менее 1 км)"
            elif dist > 100:
                status = "🧣 Теплее (~300м)"
            elif dist > 30:
                status = "🔥 Горячо! (~100м)"
            else:
                status = "🌋 Обжигает! Вы у цели! (менее 30м)"
            radar_text = f"📡 *РАДАР ДИСТАНЦИИ ПЕШЕХОДА*\nТекущий статус: {status}\nДистанция до точки: *{int(dist)} метров*"

    # 2. Собираем итоговое текстовое сообщение
    text = f"📍 *Контрольная точка!*\n\n{step.instruction_text}\n\n"
    if radar_text:
        text += f"{radar_text}\n\n"
    if hint_text:
        text += f"{hint_text}\n\n"
    if step.required_item:
        text += f"⚠️ _Требуется предмет из инвентаря:_ *{step.required_item}*\n\n"

    # 3. Пересобираем клавиатуру подсказок
    hints_list = step.hints if (hasattr(step, "hints") and step.hints) else []
    if not hints_list:
        hints_list = [
            {"text": step.hint_1_text, "price": 20, "delay_min": step.hint_1_delay},
            {"text": step.hint_2_text, "price": 0, "delay_min": step.hint_2_delay}
        ]

    # Безопасное приведение времени действия к UTC
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    
    last_action = active.last_action_at.replace(tzinfo=None) if active.last_action_at else now
    time_passed_minutes = (now - last_action).total_seconds() / 60.0
    delay_factor = 0.7 if user.rpg_class == "ranger" else 1.0

    builder = InlineKeyboardBuilder()
    for idx, h in enumerate(hints_list, 1):
        if h["price"] > 0:
            builder.button(text=f"💰 Подсказка {idx} ({h['price']} 🪙)", callback_data=f"buy_hint_{step.id}_{idx-1}")
        else:
            effective_delay = h["delay_min"] * delay_factor
            if time_passed_minutes >= effective_delay:
                builder.button(text=f"💡 Подсказка {idx} (Бесплатно)", callback_data=f"free_hint_{step.id}_{idx-1}")
            else:
                rem_min = int(effective_delay - time_passed_minutes)
                builder.button(text=f"⏳ Подсказка {idx} ({rem_min} мин)", callback_data=f"hint_locked_{rem_min}")

    has_merc = await db.check_item_in_inventory(user_id, "🧙‍♂️ Наемник")
    if has_merc:
        builder.button(text="🧙‍♂️ Попросить помощи наёмника", callback_data=f"use_merc_{step.id}")

    if step.photo_then_id and step.photo_now_id:
        photo_btn_text = "🔄 Сверить ракурс «Сейчас»" if photo_mode == "then" else "🔄 Сверить ракурс «Тогда»"
        next_mode = "now" if photo_mode == "then" else "then"
        builder.button(text=photo_btn_text, callback_data=f"toggle_photo_{next_mode}_{step.id}")

    builder.adjust(1)

    # 4. Безопасно редактируем сообщение, глуша исключения без изменений caption/text
    if active.last_game_message_id:
        try:
            if step.photo_then_id:
                await bot.edit_message_caption(
                    chat_id=user_id,
                    message_id=active.last_game_message_id,
                    caption=text,
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
            else:
                await bot.edit_message_text(
                    text=text,
                    chat_id=user_id,
                    message_id=active.last_game_message_id,
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                logger.error(f"Ошибка TelegramBadRequest в refresh_step_ui: {e}")
        except Exception as e:
            logger.error(f"Критическая ошибка в refresh_step_ui: {e}")


async def deliver_step_content(bot: Bot, user_id: int, step, radar_text: str = "") -> None:
    """Доставляет контент шага пользователю в режиме 'Одного экрана' с интерактивной сменой фото (Юзер 14)."""
    active = await db.get_active_quest(user_id)
    if not active: 
        return

    user = await db.get_user(user_id)
    if user.karma < step.min_karma_required:
        builder = InlineKeyboardBuilder()
        builder.button(text="💾 Приостановить и выйти (Чекпоинт)", callback_data="suspend_current_quest")
        await bot.send_message(
            user_id, 
            f"⛔ Доступ заблокирован!\nДля прохождения этого шага требуется карма: *{step.min_karma_required}* (ваша карма: *{user.karma}*).\n\n"
            f"Вы можете временно приостановить квест на чекпоинте, чтобы заняться другими делами и повысить карму в диалогах.",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        return

    # Очистка чата для сохранения концепции одного экрана
    if active.last_game_message_id:
        try: 
            await bot.delete_message(user_id, active.last_game_message_id)
        except Exception: 
            pass

    # Отправка аудиогида (если настроен)
    if step.audio_guide_id:
        try: 
            await bot.send_voice(chat_id=user_id, voice=step.audio_guide_id, caption="🎧 Аудиогид")
        except Exception: 
            pass

    # Отправка контента с медиафайлом «Было»
    if step.photo_then_id:
        text = f"📍 *Контрольная точка!*\n\n{step.instruction_text}\n\n"
        if radar_text:
            text += f"{radar_text}\n\n"
        if step.required_item:
            text += f"⚠️ _Требуется предмет из инвентаря:_ *{step.required_item}*\n\n"

        hints_list = step.hints if (hasattr(step, "hints") and step.hints) else []
        if not hints_list:
            hints_list = [
                {"text": step.hint_1_text, "price": 20, "delay_min": step.hint_1_delay},
                {"text": step.hint_2_text, "price": 0, "delay_min": step.hint_2_delay}
            ]

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        last_action = active.last_action_at.replace(tzinfo=None) if active.last_action_at else now
        time_passed_minutes = (now - last_action).total_seconds() / 60.0
        delay_factor = 0.7 if user.rpg_class == "ranger" else 1.0

        builder = InlineKeyboardBuilder()
        for idx, h in enumerate(hints_list, 1):
            if h["price"] > 0:
                builder.button(text=f"💰 Подсказка {idx} ({h['price']} 🪙)", callback_data=f"buy_hint_{step.id}_{idx-1}")
            else:
                effective_delay = h["delay_min"] * delay_factor
                if time_passed_minutes >= effective_delay:
                    builder.button(text=f"💡 Подсказка {idx} (Бесплатно)", callback_data=f"free_hint_{step.id}_{idx-1}")
                else:
                    rem_min = int(effective_delay - time_passed_minutes)
                    builder.button(text=f"⏳ Подсказка {idx} ({rem_min} мин)", callback_data=f"hint_locked_{rem_min}")

        has_merc = await db.check_item_in_inventory(user_id, "🧙‍♂️ Наемник")
        if has_merc:
            builder.button(text="🧙‍♂️ Попросить помощи наёмника", callback_data=f"use_merc_{step.id}")

        if step.photo_then_id and step.photo_now_id:
            builder.button(text="🔄 Сверить ракурс «Сейчас»", callback_data=f"toggle_photo_now_{step.id}")

        builder.adjust(1)

        try:
            msg = await bot.send_photo(
                chat_id=user_id,
                photo=step.photo_then_id,
                caption=text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            await db.update_active_quest_message_id(user_id, msg.message_id)
            return
        except Exception as e:
            logger.error(f"Error sending photo media: {e}")

    # Если фото нет — инициализируем обычный текстовый UI через хелпер
    msg = await bot.send_message(
        chat_id=user_id,
        text=f"📍 *Контрольная точка!*\n\n{step.instruction_text}",
        parse_mode="Markdown"
    )
    await db.update_active_quest_message_id(user_id, msg.message_id)
    await refresh_step_ui(bot, user_id, step, active, radar_text=radar_text)

# =========================================================================
# ИНТЕРАКТИВНОЕ ПЕРЕКЛЮЧЕНИЕ ФОТО ТОГДА/СЕЙЧАС И ПОДСКАЗКИ
# =========================================================================

@user_quest_router.callback_query(F.data.startswith("toggle_photo_"))
async def toggle_photo_media(call: CallbackQuery, bot: Bot):
    """
    Обрабатывает плавное переключение ракурсов фотографий 'Тогда' и 'Сейчас'
    на активном экране загадки в режиме одного окна.
    """
    parts = call.data.split("_")
    target_mode = parts[2]
    step_id = int(parts[3])
    
    step = await db.get_step_by_id(step_id)
    active = await db.get_active_quest(call.from_user.id)
    if not step or not active:
        await call.answer()
        return

    if active.current_step_id != step_id:
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return

    photo_id = step.photo_then_id if target_mode == "then" else step.photo_now_id
    next_mode = "now" if target_mode == "then" else "then"
    btn_text = "🔄 Сверить ракурс «Сейчас»" if target_mode == "then" else "🔄 Сверить ракурс «Тогда»"

    user = await db.get_user(call.from_user.id)
    hints_list = step.hints if (hasattr(step, "hints") and step.hints) else []
    if not hints_list:
        hints_list = [
            {"text": step.hint_1_text, "price": 20, "delay_min": step.hint_1_delay},
            {"text": step.hint_2_text, "price": 0, "delay_min": step.hint_2_delay}
        ]

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_action = active.last_action_at.replace(tzinfo=None) if active.last_action_at else now
    time_passed_minutes = (now - last_action).total_seconds() / 60.0
    delay_factor = 0.7 if user.rpg_class == "ranger" else 1.0

    builder = InlineKeyboardBuilder()
    for idx, h in enumerate(hints_list, 1):
        if h["price"] > 0:
            builder.button(text=f"💰 Подсказка {idx} ({h['price']} 🪙)", callback_data=f"buy_hint_{step.id}_{idx-1}")
        else:
            effective_delay = h["delay_min"] * delay_factor
            if time_passed_minutes >= effective_delay:
                builder.button(text=f"💡 Подсказка {idx} (Бесплатно)", callback_data=f"free_hint_{step.id}_{idx-1}")
            else:
                rem_min = int(effective_delay - time_passed_minutes)
                builder.button(text=f"⏳ Подсказка {idx} ({rem_min} мин)", callback_data=f"hint_locked_{rem_min}")

    has_merc = await db.check_item_in_inventory(call.from_user.id, "🧙‍♂️ Наемник")
    if has_merc:
        builder.button(text="🧙‍♂️ Попросить помощи наёмника", callback_data=f"use_merc_{step.id}")

    builder.button(text=btn_text, callback_data=f"toggle_photo_{next_mode}_{step_id}")
    builder.adjust(1)

    try:
        await bot.edit_message_media(
            chat_id=call.from_user.id,
            message_id=call.message.message_id,
            media=InputMediaPhoto(media=photo_id, caption=call.message.caption),
            reply_markup=builder.as_markup()
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error editing message media: {e}")
    except Exception as e:
        logger.error(f"Error editing message media: {e}")

    await call.answer()


@user_quest_router.callback_query(F.data.startswith("hint_locked_"))
async def hint_locked_callback(call: CallbackQuery):
    """
    Информирует пользователя об оставшемся времени блокировки подсказки.
    """
    min_left = call.data.split("_")[-1]
    await call.answer(f"⏳ Эта подсказка заблокирована таймером. Подождите еще {min_left} мин.", show_alert=True)


# =========================================================================
# СБРОС ВЕЩЕЙ И ИНТЕРАКТИВНОЕ МЕНЮ РАЗГРУЗКИ ПЕРЕГРУЗА (#27)
# =========================================================================

async def try_add_item_with_overweight_check(bot: Bot, user_id: int, item_name: str, state: FSMContext, step=None, active=None) -> bool:
    """
    Пытается добавить артефакт в инвентарь игрока.
    При перегрузе приостанавливает поток и выводит инлайновое меню очистки рюкзака.
    """
    async with db.session_pool() as session:
        from tgbot.database.models import ShopItem
        stmt = select(ShopItem).where(ShopItem.item_name == item_name)
        res = await session.execute(stmt)
        shop_item = res.scalar_one_or_none()
        weight = shop_item.weight if shop_item else 1

    overloaded, curr_weight, max_cap = await db.is_inventory_overloaded(user_id, weight)
    if overloaded:
        # Сохраняем ожидающую вещь и шаг квеста в кэш FSM
        await state.update_data(pending_item_name=item_name)
        if step and active:
            await state.update_data(pending_step_id=step.id, pending_active_quest_id=active.quest_id)
        
        inv_items = await db.get_user_inventory(user_id)
        
        builder = InlineKeyboardBuilder()
        for item in inv_items:
            builder.button(text=f"🗑 Выбросить {item}", callback_data=f"drop_item_{item}")
        builder.button(text="🔄 Попробовать снова", callback_data="retry_add_pending_item")
        builder.adjust(1)
        
        await bot.send_message(
            user_id,
            f"🚨 *РЮКЗАК ПЕРЕГРУЖЕН!*\n\n"
            f"Вы пытаетесь поднять предмет: *{item_name}* (Вес: {weight} кг).\n"
            f"Текущая грузоподъемность: *{curr_weight}/{max_cap} кг*.\n\n"
            f"Пожалуйста, выбросьте ненужные артефакты, чтобы освободить место:",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        return False

    await db.add_item_to_inventory(user_id, item_name)
    return True


@user_quest_router.callback_query(F.data.startswith("drop_item_"))
async def drop_item_callback(call: CallbackQuery, state: FSMContext):
    """
    Обрабатывает выброс вещей из рюкзака при перегрузе.
    Предохраняет от деструктивной блокировки игрового цикла.
    """
    item_name = call.data[len("drop_item_"):]
    user_id = call.from_user.id
    
    success = await db.discard_inventory_item(user_id, item_name)
    if success:
        await call.answer(
            f"⚠️ Предмет выброшен. Внимание: некоторые предметы могут понадобиться для прохождения будущих квестов!", 
            show_alert=True
        )
    else:
        await call.answer("Не удалось утилизировать предмет.", show_alert=True)
        
    data = await state.get_data()
    pending_item = data.get("pending_item_name", "Артефакт")
    
    async with db.session_pool() as session:
        from tgbot.database.models import ShopItem
        stmt = select(ShopItem).where(ShopItem.item_name == pending_item)
        res = await session.execute(stmt)
        shop_item = res.scalar_one_or_none()
        weight = shop_item.weight if shop_item else 1
        
    overloaded, curr_weight, max_cap = await db.is_inventory_overloaded(user_id, weight)
    inv_items = await db.get_user_inventory(user_id)
    
    builder = InlineKeyboardBuilder()
    for item in inv_items:
        builder.button(text=f"🗑 Выбросить {item}", callback_data=f"drop_item_{item}")
    builder.button(text="🔄 Попробовать снова", callback_data="retry_add_pending_item")
    builder.adjust(1)
    
    try:
        await call.message.edit_text(
            f"🚨 *РЮКЗАК ПЕРЕГРУЖЕН!*\n\n"
            f"Вы пытаетесь поднять предмет: *{pending_item}* (Вес: {weight} кг).\n"
            f"Текущая грузоподъемность: *{curr_weight}/{max_cap} кг*.\n\n"
            f"Выбросьте лишнее:",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass


@user_quest_router.callback_query(F.data == "retry_add_pending_item")
async def retry_add_pending_item_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """
    Повторная проверка веса рюкзака после ручной утилизации лишних вещей игроком.
    """
    user_id = call.from_user.id
    data = await state.get_data()
    pending_item = data.get("pending_item_name")
    if not pending_item:
        await call.answer("Нет ожидающих предметов.", show_alert=True)
        return
        
    async with db.session_pool() as session:
        from tgbot.database.models import ShopItem
        stmt = select(ShopItem).where(ShopItem.item_name == pending_item)
        res = await session.execute(stmt)
        shop_item = res.scalar_one_or_none()
        weight = shop_item.weight if shop_item else 1
        
    overloaded, curr_weight, max_cap = await db.is_inventory_overloaded(user_id, weight)
    if overloaded:
        await call.answer(f"❌ Все еще перегружен! ({curr_weight + weight}/{max_cap} кг)", show_alert=True)
        return
        
    # Место освобождено, укомплектовываем вещь в рюкзак
    await db.add_item_to_inventory(user_id, pending_item)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("🎒 Предмет успешно укомплектован в рюкзак!", show_alert=True)
    
    pending_step_id = data.get("pending_step_id")
    if pending_step_id:
        step = await db.get_step_by_id(pending_step_id)
        active = await db.get_active_quest(user_id)
        if step and active:
            await proceed_to_next_step(bot, user_id, step, active, state=state)
    await state.clear()


# =========================================================================
# УПРАВЛЕНИЕ ИГРОВОЙ СЕССИЕЙ, ЧЕКПОИНТАМИ И ГЛОБАЛЬНЫМИ ИВЕНТАМИ (#34)
# =========================================================================

@user_quest_router.callback_query(F.data == "suspend_current_quest")
async def suspend_current_quest_handler(call: CallbackQuery):
    """Приостанавливает текущую сессию квеста и сохраняет чекпоинт."""
    await db.suspend_active_quest(call.from_user.id)
    await call.message.edit_text("💾 Сессия сохранена на чекпоинте. Квест временно приостановлен.")
    await call.message.answer("Вы можете начать другой квест из главного меню: /start", reply_markup=ReplyKeyboardRemove())
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("start_quest_"))
async def start_quest_router(call: CallbackQuery, state: FSMContext, bot: Bot, redis: Optional[Redis] = None):
    """
    Запускает квест. Выполняет проверку на минимальный уровень игрока (гейт-ограничение #3)
    и проверяет наличие сохраненных чекпоинтов (включая Bounty Hunting заморозки #34).
    """
    quest_id = int(call.data.split("_")[-1])
    user_id = call.from_user.id
    
    user = await db.get_user(user_id)
    if not user.completed_tutorial:
        await start_tutorial_for_user(call, state)
        await call.answer()
        return

    # Проверка гейта по уровню игрока (XP Engine - #3)
    quest = await db.get_quest_by_id(quest_id)
    if not quest:
        await call.answer("❌ Выбранный квест не найден!", show_alert=True)
        return

    if user.level < quest.min_level_required:
        await call.message.answer(
            f"🔒 *Доступ заблокирован!*\n\n"
            f"Квест «{quest.title}» доступен только игрокам с *{quest.min_level_required} уровня*.\n"
            f"Ваш текущий уровень: *{user.level}*.\n\n"
            f"Выполняйте ежедневные загадки дня (/daily) и другие доступные квесты, чтобы накопить XP!",
            parse_mode="Markdown"
        )
        await call.answer()
        return

    all_sessions = await db.get_active_quests_list(user_id)
    target_suspended = next((q for q in all_sessions if q.quest_id == quest_id and q.is_suspended), None)
    
    # Механика выбора при возвращении к замороженному квесту (Bounty Hunting - #34)
    if target_suspended:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Продолжить с места заморозки", callback_data=f"resume_quest_{quest_id}")
        builder.button(text="💥 Начать квест сначала", callback_data=f"force_start_quest_{quest_id}")
        builder.button(text="❌ Отмена", callback_data="cancel_start_action")
        builder.adjust(1)
        await call.message.answer(
            "📌 У вас есть сохраненный чекпоинт для этого квеста (возможно, он был автоматически "
            "заморожен во время глобального Bounty-контракта).\n\n"
            "Как вы желаете поступить?",
            reply_markup=builder.as_markup()
        )
        await call.answer()
        return

    current_active = next((q for q in all_sessions if not q.is_suspended), None)
    if current_active:
        builder = InlineKeyboardBuilder()
        builder.button(text="💾 Приостановить текущий и начать новый", callback_data=f"force_start_quest_{quest_id}")
        builder.button(text="❌ Отмена", callback_data="cancel_start_action")
        builder.adjust(1)
        await call.message.answer("⚠️ У вас запущен другой квест. Приостановить его и сохранить чекпоинт?", reply_markup=builder.as_markup())
        await call.answer()
        return

    await _start_new_quest_logic(call, bot, user_id, quest_id, redis=redis)
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("resume_quest_"))
async def resume_quest_handler(call: CallbackQuery, bot: Bot):
    """Возобновляет квест с сохраненной контрольной точки."""
    quest_id = int(call.data.split("_")[-1])
    user_id = call.from_user.id
    
    active = await db.resume_user_quest(user_id, quest_id)
    if active:
        step = await db.get_step_by_id(active.current_step_id)
        await call.message.answer(f"🔄 Квест возобновлен с сохраненной точки!", reply_markup=get_location_keyboard())
        await deliver_step_content(bot, user_id, step)
    else:
        await call.message.answer("❌ Не удалось возобновить квест.")
    await call.answer()


@user_quest_router.callback_query(F.data == "cancel_start_action")
async def cancel_start_action(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("force_start_quest_"))
async def force_start_quest(call: CallbackQuery, bot: Bot, redis: Optional[Redis] = None):
    await db.suspend_active_quest(call.from_user.id)
    try:
        await call.message.delete()
    except Exception:
        pass
    await _start_new_quest_logic(call, bot, call.from_user.id, int(call.data.split("_")[-1]), redis=redis)
    await call.answer()


async def _start_new_quest_logic(call: CallbackQuery, bot: Bot, user_id: int, quest_id: int, redis: Optional[Redis] = None):
    """Запускает новый квест с первого шага."""
    quest = await db.get_quest_with_steps(quest_id)
    if not quest or not quest.steps:
        await call.message.answer("❌ Квест еще не заполнен администратором!")
        return
    first_step = quest.steps[0]
    if first_step.required_item and not await db.check_item_in_inventory(user_id, first_step.required_item):
        await call.message.answer(f"🔒 Доступ заблокирован! В вашем инвентаре отсутствует предмет: *{first_step.required_item}*.", parse_mode="Markdown")
        return
    is_allowed, reason, is_night, is_rain = await check_weather_and_time(first_step, redis=redis)
    if not is_allowed:
        await call.message.answer(reason)
        return
    await db.start_user_quest(user_id, quest_id, first_step.id)
    await db.update_active_quest_rpg_flags(user_id, is_night, is_rain)
    await call.message.answer(f"🏁 Вы начали квест: *{quest.title}*!", parse_mode="Markdown", reply_markup=get_location_keyboard())
    await deliver_step_content(bot, user_id, first_step)


@user_quest_router.message(F.text == "🛑 Выйти")
async def prompt_exit_quest(message: Message):
    """Запрос на полный выход из квеста со стиранием текущего прогресса."""
    if not (await db.get_active_quest(message.from_user.id)): 
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, прервать квест", callback_data="confirm_exit_quest")
    builder.button(text="Отмена", callback_data="cancel_exit_quest")
    await message.answer("🛑 Вы уверены, что хотите выйти? Текущий прогресс этого квеста будет безвозвратно удален!", reply_markup=builder.as_markup())


@user_quest_router.callback_query(F.data == "confirm_exit_quest")
async def confirm_exit_quest(call: CallbackQuery):
    await db.delete_active_quest(call.from_user.id)
    await call.message.edit_text("🧹 Игровая сессия успешно стерта.")
    await call.message.answer("Вы можете начать квест заново из меню: /start", reply_markup=ReplyKeyboardRemove())
    await call.answer()


@user_quest_router.callback_query(F.data == "cancel_exit_quest")
async def cancel_exit_quest(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


# =========================================================================
# ЖИВОЙ ТЕКСТОВЫЙ РАДАР (LIVE LOCATION) С THROTTLE-ФИЛЬТРОМ
# =========================================================================

@user_quest_router.edited_message(F.location)
async def process_radar_live_location(message: Message, bot: Bot):
    """Обновляет радар в реальном времени при трансляции Live Location."""
    user_id = message.from_user.id
    active = await db.get_active_quest(user_id)
    
    if not active or active.is_frozen or active.is_suspended or active.current_npc_node: 
        return
    
    now = time.time()
    user_cache = radar_cache.get(user_id)
    
    step = await db.get_step_by_id(active.current_step_id)
    if not step: 
        return
    
    dist = haversine_distance(message.location.latitude, message.location.longitude, step.latitude, step.longitude)
    
    if user_cache:
        time_diff = now - user_cache["last_time"]
        dist_diff = abs(dist - user_cache["last_dist"])
        if time_diff <= 3 and dist_diff <= 5:
            return
            
    radar_cache[user_id] = {
        "last_time": now,
        "last_lat": message.location.latitude,
        "last_lon": message.location.longitude,
        "last_dist": dist
    }

    if dist > 1000:
        status = "🧊 Леденящий холод (более 1 км)"
    elif dist > 300:
        status = "❄️ Холодно (менее 1 км)"
    elif dist > 100:
        status = "🧣 Теплее (~300м)"
    elif dist > 30:
        status = "🔥 Горячо! (~100м)"
    else:
        status = "🌋 Обжигает! Вы у цели! (менее 30м)"
        
    radar_text = f"📡 *РАДАР ДИСТАНЦИИ ПЕШЕХОДА*\nТекущий статус: {status}\nДистанция до точки: *{int(dist)} метров*"
    
    await refresh_step_ui(bot, user_id, step, active, radar_text=radar_text)


@user_quest_router.message(F.text == "📡 Включить радар")
async def start_radar_instructions(message: Message):
    """Инструктирует пользователя, как запустить трансляцию радара."""
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        "📡 *Трансляция радара запущена!*\n\n"
        "Чтобы радар обновлял расстояние в реальном времени, выполните:\n"
        "1. Нажмите на значок скрепки (вложения) 📎.\n"
        "2. Выберите раздел *«Геопозиция»*.\n"
        "3. Нажмите *«Транслировать мою геопозицию»* и выберите любое время.\n\n"
        "Бот начнет плавно изменять температуру радара в одном сообщении!",
        parse_mode="Markdown"
    )


# =========================================================================
# ПРОВЕРКА GPS И ДВУХЭТАПНЫЙ АНТИЧИТ ПЛАТФОРМЫ
# =========================================================================

@user_quest_router.message(StateFilter(None), F.location)
async def process_user_gps(message: Message, bot: Bot, redis: Optional[Redis] = None):
    """Проверяет координаты, скорость движения и тайм-атак для NPC."""
    user_id = message.from_user.id
    active = await db.get_active_quest(user_id)
    if not active: 
        return
    if active.is_frozen:
        await message.answer("❄️ Ваш квест заморожен! Сначала разморозьте его.")
        return

    quest = await db.get_quest_by_id(active.quest_id)
    step = await db.get_step_by_id(active.current_step_id)
    
    is_allowed, weather_reason, is_night, is_rain = await check_weather_and_time(step, redis=redis)
    if not is_allowed:
        await message.answer(weather_reason)
        return

    is_valid, reason = verify_gps_and_speed(
        current_lat=message.location.latitude,
        current_lon=message.location.longitude,
        target_lat=step.latitude,
        target_lon=step.longitude,
        prev_lat=active.prev_latitude,
        prev_lon=active.prev_longitude,
        prev_time=active.prev_time,
        max_distance_error=30.0,
        max_speed_mps=quest.max_speed_kmh / 3.6
    )

    if not is_valid:
        if "аномалия" in reason or "скорость" in reason:
            if active.prev_time:
                prev_action_time = active.prev_time.replace(tzinfo=datetime.timezone.utc) if active.prev_time.tzinfo is None else active.prev_time
                time_delta = (datetime.datetime.now(datetime.timezone.utc) - prev_action_time).total_seconds()
                if time_delta <= 0:
                    time_delta = 0.1
                dist_prev = haversine_distance(active.prev_latitude, active.prev_longitude, message.location.latitude, message.location.longitude)
                calculated_speed = dist_prev / time_delta
            else:
                calculated_speed = 0.0

            await db.add_cheat_log(
                user_id=user_id,
                quest_id=quest.id,
                speed=calculated_speed,
                lat=message.location.latitude,
                lon=message.location.longitude
            )
            
            warns = await db.increment_cheat_warning(user_id)
            if warns >= 2:
                await db.set_ban_status(user_id, True)
                await message.answer(
                    "🚨 *СИСТЕМА АНТИЧИТА СРАБОТАЛА*\n\n"
                    "Вы были автоматически забанены за повторную подмену координат или превышение скорости.\n"
                    "Для обжалования обратитесь к @admin.",
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                await message.answer(
                    "⚠️ *ВНИМАНИЕ! СИСТЕМА АНТИЧИТА*\n\n"
                    "Обнаружен резкий скачок координат или превышение скорости квеста.\n"
                    "Это предупреждение *1/2*. При следующем нарушении вы будете автоматически заблокированы.\n"
                    "Если это сбой GPS, напишите администратору @admin."
                )
        else:
                await message.answer(reason)
        return

    await db.reset_cheat_warning(user_id)
    # Фиксируем успешное прохождение GPS-верификации для текущего шага в СУБД
    await db.set_gps_verified_now(user_id, message.location.latitude, message.location.longitude)
    await db.update_active_quest_rpg_flags(user_id, is_night, is_rain)

    # Вероятность 25% запуска случайного события на маршруте
    if random.random() < 0.25:
        await trigger_random_event(bot, user_id, active, step)
        return

    await message.answer("🔓 *Геопозиция подтверждена!*", parse_mode="Markdown")

    if step.history_info:
        await message.answer(f"📖 *Историческая справка:*\n\n{step.history_info}", parse_mode="Markdown")

    if step.npc_name and step.npc_dialogue:
        step_activated_utc = active.step_activated_at.replace(tzinfo=datetime.timezone.utc) if active.step_activated_at.tzinfo is None else active.step_activated_at
        time_elapsed = (datetime.datetime.now(datetime.timezone.utc) - step_activated_utc).total_seconds()
        
        if step.time_limit_seconds and time_elapsed > step.time_limit_seconds:
            await db.update_karma(user_id, -1)
            await message.answer(
                f"⏳ Вы слишком долго добирались... Персонаж *{step.npc_name}* скрылся в тумане Камы.\n"
                f"Ваша карма снижена на *-1*.\n\n"
                f"📝 Решите основную загадку точки:\n\n"
                f"_{step.instruction_text}_",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await message.answer(
                f"🗣 Персонаж *{step.npc_name}* обращается к вам:",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            await db.update_active_quest_npc_node(user_id, "start")
            await deliver_npc_dialogue_node(bot, user_id, step, "start")
    else:
        await message.answer(
            f"📝 Решите загадку локации:\n_{step.instruction_text}_",
            parse_mode="Markdown",
            reply_markup=get_location_keyboard()
        )


# =========================================================================
# ДИАЛОГИ С NPC, ПОДДЕРЖКА КАРМЫ И МОНЕТИЗАЦИИ
# =========================================================================

async def deliver_npc_dialogue_node(bot: Bot, user_id: int, step, node_id: str) -> None:
    """Отрисовывает узел диалогового графа с NPC в режиме одного экрана."""
    dialogue_tree = step.npc_dialogue
    if hasattr(dialogue_tree, "model_dump"):
        dialogue_tree = dialogue_tree.model_dump()

    if not dialogue_tree or node_id not in dialogue_tree: 
        return
    node = dialogue_tree[node_id]
    text = f"🗣 *[{step.npc_name}]*\n\n«{node['text']}»"
    
    builder = InlineKeyboardBuilder()
    for idx, opt in enumerate(node.get("options", [])): 
        builder.button(text=opt["text"], callback_data=f"npc_opt_{step.id}_{node_id}_{idx}")
    builder.adjust(1)

    active = await db.get_active_quest(user_id)
    
    if node_id == "start":
        if active and active.last_game_message_id:
            try:
                await bot.delete_message(chat_id=user_id, message_id=active.last_game_message_id)
            except Exception:
                pass
        
        msg = await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await db.update_active_quest_message_id(user_id, msg.message_id)
    else:
        if active and active.last_game_message_id:
            try:
                await bot.edit_message_text(
                    text=text,
                    chat_id=user_id,
                    message_id=active.last_game_message_id,
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
            except Exception:
                msg = await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
                await db.update_active_quest_message_id(user_id, msg.message_id)


@user_quest_router.callback_query(F.data.startswith("npc_opt_"))
async def process_npc_dialogue_callback(call: CallbackQuery, bot: Bot):
    """Обрабатывает варианты выбора в диалогах с NPC."""
    raw_data = call.data[len("npc_opt_"):]
    parts = raw_data.split("_")
    
    step_id = int(parts[0])
    opt_idx = int(parts[-1])
    node_id = "_".join(parts[1:-1])
    
    user_id = call.from_user.id
    
    active = await db.get_active_quest(user_id)
    if not active or active.current_step_id != step_id:
        await call.answer("❌ Это диалоговое событие уже неактивно!", show_alert=True)
        return

    step = await db.get_step_by_id(step_id)
    dialogue_tree = step.npc_dialogue
    if hasattr(dialogue_tree, "model_dump"):
        dialogue_tree = dialogue_tree.model_dump()

    node = dialogue_tree[node_id]
    option = node["options"][opt_idx]

    if option.get("karma_change", 0) != 0: 
        await db.update_karma(user_id, option["karma_change"])
    if option.get("coins_change", 0) > 0: 
        await db.add_coins(user_id, option["coins_change"])
    elif option.get("coins_change", 0) < 0: 
        await db.deduct_coins(user_id, abs(option["coins_change"]))

    next_node = option.get("next_node", "exit")
    if next_node == "exit":
        await db.update_active_quest_npc_node(user_id, None)
        await call.message.edit_text(f"🗣 *[{step.npc_name}]*\n\n«Диалог завершен.»", parse_mode="Markdown")
        msg = await call.message.answer(
            f"🔓 Теперь введите правильный текстовый ответ на загадку локации:\n\n"
            f"📝 _{step.instruction_text}_",
            parse_mode="Markdown",
            reply_markup=get_location_keyboard()
        )
        await db.update_active_quest_message_id(user_id, msg.message_id)
    else:
        await db.update_active_quest_npc_node(user_id, next_node)
        await deliver_npc_dialogue_node(bot, user_id, step, next_node)


# =========================================================================
# СИСТЕМА ПОДСКАЗОК И СЕКРЕТНЫЕ ФИЛИАЛЫ
# =========================================================================

@user_quest_router.callback_query(F.data.startswith("buy_hint_"))
async def buy_hint_callback_handler(call: CallbackQuery, bot: Bot):
    """Покупка конкретного уровня динамической подсказки за внутриигровую валюту."""
    user_id = call.from_user.id
    parts = call.data.split("_")
    step_id = int(parts[2])
    hint_idx = int(parts[3])
    
    active = await db.get_active_quest(user_id)
    if not active or active.current_step_id != step_id: 
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return
        
    step = await db.get_step_by_id(step_id)
    hints_list = step.hints if (hasattr(step, "hints") and step.hints) else []
    if not hints_list:
        hints_list = [
            {"text": step.hint_1_text, "price": 20, "delay_min": step.hint_1_delay},
            {"text": step.hint_2_text, "price": 0, "delay_min": step.hint_2_delay}
        ]
        
    h = hints_list[hint_idx]
    if not await db.deduct_coins(user_id, h["price"]):
        await call.answer("❌ Недостаточно монет в кошельке!", show_alert=True)
        return
        
    await db.increment_error_count(user_id, score_penalty=h["price"])
    
    hint_display = f"🔎 *Платная подсказка:* {h['text']}\n\nСписано {h['price']} 🪙."
    await refresh_step_ui(bot, user_id, step, active, hint_text=hint_display)
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("free_hint_"))
async def free_hint_callback_handler(call: CallbackQuery, bot: Bot):
    """Получение конкретного бесплатного уровня динамической подсказки."""
    user_id = call.from_user.id
    parts = call.data.split("_")
    step_id = int(parts[2])
    hint_idx = int(parts[3])
    
    active = await db.get_active_quest(user_id)
    if not active or active.current_step_id != step_id: 
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return
    
    step = await db.get_step_by_id(step_id)
    hints_list = step.hints if (hasattr(step, "hints") and step.hints) else []
    if not hints_list:
        hints_list = [
            {"text": step.hint_1_text, "price": 20, "delay_min": step.hint_1_delay},
            {"text": step.hint_2_text, "price": 0, "delay_min": step.hint_2_delay}
        ]
        
    h = hints_list[hint_idx]
    
    hint_display = f"🔎 *Подсказка:* {h['text']}"
    await refresh_step_ui(bot, user_id, step, active, hint_text=hint_display)
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("unlock_secret_"))
async def unlock_secret_branch_callback(call: CallbackQuery, bot: Bot):
    """Разблокировка секретных веток сюжета за монеты."""
    user_id = call.from_user.id
    step_id = int(call.data.split("_")[-1])
    active = await db.get_active_quest(user_id)
    
    if not active:
        await call.answer()
        return

    step = await db.get_step_by_id(step_id)
    if not await db.deduct_coins(user_id, step.secret_price):
        await call.answer("❌ Недостаточно монет для разблокировки секретного шага!", show_alert=True)
        return
    await db.update_active_quest_step(user_id, step_id, step.latitude, step.longitude)
    await deliver_step_content(bot, user_id, step)
    await call.answer()


# =========================================================================
# ВСПЛЫВАЮЩИЕ СЛУЧАЙНЫЕ СОБЫТИЯ НА МАРШРУТЕ (RANDOM EVENTS)
# =========================================================================

async def trigger_random_event(bot: Bot, user_id: int, active, step) -> bool:
    """С вероятностью, заданной в БД (RandomEvent), запускает случайное атмосферное событие на улицах Перми."""
    async with db.session_pool() as session:
        from tgbot.database.models import RandomEvent
        res = await session.execute(select(RandomEvent))
        events = res.scalars().all()
    
    if not events:
        return False

    chosen = random.choice(events)
    # Проверка шанса срабатывания, настроенного администратором
    if random.random() * 100 > chosen.probability:
        return False

    builder = InlineKeyboardBuilder()
    cfg = await db.get_system_settings()

    if chosen.event_type == "merc":
        text = (
            f"🧙‍♂️ *СЛУЧАЙНОЕ СОБЫТИЕ: Неожиданная встреча*\n\n"
            f"{chosen.text}\n\n"
            f"Наемник поможет вам разгадать текущую загадку в один клик. "
            f"Время действия его контракта: {cfg.merc_lifetime_minutes} минут.\n\n"
            f"🤝 _Вы соглашаетесь нанять его? (+{chosen.xp_reward} XP за наем)_"
        )
        builder.button(text="🤝 Нанять бесплатно", callback_data=f"ev_choice_merc_yes_{step.id}_{chosen.id}")
        builder.button(text="❌ Отказаться", callback_data=f"ev_choice_merc_no_{step.id}_{chosen.id}")
    elif chosen.event_type == "scroll":
        text = (
            f"📜 *СЛУЧАЙНОЕ СОБЫТИЕ: Старый свиток*\n\n"
            f"{chosen.text}\n\n"
            f"Для расшифровки потребуются реактивы за {abs(chosen.coins_impact)} монет.\n\n"
            f"🧪 _Желаете расшифровать свиток? (+{chosen.karma_impact} Кармы, +{chosen.xp_reward} XP)_"
        )
        builder.button(text=f"🧪 Расшифровать ({abs(chosen.coins_impact)} 🪙)", callback_data=f"ev_choice_scroll_yes_{step.id}_{chosen.id}")
        builder.button(text="❌ Пройти мимо", callback_data=f"ev_choice_scroll_no_{step.id}_{chosen.id}")
    elif chosen.event_type == "wallet":
        text = (
            f"🪙 *СЛУЧАЙНОЕ СОБЫТИЕ: Пропавший кошелек*\n\n"
            f"{chosen.text}\n\n"
            f"Вы можете забрать монеты себе (+{chosen.coins_impact} монет, но {chosen.karma_impact} Карма), "
            f"либо оставить кошелек на месте в надежде, что владелец вернется (+2 Кармы, +{chosen.xp_reward} XP).\n\n"
            f"👇 _Как вы поступите?_"
        )
        builder.button(text=f"💰 Забрать монеты (+{chosen.coins_impact} 🪙)", callback_data=f"ev_choice_wallet_take_{step.id}_{chosen.id}")
        builder.button(text="🙌 Оставить на месте", callback_data=f"ev_choice_wallet_leave_{step.id}_{chosen.id}")
    else:
        text = f"✨ *СЛУЧАЙНОЕ СОБЫТИЕ*\n\n{chosen.text}"
        builder.button(text="▶️ Продолжить", callback_data=f"ev_continue_{step.id}")

    builder.adjust(1)
    
    if active.last_game_message_id:
        try:
            await bot.delete_message(user_id, active.last_game_message_id)
        except Exception:
            pass

    msg = await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await db.update_active_quest_message_id(user_id, msg.message_id)
    return True


@user_quest_router.callback_query(F.data.startswith("ev_choice_"))
async def process_random_event_choice(call: CallbackQuery, bot: Bot, state: FSMContext):
    """Применяет экономические и RPG эффекты выбранного исхода случайного события."""
    parts = call.data.split("_")
    event_type = parts[2]
    choice = parts[3]
    step_id = int(parts[4])
    event_id = int(parts[5])
    
    user_id = call.from_user.id
    active = await db.get_active_quest(user_id)
    if not active or active.current_step_id != step_id:
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return

    # Загружаем событие из БД
    async with db.session_pool() as session:
        from tgbot.database.models import RandomEvent
        res = await session.execute(select(RandomEvent).where(RandomEvent.id == event_id))
        chosen = res.scalar_one_or_none()

    if not chosen:
        await call.answer("Событие не найдено.", show_alert=True)
        return

    outcome_text = ""
    if event_type == "merc":
        if choice == "yes":
            added = await try_add_item_with_overweight_check(bot, user_id, "🧙‍♂️ Наемник", state)
            if added:
                await db.add_xp(user_id, chosen.xp_reward)
                outcome_text = (
                    f"🧙‍♂️ *Контракт подписан!*\n\n"
                    f"Наёмник добавлен в ваш рюкзак. "
                    f"Вы сможете активировать его помощь прямо на экране текущей загадки!\n"
                    f"Получено: *+{chosen.xp_reward} XP*."
                )
            else:
                await call.answer()
                return
        else:
            outcome_text = "👋 Вы вежливо попрощались с наёмником и пошли своей дорогой."
    elif event_type == "scroll":
        if choice == "yes":
            if await db.deduct_coins(user_id, abs(chosen.coins_impact)):
                await db.update_karma(user_id, chosen.karma_impact)
                await db.add_xp(user_id, chosen.xp_reward)
                outcome_text = (
                    f"📜 *Расшифровка завершена!*\n\n"
                    f"Вы узнали много нового о Пермской губернии.\n"
                    f"Получено: *+{chosen.karma_impact} Кармы*, *+{chosen.xp_reward} XP*."
                )
            else:
                outcome_text = "❌ У вас не хватило монет на покупку реактивов для свитка..."
        else:
            outcome_text = "📜 Вы оставили свиток лежать на мостовой."
    elif event_type == "wallet":
        if choice == "take":
            await db.add_coins(user_id, chosen.coins_impact)
            await db.update_karma(user_id, chosen.karma_impact)
            await db.add_xp(user_id, chosen.xp_reward)
            outcome_text = (
                f"💰 *Вы присвоили золото!*\n\n"
                f"Ваш кошелек пополнился на *+{chosen.coins_impact} монет*, "
                f"но карма снижена на *{chosen.karma_impact} Карма*. Получено: *+{chosen.xp_reward} XP*."
            )
        else:
            await db.update_karma(user_id, 2)
            await db.add_xp(user_id, chosen.xp_reward)
            outcome_text = f"🙌 *Вы поступили благородно!*\n\nВы не тронули чужие деньги. Ваша репутация растет: *+2 Кармы*, *+{chosen.xp_reward} XP*!"

    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Продолжить квест", callback_data=f"ev_continue_{step_id}")
    
    await call.message.edit_text(outcome_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await call.answer()


@user_quest_router.callback_query(F.data.startswith("ev_continue_"))
async def ev_continue_handler(call: CallbackQuery, bot: Bot):
    step_id = int(call.data.split("_")[-1])
    active = await db.get_active_quest(call.from_user.id)
    if not active or active.current_step_id != step_id:
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return

    step = await db.get_step_by_id(step_id)
    await deliver_step_content(bot, call.from_user.id, step)
    await call.answer()


# =========================================================================
# ИНТЕГРАЦИЯ СУПЕР-УПРАВЛЕНИЯ НАЕМНИКАМИ (Settings-Based - #6)
# =========================================================================

@user_quest_router.callback_query(F.data.startswith("use_merc_"))
async def use_mercenary_handler(call: CallbackQuery, bot: Bot):
    """Использование временного наемника с кастомизацией эффективности."""
    user_id = call.from_user.id
    step_id = int(call.data.split("_")[-1])
    
    has_merc = await db.check_item_in_inventory(user_id, "🧙‍♂️ Наемник")
    if not has_merc:
        await call.answer("🧙‍♂️ Время действия контракта наёмника вышло! Он покинул ваш рюкзак.", show_alert=True)
        return

    active = await db.get_active_quest(user_id)
    if not active or active.current_step_id != step_id:
        await call.answer("❌ Это действие относится к другому шагу квеста!", show_alert=True)
        return

    step = await db.get_step_by_id(step_id)
    
    # Списание (удаление) наёмника из инвентаря
    await db.discard_inventory_item(user_id, "🧙‍♂️ Наемник")

    await call.answer("🧙‍♂️ Наёмник мгновенно разгадал загадку за вас!", show_alert=True)
    
    # Эффективность наемника из SystemSettings
    cfg = await db.get_system_settings()
    custom_score = cfg.merc_efficiency if cfg else 100
    
    await proceed_to_next_step(bot, user_id, step, active, custom_score=custom_score)


# =========================================================================
# БЛОК КВЕСТА №0 (ОБУЧЕНИЕ ДЛЯ НОВЫХ ПОЛЬЗОВАТЕЛЕЙ)
# =========================================================================

async def start_tutorial_for_user(message_or_call, state: FSMContext) -> None:
    """Запускает Квест №0 (Обучение) для нового игрока."""
    await state.set_state(TutorialState.waiting_for_gps)
    cfg = await db.get_system_settings()
    text = (
        "🎓 *ПРОХОЖДЕНИЕ ОБУЧЕНИЯ (КВЕСТ №0)*\n\n"
        "Чтобы получить доступ к пешим городским приключениям, пройдите быстрое обучение базовым механикам бота.\n\n"
        "🗺 *Шаг 1:* Нажмите на кнопку «📍 Я на месте (Отправить GPS)» внизу экрана.\n"
        f"Тестовая точка обучения находится на координатах: `{cfg.tutorial_latitude}, {cfg.tutorial_longitude}`.\n"
        "(Для прохождения обучения система примет любые ваши координаты, главное — нажать кнопку на клавиатуре).\n\n"
        f"🔑 *Шаг 2:* Бот попросит ввести проверочное слово. Проверочное слово: *{cfg.tutorial_answer}*.\n\n"
        f"За успешное обучение вы получите приветственные *{cfg.daily_gift_base_reward} монет* 🪙 и *100 XP*!"
    )
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.answer(text, parse_mode="Markdown", reply_markup=get_location_keyboard())
    else:
        await message_or_call.answer(text, parse_mode="Markdown", reply_markup=get_location_keyboard())


@user_quest_router.message(TutorialState.waiting_for_gps, F.location)
async def process_tutorial_gps(message: Message, state: FSMContext):
    """Обрабатывает отправку GPS в рамках Квеста №0."""
    await state.set_state(TutorialState.waiting_for_word)
    await message.answer(
        "✅ *Геопозиция подтверждена!*\n\n"
        "Теперь введите проверочное слово для завершения Квеста №0:",
        parse_mode="Markdown"
    )


@user_quest_router.message(TutorialState.waiting_for_word, F.text)
async def process_tutorial_word(message: Message, state: FSMContext):
    """Сверяет проверочное слово Квеста №0 со сложным SequenceMatcher."""
    cfg = await db.get_system_settings()
    user_word = message.text.strip().lower()
    tutorial_ans = cfg.tutorial_answer.strip().lower()

    similarity = await asyncio.to_thread(calculate_matcher, user_word, tutorial_ans)
    
    if similarity >= 0.75:
        await db.set_tutorial_completed(message.from_user.id)
        await message.answer(
            f"🎉 *Обучение успешно пройдено!* (Сходство ответов: {round(similarity*100, 1)}%)\n\n"
            f"Вам начислено *+{cfg.daily_gift_base_reward} монет* 🪙 и *+100 XP*!\n"
            f"Доступ ко всем городским квестам открыт! Напишите /start, чтобы открыть меню приключений.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.clear()
    else:
        await message.answer(f"❌ Неверно! Попробуйте еще раз. Проверочное слово: *{cfg.tutorial_answer}*", parse_mode="Markdown")


# =========================================================================
# ОБРАБОТКА ТЕКСТОВЫХ ОТВЕТОВ С НЕЧЕТКИМ СРАВНЕНИЕМ И КЛАССОВЫМИ БОНУСАМИ
# =========================================================================

@user_quest_router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def process_riddle_answer(message: Message, bot: Bot, state: FSMContext):
    """Сверяет ответ пользователя с нечетким сравнением, начисляет баллы и опыт."""
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    active = await db.get_active_quest(user_id)
    
    if message.text == "❄️ Заморозить квест (Пауза)":
        if active and await db.freeze_active_quest(user_id):
            await message.answer("❄️ Квест заморожен! Прохождение приостановлено.")
        return

    if message.text == "🎒 Мой рюкзак":
        # Перенаправление на инвентарь
        from tgbot.handlers.common import inventory_cmd
        await inventory_cmd(message)
        return

    # ВСЕ СТРОКИ НИЖЕ ДОЛЖНЫ ИМЕТЬ КОРРЕКТНЫЙ ОТСТУП ВНУТРИ ФУНКЦИИ (4 ПРОБЕЛА ДЛЯ ВЕРХНЕГО УРОВНЯ)
    if not active or active.is_frozen or active.is_suspended: 
        return

    # 1. Фикс NPC-скипа: Жестко блокируем ввод текста, если у юзера открыт диалог с NPC
    if active.current_npc_node is not None:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🗣 *Завершите диалог с персонажем перед отправкой текстового ответа на загадку!*", parse_mode="Markdown")
        return

    # 2. Фикс GPS-чита: Проверяем, была ли верифицирована геолокация на ТЕКУЩЕМ шаге
    if active.prev_time is None or active.step_activated_at > active.prev_time:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "📍 *Ответ не принят!*\n\n"
            "Вы попытались угадать ответ в обход правил, не подтвердив свое физическое нахождение на точке.\n"
            "Сначала нажмите кнопку `📍 Я на месте (Отправить GPS)` для прохождения верификации координат шага!",
            parse_mode="Markdown"
        )
        return

    step = await db.get_step_by_id(active.current_step_id)
    user_answer_raw = message.text.strip().lower()
    
    try: 
        await message.delete()
    except Exception: 
        pass

    # Извлекаем ветки переходов
    branches_raw = step.branches
    if hasattr(branches_raw, "model_dump"):
        branches_dict = branches_raw.model_dump()
    elif isinstance(branches_raw, dict):
        branches_dict = branches_raw
    else:
        branches_dict = {}

    actual_branches = branches_dict.get("branches", branches_dict)

    # Неблокирующий расчет нечеткого сходства SequenceMatcher
    best_match, best_sim = None, 0.0
    for key in actual_branches:
        sim = await asyncio.to_thread(calculate_matcher, user_answer_raw, key.lower())
        if sim > best_sim: 
            best_sim, best_match = sim, key
    
    user_answer = best_match.lower() if best_sim >= 0.75 and best_match else user_answer_raw 
    if user_answer in actual_branches:
        await proceed_to_next_step(bot, user_id, step, active, user_answer=user_answer, state=state)
    else:
        await db.increment_error_count(user_id)
        await bot.send_message(user_id, "❌ Ответ неверный! Набранные очки на текущем шаге снижены. Попробуйте еще раз.")

# Хелпер продвижения по шагам квеста с контролем веса рюкзака
async def proceed_to_next_step(bot: Bot, user_id: int, step, active, user_answer: str = None, custom_score: int = None, state: FSMContext = None):
    user = await db.get_user(user_id)
    sys_set = await db.get_system_settings()
    
    # Проверка получения предмета и веса рюкзака перед переходом на следующий шаг (#27)
    if step.gives_item and state:
        added = await try_add_item_with_overweight_check(bot, user_id, step.gives_item, state, step, active)
        if not added:
            return  # Блокируем продвижение до тех пор, пока рюкзак не разгрузят

    # Расчет монетизации с пассивным бонусом Купца
    earned_coins = sys_set.base_step_coins
    if user.rpg_class == "merchant": 
        earned_coins += int(earned_coins * (sys_set.merchant_bonus / 100.0))
    await db.add_coins(user_id, earned_coins)
    
    # Расчет очков с бонусом Историка
    score_multiplier = sys_set.historian_mult if user.rpg_class == "historian" else 1.0
    added_score = custom_score if custom_score is not None else int(sys_set.base_step_score * score_multiplier)
    
    # Получаем ветку переходов
    branches_raw = step.branches
    if hasattr(branches_raw, "model_dump"):
        branches_dict = branches_raw.model_dump()
    elif isinstance(branches_raw, dict):
        branches_dict = branches_raw
    else:
        branches_dict = {}

    actual_branches = branches_dict.get("branches", branches_dict)

    next_destination = actual_branches[user_answer] if user_answer else actual_branches[list(actual_branches.keys())[0]]

# Проверка финала квеста
    if next_destination == "final" or step.is_final:
        progress, active_q = await db.finish_active_quest(user_id, int(sys_set.quest_completion_bonus * score_multiplier))
        
        # Дарим опыт за полное прохождение
        await db.add_xp(user_id, 300)
        
        # 1. Принудительно очищаем старую квестовую reply-клавиатуру на местности
        await bot.send_message(user_id, "🏁 Финишная черта пересечена! Локационные приборы отключены.", reply_markup=ReplyKeyboardRemove())
        
        # 2. Добавляем инлайн-переход в главное меню, чтобы не было тупика
        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Вернуться в Главное Меню", callback_data="back_to_main_start")
        
        await bot.send_message(
            user_id, 
            f"🎉 *КВЕСТ ПОЛНОСТЬЮ ПРОЙДЕН!*\n\n"
            f"⏱ Итоговое время: {datetime.timedelta(seconds=progress.total_time_seconds)}\n"
            f"📈 Набранные очки: *{progress.score}* (с учетом RPG бонусов)\n"
            f"🚨 Количество ошибок: *{progress.errors_count}*\n"
            f"🎖 Награда: *+300 XP*!\n\n"
            f"Нажмите кнопку ниже, чтобы вернуться к выбору новых городских треков:",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await _check_and_trigger_achievements(bot, user_id, "complete_all_quests")
        if progress.errors_count == 0:
            await _check_and_trigger_achievements(bot, user_id, "no_hints", {"errors_count": 0})
        await _check_and_trigger_achievements(bot, user_id, "speed_run", {"total_time": progress.total_time_seconds})
        return

    next_step = await db.get_step_by_id(int(next_destination))
    
    if next_step.secret_price > 0:
        builder = InlineKeyboardBuilder()
        builder.button(text=f"🔓 Открыть ветку ({next_step.secret_price} 🪙)", callback_data=f"unlock_secret_{next_step.id}")
        await bot.send_message(
            user_id, 
            f"🗺 *Обнаружена секретная сюжетная ветка!*\n"
            f"Для разблокировки требуется: *{next_step.secret_price} монет*.\n\n"
            f"Вы желаете разблокировать это приключение?", 
            parse_mode="Markdown", 
            reply_markup=builder.as_markup()
        )
        return
        
    if next_step.required_item and not await db.check_item_in_inventory(user_id, next_step.required_item):
        await bot.send_message(
            user_id, 
            f"🔒 Доступ к этой сюжетной ветке заблокирован!\nВам требуется предмет: *{next_step.required_item}*.", 
            parse_mode="Markdown"
        )
        return

    # Переход к следующему шагу
    await db.update_active_quest_step(user_id, next_step.id, step.latitude, step.longitude, added_score)
    await deliver_step_content(bot, user_id, next_step)


# =========================================================================
# АВТОМАТИЧЕСКАЯ ВЕРИФИКАЦИЯ RPG-ДОСТИЖЕНИЙ
# =========================================================================

async def _check_and_trigger_achievements(bot: Bot, user_id: int, trigger_type: str, context_data: dict = None):
    """Проверяет и начисляет ачивки игрокам при выполнении игровых условий."""
    achievements = await db.get_all_achievements()
    for ach in achievements:
        if ach.required_action != trigger_type or await db.check_achievement_earned(user_id, ach.id): 
            continue
        is_eligible = False
        if trigger_type == "complete_all_quests":
            if await db.get_user_completed_quests_count(user_id) >= len(await db.get_published_quests()) > 0: 
                is_eligible = True
        elif trigger_type == "no_hints" and context_data and context_data.get("errors_count") == 0: 
            is_eligible = True
        elif trigger_type == "speed_run" and context_data and context_data.get("total_time") <= ach.required_value: 
            is_eligible = True
        elif trigger_type == "all_items":
            all_sys = await db.get_all_quest_items_list()
            if all(item in await db.get_user_inventory(user_id) for item in all_sys) and len(all_sys) > 0: 
                is_eligible = True
        elif trigger_type in ["night_run", "rain_run"]: 
            is_eligible = True

        if is_eligible and await db.grant_achievement(user_id, ach.id):
            await bot.send_message(
                chat_id=user_id, 
                text=f"🏆 *ОБНАРУЖЕНО НОВОЕ ДОСТИЖЕНИЕ!*\n\n{ach.badge_emoji} *{ach.name}*\n📝 {ach.description}\n🎁 Награда: *+{ach.reward_coins} 🪙*", 
                parse_mode="Markdown"
            )

@user_quest_router.callback_query(F.data == "resume_active_quest_from_bag")
async def resume_active_quest_from_bag_handler(call: CallbackQuery, bot: Bot):
    """Возвращает игрока из инвентаря на текущий экран активного шага квеста."""
    user_id = call.from_user.id
    active = await db.get_active_quest(user_id)
    if active:
        step = await db.get_step_by_id(active.current_step_id)
        await deliver_step_content(bot, user_id, step)
    else:
        await call.answer("❌ Активный квест не обнаружен.", show_alert=True)
    await call.answer()