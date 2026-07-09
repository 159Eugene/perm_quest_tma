import datetime
import enum
import logging
from typing import List, Optional, Dict, Any, Union
from sqlalchemy import BigInteger, String, ForeignKey, Float, Boolean, DateTime, JSON, UniqueConstraint, Integer, text as sa_text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logger = logging.getLogger(__name__)

def get_naive_utc() -> datetime.datetime:
    """
    Возвращает текущее UTC-время как naive-объект (без tzinfo).
    Необходимо для безопасной записи в TIMESTAMP WITHOUT TIME ZONE в PostgreSQL через asyncpg.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

# -------------------------------------------------------------------------
# ИНТЕЛЛЕКТУАЛЬНАЯ ИНТЕГРАЦИЯ PYDANTIC-СХЕМ ВАЛИДАЦИИ JSONB
# -------------------------------------------------------------------------
from tgbot.schemas.npc import (
    DialogueOptionSchema, DialogueNodeSchema, 
    NPCDialogueSchema, StepBranchesSchema
)

class PydanticJSON(TypeDecorator):
    impl = JSON
    cache_ok = True
    
    def __init__(self, pydantic_model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pydantic_model = pydantic_model
        
    def process_bind_param(self, value, dialect):
        if value is None: 
            return None
        # Жесткая валидация Pydantic перед INSERT/UPDATE в БД
        parsed = self.pydantic_model.model_validate(value)
        # by_alias=True гарантирует, что ui_meta запишется обратно как _ui
        return parsed.model_dump(by_alias=True, exclude_none=True)
        
    def process_result_value(self, value, dialect):
        if value is None: 
            return None
        # Восстановление Pydantic-модели при SELECT
        return self.pydantic_model.model_validate(value)

class ShopItemType(str, enum.Enum):
    ARTIFACT = "ARTIFACT"
    PROMO = "PROMO"
    TICKET = "TICKET"
    CONSUMABLE = "CONSUMABLE"

class Base(DeclarativeBase):
    pass

# =========================================================================
# ГЛОБАЛЬНЫЕ СИСТЕМНЫЕ НАСТРОЙКИ (SYSTEM SETTINGS)
# =========================================================================
class SystemSettings(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tutorial_latitude: Mapped[float] = mapped_column(Float, default=58.0097)
    tutorial_longitude: Mapped[float] = mapped_column(Float, default=56.2444)
    tutorial_answer: Mapped[str] = mapped_column(String(100), default="пермь")
    merchant_bonus: Mapped[int] = mapped_column(Integer, default=20)
    ranger_cd_minutes: Mapped[int] = mapped_column(Integer, default=7)
    historian_mult: Mapped[float] = mapped_column(Float, default=2.0)
    base_step_coins: Mapped[int] = mapped_column(Integer, default=10)
    base_step_score: Mapped[int] = mapped_column(Integer, default=100)
    quest_completion_bonus: Mapped[int] = mapped_column(Integer, default=300)
    karma_elixir_price: Mapped[int] = mapped_column(Integer, default=50)
    karma_elixir_effect: Mapped[int] = mapped_column(Integer, default=3)
    daily_gift_base_reward: Mapped[int] = mapped_column(Integer, default=10)
    daily_gift_increment: Mapped[int] = mapped_column(Integer, default=5)
    daily_gift_max_reward: Mapped[int] = mapped_column(Integer, default=50)
    scroll_event_price: Mapped[int] = mapped_column(Integer, default=10)
    scroll_event_karma: Mapped[int] = mapped_column(Integer, default=2)
    wallet_event_coins: Mapped[int] = mapped_column(Integer, default=15)
    wallet_event_karma_penalty: Mapped[int] = mapped_column(Integer, default=-1)
    wallet_event_karma_reward: Mapped[int] = mapped_column(Integer, default=2)
    merc_lifetime_minutes: Mapped[int] = mapped_column(Integer, default=60)
    merc_summon_price: Mapped[int] = mapped_column(Integer, default=150)
    merc_efficiency: Mapped[int] = mapped_column(Integer, default=100)
    
    # --- ДОБАВЛЕНО: Глобальный пульт баланса Перми (Пакет 2) ---
    default_quest_start_cost: Mapped[int] = mapped_column(Integer, default=20)
    default_step_cost: Mapped[int] = mapped_column(Integer, default=15)
    default_npc_talk_cost: Mapped[int] = mapped_column(Integer, default=5)
    default_quest_cooldown_hours: Mapped[int] = mapped_column(Integer, default=20)
    default_npc_cooldown_hours: Mapped[int] = mapped_column(Integer, default=24)
    # --------------------------------------------------------

class LevelConfig(Base):
    """Таблица гибкой прогрессии уровней и наград за их достижение (Пакет 1)."""
    __tablename__ = "level_configs"
    level: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    xp_to_next: Mapped[int] = mapped_column(Integer, default=150, nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0)
    reward_item_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    stamina_bonus: Mapped[int] = mapped_column(Integer, default=0)

# =========================================================================
# ПОЛЬЗОВАТЕЛИ (USERS) И ИХ ПРОГРЕСС
# =========================================================================
class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    full_name: Mapped[str] = mapped_column(String(250))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    banned_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    coins: Mapped[int] = mapped_column(Integer, default=0)
    karma: Mapped[int] = mapped_column(Integer, default=0)
    gems: Mapped[int] = mapped_column(Integer, default=0)
    rpg_class: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    max_weight_capacity: Mapped[int] = mapped_column(Integer, default=10)
    income_buffer: Mapped[int] = mapped_column(Integer, default=0)
    completed_tutorial: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # --- ДОБАВЛЕНО: Колонки системы бодрости (Спринт 1) ---
    stamina: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    max_stamina: Mapped[int] = mapped_column(Integer, default=100, server_default="100") # <-- ДОБАВЛЕНО (Пакет 1)
    last_stamina_update: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, default=get_naive_utc, nullable=True)
    # ----------------------------------------------------

    cheat_warnings: Mapped[int] = mapped_column(Integer, default=0)
    daily_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_daily_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    gift_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_gift_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    current_city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auto_city_detect: Mapped[bool] = mapped_column(Boolean, default=True)
    guild_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_class_change: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    global_flags: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

    # Флаги приватности и QA-тестирования
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_tester: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    active_quests: Mapped[List["ActiveQuest"]] = relationship("ActiveQuest", back_populates="user", cascade="all, delete-orphan")
    inventory: Mapped[List["InventoryItem"]] = relationship("InventoryItem", back_populates="user", cascade="all, delete-orphan")

# =========================================================================
# ИГРОВЫЕ СУЩНОСТИ ДЛЯ RPG И КОНТЕНТА
# =========================================================================
class NPCCharacter(Base):
    __tablename__ = "npc_characters"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    avatar_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    radius: Mapped[Optional[float]] = mapped_column(Float, default=30.0, nullable=True)
    
    # --- НОВЫЕ ПОЛЯ ДЛЯ ВИЗУАЛЬНОГО РЕДАКТОРА ---
    dialogue_tree: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    drawflow_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # --- ДОБАВЛЕНО: Индивидуальный тариф NPC (Пакет 2) ---
    stamina_cost_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cooldown_override_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # ---------------------------------------------------

class Sponsor(Base):
    __tablename__ = "sponsors"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    logo_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    promo_pool_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class PlayerLocationLog(Base):
    __tablename__ = "player_location_logs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    quest_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

# =========================================================================
# КВЕСТЫ (QUESTS) И ШАГИ (STEPS)
# =========================================================================
class Quest(Base):
    __tablename__ = "quests"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    max_speed_kmh: Mapped[float] = mapped_column(Float, default=15.0)
    min_level_required: Mapped[int] = mapped_column(Integer, default=1)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    season_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    global_time_limit_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_coop: Mapped[bool] = mapped_column(Boolean, default=False)
    coop_max_size: Mapped[int] = mapped_column(Integer, default=4)
    
    # --- Спринт 1: Поле для сохранения сырого JSON черновика холста Drawflow ---
    drawflow_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # --- ДОБАВЛЕНО: Индивидуальный тариф Квеста (Пакет 2) ---
    stamina_cost_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cooldown_override_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # -----------------------------------------------------

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    
    steps: Mapped[List["Step"]] = relationship("Step", back_populates="quest", cascade="all, delete-orphan", order_by="Step.id")
    active_quests: Mapped[List["ActiveQuest"]] = relationship("ActiveQuest", back_populates="quest", cascade="all, delete-orphan")

# =========================================================================
# ЕДИНАЯ МАСТЕР-ТАБЛИЦА СУЩНОСТЕЙ (ITEMS)
# =========================================================================
class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    item_type: Mapped[str] = mapped_column(String(50), default=ShopItemType.ARTIFACT)
    weight: Mapped[int] = mapped_column(Integer, default=0)
    is_consumable: Mapped[bool] = mapped_column(Boolean, default=False)
    generates_income: Mapped[bool] = mapped_column(Boolean, default=False)
    income_per_hour: Mapped[int] = mapped_column(Integer, default=0)

    shop_items: Mapped[List["ShopItem"]] = relationship("ShopItem", back_populates="item", cascade="all, delete-orphan")
    inventory_items: Mapped[List["InventoryItem"]] = relationship("InventoryItem", back_populates="item", cascade="all, delete-orphan")


class Step(Base):
    __tablename__ = "steps"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False, index=True)
    instruction_text: Mapped[str] = mapped_column(String(3000), nullable=False)
    welcome_message: Mapped[Optional[str]] = mapped_column(String(1200), nullable=True)
    history_info: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    photo_then_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    photo_now_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    audio_guide_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    radius_meters: Mapped[int] = mapped_column(Integer, default=30)
    is_night_only: Mapped[bool] = mapped_column(Boolean, default=False)
    is_day_only: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_sun_only: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_rain_only: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_snow_only: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dry_only: Mapped[bool] = mapped_column(Boolean, default=False)
    is_weather_only: Mapped[bool] = mapped_column(Boolean, default=False)
    min_karma_required: Mapped[int] = mapped_column(Integer, default=0)
    required_flag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    granted_flag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sponsor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sponsors.id", ondelete="SET NULL"), nullable=True)
    npc_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True) 
    npc_dialogue: Mapped[Optional[NPCDialogueSchema]] = mapped_column(PydanticJSON(NPCDialogueSchema), nullable=True)
    time_limit_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    branches: Mapped[StepBranchesSchema] = mapped_column(PydanticJSON(StepBranchesSchema), default=lambda: {"branches": {}})
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    required_item: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gives_item: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gives_item_chance: Mapped[float] = mapped_column(Float, default=1.0)
    secret_price: Mapped[int] = mapped_column(Integer, default=0)
    hint_1_delay: Mapped[int] = mapped_column(Integer, default=5)
    hint_1_text: Mapped[str] = mapped_column(String(500), default="")
    hint_2_delay: Mapped[int] = mapped_column(Integer, default=10)
    hint_2_text: Mapped[str] = mapped_column(String(500), default="")
    hints: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)

    quest: Mapped["Quest"] = relationship("Quest", back_populates="steps")
    active_sessions: Mapped[List["ActiveQuest"]] = relationship("ActiveQuest", back_populates="current_step", foreign_keys="[ActiveQuest.current_step_id]")

    @property
    def upload_photo_then(self): return None
    @property
    def upload_photo_now(self): return None
    @property
    def upload_audio_guide(self): return None

class ActiveQuest(Base):
    __tablename__ = "active_quests"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), primary_key=True)
    current_step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    last_action_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    step_activated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    is_night_run: Mapped[bool] = mapped_column(Boolean, default=True)
    is_rain_run: Mapped[bool] = mapped_column(Boolean, default=True)
    current_npc_node: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_accumulated_seconds: Mapped[int] = mapped_column(Integer, default=0)
    freeze_used: Mapped[bool] = mapped_column(Boolean, default=False)
    is_maintenance_frozen: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_game_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prev_latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prev_longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prev_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    pending_coins: Mapped[int] = mapped_column(Integer, default=0)
    pending_xp: Mapped[int] = mapped_column(Integer, default=0)
    pending_karma: Mapped[int] = mapped_column(Integer, default=0)
    pending_items: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    
    user: Mapped["User"] = relationship("User", back_populates="active_quests")
    quest: Mapped["Quest"] = relationship("Quest", back_populates="active_quests")
    current_step: Mapped["Step"] = relationship("Step", back_populates="active_sessions", foreign_keys=[current_step_id])

class QuestProgress(Base):
    __tablename__ = "quest_progress"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    total_time_seconds: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    is_season_archived: Mapped[bool] = mapped_column(Boolean, default=False)

# =========================================================================
# ЭКОНОМИКА: ИНВЕНТАРЬ И МАГАЗИН (SHOP)
# =========================================================================
class InventoryItem(Base):
    __tablename__ = "inventory"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False) # Legacy slug
    
    # Мягкая ссылка на Мастер-таблицу
    item_slug: Mapped[Optional[str]] = mapped_column(ForeignKey("items.slug", ondelete="CASCADE"), nullable=True)
    
    weight: Mapped[int] = mapped_column(Integer, default=0)
    is_consumable: Mapped[bool] = mapped_column(Boolean, default=False)
    generates_income: Mapped[bool] = mapped_column(Boolean, default=False)
    income_per_hour: Mapped[int] = mapped_column(Integer, default=0)
    acquired_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    
    user: Mapped["User"] = relationship("User", back_populates="inventory")
    item: Mapped[Optional["Item"]] = relationship("Item", back_populates="inventory_items")

class QuestMarket(Base):
    __tablename__ = "quest_markets"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, default=50.0)


class ShopItem(Base):
    __tablename__ = "shop_items"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False) # Legacy slug
    
    item_slug: Mapped[Optional[str]] = mapped_column(ForeignKey("items.slug", ondelete="CASCADE"), nullable=True)
    item_type: Mapped[str] = mapped_column(String(50), default=ShopItemType.ARTIFACT)
    weight: Mapped[int] = mapped_column(Integer, default=0)
    generates_income: Mapped[bool] = mapped_column(Boolean, default=False)
    income_per_hour: Mapped[int] = mapped_column(Integer, default=0)
    market_ids: Mapped[Optional[list[int]]] = mapped_column(JSON, nullable=True)  # Массив ID рынков
    buyback_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    item: Mapped[Optional["Item"]] = relationship("Item", back_populates="shop_items")

class PromoCode(Base):
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    sponsor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sponsors.id", ondelete="CASCADE"), nullable=True)
    shop_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("shop_items.id", ondelete="CASCADE"), nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

class CraftRecipe(Base):
    __tablename__ = "craft_recipes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    result_item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ingredients: Mapped[JSON] = mapped_column(JSON, nullable=False)
    coins_cost: Mapped[int] = mapped_column(Integer, default=0)
    min_level: Mapped[int] = mapped_column(Integer, default=1)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

# =========================================================================
# ИВЕНТЫ, ЗАГАДКИ И ДОСТИЖЕНИЯ
# =========================================================================
class Achievement(Base):
    __tablename__ = "achievements"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    badge_emoji: Mapped[str] = mapped_column(String(10), nullable=False)
    required_action: Mapped[str] = mapped_column(String(50), nullable=False)
    
    # Пороги срабатывания по прогрессивным уровням
    required_value_bronze: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    required_value_silver: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    required_value_diamond: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Награда (монеты) по уровням
    reward_coins_bronze: Mapped[int] = mapped_column(Integer, default=0)
    reward_coins_silver: Mapped[int] = mapped_column(Integer, default=0)
    reward_coins_diamond: Mapped[int] = mapped_column(Integer, default=0)

class UserAchievement(Base):
    __tablename__ = "user_achievements"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True)
    achievement_id: Mapped[int] = mapped_column(ForeignKey("achievements.id", ondelete="CASCADE"), primary_key=True)
    earned_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    tier: Mapped[str] = mapped_column(String(20), default="bronze", server_default="bronze")  # 'bronze', 'silver', 'diamond'
    
class DailyRiddle(Base):
    __tablename__ = "daily_riddles"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    correct_answer: Mapped[str] = mapped_column(String(100), nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=10)

class RandomEvent(Base):
    __tablename__ = "random_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    probability: Mapped[float] = mapped_column(Float, default=10.0)
    coins_impact: Mapped[int] = mapped_column(Integer, default=0)
    karma_impact: Mapped[int] = mapped_column(Integer, default=0)
    xp_reward: Mapped[int] = mapped_column(Integer, default=0)

class GlobalEvent(Base):
    __tablename__ = "global_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=True)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

# =========================================================================
# ВНЕШНИЕ СУЩНОСТИ (АДМИНКА, LIVE-OPS, СОЦИАЛКА)
# =========================================================================

class City(Base):
    __tablename__ = "cities"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    radius_km: Mapped[float] = mapped_column(Float, default=10.0)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Yekaterinburg")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    ends_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bonus_xp_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    bonus_coins_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    reward_item_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

class Guild(Base):
    __tablename__ = "guilds"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    leader_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    total_xp: Mapped[int] = mapped_column(Integer, default=0)
    max_members: Mapped[int] = mapped_column(Integer, default=10)
    emblem_emoji: Mapped[str] = mapped_column(String(10), default="🛡")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class GuildMember(Base):
    __tablename__ = "guild_members"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="member")
    contribution_xp: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class PvPDuel(Base):
    __tablename__ = "pvp_duels"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    challenger_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opponent_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="waiting")
    challenger_score: Mapped[int] = mapped_column(Integer, default=0)
    opponent_score: Mapped[int] = mapped_column(Integer, default=0)
    winner_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bet_coins: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class PvPQuestion(Base):
    __tablename__ = "pvp_questions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    correct_answer: Mapped[str] = mapped_column(String(100), nullable=False)
    wrong_answers: Mapped[JSON] = mapped_column(JSON, nullable=False)
    difficulty: Mapped[int] = mapped_column(Integer, default=1)

class CoopSession(Base):
    __tablename__ = "coop_sessions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    leader_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invite_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, default=4)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class CoopMember(Base):
    __tablename__ = "coop_members"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class Challenge(Base):
    __tablename__ = "challenges"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    challenge_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_value: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0)
    reward_xp: Mapped[int] = mapped_column(Integer, default=0)
    reward_gems: Mapped[int] = mapped_column(Integer, default=0)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class UserChallenge(Base):
    __tablename__ = "user_challenges"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    challenge_id: Mapped[int] = mapped_column(Integer, nullable=False)
    current_value: Mapped[int] = mapped_column(Integer, default=0)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)

class QuestReview(Base):
    __tablename__ = "quest_reviews"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    is_season_archived: Mapped[bool] = mapped_column(Boolean, default=False)

class PhotoReport(Base):
    __tablename__ = "photo_reports"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    photo_file_id: Mapped[str] = mapped_column(String(250), nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class CheatLog(Base):
    __tablename__ = "cheat_logs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    speed: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class ScheduledBroadcast(Base):
    __tablename__ = "scheduled_broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    send_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject: Mapped[str] = mapped_column(String(150), nullable=False)
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="open")
    admin_response: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

class GemTransaction(Base):
    __tablename__ = "gem_transactions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=True)
    telegram_payment_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)

class ARMarker(Base):
    __tablename__ = "ar_markers"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    image_ref_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    model_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_shard_collection: Mapped[bool] = mapped_column(Boolean, default=False)
    shard_count: Mapped[int] = mapped_column(Integer, default=1)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    reward_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reward_value: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class ARScanLog(Base):
    __tablename__ = "ar_scan_logs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    marker_id: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=get_naive_utc)