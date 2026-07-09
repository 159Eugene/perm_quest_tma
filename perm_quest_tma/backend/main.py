import os
import datetime
import math
import json
import logging
import aiohttp
import random
from io import BytesIO
from typing import Optional, Dict, Any, List, Union, Tuple
import re

from fastapi import FastAPI, Depends, Header, HTTPException, status, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc

from aiogram import Bot
from aiogram.types import BufferedInputFile

# Импортируем модули настроек и базы данных
from tgbot.config import settings
from tgbot.database.db_api import db, get_utc_now
from tgbot.database.models import(
    Base, ShopItemType, ShopItem, QuestProgress, Step, Quest,
    PlayerLocationLog, ActiveQuest, User, City, QuestMarket, 
    ARMarker, CraftRecipe, Achievement, DailyRiddle, 
    GlobalEvent, RandomEvent, NPCCharacter, LevelConfig
)

from backend.auth import verify_telegram_init_data
from backend.map_admin_routes import setup_map_admin  # Оставили только новую админку!

from contextlib import asynccontextmanager
from redis.asyncio import Redis
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tgbot.services.scheduler_tasks import setup_scheduler
# Исправленный физический путь к файлу защиты:
from tgbot.middlewares.shadow_ban import TMAShadowBanMiddleware

# Конфигурация логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TMA_API")

# Создаем глобальный клиент Redis для Middleware и приложения
redis_client = Redis.from_url(settings.redis.redis_url)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Инициализируем бота для пушей (Bounty-ивенты, рассылки)
    bot = Bot(token=settings.bot.token.get_secret_value())
    app.state.bot = bot
    
    # 2. Инициализируем БД и НОВУЮ админку (старую удалили)
    setup_map_admin(app)
    try:
        await db.seed_initial_data()
        logger.info("Посев базовых системных данных проверен. Map-Админка подключена.")
    except Exception as e:
        logger.error(f"Критическая ошибка при посеве данных БД: {e}", exc_info=True)
        raise e
    
    # 3. Запускаем фоновые задачи
    scheduler = AsyncIOScheduler()
    setup_scheduler(scheduler, bot)
    scheduler.start()
    logger.info("APScheduler запущен в фоне FastAPI.")
    
    yield
    
    # Завершение работы
    scheduler.shutdown()
    await bot.session.close()
    await redis_client.close()

# СТАЛО (Отключили /docs и ограничили CORS твоим доменом):
app = FastAPI(
    title="Quest Sity API v2.0",
    description="Высоконагруженный игровой бэкенд...",
    version="2.1.0",
    docs_url=None,   # <-- ОТКЛЮЧИЛИ /docs
    redoc_url=None,  # <-- ОТКЛЮЧИЛИ /redoc
    lifespan=lifespan
)

