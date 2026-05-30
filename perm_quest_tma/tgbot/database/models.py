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
# ИНТЕЛЛЕКТУАЛЬНАЯ ИНТЕГРАЦИЯ PYDANTIC-СХЕМ ВАЛИДАЦИИ JSONB (FALLBACK-PROOF)
# -------------------------------------------------------------------------
try:
    from tgbot.schemas.npc import StepBranchesSchema, NPCDialogueSchema
except ImportError:
    # Автоматический защитный фолбек схем для бесконфликтной сборки на любых этапах и в Docker
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

    class StepBranchesSchema(BaseModel):
        """
        Фолбек-версия схемы переходов. Содержит поле branches со словарем,
        чтобы без ошибок валидировать дефолтные структуры СУБД вида {"branches": {}}.
        """
        branches: Dict[str, Union[int, str]] = Field(default_factory=dict)


class PydanticJSON(TypeDecorator):
    """
    Кастомный TypeDecorator для SQLAlchemy, автоматически валидирующий
    сложные поля JSONB через Pydantic-схемы.
    Обеспечивает 100% обратную совместимость, возвращая чистый dict в Python.
    """
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pydantic_model = pydantic_model

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, dict):
            # Валидируем структуру через схему Pydantic v2
            self.pydantic_model.model_validate(value)
            return value
        if isinstance(value, self.pydantic_model):
            return value.model_dump()
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, dict):
            try:
                # Проверяем на соответствие схеме при извлечении из базы
                validated = self.pydantic_model.model_validate(value)
                return validated.model_dump()
            except Exception as e:
                logger.error(f"Pydantic JSONB Validation failed for model {self.pydantic_model.__name__}: {e}")
                return value
        return value


class ShopItemType(str, enum.Enum):
    """
    Строго типизированный перечислитель типов товаров в магазине наград.
    Включает в себя поддержку расходных материалов (CONSUMABLE).
    """
    ARTIFACT = "ARTIFACT"
    PROMO = "PROMO"
    TICKET = "TICKET"
    CONSUMABLE = "CONSUMABLE"


class Base(DeclarativeBase):
    """
    Базовый декларативный класс для всех моделей SQLAlchemy в проекте.
    Реализует общую поддержку кастомных типов в будущем.
    """
    pass


class SystemSettings(Base):
    """
    Таблица глобальных системных настроек платформы.
    Хранит настройки баланса RPG-классов, параметры Квеста №0 (Обучение)
    и все глобальные экономические константы платформы для исключения хардкода.
    """
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    
    # Параметры Квеста №0 (Обучение)
    tutorial_latitude: Mapped[float] = mapped_column(Float, default=58.0097, server_default="58.0097")
    tutorial_longitude: Mapped[float] = mapped_column(Float, default=56.2444, server_default="56.2444")
    tutorial_answer: Mapped[str] = mapped_column(String(100), default="пермь", server_default="'пермь'")
    
    # Настройки баланса классов
    merchant_bonus: Mapped[int] = mapped_column(Integer, default=20, server_default="20")  # Процент бонуса монет купца
    ranger_cd_minutes: Mapped[int] = mapped_column(Integer, default=7, server_default="7")  # Кулдаун подсказки следопыта
    historian_mult: Mapped[float] = mapped_column(Float, default=2.0, server_default="2.0")  # Множитель очков историка

    # Внутриигровая RPG Экономика (баланс)
    base_step_coins: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    base_step_score: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    quest_completion_bonus: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    
    karma_elixir_price: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    karma_elixir_effect: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    
    daily_gift_base_reward: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    daily_gift_increment: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    daily_gift_max_reward: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    
    # Всплывающие случайные события
    scroll_event_price: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    scroll_event_karma: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    
    wallet_event_coins: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    wallet_event_karma_penalty: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    wallet_event_karma_reward: Mapped[int] = mapped_column(Integer, default=2, server_default="2")

    # Системные параметры кастомизации Наемника
    merc_lifetime_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    merc_summon_price: Mapped[int] = mapped_column(Integer, default=150, server_default="150")
    merc_efficiency: Mapped[int] = mapped_column(Integer, default=100, server_default="100")


