import datetime
import math
import logging
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

# Импортируем существующие в вашем проекте модули настроек и базы данных
from tgbot.config import settings
from tgbot.database.db_api import db, get_utc_now
from tgbot.database.models import ShopItemType
from backend.auth import verify_telegram_init_data
from backend.admin_panel import setup_admin

# Конфигурация логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TMA_API")

app = FastAPI(
    title="Perm Quest Mini App API",
    description="Высоконагруженный игровой бэкенд платформы квестов Перми",
    version="2.0.0"
)

# Middleware для сессий (требуется для авторизации в SQLAdmin)
import os
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("ADMIN_SECRET_KEY", "fallback-secret-key"))

# Разрешаем CORS-запросы для интеграции с VPS и локального тестирования
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ И ФУНКЦИИ ---

class LocationCheckSchema(BaseModel):
    latitude: float
    longitude: float

class AnswerSubmitSchema(BaseModel):
    answer: str

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Вычисляет точное расстояние в метрах между двумя координатами."""
    R = 6371000.0  # Радиус Земли в метрах
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

# --- ЗАВИСИМОСТЬ ДЛЯ АВТОРИЗАЦИИ ЧЕРЕЗ TELEGRAM INITDATA ---

async def get_current_user(x_tg_init_data: str = Header(..., alias="X-Tg-Init-Data")) -> dict:
    """
    Зависимость проверяет заголовок авторизации Telegram, извлекает и 
    возвращает проверенного пользователя. Блокирует доступ при невалидной сессии.
    """
    bot_token = settings.bot.token.get_secret_value()
    tg_user = verify_telegram_init_data(x_tg_init_data, bot_token)
    if not tg_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидная сессия Telegram. Доступ заблокирован."
        )
    return tg_user

# --- ИГРОВЫЕ API ЭНДПОИНТЫ ---

@app.on_event("startup")
async def on_startup():
    """Синхронизирует таблицы PostgreSQL и подключает админпанель при старте FastAPI."""
    await db.create_all()
    logger.info("База данных успешно синхронизирована с FastAPI сервером.")
    # Подключаем SQLAdmin после инициализации БД
    setup_admin(app, db.engine)
    logger.info("Веб-админпанель SQLAdmin подключена на /admin/")

@app.get("/api/profile")
async def get_profile(tg_user: dict = Depends(get_current_user)):
    """Возвращает расширенный RPG профиль пользователя."""
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    if not user:
        user = await db.get_or_create_user(
            telegram_id=user_id,
            full_name=tg_user.get("first_name", "Игрок"),
            username=tg_user.get("username")
        )
        
    xp_needed = user.level * 150
    return {
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "coins": user.coins,
        "karma": user.karma,
        "rpg_class": user.rpg_class or "Не выбран",
        "level": user.level,
        "xp": user.xp,
        "xp_needed": xp_needed,
        "max_weight_capacity": user.max_weight_capacity,
        "income_buffer": user.income_buffer,
        "daily_streak": user.daily_streak
    }

@app.post("/api/profile/claim-income")
async def claim_income(tg_user: dict = Depends(get_current_user)):
    """Сбор пассивного накопленного дохода из буфера на баланс игрока."""
    user_id = tg_user.get("id")
    collected = await db.collect_passive_income_buffer(user_id)
    return {"status": "success", "collected_coins": collected}

@app.get("/api/quests")
async def list_quests(tg_user: dict = Depends(get_current_user)):
    """Отдает список всех опубликованных квестов с проверкой левел-гейта."""
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    quests = await db.get_published_quests()
    
    result = []
    for q in quests:
        is_locked = user.level < q.min_level_required if user else True
        result.append({
            "id": q.id,
            "title": q.title,
            "description": q.description,
            "min_level_required": q.min_level_required,
            "max_speed_kmh": q.max_speed_kmh,
            "is_locked": is_locked
        })
    return result

@app.get("/api/quest/active")
async def get_active_quest_state(tg_user: dict = Depends(get_current_user)):
    """Возвращает подробное состояние запущенного квеста и текущего шага."""
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active:
        return {"active": False}
        
    step = await db.get_step_by_id(active.current_step_id)
    if not step:
         return {"active": False}

    # Безопасное чтение JSON полей NPC-диалогов и веток переходов
    npc_dial = step.npc_dialogue
    if hasattr(npc_dial, "model_dump"):
        npc_dial = npc_dial.model_dump()

    return {
        "active": True,
        "quest_id": active.quest_id,
        "score": active.score,
        "errors_count": active.errors_count,
        "is_frozen": active.is_frozen,
        "current_npc_node": active.current_npc_node,
        "step": {
            "id": step.id,
            "instruction_text": step.instruction_text,
            "history_info": step.history_info,
            "photo_then_id": step.photo_then_id,
            "photo_now_id": step.photo_now_id,
            "audio_guide_id": step.audio_guide_id,
            "latitude": step.latitude,
            "longitude": step.longitude,
            "min_karma_required": step.min_karma_required,
            "required_item": step.required_item,
            "gives_item": step.gives_item,
            "secret_price": step.secret_price,
            "npc_name": step.npc_name,
            "npc_dialogue": npc_dial,
            "hints": step.hints or []
        }
    }

@app.post("/api/quest/start/{quest_id}")
async def start_quest(quest_id: int, tg_user: dict = Depends(get_current_user)):
    """Запускает прохождение выбранного квеста."""
    user_id = tg_user.get("id")
    quest = await db.get_quest_with_steps(quest_id)
    if not quest or not quest.steps:
        raise HTTPException(status_code=400, detail="Квест не содержит шагов или не существует.")
        
    first_step = quest.steps[0]
    await db.start_user_quest(user_id, quest_id, first_step.id)
    return {"status": "success", "first_step_id": first_step.id}

@app.post("/api/quest/verify-location")
async def verify_location(loc: LocationCheckSchema, tg_user: dict = Depends(get_current_user)):
    """Сверяет физические GPS-координаты игрока со скоростным античитом."""
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active:
        raise HTTPException(status_code=400, detail="У вас нет запущенного квеста.")
        
    step = await db.get_step_by_id(active.current_step_id)
    distance = calculate_haversine_distance(loc.latitude, loc.longitude, step.latitude, step.longitude)
    
    # Порог успешного обнаружения — 30 метров
    if distance > 30.0:
        return {
            "status": "too_far",
            "distance": int(distance),
            "message": f"Вы еще слишком далеко. До точки: {int(distance)} метров. Подойдите ближе!"
        }
        
    # Детекция аномальной скорости перемещения
    if active.prev_latitude is not None and active.prev_longitude is not None and active.prev_time is not None:
        now = get_utc_now()
        time_diff = (now - active.prev_time).total_seconds()
        if time_diff > 1.0:
            dist_prev = calculate_haversine_distance(active.prev_latitude, active.prev_longitude, loc.latitude, loc.longitude)
            speed_mps = dist_prev / time_diff
            speed_kmh = speed_mps * 3.6
            
            quest = await db.get_quest_by_id(active.quest_id)
            if speed_kmh > quest.max_speed_kmh:
                await db.add_cheat_log(user_id, quest.id, speed_mps, loc.latitude, loc.longitude)
                warnings = await db.increment_cheat_warning(user_id)
                if warnings >= 2:
                    await db.set_ban_status(user_id, True)
                    return {"status": "banned", "message": "Вы забанены античитом за использование Fake GPS!"}
                return {"status": "speed_warning", "message": f"Внимание! Превышена скорость движения: {int(speed_kmh)} км/ч!"}

    await db.set_gps_verified_now(user_id, loc.latitude, loc.longitude)
    return {"status": "success", "distance": int(distance)}

@app.post("/api/quest/submit-answer")
async def submit_answer(ans: AnswerSubmitSchema, tg_user: dict = Depends(get_current_user)):
    """Проверяет ответ загадки."""
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active:
        raise HTTPException(status_code=400, detail="У вас нет активного квеста.")
        
    step = await db.get_step_by_id(active.current_step_id)
    user_ans = ans.answer.strip().lower()
    
    branches = step.branches
    if hasattr(branches, "model_dump"):
        branches = branches.model_dump()
    actual_branches = branches.get("branches", branches)
    
    # Проверка ответа (нечеткое сравнение опускаем на API для ускорения, сравниваем напрямую)
    matched_dest = None
    for key, dest in actual_branches.items():
        if key.strip().lower() == user_ans:
            matched_dest = dest
            break
            
    if matched_dest is None:
        await db.increment_error_count(user_id)
        return {"status": "wrong", "message": "Неверный ответ! Набранные очки снижены."}
        
    # Переход к следующей ветке или завершение
    if matched_dest == "final" or step.is_final:
        progress, _ = await db.finish_active_quest(user_id, 300)
        return {
            "status": "finished",
            "message": "Квест успешно пройден!",
            "score": progress.score,
            "errors": progress.errors_count
        }
        
    await db.update_active_quest_step(user_id, int(matched_dest), step.latitude, step.longitude, 100)
    return {"status": "next_step"}

@app.post("/api/quest/npc-choice/{choice_index}")
async def select_npc_choice(choice_index: int, tg_user: dict = Depends(get_current_user)):
    """Обрабатывает RPG выборы диалогов с NPC."""
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active or not active.current_npc_node:
        raise HTTPException(status_code=400, detail="Диалог с NPC не запущен.")
        
    step = await db.get_step_by_id(active.current_step_id)
    dialogue = step.npc_dialogue
    if hasattr(dialogue, "model_dump"):
        dialogue = dialogue.model_dump()
        
    node = dialogue.get(active.current_npc_node)
    options = node.get("options", [])
    if choice_index < 0 or choice_index >= len(options):
         raise HTTPException(status_code=400, detail="Неверный выбор.")
         
    opt = options[choice_index]
    
    # Применяем эффекты диалога
    if opt.get("karma_change", 0) != 0:
        await db.update_karma(user_id, opt["karma_change"])
    if opt.get("coins_change", 0) != 0:
        await db.add_coins(user_id, opt["coins_change"])
        
    next_node = opt.get("next_node", "exit")
    if next_node == "exit":
        await db.update_active_quest_npc_node(user_id, None)
        return {"status": "exit", "message": "Вы попрощались с персонажем."}
        
    await db.update_active_quest_npc_node(user_id, next_node)
    return {"status": "next_node", "node": next_node}

@app.get("/api/inventory")
async def get_inventory(tg_user: dict = Depends(get_current_user)):
    """Получает текущие предметы в инвентаре."""
    user_id = tg_user.get("id")
    items = await db.get_user_inventory(user_id)
    weight = await db.get_user_current_weight(user_id)
    return {"items": items, "current_weight": weight}

@app.post("/api/inventory/use/{item_name}")
async def use_item(item_name: str, tg_user: dict = Depends(get_current_user)):
    """Активирует расходный предмет."""
    user_id = tg_user.get("id")
    success, msg = await db.activate_consumable_item(user_id, item_name)
    if not success:
         raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@app.post("/api/inventory/discard/{item_name}")
async def discard_item(item_name: str, tg_user: dict = Depends(get_current_user)):
    """Выбрасывает предмет из инвентаря для разгрузки веса."""
    user_id = tg_user.get("id")
    success = await db.discard_inventory_item(user_id, item_name)
    if not success:
        raise HTTPException(status_code=400, detail="Предмет не найден.")
    return {"status": "success"}

@app.get("/api/shop")
async def get_shop_catalog(tg_user: dict = Depends(get_current_user)):
    """Отдает витрину глобального купеческого магазина."""
    items = await db.get_shop_items()
    # Отдаем только глобальные товары (market_id is None)
    catalog = [i for i in items if i.market_id is None]
    return [
        {
            "id": i.id,
            "name": i.name,
            "description": i.description,
            "price": i.price,
            "item_name": i.item_name,
            "item_type": i.item_type,
            "weight": i.weight,
            "generates_income": i.generates_income,
            "income_per_hour": i.income_per_hour
        } for i in catalog
    ]

@app.post("/api/shop/buy/{item_id}")
async def buy_item(item_id: int, tg_user: dict = Depends(get_current_user)):
    """Совершает транзакционную покупку предмета на витрине."""
    user_id = tg_user.get("id")
    shop_item = await db.get_shop_item_by_id(item_id)
    if not shop_item:
        raise HTTPException(status_code=404, detail="Товар не найден.")
        
    user = await db.get_user(user_id)
    if user.coins < shop_item.price:
        raise HTTPException(status_code=400, detail="Недостаточно монет.")
        
    overloaded, _, _ = await db.is_inventory_overloaded(user_id, shop_item.weight)
    if overloaded:
        raise HTTPException(status_code=400, detail="Рюкзак перегружен! Выбросите лишние вещи.")
        
    await db.deduct_coins(user_id, shop_item.price)
    await db.add_item_to_inventory(user_id, shop_item.item_name)
    return {"status": "success", "item_name": shop_item.name}

@app.get("/api/leaderboard")
async def get_leaderboard_data(period: str = "global", tg_user: dict = Depends(get_current_user)):
    """Получает таблицу лидеров рейтинга."""
    if period == "global":
        data = await db.get_leaderboard(limit=15)
    else:
        data = await db.get_seasonal_leaderboard(period=period, limit=15)
    return data