app.add_middleware(TMAShadowBanMiddleware, redis=redis_client, rate_limit=10) # <-- ДАЛИ КЛИЕНТУ ДЫШАТЬ
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("ADMIN_SECRET_KEY", "fallback-secret-key"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://questsity.ru", "https://www.questsity.ru"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# НОВЫЙ ЖЕЛЕЗОБЕТОННЫЙ ЗАМОК ТЕХ. РАБОТ:
@app.middleware("http")
async def enforce_maintenance_mode(request: Request, call_next):
    # Блокируем весь геймплей, если в Redis висит флаг. 
    # Админку и роут /profile пропускаем, чтобы телефон игрока смог прочитать статус "under_maintenance"
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/admin"):
        if request.url.path != "/api/profile":
            if await redis_client.get("system:maintenance"):
                return JSONResponse(
                    status_code=503,
                    content={"status": "maintenance", "message": "🛠 Сервер на техническом обслуживании."}
                )
    return await call_next(request)

# --- Pydantic Схемы ---
class RandomEventChoiceSchema(BaseModel):
    choice: str  # "yes", "no", "take", "leave"

class LocationCheckSchema(BaseModel):
    latitude: float
    longitude: float

class AnswerSubmitSchema(BaseModel):
    answer: str

class RiddleSolveSchema(BaseModel):
    riddle_id: int
    answer: str

class ClassChangeSchema(BaseModel):
    rpg_class: str

class CitySelectionSchema(BaseModel):
    city_id: Optional[int] = None
    auto_detect: bool = True

class LocationAutoCitySchema(BaseModel):
    latitude: float
    longitude: float

class ShopEnterSchema(BaseModel):
    market_id: int
    latitude: float
    longitude: float

class PresenceHeartbeatSchema(BaseModel):
    market_id: int

class NPCInteractSchema(BaseModel):
    latitude: float
    longitude: float
    current_node: str = "start"
    choice_idx: Optional[int] = None

class CreateNPCSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    latitude: float
    longitude: float
    radius: float = Field(default=30.0, gt=0)
    stamina_cost_override: Optional[int] = None
    cooldown_override_hours: Optional[int] = None
    is_free: bool = False

class CreateQuestSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: str = Field(..., min_length=1, max_length=2000)
    latitude: float
    longitude: float
    min_level_required: int = Field(default=1, ge=1)
    max_speed_kmh: float = Field(default=15.0, gt=0)
    is_coop: bool = Field(default=False)
    is_published: bool = Field(default=False)
    coop_max_size: int = Field(default=4, ge=2)
    global_time_limit_seconds: Optional[int] = None
    stamina_cost_override: Optional[int] = None
    cooldown_override_hours: Optional[int] = None
    is_free: bool = False

class CreateCitySchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    latitude: float
    longitude: float
    radius_km: float = Field(default=5.0, gt=0)
    is_active: bool = Field(default=True)

class CreateMarketSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    latitude: float
    longitude: float
    radius: float = Field(default=50.0, gt=0)
    item_ids: Optional[list[int]] = None  # <-- Список ID привязываемых товаров

class UpdateQuestSchema(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    min_level_required: Optional[int] = None
    max_speed_kmh: Optional[float] = None
    is_coop: Optional[bool] = None
    is_published: Optional[bool] = None
    coop_max_size: Optional[int] = None
    global_time_limit_seconds: Optional[int] = None
    stamina_cost_override: Optional[int] = None
    cooldown_override_hours: Optional[int] = None
    is_free: Optional[bool] = None

class UpdateCitySchema(BaseModel):
    name: Optional[str] = None
    radius_km: Optional[float] = None
    is_active: Optional[bool] = None

class UpdateMarketSchema(BaseModel):
    name: Optional[str] = None
    radius: Optional[float] = None
    item_ids: Optional[list[int]] = None  # <-- Список ID привязываемых товаров

class UpdateNPCSchema(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    radius: Optional[float] = None
    stamina_cost_override: Optional[int] = None
    cooldown_override_hours: Optional[int] = None
    is_free: Optional[bool] = None

class AdminShopItemSchema(BaseModel):
    id: Optional[int] = None
    name: str
    item_name: str
    description: str = ""
    price: int = 0
    weight: int = 0
    market_ids: Optional[list[int]] = None  # Список привязанных магазинов

class AdminRecipeSchema(BaseModel):
    id: Optional[int] = None
    name: str
    description: str = ""
    result_item_name: str
    ingredients: Dict[str, int] = Field(default_factory=dict)
    coins_cost: int = 0
    min_level: int = 1

class AdminAchievementSchema(BaseModel):
    id: Optional[int] = None
    name: str
    description: str = ""
    badge_emoji: str = "🏆"
    required_action: str
    required_value: Optional[int] = None
    reward_coins: int = 0
    required_value_bronze: Optional[int] = None
    required_value_silver: Optional[int] = None
    required_value_diamond: Optional[int] = None
    reward_coins_bronze: int = 0
    reward_coins_silver: int = 0
    reward_coins_diamond: int = 0

class AdminRiddleSchema(BaseModel):
    id: Optional[int] = None
    question: str
    correct_answer: str
    reward_coins: int = 0

class AdminGlobalEventSchema(BaseModel):
    id: Optional[int] = None
    name: str
    description: str = ""
    city_id: Optional[int] = None
    is_active: bool = False

class AdminRandomEventSchema(BaseModel):
    id: Optional[int] = None
    event_type: str
    text: str
    probability: float = 10.0
    coins_impact: int = 0
    karma_impact: int = 0
    xp_reward: int = 0

class DrawflowConnectionTargetSchema(BaseModel):
    node: Union[str, int]
    output: Optional[str] = None
    input: Optional[str] = None

class DrawflowOutputPortSchema(BaseModel):
    connections: List[DrawflowConnectionTargetSchema]
    branch_key: Optional[str] = None

class DrawflowNodeDataSchema(BaseModel):
    step_id: Optional[Union[int, str]] = None
    instruction_text: Optional[str] = ""
    welcome_message: Optional[str] = None
    branch_key: Optional[str] = None
    answer_key: Optional[str] = None
    npc_text: Optional[str] = None
    options: Optional[Any] = None  
    
    # --- Спринт 2: Прокидываем GPS-координаты холста в бэкенд ---
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    target_lat: Optional[float] = None
    target_lng: Optional[float] = None
    
    karma_change: Optional[Union[int, str]] = 0
    gives_item: Optional[str] = None
    coins: Optional[Union[int, str]] = 0
    required_item: Optional[str] = None
    min_karma: Optional[Union[int, str]] = 0
    min_level: Optional[Union[int, str]] = 1
    required_class: Optional[str] = None
    history_info: Optional[str] = None
    photo_then_id: Optional[str] = None
    photo_now_id: Optional[str] = None
    audio_guide_id: Optional[str] = None
    npc_name: Optional[str] = None
    hints: Optional[Union[str, List[Dict[str, Any]]]] = None
    radius_meters: Optional[Union[int, str]] = 30
    is_day_only: Optional[bool] = False
    is_night_only: Optional[bool] = False
    weather_sun_only: Optional[bool] = False
    weather_rain_only: Optional[bool] = False
    required_flag: Optional[str] = None
    granted_flag: Optional[str] = None
    item_give: Optional[str] = None
    item_take: Optional[str] = None
    xp_change: Optional[Union[int, str]] = 0

class DrawflowNodeSchema(BaseModel):
    id: int
    name: str
    data: DrawflowNodeDataSchema
    class_name: Optional[str] = Field(None, alias="class")
    html: Optional[str] = None
    typenode: Optional[bool] = None
    inputs: Dict[str, Any]
    outputs: Dict[str, DrawflowOutputPortSchema]
    pos_x: float
    pos_y: float

class SaveGraphSchema(BaseModel):
    drawflow_data: Dict[str, DrawflowNodeSchema]

class AdminPlayerUpdateSchema(BaseModel):
    field: str
    value: Union[int, str]
    mode: str = "set"  # "set" (установить цифру) или "add" (прибавить/отнять)

class AdminPlayerInventorySchema(BaseModel):
    action: str  # "add" или "remove"
    item_name: str


# --- Вспомогательные функции ---
def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

async def get_current_user(x_tg_init_data: str = Header(..., alias="X-Tg-Init-Data")) -> dict:
    bot_token = settings.bot.token.get_secret_value()
    tg_user = verify_telegram_init_data(x_tg_init_data, bot_token)
    if not tg_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидная сессия Telegram. Доступ заблокирован."
        )
    return tg_user

def normalize_npc_dialogue(npc_dialogue: Any) -> Optional[Dict[str, Any]]:
    if not npc_dialogue: return None
    if hasattr(npc_dialogue, "model_dump"): npc_dialogue = npc_dialogue.model_dump()
    if isinstance(npc_dialogue, str):
        try: npc_dialogue = json.loads(npc_dialogue)
        except json.JSONDecodeError: return None
    if isinstance(npc_dialogue, dict): return npc_dialogue
    return None

def get_npc_start_node(npc_dialogue: Any) -> Optional[str]:
    dialogue = normalize_npc_dialogue(npc_dialogue)
    if not dialogue: return None
    if "start" in dialogue: return "start"
    return next(iter(dialogue.keys()), None)

def normalize_json_field(value: Any) -> Any:
    if value is None: 
        return None
    if hasattr(value, "model_dump"): 
        # ИСПРАВЛЕНИЕ: Добавлен by_alias=True, чтобы не терять _ui координаты
        return value.model_dump(by_alias=True)
    if isinstance(value, str):
        try: return json.loads(value)
        except Exception: return value
    return value

def generate_slug(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug.strip('-')

async def check_and_grant_achievements(user_id: int, trigger_type: str, context_data: dict = None) -> list:
    achievements = await db.get_all_achievements()
    earned_dict = await db.get_user_earned_achievements_dict(user_id)
    newly_granted = []
    
    tiers_order = {'bronze': 1, 'silver': 2, 'diamond': 3}
    
    for ach in achievements:
        if ach.required_action != trigger_type: continue
        
        curr_tier = earned_dict.get(ach.id)
        curr_tier_num = tiers_order.get(curr_tier, 0)
        if curr_tier_num == 3: continue  # Уже получен максимальный (алмазный) ранг
        
        val = 0
        if trigger_type == "complete_all_quests":
            val = await db.get_user_completed_quests_count(user_id)
        elif trigger_type == "no_hints" and context_data and context_data.get("errors_count") == 0:
            val = 10
        elif trigger_type == "speed_run" and context_data:
            val = context_data.get("total_time", 9999)
        elif trigger_type == "all_items":
            all_sys = await db.get_all_quest_items_list()
            val = len(await db.get_user_inventory(user_id)) if len(all_sys) > 0 and all(item in await db.get_user_inventory(user_id) for item in all_sys) else 0
        elif trigger_type in ["night_run", "rain_run"]:
            val = 10

        target_tier = None
        target_coins = 0
        
        # Спидран: чем меньше секунд, тем выше ранг. Остальные: чем больше, тем лучше
        if trigger_type == "speed_run":
            if ach.required_value_diamond and val <= ach.required_value_diamond:
                target_tier, target_coins = 'diamond', ach.reward_coins_diamond
            elif ach.required_value_silver and val <= ach.required_value_silver:
                target_tier, target_coins = 'silver', ach.reward_coins_silver
            elif ach.required_value_bronze and val <= ach.required_value_bronze:
                target_tier, target_coins = 'bronze', ach.reward_coins_bronze
        else:
            if ach.required_value_diamond and val >= ach.required_value_diamond:
                target_tier, target_coins = 'diamond', ach.reward_coins_diamond
            elif ach.required_value_silver and val >= ach.required_value_silver:
                target_tier, target_coins = 'silver', ach.reward_coins_silver
            elif ach.required_value_bronze and val >= ach.required_value_bronze:
                target_tier, target_coins = 'bronze', ach.reward_coins_bronze
                
        if target_tier and tiers_order.get(target_tier, 0) > curr_tier_num:
            prev_coins = 0
            if curr_tier == 'bronze': prev_coins = ach.reward_coins_bronze
            elif curr_tier == 'silver': prev_coins = ach.reward_coins_silver
            
            diff_coins = max(0, target_coins - prev_coins)
            
            upgraded = await db.upsert_user_achievement(user_id, ach.id, target_tier, diff_coins)
            if upgraded:
                newly_granted.append({
                    "name": ach.name,
                    "badge": ach.badge_emoji,
                    "desc": ach.description,
                    "coins": diff_coins,
                    "tier": target_tier
                })
                
    return newly_granted


# =====================================================================
# АДМИН-ПАНЕЛЬ: LIVE-OPS И CRUD
# =====================================================================

class AdminBalanceSettingsSchema(BaseModel):
    default_quest_start_cost: int
    default_step_cost: int
    default_npc_talk_cost: int
    default_quest_cooldown_hours: int
    default_npc_cooldown_hours: int

@app.get("/api/admin/settings/balance")
async def get_admin_balance_settings_route():
    """Отдает глобальные LiveOps настройки баланса Перми"""
    cfg = await db.get_system_settings()
    return {
        "default_quest_start_cost": getattr(cfg, 'default_quest_start_cost', 20) or 20,
        "default_step_cost": getattr(cfg, 'default_step_cost', 15) or 15,
        "default_npc_talk_cost": getattr(cfg, 'default_npc_talk_cost', 5) or 5,
        "default_quest_cooldown_hours": getattr(cfg, 'default_quest_cooldown_hours', 20) or 20,
        "default_npc_cooldown_hours": getattr(cfg, 'default_npc_cooldown_hours', 24) or 24,
    }

@app.post("/api/admin/settings/balance")
async def update_admin_balance_settings_route(payload: AdminBalanceSettingsSchema):
    """Сохраняет глобальные LiveOps настройки баланса"""
    await db.update_system_settings(**payload.model_dump())
    return {"status": "success"}

@app.get("/api/admin/players")
async def get_all_players_for_crm():
    """Отдает полный массив игроков для CRM-таблицы"""
    return await db.get_all_players_crm()

@app.post("/api/admin/players/{user_id}/ban")
async def toggle_player_ban_crm(user_id: int, request: Request):
    """Переключатель блокировки юзера"""
    data = await request.json()
    target_ban = bool(data.get("is_banned", False))
    await db.set_ban_status(user_id, target_ban)
    return {"status": "success", "is_banned": target_ban}

@app.post("/api/admin/players/{user_id}/update")
async def update_player_stat_crm(user_id: int, payload: AdminPlayerUpdateSchema):
    """Точечное изменение статов (плюс/минус или ввод числа)"""
    success, msg = await db.admin_update_player_field(
        user_id=user_id, field=payload.field, value=payload.value, mode=payload.mode
    )
    if not success: raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@app.post("/api/admin/players/{user_id}/inventory")
async def edit_player_inventory_crm(user_id: int, payload: AdminPlayerInventorySchema):
    """Выдача или изъятие конкретного предмета из сумки игрока"""
    if payload.action == "add":
        res = await db.add_item_to_inventory(user_id, payload.item_name)
        if not res: raise HTTPException(status_code=400, detail="Этот артефакт уже есть в рюкзаке игрока")
        msg = f"Артефакт '{payload.item_name}' успешно выдан!"
    elif payload.action == "remove":
        res = await db.discard_inventory_item(user_id, payload.item_name)
        if not res: raise HTTPException(status_code=400, detail="Предмет не найден в рюкзаке")
        msg = f"Предмет '{payload.item_name}' изъят"
    else:
        raise HTTPException(status_code=400, detail="Неизвестная операция")

    return {"status": "success", "message": msg}

@app.post("/api/admin/players/{user_id}/tester")
async def toggle_tester_crm(user_id: int):
    is_now_tester = await db.toggle_tester_status(user_id)
    return {"status": "success", "is_tester": is_now_tester}

@app.post("/api/admin/upload-media")
async def upload_media(file: UploadFile = File(...)):
    dump_channel_id = getattr(settings.bot, 'dump_channel_id', None)
    if not dump_channel_id:
        raise HTTPException(status_code=500, detail="DUMP_CHANNEL_ID не настроен. Загрузка медиа отключена.")

    try:
        file_bytes = await file.read()
        if len(file_bytes) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Файл слишком большой (> 50 МБ).")

        bot = Bot(token=settings.bot.token.get_secret_value())
        input_file = BufferedInputFile(file_bytes, filename=file.filename or "uploaded_file")

        if file.content_type and file.content_type.startswith("image/"):
            msg = await bot.send_photo(chat_id=dump_channel_id, photo=input_file)
            file_id = msg.photo[-1].file_id
        elif file.content_type and file.content_type.startswith("audio/"):
            msg = await bot.send_audio(chat_id=dump_channel_id, audio=input_file)
            file_id = msg.audio.file_id
        elif file.content_type and file.content_type.startswith("video/"):
            msg = await bot.send_video(chat_id=dump_channel_id, video=input_file)
            file_id = msg.video.file_id
        else:
            msg = await bot.send_document(chat_id=dump_channel_id, document=input_file)
            file_id = msg.document.file_id
            
        return {"file_id": file_id, "filename": file.filename}

    except Exception as e:
        logger.error(f"Ошибка загрузки файла в Telegram: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка Bot API: {str(e)}")
        
    finally:
        if 'bot' in locals():
            await bot.session.close()

os.makedirs("media_cache", exist_ok=True)

@app.get("/api/media/{file_id}")
async def get_telegram_media(file_id: str):
    bot_token = settings.bot.token.get_secret_value()
    file_id = file_id.strip()
    
    cache_path = f"media_cache/{file_id}"
    if os.path.exists(cache_path):
        return FileResponse(
            path=cache_path, 
            headers={
                "Cache-Control": "public, max-age=31536000", 
                "Access-Control-Allow-Origin": "*"
            }
        )

    try:
        async with aiohttp.ClientSession() as session:
            tg_api_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
            async with session.get(tg_api_url) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404, detail="Media not found in TG")
                
                data = await resp.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=404, detail="Invalid file_id")
                
                file_path = data["result"]["file_path"]

            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            async with session.get(download_url) as file_resp:
                if file_resp.status != 200:
                    raise HTTPException(status_code=500, detail="Error downloading media")
                
                content = await file_resp.read()
                content_type = file_resp.headers.get("Content-Type", "image/jpeg")

        with open(cache_path, "wb") as f:
            f.write(content)

        headers = {
            "Cache-Control": "public, max-age=31536000",
            "Access-Control-Allow-Origin": "*"
        }
        return StreamingResponse(BytesIO(content), media_type=content_type, headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal proxy error: {str(e)}")

@app.get("/api/admin/recipes")
async def get_admin_recipes():
    recipes = await db.get_all_craft_recipes()
    return [{
        "id": r.id, "name": r.name, "description": r.description, 
        "result_item_name": r.result_item_name, "ingredients": r.ingredients, 
        "coins_cost": r.coins_cost, "min_level": r.min_level
    } for r in recipes]

@app.post("/api/admin/recipes")
async def create_admin_recipe(req: AdminRecipeSchema):
    await db.create_craft_recipe(
        req.name, req.description, req.result_item_name, 
        req.ingredients, req.coins_cost, req.min_level
    )
    return {"status": "success"}

@app.put("/api/admin/recipes")
async def update_admin_recipe(req: AdminRecipeSchema):
    if req.id:
        data = req.model_dump(exclude={'id'})
        await db.update_craft_recipe(req.id, **data)
    return {"status": "success"}

@app.delete("/api/admin/recipes/{recipe_id}")
async def delete_admin_recipe(recipe_id: int):
    await db.delete_craft_recipe(recipe_id)
    return {"status": "success"}

@app.get("/api/admin/achievements")
async def get_admin_achievements():
    achievements = await db.get_all_achievements()
    return [{
        "id": a.id, "name": a.name, "description": a.description, 
        "badge_emoji": a.badge_emoji, "required_action": a.required_action, 
        "required_value": a.required_value, "reward_coins": a.reward_coins
    } for a in achievements]

@app.post("/api/admin/achievements")
async def create_admin_achievement(req: AdminAchievementSchema):
    await db.create_achievement(
        req.name, req.description, req.badge_emoji, 
        req.required_action, req.required_value, req.reward_coins
    )
    return {"status": "success"}

@app.put("/api/admin/achievements")
async def update_admin_achievement(req: AdminAchievementSchema):
    if req.id:
        data = req.model_dump(exclude={'id'})
        await db.update_achievement(req.id, **data)
    return {"status": "success"}

@app.delete("/api/admin/achievements/{ach_id}")
async def delete_admin_achievement(ach_id: int):
    await db.delete_achievement(ach_id)
    return {"status": "success"}

@app.get("/api/admin/riddles")
async def get_admin_riddles():
    riddles = await db.get_all_daily_riddles()
    return [{
        "id": r.id, "question": r.question, "correct_answer": r.correct_answer, 
        "reward_coins": r.reward_coins
    } for r in riddles]

@app.post("/api/admin/riddles")
async def create_admin_riddle(req: AdminRiddleSchema):
    await db.create_daily_riddle(req.question, req.correct_answer, req.reward_coins)
    return {"status": "success"}

@app.put("/api/admin/riddles")
async def update_admin_riddle(req: AdminRiddleSchema):
    if req.id:
        data = req.model_dump(exclude={'id'})
        await db.update_daily_riddle(req.id, **data)
    return {"status": "success"}

@app.delete("/api/admin/riddles/{riddle_id}")
async def delete_admin_riddle(riddle_id: int):
    await db.delete_daily_riddle(riddle_id)
    return {"status": "success"}

@app.get("/api/admin/global-events")
async def get_admin_global_events():
    events = await db.get_all_global_events()
    return [{
        "id": e.id, "name": e.name, "description": e.description, 
        "city_id": e.city_id, "is_active": e.is_active
    } for e in events]

@app.post("/api/admin/global-events")
async def create_admin_global_event(req: AdminGlobalEventSchema):
    await db.create_global_event(req.name, req.description, req.city_id, req.is_active)
    return {"status": "success"}

@app.put("/api/admin/global-events")
async def update_admin_global_event(req: AdminGlobalEventSchema):
    if req.id:
        data = req.model_dump(exclude={'id'})
        await db.update_global_event(req.id, **data)
    return {"status": "success"}

@app.delete("/api/admin/global-events/{event_id}")
async def delete_admin_global_event(event_id: int):
    await db.delete_global_event(event_id)
    return {"status": "success"}

@app.get("/api/admin/random-events")
async def get_admin_random_events():
    events = await db.get_all_random_events()
    return [{
        "id": e.id, "event_type": e.event_type, "text": e.text, 
        "probability": e.probability, "coins_impact": e.coins_impact, 
        "karma_impact": e.karma_impact, "xp_reward": e.xp_reward
    } for e in events]

@app.post("/api/admin/random-events")
async def create_admin_random_event(req: AdminRandomEventSchema):
    await db.create_random_event(
        req.event_type, req.text, req.probability, 
        req.coins_impact, req.karma_impact, req.xp_reward
    )
    return {"status": "success"}

@app.put("/api/admin/random-events")
async def update_admin_random_event(req: AdminRandomEventSchema):
    if req.id:
        data = req.model_dump(exclude={'id'})
        await db.update_random_event(req.id, **data)
    return {"status": "success"}

@app.delete("/api/admin/random-events/{event_id}")
async def delete_admin_random_event(event_id: int):
    await db.delete_random_event(event_id)
    return {"status": "success"}

@app.get("/api/admin/quests/{quest_id}/graph")
async def get_quest_graph(quest_id: int):
    async with db.session_pool() as session:
        quest = (await session.execute(select(Quest).where(Quest.id == quest_id))).scalar_one_or_none()
        if quest and quest.drawflow_data:
            return quest.drawflow_data

        stmt = select(Step).where(Step.quest_id == quest_id).order_by(Step.id)
        steps = (await session.execute(stmt)).scalars().all()
        if not steps: return {"drawflow": {"Home": {"data": {}}}}
            
        drawflow_nodes = {}
        node_counter = 1000000 
        step_to_node_id = {}
        
        for step in steps:
            branches_data = normalize_json_field(step.branches)
            if not isinstance(branches_data, dict): branches_data = {}
            ui_meta = branches_data.get("_ui", {}) if isinstance(branches_data.get("_ui"), dict) else {}
            pos_x = ui_meta.get("pos_x", 150 + (step.id * 200)) 
            pos_y = ui_meta.get("pos_y", 200)
            actual_branches = branches_data.get("branches", {}) if isinstance(branches_data.get("branches"), dict) else {}
            ans_key = next(iter(actual_branches.keys())) if actual_branches else ""

            n_id = step.id
            drawflow_nodes[str(n_id)] = {
                "id": n_id, "name": "step_node",
                "data": {
                    "step_id": step.id, "welcome_message": getattr(step, "welcome_message", None) or "",
                    "instruction_text": step.instruction_text, "history_info": step.history_info or "",
                    "photo_then_id": step.photo_then_id or "", "photo_now_id": step.photo_now_id or "",
                    "audio_guide_id": step.audio_guide_id or "", "latitude": getattr(step, "latitude", 58.0129),
                    "longitude": getattr(step, "longitude", 56.2337), "npc_name": step.npc_name or "",
                    "hints": step.hints or "[]", "answer_key": ans_key, "radius_meters": getattr(step, "radius_meters", 30),
                    "is_day_only": getattr(step, "is_day_only", False), "is_night_only": getattr(step, "is_night_only", False),
                    "weather_sun_only": getattr(step, "weather_sun_only", False), "weather_rain_only": getattr(step, "weather_rain_only", False),
                    "required_flag": getattr(step, "required_flag", ""), "granted_flag": getattr(step, "granted_flag", "")
                },
                "class": "step_node", "html": "step_node", "typenode": False,
                "inputs": {"input_1": {"connections": []}}, "outputs": {}, "pos_x": pos_x, "pos_y": pos_y
            }
            step_to_node_id[step.id] = n_id

        for step in steps:
            start_node_id = step_to_node_id[step.id]
            curr_tail_id = start_node_id
            curr_x = drawflow_nodes[str(start_node_id)]["pos_x"]
            curr_y = drawflow_nodes[str(start_node_id)]["pos_y"]
            
            if "output_1" not in drawflow_nodes[str(start_node_id)]["outputs"]:
                drawflow_nodes[str(start_node_id)]["outputs"]["output_1"] = {"connections": []}
            out_port = "output_1"

            dialogue = normalize_json_field(step.npc_dialogue)
            if dialogue and isinstance(dialogue, dict):
                for node_key, node_data in dialogue.items():
                    opt = node_data.get("options", [{}])[0] if node_data.get("options") else {}
                    curr_x += 300
                    n_id = node_counter; node_counter += 1
                    drawflow_nodes[str(n_id)] = {
                        "id": n_id, "name": "npc_node",
                        "data": {"answer_key": opt.get("text", ""), "npc_text": node_data.get("text", ""), "karma_change": opt.get("karma_change", 0), "item_give": opt.get("item_give", ""), "item_take": opt.get("item_take", ""), "xp_change": opt.get("xp_change", 0)},
                        "class": "npc_node", "html": "npc_node", "typenode": False,
                        "inputs": {"input_1": {"connections": [{"node": str(curr_tail_id), "input": out_port}]}},
                        "outputs": {"output_1": {"connections": []}}, "pos_x": curr_x, "pos_y": curr_y
                    }
                    drawflow_nodes[str(curr_tail_id)]["outputs"][out_port]["connections"].append({"node": str(n_id), "output": "input_1"})
                    curr_tail_id = n_id; out_port = "output_1"

            if step.gives_item or step.secret_price:
                curr_x += 300
                n_id = node_counter; node_counter += 1
                drawflow_nodes[str(n_id)] = {
                    "id": n_id, "name": "reward_node",
                    "data": {"answer_key": "", "gives_item": step.gives_item or "", "coins": step.secret_price or 0},
                    "class": "reward_node", "html": "reward_node", "typenode": False,
                    "inputs": {"input_1": {"connections": [{"node": str(curr_tail_id), "input": out_port}]}},
                    "outputs": {"output_1": {"connections": []}}, "pos_x": curr_x, "pos_y": curr_y
                }
                drawflow_nodes[str(curr_tail_id)]["outputs"][out_port]["connections"].append({"node": str(n_id), "output": "input_1"})
                curr_tail_id = n_id; out_port = "output_1"

            branches_data = normalize_json_field(step.branches) or {}
            actual_branches = branches_data.get("branches", {}) if isinstance(branches_data, dict) else {}
            
            if actual_branches:
                for idx, (ans_key, target_data) in enumerate(actual_branches.items()):
                    req_item, min_k, min_level, req_class = "", 0, 1, ""
                    target_step_id, fail_target_id = None, None
                    if isinstance(target_data, dict):
                        target_step_id = target_data.get("target"); fail_target_id = target_data.get("fail_target")
                        req_item = target_data.get("required_item", ""); min_k = target_data.get("min_karma", 0)
                        min_level = target_data.get("min_level", 1); req_class = target_data.get("required_class", "")
                    else: target_step_id = target_data
                        
                    is_final = str(target_step_id) == "final"
                    target_node_id = step_to_node_id.get(int(target_step_id)) if not is_final and target_step_id and str(target_step_id).isdigit() else None
                    
                    cond_node_id = None
                    if req_item or min_k > 0 or min_level > 1 or req_class:
                        curr_x += 300
                        n_id = node_counter; node_counter += 1
                        cond_y = curr_y + (idx * 150)
                        drawflow_nodes[str(n_id)] = {
                            "id": n_id, "name": "condition_node",
                            "data": {"answer_key": ans_key, "required_item": req_item, "min_karma": min_k, "min_level": min_level, "required_class": req_class, "target_lat": target_data.get("target_lat") if isinstance(target_data, dict) else None, "target_lng": target_data.get("target_lng") if isinstance(target_data, dict) else None, "target_radius": target_data.get("target_radius") if isinstance(target_data, dict) else None, "welcome_message": target_data.get("welcome_message") if isinstance(target_data, dict) else None},
                            "class": "condition_node", "html": "condition_node", "typenode": False,
                            "inputs": {"input_1": {"connections": [{"node": str(curr_tail_id), "input": out_port}]}},
                            "outputs": {"output_1": {"connections": []}, "output_2": {"connections": []}}, "pos_x": curr_x, "pos_y": cond_y
                        }
                        drawflow_nodes[str(curr_tail_id)]["outputs"][out_port]["connections"].append({"node": str(n_id), "output": "input_1"})
                        cond_node_id = n_id
                        
                        if fail_target_id:
                            f_tgt_node = step_to_node_id.get(int(fail_target_id)) if str(fail_target_id) != "final" and str(fail_target_id).isdigit() else None
                            if str(fail_target_id) == "final":
                                curr_x += 200; f_id = node_counter; node_counter += 1
                                drawflow_nodes[str(f_id)] = {"id": f_id, "name": "final_node", "data": {"step_id": "final"}, "class": "final_node", "html": "final_node", "typenode": False, "inputs": {"input_1": {"connections": [{"node": str(cond_node_id), "input": "output_2"}]}}, "outputs": {}, "pos_x": curr_x, "pos_y": cond_y + 150}
                                drawflow_nodes[str(cond_node_id)]["outputs"]["output_2"]["connections"].append({"node": str(f_id), "output": "input_1"})
                            elif f_tgt_node:
                                drawflow_nodes[str(cond_node_id)]["outputs"]["output_2"]["connections"].append({"node": str(f_tgt_node), "output": "input_1"})
                                if "input_1" not in drawflow_nodes[str(f_tgt_node)]["inputs"]: drawflow_nodes[str(f_tgt_node)]["inputs"]["input_1"] = {"connections": []}
                                drawflow_nodes[str(f_tgt_node)]["inputs"]["input_1"]["connections"].append({"node": str(cond_node_id), "input": "output_2"})

                    prev_id = cond_node_id if cond_node_id else curr_tail_id
                    prev_port = "output_1" if cond_node_id else out_port
                    
                    if is_final:
                        curr_x += 300; f_id = node_counter; node_counter += 1
                        drawflow_nodes[str(f_id)] = {"id": f_id, "name": "final_node", "data": {"step_id": "final"}, "class": "final_node", "html": "final_node", "typenode": False, "inputs": {"input_1": {"connections": [{"node": str(prev_id), "input": prev_port}]}}, "outputs": {}, "pos_x": curr_x, "pos_y": curr_y + (idx * 150)}
                        drawflow_nodes[str(prev_id)]["outputs"][prev_port]["connections"].append({"node": str(f_id), "output": "input_1"})
                    elif target_node_id:
                        drawflow_nodes[str(prev_id)]["outputs"][prev_port]["connections"].append({"node": str(target_node_id), "output": "input_1"})
                        if "input_1" not in drawflow_nodes[str(target_node_id)]["inputs"]: drawflow_nodes[str(target_node_id)]["inputs"]["input_1"] = {"connections": []}
                        drawflow_nodes[str(target_node_id)]["inputs"]["input_1"]["connections"].append({"node": str(prev_id), "input": prev_port})
            else:
                if "output_1" not in drawflow_nodes[str(curr_tail_id)]["outputs"]: drawflow_nodes[str(curr_tail_id)]["outputs"]["output_1"] = {"connections": []}

        if steps:
            first_step_node_id = str(step_to_node_id[steps[0].id])
            drawflow_nodes["999999"] = {"id": 999999, "name": "start_node", "data": {}, "class": "start_node", "html": "start_node", "typenode": False, "inputs": {}, "outputs": {"output_1": {"connections": [{"node": first_step_node_id, "output": "input_1"}]}}, "pos_x": 50, "pos_y": 200}
            if "input_1" not in drawflow_nodes[first_step_node_id]["inputs"]: drawflow_nodes[first_step_node_id]["inputs"]["input_1"] = {"connections": []}
            drawflow_nodes[first_step_node_id]["inputs"]["input_1"]["connections"].append({"node": "999999", "input": "output_1"})

        return {"drawflow": {"Home": {"data": drawflow_nodes}}}

def validate_quest_graph_topology(nodes: Dict[str, DrawflowNodeSchema]) -> tuple[bool, str]:
    """Валидатор топологии Drawflow (проверка циклов ампутирована для поддержки петель возврата)."""
    start_node_id = next((nid for nid, n in nodes.items() if n.name == "start_node"), None)
    if not start_node_id: return False, "Ошибка: В графе отсутствует обязательный стартовый компонент ('Старт')."
    return True, "Топология корректна"

@app.post("/api/admin/quests/{quest_id}/graph")
async def save_quest_graph(quest_id: int, payload: SaveGraphSchema):
    nodes = payload.drawflow_data
    is_valid, error_msg = validate_quest_graph_topology(nodes)
    if not is_valid: raise HTTPException(status_code=400, detail=error_msg)

    async with db.session_pool() as session:
        quest = (await session.execute(select(Quest).where(Quest.id == quest_id))).scalar_one_or_none()
        if not quest: raise HTTPException(status_code=404, detail="Квест не найден")
        quest.drawflow_data = {"drawflow": {"Home": {"data": {k: v.model_dump(by_alias=True) for k, v in nodes.items()}}}}
        session.add(quest)

        stmt = select(Step).where(Step.quest_id == quest_id)
        steps_res = await session.execute(stmt)
        steps_dict = {step.id: step for step in steps_res.scalars().all()}
        
        base_lat, base_lon = 58.0129, 56.2337
        if steps_dict:
            first_s = next(iter(steps_dict.values()))
            base_lat, base_lon = first_s.latitude, first_s.longitude
        
        node_to_step_map = {}
        for node_id, node_obj in nodes.items():
            if node_obj.name == "step_node":
                raw_lat = getattr(node_obj.data, 'latitude', None)
                raw_lon = getattr(node_obj.data, 'longitude', None)
                step_lat = float(raw_lat) if raw_lat is not None else base_lat
                step_lon = float(raw_lon) if raw_lon is not None else base_lon

                s_id = node_obj.data.step_id
                if s_id and str(s_id).isdigit():
                    s_id = int(s_id)
                    node_to_step_map[int(node_id)] = s_id
                    if s_id in steps_dict:
                        steps_dict[s_id].latitude = step_lat
                        steps_dict[s_id].longitude = step_lon
                        if node_obj.data.instruction_text:
                            steps_dict[s_id].instruction_text = node_obj.data.instruction_text
                        steps_dict[s_id].welcome_message = node_obj.data.welcome_message
                        steps_dict[s_id].history_info = node_obj.data.history_info
                        steps_dict[s_id].photo_then_id = node_obj.data.photo_then_id
                        steps_dict[s_id].photo_now_id = node_obj.data.photo_now_id
                        steps_dict[s_id].audio_guide_id = node_obj.data.audio_guide_id
                        steps_dict[s_id].npc_name = node_obj.data.npc_name
                        steps_dict[s_id].radius_meters = int(node_obj.data.radius_meters) if node_obj.data.radius_meters else 30
                        steps_dict[s_id].is_day_only = bool(node_obj.data.is_day_only)
                        steps_dict[s_id].is_night_only = bool(node_obj.data.is_night_only)
                        steps_dict[s_id].weather_sun_only = bool(node_obj.data.weather_sun_only)
                        steps_dict[s_id].weather_rain_only = bool(node_obj.data.weather_rain_only)
                        steps_dict[s_id].required_flag = node_obj.data.required_flag or None
                        steps_dict[s_id].granted_flag = node_obj.data.granted_flag or None
                        if node_obj.data.hints:
                            try:
                                hints = json.loads(node_obj.data.hints) if isinstance(node_obj.data.hints, str) else node_obj.data.hints
                                steps_dict[s_id].hints = hints
                            except Exception: pass
                else:
                    try:
                        hints_arr = json.loads(node_obj.data.hints) if isinstance(node_obj.data.hints, str) else node_obj.data.hints
                    except Exception:
                        hints_arr = []
                    new_step = Step(
                        quest_id=quest_id,
                        latitude=step_lat,
                        longitude=step_lon,
                        instruction_text=node_obj.data.instruction_text or "Новый шаг из редактора",
                        welcome_message=node_obj.data.welcome_message,
                        history_info=node_obj.data.history_info,
                        photo_then_id=node_obj.data.photo_then_id,
                        photo_now_id=node_obj.data.photo_now_id,
                        audio_guide_id=node_obj.data.audio_guide_id,
                        npc_name=node_obj.data.npc_name,
                        hints=hints_arr,
                        radius_meters=int(node_obj.data.radius_meters) if node_obj.data.radius_meters else 30,
                        is_day_only=bool(node_obj.data.is_day_only),
                        is_night_only=bool(node_obj.data.is_night_only),
                        weather_sun_only=bool(node_obj.data.weather_sun_only),
                        weather_rain_only=bool(node_obj.data.weather_rain_only),
                        required_flag=node_obj.data.required_flag or None,
                        granted_flag=node_obj.data.granted_flag or None,
                        branches={"branches": {}}
                    )
                    session.add(new_step)
                    await session.flush()
                    steps_dict[new_step.id] = new_step
                    node_to_step_map[int(node_id)] = new_step.id
                    node_obj.data.step_id = new_step.id
                    
        for node_id, node_obj in nodes.items():
            if node_obj.name != "step_node": continue
            step_id = node_to_step_map.get(int(node_id))
            if not step_id: continue
            step = steps_dict[step_id]
            
            branches = {}
            dialogue_dict = {}
            gives_item = None
            coins = 0
            start_ans_key = node_obj.data.answer_key or ""
            
            paths = [{"curr_id": int(node_id), "ans_key": start_ans_key, "req_item": "", "min_k": 0, "min_level": 1, "req_class": "", "is_fail_branch": False}]
            visited = set()
            
            while paths:
                path = paths.pop(0)
                curr_str = str(path["curr_id"])
                if curr_str not in nodes: continue
                curr = nodes[curr_str]
                
                visit_key = f"{curr_str}_{path['ans_key']}_{path.get('is_fail_branch', False)}"
                if visit_key in visited: continue
                visited.add(visit_key)
                
                if curr.name == "npc_node":
                    node_key = "start" if "start" not in dialogue_dict else f"node_{curr_str}"
                    next_npc_id = "exit"
                    for out_port, out_data in curr.outputs.items():
                        for conn in out_data.connections:
                            next_node_obj = nodes.get(str(conn.node))
                            if next_node_obj:
                                if next_node_obj.name == "npc_node":
                                    next_npc_id = f"node_{conn.node}"
                                    break
                                elif next_node_obj.name == "step_node":
                                    target_s_id = node_to_step_map.get(int(conn.node))
                                    if target_s_id:
                                        next_npc_id = f"step_{target_s_id}"
                                        break
                                
                    dialogue_dict[node_key] = {
                        "text": curr.data.npc_text or "Привет!",
                        "options": [{
                            "text": curr.data.answer_key or "Далее",
                            "next_node": next_npc_id,
                            "karma_change": int(curr.data.karma_change or 0),
                            "item_give": curr.data.item_give or None,
                            "item_take": curr.data.item_take or None,
                            "xp_change": int(curr.data.xp_change or 0)
                        }]
                    }
                    path["ans_key"] = curr.data.answer_key or path["ans_key"]
                    if curr.data.npc_name: step.npc_name = curr.data.npc_name
                    
                elif curr.name == "reward_node":
                    if curr.data.gives_item: gives_item = curr.data.gives_item
                    if curr.data.coins: coins = int(curr.data.coins)
                    if curr.data.answer_key: path["ans_key"] = curr.data.answer_key
                    
                elif curr.name == "condition_node":
                    if curr.data.answer_key: path["ans_key"] = curr.data.answer_key
                    if curr.data.required_item: path["req_item"] = curr.data.required_item
                    if curr.data.min_karma: path["min_k"] = int(curr.data.min_karma)
                    if curr.data.min_level: path["min_level"] = int(curr.data.min_level)
                    if curr.data.required_class: path["req_class"] = curr.data.required_class
                    if getattr(curr.data, "target_lat", None) is not None: path["target_lat"] = curr.data.target_lat
                    if getattr(curr.data, "target_lng", None) is not None: path["target_lng"] = curr.data.target_lng
                    if getattr(curr.data, "target_radius", None) is not None: path["target_radius"] = curr.data.target_radius
                    if getattr(curr.data, "welcome_message", None): path["welcome_message"] = curr.data.welcome_message
                    
                elif curr.name == "final_node":
                    ans = path.get("fail_for_ans_key") or path["ans_key"] or "final"
                    if ans not in branches: branches[ans] = {}
                    elif not isinstance(branches[ans], dict): branches[ans] = {"target": branches[ans]}
                    if path.get("is_fail_branch"):
                        branches[ans]["fail_target"] = "final"
                    else:
                        branches[ans]["target"] = "final"
                        if path["req_item"]: branches[ans]["required_item"] = path["req_item"]
                        if path["min_k"]: branches[ans]["min_karma"] = path["min_k"]
                        if path.get("min_level", 1) > 1: branches[ans]["min_level"] = path["min_level"]
                        if path.get("req_class"): branches[ans]["required_class"] = path["req_class"]
                        if path.get("target_lat") is not None: branches[ans]["target_lat"] = path["target_lat"]
                        if path.get("target_lng") is not None: branches[ans]["target_lng"] = path["target_lng"]
                        if path.get("target_radius") is not None: branches[ans]["target_radius"] = path["target_radius"]
                        if path.get("welcome_message"): branches[ans]["welcome_message"] = path["welcome_message"]
                    continue 
                    
                elif curr.name == "step_node" and curr_str != str(node_id):
                    target_id = node_to_step_map.get(int(curr_str))
                    if target_id:
                        ans = path.get("fail_for_ans_key") or path["ans_key"] or str(target_id)
                        if ans not in branches: branches[ans] = {}
                        elif not isinstance(branches[ans], dict): branches[ans] = {"target": branches[ans]}
                        if path.get("is_fail_branch"):
                            branches[ans]["fail_target"] = target_id
                        else:
                            branches[ans]["target"] = target_id
                            if path["req_item"]: branches[ans]["required_item"] = path["req_item"]
                            if path["min_k"]: branches[ans]["min_karma"] = path["min_k"]
                            if path.get("min_level", 1) > 1: branches[ans]["min_level"] = path["min_level"]
                            if path.get("req_class"): branches[ans]["required_class"] = path["req_class"]
                            if path.get("target_lat") is not None: branches[ans]["target_lat"] = path["target_lat"]
                            if path.get("target_lng") is not None: branches[ans]["target_lng"] = path["target_lng"]
                            if path.get("target_radius") is not None: branches[ans]["target_radius"] = path["target_radius"]
                            if path.get("welcome_message"): branches[ans]["welcome_message"] = path["welcome_message"]
                    continue 
                    
                for out_port, out_data in curr.outputs.items():
                    is_fail_conn = (curr.name == "condition_node" and out_port == "output_2")
                    for conn in out_data.connections:
                        new_path = path.copy()
                        new_path["curr_id"] = int(conn.node)
                        if is_fail_conn:
                            new_path["is_fail_branch"] = True
                            new_path["fail_for_ans_key"] = path["ans_key"]
                        paths.append(new_path)
                        
            step.npc_dialogue = dialogue_dict if dialogue_dict else None
            step.gives_item = gives_item
            step.secret_price = coins
            step.branches = {"branches": branches, "_ui": {"pos_x": node_obj.pos_x, "pos_y": node_obj.pos_y}}
            session.add(step)
            
        entry_step_id = None
        start_canvas_id = None
        start_npc_key = None
        intro_subgraph = {}

        for n_id, n_obj in nodes.items():
            if n_obj.name == "start_node":
                start_canvas_id = str(n_id)
                break

        if start_canvas_id:
            start_port = nodes[start_canvas_id].outputs.get("output_1")
            start_outputs = start_port.connections if start_port else []
            first_target_id = str(start_outputs[0].node) if start_outputs else None

            if first_target_id and first_target_id in nodes:
                first_obj = nodes[first_target_id]

                if first_obj.name == "step_node":
                    entry_step_id = node_to_step_map.get(int(first_target_id))

                elif first_obj.name == "npc_node":
                    start_npc_key = "intro_start"
                    curr_id = first_target_id
                    visited_intro = set()

                    while curr_id and curr_id not in visited_intro:
                        visited_intro.add(curr_id)
                        curr_obj = nodes.get(curr_id)
                        if not curr_obj: break

                        if curr_obj.name == "step_node":
                            entry_step_id = node_to_step_map.get(int(curr_id))
                            break

                        if curr_obj.name == "npc_node":
                            n_key = "intro_start" if curr_id == first_target_id else f"intro_node_{curr_id}"
                            raw_opts = getattr(curr_obj.data, 'options', None)
                            if isinstance(raw_opts, str):
                                try: raw_opts = json.loads(raw_opts)
                                except: raw_opts = []
                            elif hasattr(raw_opts, "model_dump"):
                                raw_opts = raw_opts.model_dump()
                            if not isinstance(raw_opts, list) or not raw_opts:
                                raw_opts = [{"text": "Далее"}]

                            opts_formatted = []
                            next_curr = None

                            for out_port, out_data in curr_obj.outputs.items():
                                try: p_idx = int(out_port.split('_')[1]) - 1
                                except: p_idx = 0

                                opt_meta = raw_opts[p_idx] if p_idx < len(raw_opts) else {}
                                next_node_key = "exit"

                                for conn in out_data.connections:
                                    conn_id_str = str(conn.node)
                                    conn_obj = nodes.get(conn_id_str)
                                    if conn_obj:
                                        if conn_obj.name == "npc_node":
                                            next_node_key = f"intro_node_{conn_id_str}"
                                            if next_curr is None: next_curr = conn_id_str
                                        elif conn_obj.name == "step_node":
                                            entry_step_id = node_to_step_map.get(int(conn_id_str))
                                            next_node_key = "exit"
                                            if next_curr is None: next_curr = conn_id_str

                                opts_formatted.append({
                                    "text": opt_meta.get("text") or "Далее",
                                    "next_node": next_node_key,
                                    "karma_change": int(opt_meta.get("karma_change") or 0),
                                    "coins_change": int(opt_meta.get("coins_change") or 0),
                                    "xp_change": int(opt_meta.get("xp_change") or 0),
                                    "item_give": opt_meta.get("item_give") or None,
                                    "item_take": opt_meta.get("item_take") or None
                                })

                            intro_subgraph[n_key] = {
                                "text": curr_obj.data.npc_text or "Приветствую!",
                                "options": opts_formatted
                            }
                            curr_id = next_curr

        if entry_step_id and intro_subgraph:
            target_step_obj = steps_dict.get(entry_step_id)
            if target_step_obj:
                existing_dial = normalize_json_field(target_step_obj.npc_dialogue) or {}
                merged_dial = {**existing_dial, **intro_subgraph}
                target_step_obj.npc_dialogue = merged_dial
                session.add(target_step_obj)

        if entry_step_id:
            quest.drawflow_data = {
                "drawflow": {"Home": {"data": {k: v.model_dump(by_alias=True) for k, v in nodes.items()}}},
                "entry_point_step_id": entry_step_id,
                "welcome_npc_key": start_npc_key
            }
            session.add(quest)

        await session.commit()
        return {"status": "success", "message": "Топология графа успешно обновлена."}

@app.get("/api/admin/radar")
async def get_live_radar():
    async with db.session_pool() as session:
        time_threshold = get_utc_now() - datetime.timedelta(minutes=60)
        stmt = select(PlayerLocationLog, User.full_name, User.rpg_class).join(
            User, PlayerLocationLog.user_id == User.telegram_id
        ).where(PlayerLocationLog.timestamp >= time_threshold).order_by(desc(PlayerLocationLog.timestamp))
        results = await session.execute(stmt)
        players = {}
        for log, full_name, rpg_class in results.all():
            if log.user_id not in players:
                players[log.user_id] = {
                    "user_id": log.user_id,
                    "name": full_name,
                    "class": rpg_class or "Без класса",
                    "lat": log.latitude,
                    "lng": log.longitude,
                    "quest_id": log.quest_id,
                    "time": log.timestamp.isoformat()
                }
        return list(players.values())

@app.get("/api/admin/heatmap/{quest_id}")
async def get_heatmap(quest_id: int):
    async with db.session_pool() as session:
        stmt = select(PlayerLocationLog.latitude, PlayerLocationLog.longitude).where(
            PlayerLocationLog.quest_id == quest_id
        )
        results = await session.execute(stmt)
        return [{"lat": r[0], "lng": r[1]} for r in results.all()]

@app.get("/api/admin/dicts")
async def get_admin_dicts():
    async with db.session_pool() as session:
        q_stmt = select(Quest.id, Quest.title).order_by(Quest.id)
        quests = [{"id": r[0], "title": f"[{r[0]}] {r[1]}"} for r in (await session.execute(q_stmt)).all()]
        s_stmt = select(Step.id, Step.instruction_text, Quest.title).join(Quest, Step.quest_id == Quest.id).order_by(Step.id)
        steps = [{"id": r[0], "text": f"[{r[2][:20]}] {r[1][:35]}..."} for r in (await session.execute(s_stmt)).all()]
        return {"quests": quests, "steps": steps}

@app.get("/api/admin/quests-map")
async def get_quests_for_map():
    async with db.session_pool() as session:
        stmt = select(
            Quest.id, Quest.title, Quest.description, Quest.is_published, 
            Quest.min_level_required, Quest.max_speed_kmh, Quest.is_coop, 
            Quest.coop_max_size, Quest.global_time_limit_seconds,
            Quest.stamina_cost_override, Quest.cooldown_override_hours, Quest.is_free # <-- ДОБАВЛЕНО
        ).order_by(Quest.id)
        quests = await session.execute(stmt)
        result = []
        for row in quests.all():
            q_id, title, descr, is_pub, min_lvl, max_spd, is_coop, coop_sz, g_lim, st_ov, cd_ov, is_fr = row
            step_stmt = select(Step.latitude, Step.longitude).where(Step.quest_id == q_id).order_by(Step.id).limit(1)
            step_row = (await session.execute(step_stmt)).first()
            if step_row:
                cnt_stmt = select(func.count()).select_from(ActiveQuest).where(ActiveQuest.quest_id == q_id)
                p_inside = (await session.execute(cnt_stmt)).scalar() or 0
                result.append({
                    "id": q_id, "title": title, "description": descr,
                    "is_published": is_pub, "min_level_required": min_lvl,
                    "max_speed_kmh": max_spd, "is_coop": is_coop,
                    "coop_max_size": coop_sz, "global_time_limit_seconds": g_lim,
                    "lat": step_row[0], "lng": step_row[1], "type": "quest",
                    "people_inside": p_inside,
                    "stamina_cost_override": st_ov, "cooldown_override_hours": cd_ov, "is_free": bool(is_fr)
                })
        return result

@app.get("/api/admin/cities")
async def get_cities_list():
    async with db.session_pool() as session:
        stmt = select(City.id, City.name, City.latitude, City.longitude, City.radius_km, City.is_active).order_by(City.id)
        cities = await session.execute(stmt)
        result = []
        for city_id, name, lat, lng, radius, is_active in cities.all():
            result.append({
                "id": city_id, "name": name, "lat": lat, "lng": lng,
                "radius_km": radius, "is_active": is_active, "type": "city"
            })
        return result

@app.post("/api/admin/markets")
async def create_admin_market(req: CreateMarketSchema):
    async with db.session_pool() as session:
        existing = await session.execute(select(QuestMarket).where(QuestMarket.name == req.name))
        if existing.first():
            raise HTTPException(status_code=400, detail=f"Магазин '{req.name}' уже существует.")
        new_market = await db.create_market(name=req.name, lat=req.latitude, lon=req.longitude, radius=req.radius)
        
        # Если при создании лавки сразу отметили товары — вшиваем её ID в их массивы
        if req.item_ids:
            all_items = (await session.execute(select(ShopItem))).scalars().all()
            target_set = set(req.item_ids)
            for it in all_items:
                if it.id in target_set:
                    m_list = list(getattr(it, 'market_ids', []) or [])
                    if new_market.id not in m_list: m_list.append(new_market.id)
                    it.market_ids = m_list
                    session.add(it)
            await session.commit()
            
        return {"status": "success", "id": new_market.id, "message": f"✅ Магазин '{req.name}' создан!"}

@app.get("/api/admin/markets")
async def get_admin_markets_list():
    """Возвращает список торговых лавок для выпадающих меню админки."""
    markets = await db.get_all_markets()
    return [{
        "id": m.id, "name": m.name, "latitude": m.latitude, 
        "longitude": m.longitude, "radius": m.radius
    } for m in markets]

@app.get("/api/admin/npcs/map")
async def get_npcs_for_map():
    npcs = await db.get_all_npcs()
    result = []
    for n in npcs:
        if not n.latitude or not n.longitude: continue
        keys = await redis_client.keys(f"presence:npc:{n.id}:*")
        result.append({
            "id": n.id, "name": n.name, "description": n.description, 
            "lat": n.latitude, "lng": n.longitude, "radius": n.radius,
            "has_dialogue": bool(n.dialogue_tree),
            "people_inside": len(keys),
            "stamina_cost_override": getattr(n, 'stamina_cost_override', None),
            "cooldown_override_hours": getattr(n, 'cooldown_override_hours', None),
            "is_free": getattr(n, 'is_free', False)
        })
    return result

@app.post("/api/admin/npcs")
async def create_admin_npc(req: CreateNPCSchema):
    async with db.session_pool() as session:
        existing = await session.execute(select(NPCCharacter).where(NPCCharacter.name == req.name))
        if existing.first(): raise HTTPException(status_code=400, detail=f"NPC '{req.name}' уже существует.")
        new_npc = await db.create_npc_character(req.name, req.description, req.latitude, req.longitude, req.radius)
        return {"status": "success", "id": new_npc.id, "message": f"NPC '{req.name}' успешно размещен!"}

@app.delete("/api/admin/npcs/{npc_id}")
async def delete_admin_npc(npc_id: int):
    success = await db.delete_npc(npc_id)
    if not success: raise HTTPException(status_code=404, detail="NPC не найден")
    return {"status": "success"}

@app.post("/api/presence/heartbeat")
async def market_presence_heartbeat(req: PresenceHeartbeatSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    # Железно пишем присутствие игрока в лавке на 60 секунд:
    await redis_client.set(f"presence:market:{req.market_id}:{user_id}", "1", ex=60)
    return {"status": "ok"}

@app.get("/api/admin/markets/map")
async def get_markets_for_map():
    async with db.session_pool() as session:
        stmt = select(QuestMarket.id, QuestMarket.name, QuestMarket.latitude, QuestMarket.longitude, QuestMarket.radius).order_by(QuestMarket.id)
        res = await session.execute(stmt)
        markets = []
        for r in res.all():
            # Считаем живые ключи присутствия в этой лавке:
            keys = await redis_client.keys(f"presence:market:{r[0]}:*")
            markets.append({
                "id": r[0], "name": r[1], "lat": r[2], "lng": r[3], 
                "radius": r[4], "people_inside": len(keys)
            })
        return markets

@app.delete("/api/admin/markets/{market_id}")
async def delete_admin_market(market_id: int):
    success = await db.delete_market(market_id)
    if not success: raise HTTPException(status_code=404, detail="Магазин не найден")
    return {"status": "success"}

@app.post("/api/admin/quests")
async def create_quest(req: CreateQuestSchema):
    async with db.session_pool() as session:
        existing = await session.execute(select(Quest).where(Quest.title == req.title))
        if existing.first(): raise HTTPException(status_code=400, detail=f"Квест '{req.title}' уже существует.")
        
        new_quest = Quest(
            title=req.title, description=req.description, is_published=req.is_published,
            max_speed_kmh=req.max_speed_kmh, min_level_required=req.min_level_required,
            is_coop=req.is_coop, coop_max_size=req.coop_max_size,
            global_time_limit_seconds=req.global_time_limit_seconds,
            stamina_cost_override=req.stamina_cost_override, cooldown_override_hours=req.cooldown_override_hours, is_free=req.is_free,
            created_at=get_utc_now()
        )
        session.add(new_quest)
        await session.flush()
        
        first_step = Step(
            quest_id=new_quest.id, instruction_text=f"Начало квеста: {req.title}",
            latitude=req.latitude, longitude=req.longitude, radius_meters=30,
            min_karma_required=0, is_final=False, branches={"branches": {}}
        )
        session.add(first_step)
        await session.commit()
        return {"status": "success", "id": new_quest.id, "title": new_quest.title, "message": f"✅ Квест '{req.title}' создан! ID: {new_quest.id}"}

@app.delete("/api/admin/quests/{quest_id}")
async def delete_admin_quest(quest_id: int):
    await db.delete_quest(quest_id)
    return {"status": "success"}

@app.post("/api/admin/quests/{quest_id}/clone")
async def clone_admin_quest(quest_id: int):
    cloned_quest = await db.clone_quest_db(quest_id)
    if not cloned_quest:
        raise HTTPException(status_code=404, detail="Исходный квест не найден")
    return {"status": "success", "id": cloned_quest.id, "message": f"✅ Квест успешно склонирован (ID: {cloned_quest.id})"}

@app.post("/api/admin/cities")
async def create_city(req: CreateCitySchema):
    async with db.session_pool() as session:
        slug = generate_slug(req.name)
        existing = await session.execute(select(City).where(City.slug == slug))
        if existing.first(): raise HTTPException(status_code=400, detail=f"Город '{req.name}' уже существует.")
        
        new_city = City(
            name=req.name, slug=slug, latitude=req.latitude, longitude=req.longitude,
            radius_km=req.radius_km, is_active=req.is_active, timezone="Asia/Yekaterinburg", created_at=get_utc_now()
        )
        session.add(new_city)
        await session.commit()
        return {"status": "success", "id": new_city.id, "name": new_city.name, "message": f"✅ Город '{req.name}' создан! ID: {new_city.id}"}

@app.put("/api/admin/quests/{quest_id}")
async def update_quest_meta(quest_id: int, req: UpdateQuestSchema):
    await db.update_quest(quest_id, **req.model_dump(exclude_unset=True))
    return {"status": "success"}

@app.put("/api/admin/cities/{city_id}")
async def update_city_meta(city_id: int, req: UpdateCitySchema):
    await db.update_city(city_id, **req.model_dump(exclude_unset=True))
    return {"status": "success"}

@app.put("/api/admin/markets/{market_id}")
async def update_market_meta_route(market_id: int, req: UpdateMarketSchema):
    # 1. Обновляем координаты и имя лавки
    meta_dict = req.model_dump(exclude={'item_ids'}, exclude_unset=True)
    if meta_dict:
        await db.update_market_meta(market_id, **meta_dict)
        
    # 2. Если с фронта пришел массив item_ids — делаем двустороннюю синхронизацию!
    if req.item_ids is not None:
        async with db.session_pool() as session:
            all_items = (await session.execute(select(ShopItem))).scalars().all()
            target_set = set(req.item_ids)
            for it in all_items:
                m_list = list(getattr(it, 'market_ids', []) or [])
                if it.id in target_set:
                    if market_id not in m_list: m_list.append(market_id)
                else:
                    if market_id in m_list: m_list.remove(market_id)
                it.market_ids = m_list
                session.add(it)
            await session.commit()

    return {"status": "success"}

@app.put("/api/admin/npcs/{npc_id}")
async def update_npc_meta_route(npc_id: int, req: UpdateNPCSchema):
    await db.update_npc_meta(npc_id, **req.model_dump(exclude_unset=True))
    return {"status": "success"}

@app.post("/api/admin/maintenance/toggle")
async def toggle_maintenance():
    is_active = await redis_client.get("system:maintenance")
    if is_active:
        await redis_client.delete("system:maintenance")
        return {"status": "success", "maintenance": False}
    else:
        await redis_client.set("system:maintenance", "1")
        return {"status": "success", "maintenance": True}

@app.post("/api/admin/force-reseed")
async def force_reseed_route():
    """ЯДЕРНЫЙ СБРОС: Жестко вычищает таблицы Постгреса до дна через TRUNCATE CASCADE."""
    await db.wipe_and_reseed_all_db()
    await redis_client.flushdb()
    return {"status": "success", "message": "💥 ЯДЕРНЫЙ СБРОС УСПЕШЕН! База очищена до дна и засеяна заново."}

@app.post("/api/admin/evacuate/{entity_type}/{entity_id}")
async def evacuate_entity_location(entity_type: str, entity_id: int, request: Request):
    if entity_type not in ["quest", "market", "npc"]:
        raise HTTPException(status_code=400, detail="Невалидный тип локации")
        
    kicked_users = await db.evacuate_location_players(entity_type, entity_id)
    
    # ВЕШАЕМ МЕТКУ, НО БОТ БОЛЬШЕ НИЧЕГО НЕ ПИШЕТ В ТЕЛЕГРАМ!
    for uid in kicked_users:
        await redis_client.set(f"evacuated:{uid}", "1", ex=60)
        
    return {
        "status": "success", 
        "total_kicked": len(kicked_users), 
        "pushes_delivered": 0,
        "message": f"💥 Локация перезагружена. Эвакуировано игроков: {len(kicked_users)}"
    }

class AdminLevelConfigSchema(BaseModel):
    level: int
    xp_to_next: int
    reward_coins: int = 0
    reward_item_name: Optional[str] = None
    stamina_bonus: int = 0

@app.get("/api/admin/levels")
async def get_admin_levels_route():
    cfgs = await db.get_all_level_configs()
    return [{
        "level": c.level, "xp_to_next": c.xp_to_next, "reward_coins": c.reward_coins,
        "reward_item_name": c.reward_item_name, "stamina_bonus": c.stamina_bonus
    } for c in cfgs]

@app.post("/api/admin/levels")
async def save_admin_level_route(req: AdminLevelConfigSchema):
    await db.upsert_level_config(**req.model_dump())
    return {"status": "success"}

@app.delete("/api/admin/levels/{level}")
async def delete_admin_level_route(level: int):
    await db.delete_level_config(level)
    return {"status": "success"}

@app.get("/api/admin/players/{user_id}/analytics")
async def get_player_bi_analytics_route(user_id: int):
    """Отдает Chart.js телеметрию конкретного игрока для CRM шторки"""
    return await db.get_player_bi_analytics(user_id)

class MoveEntitySchema(BaseModel):
    latitude: float
    longitude: float

@app.post("/api/admin/move/{entity_type}/{entity_id}")
async def move_entity_route(entity_type: str, entity_id: int, payload: MoveEntitySchema):
    """Единый пульт визуального маппинга сущностей (Телепорт)."""
    if entity_type not in ["quest", "market", "npc"]:
        raise HTTPException(status_code=400, detail="Неверный тип объекта")
    success, msg = await db.move_entity_db(entity_type, entity_id, payload.latitude, payload.longitude)
    if not success:
        raise HTTPException(status_code=404, detail=msg)
    return {"status": "success", "message": msg}

# =====================================================================
# ИГРОВОЙ ДВИЖОК: ПРОФИЛЬ, КВЕСТЫ, ЭКОНОМИКА, ЛОКАЦИИ И МАГАЗИНЫ
# =====================================================================

@app.get("/api/cities")
async def get_client_cities():
    async with db.session_pool() as session:
        stmt = select(City.id, City.name, City.latitude, City.longitude, City.radius_km).where(City.is_active == True)
        cities = await session.execute(stmt)
        return [{"id": r[0], "name": r[1], "lat": r[2], "lng": r[3], "radius_km": r[4]} for r in cities.all()]

@app.post("/api/profile/city")
async def set_profile_city(req: CitySelectionSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    async with db.session_pool() as session:
        user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
        if not user: raise HTTPException(status_code=404, detail="Пользователь не найден")
        user.city_id = req.city_id
        user.auto_city_detect = req.auto_detect
        await session.commit()
    return {"status": "success", "city_id": req.city_id, "auto_detect": req.auto_detect}

@app.post("/api/location/auto-city")
async def auto_update_city(req: LocationAutoCitySchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    async with db.session_pool() as session:
        user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
        if not user or not user.auto_city_detect: return {"status": "skipped", "message": "Автоопределение выключено или профиль не найден"}
        
        cities = (await session.execute(select(City).where(City.is_active == True))).scalars().all()
        for city in cities:
            dist = calculate_haversine_distance(req.latitude, req.longitude, city.latitude, city.longitude)
            if dist <= city.radius_km * 1000:
                if user.city_id != city.id:
                    user.city_id = city.id
                    await session.commit()
                    return {"status": "updated", "city_id": city.id, "city_name": city.name}
                return {"status": "unchanged", "city_id": city.id}
    return {"status": "no_city_found"}




@app.get("/api/profile")
async def get_profile(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    if not user:
        user = await db.get_or_create_user(telegram_id=user_id, full_name=tg_user.get("first_name", "Игрок"), username=tg_user.get("username"))
        
    class_map = {"merchant": "Купец", "ranger": "Следопыт", "historian": "Историк"}
    rpg_class_ru = class_map.get(user.rpg_class, "Не выбран") if user.rpg_class else "Не выбран"
        
    async with db.session_pool() as s_read:
        cfg_res = await s_read.execute(select(LevelConfig.xp_to_next).where(LevelConfig.level == user.level))
        xp_cfg_val = cfg_res.scalar_one_or_none()
        
    xp_needed = xp_cfg_val if xp_cfg_val is not None else user.level * 150
    completed_quests = await db.get_user_completed_quests_count(user_id)
    total_score = await db.get_user_total_score(user_id)
    achievements_dto = await db.get_user_achievements(user_id)
    daily_riddle = await db.get_random_daily_riddle()
    
    # Проверяем в Redis, решена ли загадка сегодня
    riddle_solved = bool(await redis_client.get(f"riddle_solved:{user_id}"))
    is_maint = bool(await redis_client.get("system:maintenance")) # <-- ЧИТАЕМ ТУМБЛЕР
    
    return {
        "under_maintenance": is_maint, # <-- ТЕЛЕФОН УВИДИТ ЭТО И ЗАКРОЕТ ИНТЕРФЕЙС
        "is_anonymous": getattr(user, 'is_anonymous', False),
        "is_tester": getattr(user, 'is_tester', False),
        "telegram_id": user.telegram_id, "full_name": user.full_name,
        "coins": user.coins, "karma": user.karma, "score": total_score,
        "rpg_class": rpg_class_ru, "level": user.level, "xp": user.xp, "xp_needed": xp_needed,
        "max_weight_capacity": user.max_weight_capacity, "daily_streak": user.daily_streak,
        
        # --- ДОБАВЛЕНО: Телеметрия бодрости для верхнего HUD (Спринт 2) ---
        "stamina": user.stamina if getattr(user, 'stamina', None) is not None else (getattr(user, 'max_stamina', 100) or 100),
        "max_stamina": getattr(user, 'max_stamina', 100) or 100,
        # ----------------------------------------------------------------

        "city_id": getattr(user, 'city_id', None), "auto_detect_city": user.auto_city_detect,
        "global_flags": getattr(user, 'global_flags', []) or [],
        "completed_quests_count": completed_quests,
        "achievements": achievements_dto,
        "riddle_solved_today": riddle_solved,  # <-- ПЕРЕДАЕМ СТАТУС РЕШЕНИЯ НА ФРОНТ
        "daily_riddle": {
            "id": daily_riddle.id, "question": daily_riddle.question, "reward_coins": daily_riddle.reward_coins,
        } if daily_riddle else None
    }

@app.post("/api/profile/claim-income")
async def claim_income(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    collected = await db.collect_passive_income_buffer(user_id)
    return {"status": "success", "collected_coins": collected}


@app.post("/api/profile/change-class")
async def change_rpg_class(req: ClassChangeSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    success, days_left = await db.update_user_class_with_cooldown(user_id, req.rpg_class, 30)
    if not success: raise HTTPException(status_code=400, detail=f"Класс можно сменить через {days_left} дн.")
    return {"status": "success"}

@app.post("/api/profile/notify-coop")
async def request_coop_notification(tg_user: dict = Depends(get_current_user)):
    """Запись игрока в базу ожидания релиза кооператива"""
    user_id = tg_user.get("id")
    await db.grant_global_flag(user_id, "wants_coop_notification")
    return {"status": "success"}

@app.post("/api/riddle/solve")
async def solve_daily_riddle(req: RiddleSolveSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    
    redis_key = f"riddle_solved:{user_id}"
    if await redis_client.get(redis_key):
        return {"status": "already_solved", "message": "⏳ Вы уже разгадали сегодняшнюю загадку! Новая появится через 24 часа."}
        
    riddle = await db.get_daily_riddle_by_id(req.riddle_id)
    if not riddle: 
        raise HTTPException(status_code=404, detail="Загадка не найдена.")
        
    if req.answer.strip().lower() == riddle.correct_answer.strip().lower():
        await db.add_coins(user_id, riddle.reward_coins)
        await redis_client.set(redis_key, "1", ex=86400)  # Блокировка ровно на 24 часа (86400 сек)
        return {
            "status": "success", 
            "reward": riddle.reward_coins,
            "message": f"✨ Абсолютно верно! Начислено: +{riddle.reward_coins} монет."
        }
        
    return {"status": "wrong", "message": "❌ Неверный ответ. Попробуйте еще раз!"}

@app.get("/api/quests")
async def list_quests(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    quests = await db.get_published_quests()
    
    result = []
    for q in quests:
        is_locked = user.level < q.min_level_required if user else True
        result.append({
            "id": q.id, "title": q.title, "description": q.description,
            "min_level_required": q.min_level_required, "max_speed_kmh": q.max_speed_kmh,
            "is_locked": is_locked, "is_coop": getattr(q, 'is_coop', False), "global_time_limit": getattr(q, 'global_time_limit_seconds', None)
        })
    return result

@app.get("/api/map/points")
async def get_map_points(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    user_level = user.level if user else 1
    return await db.get_all_map_points(user_level)

@app.get("/api/quest/active")
async def get_active_quest_state(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    
    # Мягкий конвой: сообщаем клиенту статус завершения без удаления ключа
    if await redis_client.get(f"evacuated:{user_id}"):
        return {
            "active": False,
            "status": "finished",
            "evacuated": True,
            "message": "⚠️ Локация была перезагружена Гейм-мастером. Вы были безопасно эвакуированы."
        }

    active = await db.get_active_quest(user_id)
    if not active: return {"active": False}

    step = await db.get_step_by_id(active.current_step_id)
    if not step: return {"active": False}

    npc_dial = normalize_npc_dialogue(step.npc_dialogue)
    hints = normalize_json_field(step.hints) or []
    formatted_hints = []
    if isinstance(hints, list) and len(hints) > 0:
        for h in hints:
            formatted_hints.append({"text": h.get("text", ""), "price": h.get("price", 0), "delay": h.get("delay", 0)})
    else:
        formatted_hints = [
            {"text": getattr(step, "hint_1_text", ""), "price": 20, "delay": getattr(step, "hint_1_delay", 5) * 60}, 
            {"text": getattr(step, "hint_2_text", ""), "price": 0, "delay": getattr(step, "hint_2_delay", 10) * 60}
        ]

    now = get_utc_now()
    last_action = active.step_activated_at if active.step_activated_at else now
    time_passed_seconds = (now - last_action).total_seconds()

    gps_verified = False
    if active.prev_time and active.step_activated_at:
        if active.prev_time > active.step_activated_at: gps_verified = True

    current_npc_node = getattr(active, 'current_npc_node', None)
    if current_npc_node and "|DEST:" in current_npc_node:
        current_npc_node = current_npc_node.split("|DEST:")[0]

    return {
        "active": True, "quest_id": active.quest_id, "score": active.score, "errors_count": active.errors_count,
        "is_frozen": getattr(active, 'is_frozen', False), "current_npc_node": current_npc_node,
        "gps_verified": gps_verified, "time_passed_seconds": time_passed_seconds,
        "pending_coins": getattr(active, 'pending_coins', 0), "pending_xp": getattr(active, 'pending_xp', 0), "pending_karma": getattr(active, 'pending_karma', 0),
        "step": {
            "id": step.id, "instruction_text": step.instruction_text, "history_info": step.history_info,
            "photo_then_id": step.photo_then_id, "photo_now_id": step.photo_now_id, "audio_guide_id": step.audio_guide_id,
            "latitude": step.latitude, "longitude": step.longitude, "radius_meters": getattr(step, "radius_meters", 30),
            "min_karma_required": step.min_karma_required, "required_item": step.required_item, "gives_item": step.gives_item,
            "secret_price": getattr(step, "secret_price", 0), "npc_name": step.npc_name, "npc_dialogue": npc_dial,
            "hints": formatted_hints, "is_final": step.is_final,
            "welcome_message": getattr(step, "welcome_message", None) or ""
        }
    }

@app.post("/api/quest/start/{quest_id}")
async def start_quest(quest_id: int, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    quest = await db.get_quest_with_steps(quest_id)
    if not quest or not quest.steps: 
        raise HTTPException(status_code=400, detail="Квест не содержит шагов или не существует.")
        
    cfg = await db.get_system_settings()

    # 1. Каскад кулдауна (Сон квеста)
    cd_hours = quest.cooldown_override_hours if getattr(quest, 'cooldown_override_hours', None) is not None else cfg.default_quest_cooldown_hours
    if cd_hours > 0:
        last_done = await db.get_last_quest_completion_time(user_id, quest_id)
        if last_done:
            cooldown_sec = int(cd_hours * 3600)
            elapsed = (get_utc_now() - last_done).total_seconds()
            if elapsed < cooldown_sec:
                perm_avail = (last_done + datetime.timedelta(seconds=cooldown_sec)) + datetime.timedelta(hours=5)
                raise HTTPException(status_code=400, detail=f"⏳ Экспедиция отдыхает! Повторный запуск доступен в {perm_avail.strftime('%H:%M')}.")

    # 2. Каскад цены бодрости
    start_cost = 0 if getattr(quest, 'is_free', False) else (quest.stamina_cost_override if getattr(quest, 'stamina_cost_override', None) is not None else cfg.default_quest_start_cost)
    if start_cost > 0:
        has_stamina, curr_st = await db.spend_user_stamina(user_id, cost=start_cost)
        if not has_stamina:
            user = await db.get_user(user_id)
            return {"status": "no_stamina", "stamina": curr_st, "max_stamina": getattr(user, 'max_stamina', 100) or 100}

    if not quest or not quest.steps: 
        raise HTTPException(status_code=400, detail="Квест не содержит шагов или не существует.")
    
    start_step_id = None
    welcome_npc_key = None
    
    if quest.drawflow_data and isinstance(quest.drawflow_data, dict):
        start_step_id = quest.drawflow_data.get("entry_point_step_id")
        welcome_npc_key = quest.drawflow_data.get("welcome_npc_key")

    if start_step_id:
        if not any(s.id == start_step_id for s in quest.steps):
            start_step_id = None

    if not start_step_id:
        start_step_id = quest.steps[0].id

    # 1. Создаем физическую сессию на целевом шаге
    await db.start_user_quest(user_id, quest_id, start_step_id)
    
    # 2. Если у квеста есть Welcome-NPC — немедленно взводим его триггер!
    if welcome_npc_key:
        await db.update_active_quest_npc_node(user_id, welcome_npc_key)

    return {"status": "success", "first_step_id": start_step_id}

@app.post("/api/quest/exit")
async def exit_quest(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active: raise HTTPException(status_code=400, detail="У вас нет активного квеста.")
        
    lost = await db.delete_active_quest(user_id)
    msg_parts = []
    if lost.get("coins"): msg_parts.append(f"{lost['coins']} монет")
    if lost.get("karma"): msg_parts.append(f"☯️ {lost['karma']} кармы")
    if lost.get("xp"): msg_parts.append(f"🌟 {lost['xp']} XP")
    if lost.get("items") and len(lost["items"]) > 0: msg_parts.append(f"📦 Предметы: {', '.join(lost['items'])}")
    lost_str = "\n".join(msg_parts) if msg_parts else "Вы ничего не успели заработать."
    return {"status": "success", "message": f"🛑 Квест прерван!\n\nУсловно заработанные в этой сессии ресурсы сгорели:\n{lost_str}"}

@app.post("/api/quest/verify-location")
async def verify_location(loc: LocationCheckSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active: raise HTTPException(status_code=400, detail="У вас нет запущенного квеста.")
        
    step = await db.get_step_by_id(active.current_step_id)
    quest = await db.get_quest_by_id(active.quest_id)
    
    async with db.session_pool() as session:
        log_entry = PlayerLocationLog(user_id=user_id, quest_id=active.quest_id, latitude=loc.latitude, longitude=loc.longitude)
        session.add(log_entry)
        await session.commit()

    now_hour = get_utc_now().hour
    if getattr(step, 'is_night_only', False) and (6 <= now_hour <= 20): return {"status": "condition_failed", "message": "Эту локацию можно посетить только ночью!"}
    if getattr(step, 'is_day_only', False) and (not (6 <= now_hour <= 20)): return {"status": "condition_failed", "message": "Эту локацию можно посетить только днем!"}

    target_radius = getattr(step, "radius_meters", 30) or 30
    distance = calculate_haversine_distance(loc.latitude, loc.longitude, step.latitude, step.longitude)
    
    if distance > target_radius:
        return {"status": "too_far", "distance": int(distance), "message": f"Вы еще слишком далеко. До точки: {int(distance)} метров. (Необходимый радиус: {target_radius}м)"}
        
    if active.prev_latitude is not None and active.prev_longitude is not None and active.prev_time is not None:
        now = get_utc_now()
        time_diff = (now - active.prev_time).total_seconds()
        if time_diff > 1.0:
            dist_prev = calculate_haversine_distance(active.prev_latitude, active.prev_longitude, loc.latitude, loc.longitude)
            speed_mps = dist_prev / time_diff
            speed_kmh = speed_mps * 3.6
            user_meta = await db.get_user(user_id)
            if speed_kmh > quest.max_speed_kmh and not getattr(user_meta, 'is_tester', False):
                await db.add_cheat_log(user_id, quest.id, speed_mps, loc.latitude, loc.longitude)
                warnings = await db.increment_cheat_warning(user_id)
                if warnings >= 2:
                    await db.set_ban_status(user_id, True)
                    return {"status": "banned", "message": "Вы забанены античитом за использование Fake GPS!"}
                return {"status": "speed_warning", "message": f"Внимание! Превышена скорость движения: {int(speed_kmh)} км/ч!"}

    # --- ДОБАВЛЕНО: Списание -15 бодрости за взятие чекпоинта ---
    has_stamina, curr_st = await db.spend_user_stamina(user_id, cost=15)
    if not has_stamina:
        return {
            "status": "no_stamina",
            "stamina": curr_st,
            "max_stamina": 100,
            "message": f"⚡️ Вы сильно выдохлись ({curr_st}/100)! Переведите дух перед взятием контрольной точки или загляните в Магазин за кофе."
        }
    # ---------------------------------------------------------

    await db.set_gps_verified_now(user_id, loc.latitude, loc.longitude)

    npc_started = False

    random_event_data = None
    if random.random() < 0.25:
        async with db.session_pool() as session:
            events = (await session.execute(select(RandomEvent))).scalars().all()
            if events:
                chosen = random.choice(events)
                if random.random() * 100 <= chosen.probability:
                    random_event_data = {
                        "id": chosen.id,
                        "type": chosen.event_type,
                        "text": chosen.text,
                        "coins": chosen.coins_impact,
                        "karma": chosen.karma_impact,
                        "xp": chosen.xp_reward
                    }

    return {
        "status": "success", 
        "distance": int(distance), 
        "npc_started": npc_started,
        "random_event": random_event_data
    }

@app.post("/api/quest/random-event/{event_id}/choice")
async def process_random_event(event_id: int, req: RandomEventChoiceSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    async with db.session_pool() as session:
        chosen = (await session.execute(select(RandomEvent).where(RandomEvent.id == event_id))).scalar_one_or_none()
    
    if not chosen: raise HTTPException(status_code=404, detail="Событие не найдено.")

    outcome_text = ""
    choice = req.choice

    if chosen.event_type == "merc":
        if choice == "yes":
            overloaded, _, _ = await db.is_inventory_overloaded(user_id, 1)
            if overloaded: return {"status": "error", "message": "🎒 Рюкзак перегружен! Вы не можете взять наёмника."}
            await db.add_item_to_inventory(user_id, "🧙‍♂️ Наемник")
            await db.add_xp(user_id, chosen.xp_reward)
            outcome_text = f"🧙‍♂️ Контракт подписан!\nНаёмник добавлен в рюкзак. Получено: +{chosen.xp_reward} XP."
        else: outcome_text = "👋 Вы вежливо попрощались с наёмником."
            
    elif chosen.event_type == "scroll":
        if choice == "yes":
            if await db.deduct_coins(user_id, abs(chosen.coins_impact)):
                await db.update_karma(user_id, chosen.karma_impact)
                await db.add_xp(user_id, chosen.xp_reward)
                outcome_text = f"📜 Свиток расшифрован!\nПолучено: +{chosen.karma_impact} Кармы, +{chosen.xp_reward} XP."
            else: return {"status": "error", "message": "❌ Недостаточно монет для расшифровки."}
        else: outcome_text = "📜 Вы прошли мимо свитка."
            
    elif chosen.event_type == "wallet":
        if choice == "take":
            await db.add_coins(user_id, chosen.coins_impact)
            await db.update_karma(user_id, chosen.karma_impact)
            await db.add_xp(user_id, chosen.xp_reward)
            outcome_text = f"💰 Вы присвоили золото!\n+{chosen.coins_impact} монет, {chosen.karma_impact} Кармы. +{chosen.xp_reward} XP."
        else:
            await db.update_karma(user_id, 2)
            await db.add_xp(user_id, chosen.xp_reward)
            outcome_text = f"🙌 Благородный поступок!\nВаша репутация растет: +2 Кармы, +{chosen.xp_reward} XP."

    return {"status": "success", "message": outcome_text}

@app.post("/api/quest/ping-location")
async def ping_location(loc: LocationCheckSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    quest_id = active.quest_id if active else None
    
    async with db.session_pool() as session:
        log_entry = PlayerLocationLog(user_id=user_id, quest_id=quest_id, latitude=loc.latitude, longitude=loc.longitude)
        session.add(log_entry)
        await session.commit()
        
    if active and active.prev_latitude is not None and active.prev_longitude is not None and active.prev_time is not None:
        quest = await db.get_quest_by_id(active.quest_id)
        if quest:
            now = get_utc_now()
            time_diff = (now - active.prev_time).total_seconds()
            if time_diff > 1.0:
                dist_prev = calculate_haversine_distance(active.prev_latitude, active.prev_longitude, loc.latitude, loc.longitude)
                speed_mps = dist_prev / time_diff
                speed_kmh = speed_mps * 3.6
                user_meta = await db.get_user(user_id)
                if speed_kmh > quest.max_speed_kmh and not getattr(user_meta, 'is_tester', False):
                    await db.add_cheat_log(user_id, quest.id, speed_mps, loc.latitude, loc.longitude)
                    warnings = await db.increment_cheat_warning(user_id)
                    if warnings >= 2:
                        await db.set_ban_status(user_id, True)
                        return {"status": "banned", "message": "🚨 Вы забанены античитом за использование Fake GPS или Транспорта!"}
                    return {"status": "speed_warning", "message": f"⚠️ Внимание! Превышена скорость движения: {int(speed_kmh)} км/ч! (Лимит: {quest.max_speed_kmh})"}
    return {"status": "ok"}

@app.post("/api/quest/buy-hint/{hint_idx}")
async def buy_hint(hint_idx: int, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    if not active: raise HTTPException(status_code=400, detail="Нет активного квеста")
        
    step = await db.get_step_by_id(active.current_step_id)
    hints = normalize_json_field(step.hints) or []
    if not hints:
        hints = [
            {"price": 20, "delay": getattr(step, "hint_1_delay", 5) * 60}, 
            {"price": 0, "delay": getattr(step, "hint_2_delay", 10) * 60}
        ]

    if hint_idx < 0 or hint_idx >= len(hints): raise HTTPException(status_code=400, detail="Подсказка не найдена")
    price = hints[hint_idx].get("price", 0)
    if price > 0:
        if not await db.spend_quest_coins(user_id, price): raise HTTPException(status_code=400, detail="Недостаточно монет для покупки подсказки!")
        await db.increment_error_count(user_id, score_penalty=price)
    return {"status": "success"}

@app.post("/api/quest/submit-answer")
async def submit_answer(ans: AnswerSubmitSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    active = await db.get_active_quest(user_id)
    
    # ПЕРЕХВАТЧИК: Игрок отправил ответ, но его сессию уже удалили
    if not active or await redis_client.get(f"evacuated:{user_id}"):
        await redis_client.delete(f"evacuated:{user_id}")
        return {
            "status": "finished",
            "score": 0, "errors": 0, "achievements": [],
            "message": "⚠️ Внимание! Гейм-мастер перезапустил эту локацию. Вы были безопасно эвакуированы."
        }

    step = await db.get_step_by_id(active.current_step_id)
    user = await db.get_user(user_id)
    sys_set = await db.get_system_settings()

    user_ans = ans.answer.strip().lower()
    
    branches = normalize_json_field(step.branches) or {}
    if not isinstance(branches, dict): branches = {}
    actual_branches = branches.get("branches", branches)
    
    matched_dest = None
    matched_branch_data = None
    for key, dest in actual_branches.items():
        if key.strip().lower() == user_ans:
            if isinstance(dest, dict):
                matched_dest = dest.get("target")
                matched_branch_data = dest
            else:
                matched_dest = dest
            break
            
    if matched_dest is None:
        penalty = 20
        await db.increment_error_count(user_id, score_penalty=penalty)
        return {"status": "wrong", "message": f"❌ Неверный ответ! Штраф: -{penalty} очков."}

    if matched_branch_data:
        req_item = matched_branch_data.get("required_item")
        min_k = int(matched_branch_data.get("min_karma", 0))
        min_level = int(matched_branch_data.get("min_level", 1))
        req_class = matched_branch_data.get("required_class", "")
        fail_target = matched_branch_data.get("fail_target")

        fail_msg = None
        
        if req_item:
            items = await db.get_user_inventory(user_id)
            if not any(getattr(i, 'item_name', '') == req_item for i in items):
                fail_msg = f"❌ Для перехода требуется предмет: {req_item}"
                
        if not fail_msg and min_k > 0 and user.karma < min_k:
            fail_msg = f"❌ Недостаточно кармы! Требуется минимум {min_k}."
            
        if not fail_msg and min_level > 1 and user.level < min_level:
            fail_msg = f"❌ Требуется уровень {min_level}."
            
        if not fail_msg and req_class and user.rpg_class != req_class:
            fail_msg = f"❌ Требуется класс {req_class}."

        if fail_msg:
            if fail_target:
                matched_dest = fail_target
            else:
                return {"status": "condition_failed", "message": fail_msg}

    earned_coins = sys_set.base_step_coins
    if user.rpg_class == "merchant": 
        earned_coins += int(earned_coins * (sys_set.merchant_bonus / 100.0))
        
    score_multiplier = sys_set.historian_mult if user.rpg_class == "historian" else 1.0
    added_score = int(sys_set.base_step_score * score_multiplier)
    
    earned_item = step.gives_item
    
    await db.add_quest_rewards(user_id, coins=earned_coins, item_name=earned_item)

    start_node = get_npc_start_node(step.npc_dialogue)
    if step.npc_name and start_node:
        packed_state = f"{start_node}|DEST:{matched_dest}"
        await db.update_active_quest_npc_node(user_id, packed_state)
        return {"status": "next_step", "message": "✅ Верно! Но тут появляется персонаж..."}

    if str(matched_dest) == "final" or str(matched_dest) == "exit" or step.is_final:
        progress, _ = await db.finish_active_quest(user_id, int(sys_set.quest_completion_bonus * score_multiplier))
        
        unlocked_achievements = []
        unlocked_achievements.extend(await check_and_grant_achievements(user_id, "complete_all_quests"))
        if progress.errors_count == 0:
            unlocked_achievements.extend(await check_and_grant_achievements(user_id, "no_hints", {"errors_count": 0}))
        unlocked_achievements.extend(await check_and_grant_achievements(user_id, "speed_run", {"total_time": progress.total_time_seconds}))

        msg = (
            f"🎉 Квест успешно пройден!\n\n"
            f"💰 Все накопленные монеты, карма и артефакты перенесены в ваш профиль!\n"
            f"🌟 Опыт: +300 XP\n"
            f"📈 Итоговые очки: {progress.score}"
        )
        return {
            "status": "finished", 
            "message": msg, 
            "score": progress.score, 
            "errors": progress.errors_count,
            "achievements": unlocked_achievements
        }
 
    await db.update_active_quest_step(user_id, int(matched_dest), step.latitude, step.longitude, added_score)
    msg = f"✅ Верно! Переходим дальше.\n\nВ буфер добавлено: +{earned_coins} монет\n   Очки рейтинга: +{added_score}"
    if earned_item: msg += f"\n📦 Получен артефакт: {earned_item}"

    return {"status": "next_step", "message": msg}

@app.post("/api/quest/npc-choice/{choice_index}")
async def select_npc_choice(choice_index: int, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")

    if await redis_client.get(f"evacuated:{user_id}"):
        return {"status": "exit", "message": "⚠️ Диалог прерван Гейм-мастером."}

    active = await db.get_active_quest(user_id)
    if not active or not getattr(active, 'current_npc_node', None): raise HTTPException(status_code=400, detail="Диалог с NPC не запущен.")
        
    step = await db.get_step_by_id(active.current_step_id)
    dialogue = normalize_npc_dialogue(step.npc_dialogue)
    if dialogue is None: raise HTTPException(status_code=400, detail="Диалог с NPC недоступен.")

    packed_node = active.current_npc_node
    matched_dest = None
    actual_node = packed_node
    if packed_node and "|DEST:" in packed_node:
        actual_node, matched_dest = packed_node.split("|DEST:", 1)

    node = dialogue.get(actual_node)
    if node is None:
        fallback_node = get_npc_start_node(dialogue)
        if fallback_node is None: raise HTTPException(status_code=400, detail="Ни один узел диалога не найден.")
        node = dialogue.get(fallback_node)
        new_state = fallback_node if not matched_dest else f"{fallback_node}|DEST:{matched_dest}"
        await db.update_active_quest_npc_node(user_id, new_state)

    options = node.get("options", [])
    if choice_index < 0 or choice_index >= len(options): raise HTTPException(status_code=400, detail="Неверный выбор.")
         
    opt = options[choice_index]
    msg_parts = []
    k_change = opt.get("karma_change", 0)
    c_change = opt.get("coins_change", 0)
    i_give = opt.get("item_give")
    i_take = opt.get("item_take")
    x_change = opt.get("xp_change", 0)
    
    if k_change != 0:
        await db.add_quest_rewards(user_id, karma=k_change)
        msg_parts.append(f"☯️ Карма (в буфер): {'+' if k_change > 0 else ''}{k_change}")
        
    if c_change > 0:
        await db.add_quest_rewards(user_id, coins=c_change)
        msg_parts.append(f"В буфер: +{c_change} монет")
    elif c_change < 0:
        spent = await db.spend_quest_coins(user_id, abs(c_change))
        if not spent: raise HTTPException(status_code=400, detail="Недостаточно монет для этого выбора!")
        msg_parts.append(f"💸 Потрачено: {abs(c_change)} монет")
        
    if x_change != 0:
        await db.add_quest_rewards(user_id, xp=x_change)
        msg_parts.append(f"🌟 Опыт (в буфер): {'+' if x_change > 0 else ''}{x_change} XP")
        
    if i_take:
        success = await db.discard_inventory_item(user_id, i_take)
        if success: msg_parts.append(f"➖ Отдан предмет: {i_take}")
        else: raise HTTPException(status_code=400, detail=f"У вас нет требуемого предмета: {i_take}")

    if i_give:
        await db.add_item_to_inventory(user_id, i_give)
        msg_parts.append(f"📦 Получен предмет: {i_give}")

    reward_str = "\n".join(msg_parts)
    next_node = opt.get("next_node", "exit")
    
    # --- Спринт 2: Перехват команды физической телепортации от NPC ---
    if next_node.startswith("step_"):
        target_step_id = int(next_node.split("_")[1])
        sys_set = await db.get_system_settings()
        user = await db.get_user(user_id)
        score_multiplier = sys_set.historian_mult if user.rpg_class == "historian" else 1.0
        added_score = int(sys_set.base_step_score * score_multiplier)
        
        target_step = await db.get_step_by_id(target_step_id)
        if not target_step:
            raise HTTPException(status_code=400, detail="Сюжетный шаг потерян из базы")

        # Закрываем диалоговое окно NPC и переводим сессию игрока на новые координаты
        await db.update_active_quest_npc_node(user_id, None)
        await db.update_active_quest_step(
            user_id, target_step.id, 
            target_step.latitude, target_step.longitude, 
            added_score
        )
        
        final_msg = "Договорились! Собеседник указывает вам новое направление."
        if reward_str: final_msg += f"\n\n{reward_str}"
        return {"status": "next_step", "message": final_msg}

    if next_node == "exit":
        await db.update_active_quest_npc_node(user_id, None)
        if matched_dest:
            user = await db.get_user(user_id)
            sys_set = await db.get_system_settings()
            score_multiplier = sys_set.historian_mult if user.rpg_class == "historian" else 1.0

            if str(matched_dest) == "final" or str(matched_dest) == "exit":
                progress, _ = await db.finish_active_quest(user_id, int(sys_set.quest_completion_bonus * score_multiplier))
                
                unlocked_achievements = []
                unlocked_achievements.extend(await check_and_grant_achievements(user_id, "complete_all_quests"))
                if progress.errors_count == 0:
                    unlocked_achievements.extend(await check_and_grant_achievements(user_id, "no_hints", {"errors_count": 0}))
                unlocked_achievements.extend(await check_and_grant_achievements(user_id, "speed_run", {"total_time": progress.total_time_seconds}))

                msg = (
                    f"🎉 Квест успешно пройден!\n\n"
                    f"💰 Все накопленные монеты, карма и артефакты перенесены в ваш профиль!\n"
                    f"🌟 Опыт: +300 XP\n"
                    f"📈 Итоговые очки: {progress.score}"
                )
                if reward_str:
                    msg += f"\n\n{reward_str}"
                return {"status": "finished", "message": msg, "achievements": unlocked_achievements}

            added_score = int(sys_set.base_step_score * score_multiplier)
            await db.update_active_quest_step(user_id, int(matched_dest), step.latitude, step.longitude, added_score)
            final_msg = "Диалог завершен. Двигаемся дальше!"
            if reward_str: final_msg += f"\n\n{reward_str}"
            return {"status": "next_step", "message": final_msg}

        final_msg = "Диалог завершен."
        if reward_str: final_msg += f"\n\n{reward_str}"
        return {"status": "exit", "message": final_msg}
        
    new_state = next_node if not matched_dest else f"{next_node}|DEST:{matched_dest}"
    await db.update_active_quest_npc_node(user_id, new_state)
    final_msg = "Диалог обновлен."
    if reward_str: final_msg += f"\n\n{reward_str}"
    return {"status": "next_node", "node": next_node, "message": final_msg}

@app.post("/api/npc/{npc_id}/interact")
async def interact_with_npc(npc_id: int, req: NPCInteractSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")

    # ПРЕДОХРАНИТЕЛЬ: Персонаж отказывается говорить
    if await redis_client.get(f"evacuated:{user_id}"):
        return {"status": "exit", "message": "⚠️ Персонаж растворился в тумане. Локация была перезагружена."}

    async with db.session_pool() as session:
        npc = (await session.execute(select(NPCCharacter).where(NPCCharacter.id == npc_id))).scalar_one_or_none()
    if not npc: raise HTTPException(status_code=404, detail="NPC не найден.")

    cfg = await db.get_system_settings()

    # 1. Каскад памяти NPC
    cd_hours = npc.cooldown_override_hours if getattr(npc, 'cooldown_override_hours', None) is not None else cfg.default_npc_cooldown_hours
    if cd_hours > 0:
        if await redis_client.get(f"npc_rewarded:{npc_id}:{user_id}"):
            if req.choice_idx is not None: return {"status": "exit", "message": "До встречи!"}
            return {"status": "dialogue", "node_id": "cooldown", "npc_name": npc.name, "text": "Я уже благословил тебя сегодня. Приходи позже!", "options": [{"text": "🙌 До встречи!"}]}

    # 2. Каскад цены бодрости
    talk_cost = 0 if getattr(npc, 'is_free', False) else (npc.stamina_cost_override if getattr(npc, 'stamina_cost_override', None) is not None else cfg.default_npc_talk_cost)
    if req.current_node == "start" and req.choice_idx is None and talk_cost > 0:
        has_stamina, curr_st = await db.spend_user_stamina(user_id, cost=talk_cost)
        if not has_stamina:
            user = await db.get_user(user_id)
            return {"status": "no_stamina", "stamina": curr_st, "max_stamina": getattr(user, 'max_stamina', 100) or 100}

    await redis_client.set(f"presence:npc:{npc_id}:{user_id}", "1", ex=120)
        
    if not npc: raise HTTPException(status_code=404, detail="NPC не найден.")
    
    # Проверка геопозиции
    dist = calculate_haversine_distance(req.latitude, req.longitude, npc.latitude, npc.longitude)
    if dist > (npc.radius or 30):
        return {"status": "too_far", "message": f"Подойдите ближе! Вы в {int(dist)}м от персонажа."}
        
    dialogue = npc.dialogue_tree
    if not dialogue:
        return {"status": "error", "message": "У этого персонажа пока нет реплик."}
        
    current_node_key = req.current_node
    msg_parts = []
    
    if req.choice_idx is not None:
        node = dialogue.get(current_node_key)
        if not node: raise HTTPException(status_code=400, detail="Ошибка диалога.")
        opts = node.get("options", [])
        if req.choice_idx < 0 or req.choice_idx >= len(opts): raise HTTPException(status_code=400, detail="Неверный выбор.")
        
        opt = opts[req.choice_idx]
        
        # Начисляем награды СРАЗУ на аккаунт (это свободный NPC, буфера квеста здесь нет)
        k_change = opt.get("karma_change", 0)
        c_change = opt.get("coins_change", 0)
        x_change = opt.get("xp_change", 0)
        i_give = opt.get("item_give")
        i_take = opt.get("item_take")
        
        if i_take:
            success = await db.discard_inventory_item(user_id, i_take)
            if not success: return {"status": "error", "message": f"Нужен предмет: {i_take}"}
            msg_parts.append(f"➖ Отдан предмет: {i_take}")
            
        if k_change != 0: await db.update_karma(user_id, k_change); msg_parts.append(f"☯️ Карма: {'+' if k_change > 0 else ''}{k_change}")
        if c_change > 0: await db.add_coins(user_id, c_change); msg_parts.append(f"💰 +{c_change} монет")
        elif c_change < 0:
            if not await db.deduct_coins(user_id, abs(c_change)): return {"status": "error", "message": "Недостаточно монет!"}
            msg_parts.append(f"💸 -{abs(c_change)} монет")
        if x_change != 0: await db.add_xp(user_id, x_change); msg_parts.append(f"🌟 Опыт: {'+' if x_change > 0 else ''}{x_change} XP")
        if i_give: await db.add_item_to_inventory(user_id, i_give); msg_parts.append(f"📦 Получен предмет: {i_give}")
        
        # Запоминаем выдачу лута на динамическое время из Матрешки
        if c_change > 0 or x_change > 0 or i_give:
            cd_sec = int((npc.cooldown_override_hours if getattr(npc, 'cooldown_override_hours', None) is not None else cfg.default_npc_cooldown_hours) * 3600)
            if cd_sec > 0:
                await redis_client.set(f"npc_rewarded:{npc_id}:{user_id}", "1", ex=cd_sec)

        current_node_key = opt.get("next_node", "exit")
        
    if current_node_key == "exit":
        return {"status": "exit", "message": "\n".join(msg_parts) if msg_parts else "Диалог завершен."}
        
    next_node_data = dialogue.get(current_node_key)
    if not next_node_data:
        return {"status": "exit", "message": "\n".join(msg_parts) if msg_parts else "Диалог завершен."}
        
    return {
        "status": "dialogue", 
        "node_id": current_node_key, 
        "npc_name": npc.name,
        "text": next_node_data.get("text", ""),
        "options": next_node_data.get("options", []),
        "reward_msg": "\n".join(msg_parts) if msg_parts else None
    }


@app.get("/api/inventory")
async def get_inventory(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    items = await db.get_user_inventory(user_id)
    weight = await db.get_user_current_weight(user_id)
    return {"items": items, "current_weight": weight}

@app.post("/api/inventory/use/{item_name}")
async def use_item(item_name: str, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    success, msg = await db.activate_consumable_item(user_id, item_name)
    if not success: raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@app.post("/api/inventory/discard/{item_name}")
async def discard_item(item_name: str, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    success = await db.discard_inventory_item(user_id, item_name)
    if not success: raise HTTPException(status_code=400, detail="Предмет не найден.")
    return {"status": "success"}


# =====================================================================
# НОВАЯ МЕХАНИКА: КРАФТИНГ (СОЗДАНИЕ АРТЕФАКТОВ)
# =====================================================================
@app.get("/api/craft/recipes")
async def get_craft_recipes(tg_user: dict = Depends(get_current_user)):
    recipes = await db.get_all_craft_recipes()
    return [
        {
            "id": r.id, "name": r.name, "description": r.description,
            "result_item_name": r.result_item_name, "ingredients": r.ingredients,
            "coins_cost": r.coins_cost, "min_level": r.min_level
        } for r in recipes
    ]

@app.post("/api/craft/{recipe_id}")
async def craft_item(recipe_id: int, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    user = await db.get_user(user_id)
    
    async with db.session_pool() as session:
        recipe = (await session.execute(select(CraftRecipe).where(CraftRecipe.id == recipe_id))).scalar_one_or_none()
        
    if not recipe: 
        raise HTTPException(status_code=404, detail="Рецепт не найден.")
    if user.level < recipe.min_level: 
        raise HTTPException(status_code=400, detail=f"Для создания требуется {recipe.min_level} уровень.")
    if user.coins < recipe.coins_cost: 
        raise HTTPException(status_code=400, detail="Недостаточно монет для создания.")
        
    inventory_items = await db.get_user_inventory(user_id)
    item_counts = {}
    for item in inventory_items:
        item_counts[item] = item_counts.get(item, 0) + 1
        
    # Проверка наличия всех ингредиентов
    for ing_name, req_amount in recipe.ingredients.items():
        if item_counts.get(ing_name, 0) < req_amount:
            raise HTTPException(status_code=400, detail=f"Не хватает материалов: {ing_name} (нужно {req_amount} шт.)")
            
    # Проверка места в рюкзаке (вес нового предмета условно 1кг)
    overloaded, _, _ = await db.is_inventory_overloaded(user_id, 1)
    if overloaded: 
        raise HTTPException(status_code=400, detail="Рюкзак перегружен! Выбросьте лишние вещи.")
        
    # Списание ресурсов
    if recipe.coins_cost > 0:
        await db.deduct_coins(user_id, recipe.coins_cost)
        
    for ing_name, req_amount in recipe.ingredients.items():
        for _ in range(req_amount):
            await db.discard_inventory_item(user_id, ing_name)
            
    # Выдача результата
    await db.add_item_to_inventory(user_id, recipe.result_item_name)
    return {"status": "success", "result_item": recipe.result_item_name}


@app.get("/api/items/catalog")
async def get_public_items_catalog(tg_user: dict = Depends(get_current_user)):
    """Отдает полный клиентский словарь предметов: slug -> {name, description, type, weight}"""
    async with db.session_pool() as session:
        stmt = select(ShopItem.item_name, ShopItem.name, ShopItem.description, ShopItem.item_type, ShopItem.weight)
        res = await session.execute(stmt)
        catalog = {}
        for slug, name, desc, i_type, weight in res.all():
            catalog[slug] = {
                "name": name,
                "description": desc or "Описание скрыто в архивах Пермской губернии...",
                "type": i_type,
                "weight": weight
            }
        return catalog

# --- ЕДИНЫЙ ПРАВИЛЬНЫЙ БЛОК CRUD ДЛЯ ПРЕДМЕТОВ (С поддержкой market_ids) ---
@app.get("/api/admin/items")
async def get_admin_items():
    async with db.session_pool() as session:
        items = (await session.execute(select(ShopItem))).scalars().all()
        # Безопасно отдаем market_ids как список
        return [{
            "id": i.id, "name": i.name, "item_name": i.item_name, 
            "description": i.description, "price": i.price, 
            "weight": i.weight, 
            "market_ids": getattr(i, 'market_ids', []) or []
        } for i in items]

@app.post("/api/admin/items")
async def create_admin_item(item: AdminShopItemSchema):
    async with db.session_pool() as session:
        new_item = ShopItem(
            name=item.name, item_name=item.item_name, 
            description=item.description, price=item.price, 
            weight=item.weight, 
            market_ids=item.market_ids or []
        )
        session.add(new_item)
        await session.commit()
        return {"status": "success"}

@app.put("/api/admin/items")
async def update_admin_item(item: AdminShopItemSchema):
    async with db.session_pool() as session:
        db_item = (await session.execute(select(ShopItem).where(ShopItem.id == item.id))).scalar_one_or_none()
        if db_item:
            db_item.name = item.name
            db_item.item_name = item.item_name
            db_item.description = item.description
            db_item.price = item.price
            db_item.weight = item.weight
            db_item.market_ids = item.market_ids or []
            await session.commit()
        return {"status": "success"}

@app.delete("/api/admin/items/{item_id}")
async def delete_admin_item(item_id: int):
    async with db.session_pool() as session:
        db_item = (await session.execute(select(ShopItem).where(ShopItem.id == item_id))).scalar_one_or_none()
        if db_item:
            await session.delete(db_item)
            await session.commit()
        return {"status": "success"}

@app.get("/api/shop")
async def get_shop_catalog(market_id: Optional[int] = None, tg_user: dict = Depends(get_current_user)):
    items = await db.get_shop_items()
    if market_id is not None: 
        catalog = [i for i in items if i.market_ids and (market_id in i.market_ids)]
    else: 
        # Глобальный магазин: отдаем строго товары, у которых в массиве есть 0. Квестовый лут ([]) надежно скрыт!
        catalog = [i for i in items if i.market_ids and (0 in i.market_ids)]
        
    return [
        {
            "id": i.id, "name": i.name, "description": i.description, "price": i.price,
            "item_name": i.item_name, "item_type": i.item_type, "weight": getattr(i, 'weight', 0),
            "generates_income": getattr(i, 'generates_income', False), "income_per_hour": getattr(i, 'income_per_hour', 0)
        } for i in catalog
    ]

@app.get("/api/player/{telegram_id}")
async def get_player_info_modal(telegram_id: int, tg_user: dict = Depends(get_current_user)):
    profile_data = await db.get_player_public_profile(telegram_id)
    if not profile_data:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return profile_data


@app.post("/api/admin/tester/mortal-mode")
async def toggle_qa_mortal_mode(tg_user: dict = Depends(get_current_user)):
    """Переключатель режима смертного для QA-тестировщиков (Пакет 1)."""
    user_id = tg_user.get("id")
    key = f"qa:mortal_mode:{user_id}"
    if await redis_client.get(key):
        await redis_client.delete(key)
        return {"status": "success", "mortal_mode": False, "message": "🔌 Включен режим Бога (Безлимит энергии)"}
    else:
        await redis_client.set(key, "1", ex=86400)
        return {"status": "success", "mortal_mode": True, "message": "🔋 Включена симуляция смертного (Энергия расходуется)"}


@app.post("/api/profile/anonymity")
async def toggle_anonymity_route(tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    new_status = await db.toggle_user_anonymity(user_id)
    return {"status": "success", "is_anonymous": new_status}

@app.post("/api/shop/enter")
async def enter_shop(req: ShopEnterSchema, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")
    async with db.session_pool() as session:
        market = (await session.execute(select(QuestMarket).where(QuestMarket.id == req.market_id))).scalar_one_or_none()
        if not market: raise HTTPException(status_code=404, detail="Магазин не найден.")
        dist = calculate_haversine_distance(req.latitude, req.longitude, market.latitude, market.longitude)
        if dist > market.radius: 
            return {"status": "too_far", "distance": int(dist), "message": f"Подойдите ближе! Вы в {int(dist)}м от лавки."}
        
        # МГНОВЕННАЯ ФИКСАЦИЯ ПРИСУТСТВИЯ НА 180 СЕКУНД:
        await redis_client.set(f"presence:market:{req.market_id}:{user_id}", "1", ex=180)
        await db.track_market_presence(req.market_id, user_id)

        return {"status": "success", "message": f"Добро пожаловать в {market.name}!"}

@app.post("/api/shop/buy/{item_id}")
async def buy_item(item_id: int, tg_user: dict = Depends(get_current_user)):
    user_id = tg_user.get("id")

    # ПРЕДОХРАНИТЕЛЬ: Если игрока эвакуировали, касса закрыта
    if await redis_client.get(f"evacuated:{user_id}"):
        raise HTTPException(status_code=403, detail="Витрина заблокирована. Локация закрыта на технический аудит.")

    shop_item = await db.get_shop_item_by_id(item_id)
    if not shop_item: raise HTTPException(status_code=404, detail="Товар не найден.")
    user = await db.get_user(user_id)
    if user.coins < shop_item.price: raise HTTPException(status_code=400, detail="Недостаточно монет.")
        
    overloaded, _, _ = await db.is_inventory_overloaded(user_id, getattr(shop_item, 'weight', 0))
    if overloaded: raise HTTPException(status_code=400, detail="Рюкзак перегружен! Выбросите лишние вещи.")
        
    await db.deduct_coins(user_id, shop_item.price)
    await db.add_item_to_inventory(user_id, shop_item.item_name)
    return {"status": "success", "item_name": shop_item.name}

@app.get("/api/leaderboard")
async def get_leaderboard_data(period: str = "global", tg_user: dict = Depends(get_current_user)):
    if period == "global": data = await db.get_leaderboard(limit=15)
    else: data = await db.get_seasonal_leaderboard(period=period, limit=15)
    return data
# =====================================================================
# НОДОВЫЙ РЕДАКТОР ДЛЯ БАЗ NPC (STANDALONE ДИАЛОГИ)
# =====================================================================
@app.get("/api/admin/npcs/{npc_id}/graph")
async def get_npc_graph(npc_id: int):
    async with db.session_pool() as session:
        npc = (await session.execute(select(NPCCharacter).where(NPCCharacter.id == npc_id))).scalar_one_or_none()
        if not npc: return {"drawflow": {"Home": {"data": {}}}}
        if npc.drawflow_data: return npc.drawflow_data
        
        if npc.dialogue_tree and isinstance(npc.dialogue_tree, dict):
            start_n = npc.dialogue_tree.get("start") or next(iter(npc.dialogue_tree.values()), {})
            options = start_n.get("options") or [{"text": "Далее"}]
            return {"drawflow": {"Home": {"data": {"1": {"id": 1, "name": "npc_node", "data": {"npc_text": start_n.get("text", ""), "options": json.dumps(options), "npc_name": npc.name}, "class": "npc_node", "html": "npc_node", "typenode": False, "inputs": {}, "outputs": {}, "pos_x": 250, "pos_y": 200}}}}}
        return {"drawflow": {"Home": {"data": {}}}}

@app.post("/api/admin/npcs/{npc_id}/graph")
async def save_npc_graph(npc_id: int, payload: SaveGraphSchema):
    nodes = payload.drawflow_data
    dialogue_dict = {}
    for node_id, node_obj in nodes.items():
        if node_obj.name == "npc_node":
            node_key = "start" if len(dialogue_dict) == 0 else f"node_{node_id}"
            raw_opts = getattr(node_obj.data, 'options', None)
            if isinstance(raw_opts, str):
                try: raw_opts = json.loads(raw_opts)
                except Exception: raw_opts = []
            if not isinstance(raw_opts, list) or not raw_opts: raw_opts = [{"text": "Далее"}]
            options_list = [{"text": o.get("text", "Далее"), "next_node": o.get("next_node", "exit"), "karma_change": int(o.get("karma_change") or 0), "coins_change": int(o.get("coins_change") or 0)} for o in raw_opts]
            dialogue_dict[node_key] = {"text": node_obj.data.npc_text or "Приветствую!", "options": options_list}

    async with db.session_pool() as session:
        npc = (await session.execute(select(NPCCharacter).where(NPCCharacter.id == npc_id))).scalar_one_or_none()
        if npc:
            npc.drawflow_data = {"drawflow": {"Home": {"data": {k: v.model_dump(by_alias=True) for k, v in nodes.items()}}}}
            npc.dialogue_tree = dialogue_dict if dialogue_dict else None
            session.add(npc)
            await session.commit()
    return {"status": "success", "message": "Диалог NPC сохранен!"}