class User(Base):
    """
    Модель пользователя Telegram-бота.
    """
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    full_name: Mapped[str] = mapped_column(String(250))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    banned_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    
    # Виртуальная экономика
    coins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    
    # RPG-составляющая: Карма игрока
    karma: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # RPG-класс: "merchant" (Купец), "ranger" (Следопыт), "historian" (Историк)
    rpg_class: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Дата и время последнего изменения класса (для сезонного лимита)
    last_class_change: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    # Статус прохождения Квеста №0 (Обучение)
    completed_tutorial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Двухэтапный античит: счетчик предупреждений
    cheat_warnings: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Системные поля для ежедневных загадок (дейликов)
    daily_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_daily_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    # Системные поля для ежедневного подарка за вход
    last_gift_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    gift_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Движок уровней и опыта (XP Engine)
    xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Механика веса и лимитов рюкзака
    max_weight_capacity: Mapped[int] = mapped_column(Integer, default=10, server_default="10")

    # Буферизация дохода от элитных артефактов
    income_buffer: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Премиум-валюта (гемы) — покупается за Telegram Stars
    gems: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Привязка к текущему городу
    current_city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)

    # Привязка к гильдии (use_alter разрывает циклическую зависимость users ↔ guilds)
    guild_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("guilds.id", ondelete="SET NULL", use_alter=True, name="fk_user_guild_id"),
        nullable=True, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    # Отношения
    active_quests: Mapped[List["ActiveQuest"]] = relationship(
        "ActiveQuest", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )
    inventory: Mapped[List["InventoryItem"]] = relationship(
        "InventoryItem", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )
    progress: Mapped[List["QuestProgress"]] = relationship(
        "QuestProgress", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )
    achievements: Mapped[List["UserAchievement"]] = relationship(
        "UserAchievement",
        back_populates="user",
        cascade="all, delete-orphan"
    )


class Quest(Base):
    """
    Модель квеста. Описывает карточку квеста.
    """
    __tablename__ = "quests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    
    # Лимит скорости для античит-системы (км/ч): 15.0 для пеших, до 90.0 для автомобильных
    max_speed_kmh: Mapped[float] = mapped_column(Float, default=15.0, server_default="15.0")
    
    # Квест-гейт по уровню игрока
    min_level_required: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Привязка к городу
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)

    # Сезонный квест (привязка к сезону)
    season_id: Mapped[Optional[int]] = mapped_column(ForeignKey("seasons.id", ondelete="SET NULL"), nullable=True, index=True)

    # Кооперативный квест (поддержка мультиплеера)
    is_coop: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    # Отношения
    steps: Mapped[List["Step"]] = relationship(
        "Step", 
        back_populates="quest", 
        cascade="all, delete-orphan",
        order_by="Step.id"
    )
    active_quests: Mapped[List["ActiveQuest"]] = relationship(
        "ActiveQuest", 
        back_populates="quest", 
        cascade="all, delete-orphan"
    )
    progress_records: Mapped[List["QuestProgress"]] = relationship(
        "QuestProgress",
        back_populates="quest",
        cascade="all, delete-orphan"
    )


class Step(Base):
    """
    Модель шага (контрольной точки) квеста. 
    Описывает конкретную геолокацию, загадку и правила перехода к следующим узлам графа.
    """
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quest_id: Mapped[int] = mapped_column(
        ForeignKey("quests.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    
    # Контентная составляющая шага
    instruction_text: Mapped[str] = mapped_column(String(3000), nullable=False)
    history_info: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    
    # Telegram File IDs для медиафайлов
    photo_then_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    photo_now_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)
    audio_guide_id: Mapped[Optional[str]] = mapped_column(String(250), nullable=True)

    # Географические координаты целевой точки
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    # Зависимость от времени суток и погоды (сохранена совместимость + расширено)
    is_night_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_day_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    
    is_weather_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_dry_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Минимальная карма, необходимая для открытия этого шага
    min_karma_required: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Настройки интерактива с NPC
    npc_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    # Декларативная JSONB-валидация через кастомный декоратор
    npc_dialogue: Mapped[Optional[NPCDialogueSchema]] = mapped_column(
        PydanticJSON(NPCDialogueSchema), 
        nullable=True
    )
    
    # Ограничение по времени для NPC (в секундах) — Тайм-атак
    time_limit_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Сюжетные переходы (валидируемые через Pydantic)
    branches: Mapped[StepBranchesSchema] = mapped_column(
        PydanticJSON(StepBranchesSchema),
        default=lambda: {"branches": {}},
        server_default=sa_text("'{\"branches\": {}}'::jsonb")
    )
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Сюжетный инвентарь на шаге
    required_item: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gives_item: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Монетизация веток
    secret_price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Временные интервалы для автоматических подсказок (для обратной совместимости)
    hint_1_delay: Mapped[int] = mapped_column(default=5, server_default="5")
    hint_1_text: Mapped[str] = mapped_column(String(1000), default="Присмотритесь к элементам фасада здания.")
    hint_2_delay: Mapped[int] = mapped_column(default=10, server_default="10")
    hint_2_text: Mapped[str] = mapped_column(String(1000), default="Цель находится рядом с главным входом.")

    # Динамический список подсказок любой вложенности (Многоуровневые подсказки)
    hints: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)

    # Обратные связи
    quest: Mapped["Quest"] = relationship("Quest", back_populates="steps")
    active_sessions: Mapped[List["ActiveQuest"]] = relationship(
        "ActiveQuest", 
        back_populates="current_step",
        foreign_keys="[ActiveQuest.current_step_id]"
    )


class InventoryItem(Base):
    """
    Таблица инвентаря пользователей.
    Связывает игрока с найденными им уникальными виртуальными предметами.
    """
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.telegram_id", ondelete="CASCADE"), 
        nullable=False,
        index=True
    )
    item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # Характеристики веса и пассивного дохода
    weight: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generates_income: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    income_per_hour: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    acquired_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    # Обратная связь с пользователем
    user: Mapped["User"] = relationship("User", back_populates="inventory")

    # Уникальный индекс, гарантирующий, что игрок не получит один предмет дважды
    __table_args__ = (
        UniqueConstraint("user_id", "item_name", name="uq_user_item"),
    )


class ActiveQuest(Base):
    """
    Таблица текущих игровых сессий (состояние "в процессе").
    Обеспечивает сохранение прогресса, работу античета по геопозиции и механизм заморозки.
    Использует составной первичный ключ (user_id, quest_id) для поддержки чекпоинтов.
    """
    __tablename__ = "active_quests"

    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.telegram_id", ondelete="CASCADE"), 
        primary_key=True,
        index=True
    )
    quest_id: Mapped[int] = mapped_column(
        ForeignKey("quests.id", ondelete="CASCADE"), 
        primary_key=True,
        index=True
    )
    current_step_id: Mapped[int] = mapped_column(
        ForeignKey("steps.id", ondelete="CASCADE"), 
        nullable=False,
        index=True
    )
    
    # Метрики прохождения
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )
    last_action_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )
    
    # Флаг приостановки квеста (сохранение на чекпоинте)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Время активации текущего шага (для тайм-атака NPC)
    step_activated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    # ID последнего отправленного игрового сообщения для интерфейса "Одного экрана"
    last_game_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Механика "Заморозки" времени
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    frozen_accumulated_seconds: Mapped[int] = mapped_column(default=0, server_default="0")
    freeze_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Поля для античит-системы (последняя успешно пройденная точка)
    prev_latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prev_longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prev_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    # Счетчики набранных баллов и ошибок на текущем квесте
    score: Mapped[int] = mapped_column(default=0, server_default="0")
    errors_count: Mapped[int] = mapped_column(default=0, server_default="0")

    # Отслеживание RPG-достижений для "сталкерства" на лету
    is_night_run: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_rain_run: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Текущая нода диалога с NPC, если игрок ведет беседу
    current_npc_node: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="active_quests")
    quest: Mapped["Quest"] = relationship("Quest", back_populates="active_quests")
    current_step: Mapped["Step"] = relationship(
        "Step", 
        back_populates="active_sessions",
        foreign_keys=[current_step_id]
    )


class QuestProgress(Base):
    """
    Архив завершенных прохождений. На его основе формируется Leaderboard.
    """
    __tablename__ = "quest_progress"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.telegram_id", ondelete="CASCADE"), 
        nullable=False,
        index=True
    )
    quest_id: Mapped[int] = mapped_column(
        ForeignKey("quests.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    
    total_time_seconds: Mapped[int] = mapped_column(nullable=False)
    score: Mapped[int] = mapped_column(nullable=False)
    errors_count: Mapped[int] = mapped_column(nullable=False)
    completed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    # Фильтр для сезонов (архивация пройденных рекордов без стирания из общего зачета)
    is_season_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="progress")
    quest: Mapped["Quest"] = relationship("Quest", back_populates="progress_records")


class Achievement(Base):
    """
    Модель достижения, доступного в системе.
    """
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    badge_emoji: Mapped[str] = mapped_column(String(10), nullable=False)
    required_action: Mapped[str] = mapped_column(String(50), nullable=False)
    required_value: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    earned_by: Mapped[List["UserAchievement"]] = relationship(
        "UserAchievement", 
        back_populates="achievement", 
        cascade="all, delete-orphan"
    )


class UserAchievement(Base):
    """
    Таблица связи пользователей и их заработанных достижений.
    """
    __tablename__ = "user_achievements"

    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.telegram_id", ondelete="CASCADE"), 
        primary_key=True,
        index=True
    )
    achievement_id: Mapped[int] = mapped_column(
        ForeignKey("achievements.id", ondelete="CASCADE"), 
        primary_key=True,
        index=True
    )
    earned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    user: Mapped["User"] = relationship("User", back_populates="achievements")
    achievement: Mapped["Achievement"] = relationship("Achievement", back_populates="earned_by")


class ShopItem(Base):
    """
    Витрина предметов внутриигрового магазина.
    Типы товаров жестко типизированы через ShopItemType.
    Колонка item_type переведена на String(50), что гарантирует 100% совместимость
    с PostgreSQL и исключает капризы экранирования Enum-типов.
    """
    __tablename__ = "shop_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # Решение: Чистый String(50) на стороне базы данных с Python-дефолтом
    item_type: Mapped[ShopItemType] = mapped_column(
        String(50),
        default=ShopItemType.ARTIFACT,
        nullable=False
    )

    # Новые RPG характеристики предметов
    weight: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generates_income: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    income_per_hour: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Привязка уникальных товаров к конкретной географической лавке
    market_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("quest_markets.id", ondelete="SET NULL"), 
        nullable=True,
        index=True
    )
    # Цена обратного выкупа скупщиком
    buyback_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Отношения
    promo_codes: Mapped[List["PromoCode"]] = relationship(
        "PromoCode",
        back_populates="shop_item",
        cascade="all, delete-orphan"
    )
    market: Mapped[Optional["QuestMarket"]] = relationship(
        "QuestMarket", 
        back_populates="unique_items"
    )


class PromoCode(Base):
    """
    Промокоды на реальные товары для магазина.
    """
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shop_item_id: Mapped[int] = mapped_column(
        ForeignKey("shop_items.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    used_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    shop_item: Mapped["ShopItem"] = relationship("ShopItem", back_populates="promo_codes")


class DailyRiddle(Base):
    """
    База ежедневных загадок на знание Перми.
    """
    __tablename__ = "daily_riddles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    correct_answer: Mapped[str] = mapped_column(String(200), nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=20, server_default="20")


class CheatLog(Base):
    """
    Логи подозрительной активности игроков (превышение скорости / прыжки GPS).
    """
    __tablename__ = "cheat_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.telegram_id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    quest_id: Mapped[int] = mapped_column(
        ForeignKey("quests.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    speed: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )


class ScheduledBroadcast(Base):
    """
    Модель отложенных рассылок для APScheduler.
    """
    __tablename__ = "scheduled_broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(4000), nullable=False)
    send_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=get_naive_utc,
        server_default=sa_text("TIMEZONE('utc', NOW())")
    )


# =========================================================================
# НОВЫЕ СТРУКТУРЫ ДАННЫХ ДЛЯ ДОПОЛНИТЕЛЬНЫХ ИГРОВЫХ RPG-СИСТЕМ
# =========================================================================

class QuestMarket(Base):
    """
    Модель географических рынков сбыта и лавок скупщиков в Перми.
    """
    __tablename__ = "quest_markets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, default=50.0, server_default="50.0")

    # Уникальные товары, закрепленные за конкретным рынком
    unique_items: Mapped[List["ShopItem"]] = relationship(
        "ShopItem", 
        back_populates="market",
        cascade="all, delete-orphan"
    )


class RandomEvent(Base):
    """
    Пул кастомизируемых случайных событий на маршрутах пешеходов.
    """
    __tablename__ = "random_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False) # 'merc', 'scroll', 'wallet' и т.д.
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False, default=25.0) # вероятность 0-100%
    coins_impact: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    karma_impact: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    xp_reward: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class GlobalEvent(Base):
    """
    Модель глобальных ивентов и временных контрактов (Bounty Hunting).
    """
    __tablename__ = "global_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


# =========================================================================
# МУЛЬТИГОРОД — СИСТЕМА ГОРОДОВ
# =========================================================================

class City(Base):
    """
    Модель города. Все квесты, лавки, события привязаны к конкретному городу.
    """
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    radius_km: Mapped[float] = mapped_column(Float, default=15.0, server_default="15.0")
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Yekaterinburg")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


# =========================================================================
# СЕЗОННЫЕ КВЕСТЫ
# =========================================================================

class Season(Base):
    """
    Модель игрового сезона с временными рамками и уникальными наградами.
    """
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    starts_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    bonus_xp_multiplier: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    bonus_coins_multiplier: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reward_item_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


# =========================================================================
# КООПЕРАТИВНЫЕ КВЕСТЫ
# =========================================================================

class CoopSession(Base):
    """
    Сессия кооперативного прохождения квеста группой игроков.
    """
    __tablename__ = "coop_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False, index=True)
    leader_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


class CoopMember(Base):
    """
    Участник кооперативной сессии.
    """
    __tablename__ = "coop_members"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("coop_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    joined_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )
    score_contribution: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


# =========================================================================
# PVP-ДУЭЛИ НА ЗНАНИЕ ГОРОДА
# =========================================================================

class PvPDuel(Base):
    """
    Модель PvP-дуэли между двумя игроками на знание города.
    """
    __tablename__ = "pvp_duels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    challenger_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    opponent_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="waiting", server_default="'waiting'")  # waiting, active, finished
    challenger_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    opponent_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    winner_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bet_coins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


class PvPQuestion(Base):
    """
    Банк вопросов для PvP-дуэлей, привязанных к городу.
    """
    __tablename__ = "pvp_questions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    correct_answer: Mapped[str] = mapped_column(String(200), nullable=False)
    wrong_answers: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)  # JSON array as string
    difficulty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # 1-easy, 2-medium, 3-hard


# =========================================================================
# СИСТЕМА ГИЛЬДИЙ / КЛАНОВ
# =========================================================================

class Guild(Base):
    """
    Модель гильдии (клана) игроков.
    """
    __tablename__ = "guilds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    leader_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    emblem_emoji: Mapped[str] = mapped_column(String(10), default="⚔️")
    level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    total_xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_members: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


class GuildMember(Base):
    """
    Связь игрока с гильдией.
    """
    __tablename__ = "guild_members"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guilds.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="member", server_default="'member'")  # leader, officer, member
    joined_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )
    contribution_xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_guild_member"),
    )


# =========================================================================
# КРАФТИНГ
# =========================================================================

class CraftRecipe(Base):
    """
    Рецепт крафта: какие предметы нужны и что получится.
    """
    __tablename__ = "craft_recipes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    result_item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ingredients: Mapped[str] = mapped_column(String(2000), nullable=False)  # JSON: [{"item_name": "...", "quantity": 1}, ...]
    coins_cost: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    min_level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)


# =========================================================================
# ЕЖЕДНЕВНЫЕ / ЕЖЕНЕДЕЛЬНЫЕ ЧЕЛЛЕНДЖИ
# =========================================================================

class Challenge(Base):
    """
    Модель челленджа (ежедневного или еженедельного задания).
    """
    __tablename__ = "challenges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    challenge_type: Mapped[str] = mapped_column(String(20), nullable=False)  # daily, weekly
    target_action: Mapped[str] = mapped_column(String(50), nullable=False)  # complete_quests, find_items, walk_distance, etc.
    target_value: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reward_xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reward_gems: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class UserChallenge(Base):
    """
    Прогресс игрока по конкретному челленджу.
    """
    __tablename__ = "user_challenges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    challenge_id: Mapped[int] = mapped_column(ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


# =========================================================================
# СИСТЕМА ОТЗЫВОВ НА КВЕСТЫ
# =========================================================================

class QuestReview(Base):
    """
    Отзыв игрока на пройденный квест.
    """
    __tablename__ = "quest_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5 stars
    comment: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )

    __table_args__ = (
        UniqueConstraint("user_id", "quest_id", name="uq_user_quest_review"),
    )


# =========================================================================
# ФОТО-ОТЧЁТЫ (АЛЬБОМ ПУТЕШЕСТВЕННИКА)
# =========================================================================

class PhotoReport(Base):
    """
    Фотография, сделанная игроком на локации квеста.
    """
    __tablename__ = "photo_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    quest_id: Mapped[Optional[int]] = mapped_column(ForeignKey("quests.id", ondelete="SET NULL"), nullable=True, index=True)
    step_id: Mapped[Optional[int]] = mapped_column(ForeignKey("steps.id", ondelete="SET NULL"), nullable=True)
    photo_file_id: Mapped[str] = mapped_column(String(250), nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


# =========================================================================
# СИСТЕМА ТИКЕТОВ ПОДДЕРЖКИ
# =========================================================================

class SupportTicket(Base):
    """
    Тикет обращения в поддержку от игрока.
    """
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(String(3000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="'open'")  # open, in_progress, closed
    admin_response: Mapped[Optional[str]] = mapped_column(String(3000), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


# =========================================================================
# ПРЕМИУМ-ВАЛЮТА (ГЕМЫ) — ПОКУПКА ЗА TELEGRAM STARS
# =========================================================================

class GemTransaction(Base):
    """
    Транзакция покупки/траты премиум-валюты (гемов).
    Гемы покупаются за Telegram Stars и тратятся на эксклюзивный контент.
    """
    __tablename__ = "gem_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # положительное = покупка, отрицательное = трата
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False)  # purchase, spend, reward
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    telegram_payment_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )


# =========================================================================
# AR-ЭЛЕМЕНТЫ (QR-КОДЫ НА ЗДАНИЯХ)
# =========================================================================

class ARMarker(Base):
    """
    AR-маркер (QR-код) размещённый на реальном здании/объекте.
    При сканировании даёт бонус или подсказку.
    """
    __tablename__ = "ar_markers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    city_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    reward_type: Mapped[str] = mapped_column(String(50), nullable=False)  # coins, xp, item, hint, gems
    reward_value: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reward_item_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    scan_limit: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # сколько раз один игрок может сканировать


class ARScanLog(Base):
    """
    Лог сканирований AR-маркеров игроками.
    """
    __tablename__ = "ar_scan_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    marker_id: Mapped[int] = mapped_column(ForeignKey("ar_markers.id", ondelete="CASCADE"), nullable=False, index=True)
    scanned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=get_naive_utc, server_default=sa_text("TIMEZONE('utc', NOW())")
    )
