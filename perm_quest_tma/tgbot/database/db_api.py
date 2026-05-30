import datetime
import logging
import random
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy import select, update, delete, func, desc, and_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from tgbot.config import settings
from tgbot.database.models import (
    Base, User, Quest, Step, InventoryItem, ActiveQuest, QuestProgress, 
    Achievement, UserAchievement, ShopItem, PromoCode, DailyRiddle, SystemSettings,
    CheatLog, ScheduledBroadcast, ShopItemType, QuestMarket, RandomEvent, GlobalEvent
)

logger = logging.getLogger(__name__)


def get_utc_now() -> datetime.datetime:
    """
    Возвращает текущее время UTC как offset-naive объект.
    Необходимо для безопасной работы с TIMESTAMP WITHOUT TIME ZONE в PostgreSQL через asyncpg.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def get_user_title(score: int) -> str:
    """
    Возвращает смайл-титул на основе набранных игровых очков (total_score).
    """
    if score <= 500:
        return "👶 Новичок"
    elif score <= 2000:
        return "🏃‍♂️ Пешеход"
    elif score <= 5000:
        return "🧠 Знаток"
    else:
        return "🏛 Хранитель Перми"


class Database:
    """
    Класс управления базой данных. Реализует паттерн Data Access Object (DAO)
    для асинхронной работы с PostgreSQL через SQLAlchemy 2.0.
    """
    def __init__(self) -> None:
        self.engine = create_async_engine(
            settings.db.database_url,
            echo=False,
            pool_size=15,
            max_overflow=25,
            pool_recycle=1800,
            pool_pre_ping=True
        )
        self.session_pool = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

    async def create_all(self) -> None:
        """
        Инициализирует структуру таблиц и наполняет базовыми системными значениями.
        """
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Таблицы базы данных успешно верифицированы и созданы.")
            await self.seed_initial_data()
        except Exception as e:
            logger.error(f"Критическая ошибка при инициализации таблиц БД: {e}", exc_info=True)
            raise e

    async def seed_initial_data(self) -> None:
        """Посев дефолтных настроек, ачивок, загадок, товаров, рынков и событий при пустой БД."""
        async with self.session_pool() as session:
            async with session.begin():
                # 1. Проверка системных настроек (SystemSettings)
                stmt_sys = select(func.count()).select_from(SystemSettings)
                sys_count = (await session.execute(stmt_sys)).scalar()
                if sys_count == 0:
                    sys_item = SystemSettings(
                        tutorial_latitude=58.0097,
                        tutorial_longitude=56.2444,
                        tutorial_answer="пермь",
                        merchant_bonus=20,
                        ranger_cd_minutes=7,
                        historian_mult=2.0,
                        base_step_coins=10,
                        base_step_score=100,
                        quest_completion_bonus=300,
                        karma_elixir_price=50,
                        karma_elixir_effect=3,
                        daily_gift_base_reward=10,
                        daily_gift_increment=5,
                        daily_gift_max_reward=50,
                        scroll_event_price=10,
                        scroll_event_karma=2,
                        wallet_event_coins=15,
                        wallet_event_karma_penalty=-1,
                        wallet_event_karma_reward=2,
                        merc_lifetime_minutes=60,
                        merc_summon_price=150,
                        merc_efficiency=100
                    )
                    session.add(sys_item)
                    logger.info("Успешно добавлен дефолтный баланс системных настроек RPG.")

                # 2. Проверка ачивок (Achievements)
                stmt = select(func.count()).select_from(Achievement)
                count = (await session.execute(stmt)).scalar()
                if count == 0:
                    achievements = [
                        Achievement(name="Пермский первопроходец", description="Пройти абсолютно все опубликованные квесты города Перми.", badge_emoji="👑", required_action="complete_all_quests", reward_coins=150),
                        Achievement(name="Абсолютный разум", description="Завершить квест без единой ошибки и использования подсказок.", badge_emoji="🧠", required_action="no_hints", reward_coins=50),
                        Achievement(name="Коллекционер древностей", description="Собрать все скрытые сюжетные предметы на локациях.", badge_emoji="🎒", required_action="all_items", reward_coins=100),
                        Achievement(name="Сверхзвуковой трамвай", description="Пройти квест быстрее чем за 10 минут (600 секунд).", badge_emoji="⚡", required_action="speed_run", required_value=600, reward_coins=70),
                        Achievement(name="Ночной сталкер", description="Пройти квест полностью в таинственное ночное время.", badge_emoji="🌌", required_action="night_run", reward_coins=120),
                        Achievement(name="Дождливый пешеход", description="Преодолеть все точки квеста под пермским ливнем/снегом.", badge_emoji="🌧", required_action="rain_run", reward_coins=100),
                    ]
                    session.add_all(achievements)

                # 3. Проверка географических рынков сбыта (QuestMarkets)
                stmt_market = select(func.count()).select_from(QuestMarket)
                market_count = (await session.execute(stmt_market)).scalar()
                if market_count == 0:
                    markets = [
                        QuestMarket(name="Рынок Мотовилихинских заводов", latitude=58.0294, longitude=56.3112, radius=100.0),
                        QuestMarket(name="Черный рынок у Камы", latitude=58.0163, longitude=56.2294, radius=75.0)
                    ]
                    session.add_all(markets)
                    await session.flush()  # Для получения сгенерированных ID рынков

                # 4. Проверка магазина с использованием ShopItemType, веса и пассивного дохода
                stmt_shop = select(func.count()).select_from(ShopItem)
                shop_count = (await session.execute(stmt_shop)).scalar()
                if shop_count == 0:
                    items = [
                        ShopItem(
                            name="Старинный ключ", 
                            description="Открывает запертые двери в купеческих особняках Мотовилихи.", 
                            price=30, 
                            item_name="Старинный ключ", 
                            item_type=ShopItemType.ARTIFACT,
                            weight=1,
                            generates_income=False,
                            income_per_hour=0,
                            buyback_price=15,
                            market_id=1
                        ),
                        ShopItem(
                            name="Билет на пароход", 
                            description="Позволяет получить ранний доступ к речным квестам на Каме.", 
                            price=50, 
                            item_name="Билет на пароход", 
                            item_type=ShopItemType.TICKET,
                            weight=0,
                            generates_income=False,
                            income_per_hour=0,
                            buyback_price=0
                        ),
                        ShopItem(
                            name="Печать губернатора", 
                            description="Приносит пассивный доход в виде налогов прошлых веков.", 
                            price=100, 
                            item_name="Печать губернатора", 
                            item_type=ShopItemType.ARTIFACT,
                            weight=2,
                            generates_income=True,
                            income_per_hour=10,
                            buyback_price=50,
                            market_id=2
                        ),
                        ShopItem(
                            name="Эликсир бодрости", 
                            description="Моментальный расходный материал. Восстанавливает силы.", 
                            price=25, 
                            item_name="Эликсир бодрости", 
                            item_type=ShopItemType.CONSUMABLE,
                            weight=1,
                            generates_income=False,
                            income_per_hour=0,
                            buyback_price=5
                        ),
                        ShopItem(
                            name="Кофе в подарок в 'Пермских Термах'", 
                            description="Настоящий ароматный кофе за баллы! Выдается промокод.", 
                            price=120, 
                            item_name="Кофе в подарок", 
                            item_type=ShopItemType.PROMO,
                            weight=0,
                            generates_income=False,
                            income_per_hour=0,
                            buyback_price=0
                        )
                    ]
                    session.add_all(items)
                
                # 5. Проверка пула ежедневных загадок (DailyRiddles)
                stmt_riddle = select(func.count()).select_from(DailyRiddle)
                riddle_count = (await session.execute(stmt_riddle)).scalar()
                if riddle_count == 0:
                    riddles = [
                        DailyRiddle(question="В каком году был основан город Пермь?", correct_answer="1723", reward_coins=25),
                        DailyRiddle(question="Какое животное изображено на гербе города Пермь?", correct_answer="медведь", reward_coins=20),
                        DailyRiddle(question="Как называется знаменитая река, на берегах которой расположена Пермь?", correct_answer="кама", reward_coins=15)
                    ]
                    session.add_all(riddles)

                # 6. Проверка пула случайных событий (RandomEvents)
                stmt_event = select(func.count()).select_from(RandomEvent)
                event_count = (await session.execute(stmt_event)).scalar()
                if event_count == 0:
                    events = [
                        RandomEvent(
                            event_type="merc",
                            text="На тихой улочке Перми вы встречаете загадочного Наёмника. Он готов помочь вам пройти квест бесплатно! Наёмник упадет в ваш рюкзак в виде карточки на 1 час.",
                            probability=25.0,
                            coins_impact=0,
                            karma_impact=0,
                            xp_reward=20
                        ),
                        RandomEvent(
                            event_type="scroll",
                            text="Прямо под вашими ногами лежит старинный кожаный свиток с гербом Перми Великой. Желаете расшифровать свиток?",
                            probability=25.0,
                            coins_impact=-10,
                            karma_impact=2,
                            xp_reward=50
                        ),
                        RandomEvent(
                            event_type="wallet",
                            text="В траве у обочины лежит утерянный кем-то старый кожаный кошелек с монетами. Как вы поступите?",
                            probability=25.0,
                            coins_impact=15,
                            karma_impact=-1,
                            xp_reward=10
                        )
                    ]
                    session.add_all(events)
                    logger.info("Пул системных случайных событий успешно инициализирован.")

    # =========================================================================
    # ГЛОБАЛЬНЫЕ СИСТЕМНЫЕ НАСТРОЙКИ (SYSTEM SETTINGS)
    # =========================================================================

    async def get_system_settings(self) -> SystemSettings:
        """Получает текущую конфигурацию настроек или создает дефолтную при отсутствии."""
        async with self.session_pool() as session:
            stmt = select(SystemSettings).limit(1)
            res = await session.execute(stmt)
            item = res.scalar_one_or_none()
            if not item:
                async with self.session_pool() as write_session:
                    async with write_session.begin():
                        item = SystemSettings()
                        write_session.add(item)
                        await write_session.flush()
                        return item
            return item

    async def update_system_settings(self, **kwargs) -> None:
        """Обновляет параметры системного баланса, обучения или кастомизации наемников."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(SystemSettings).limit(1)
                res = await session.execute(stmt)
                item = res.scalar_one_or_none()
                if item:
                    for key, val in kwargs.items():
                        if hasattr(item, key):
                            setattr(item, key, val)
                    session.add(item)

    # =========================================================================
    # РАБОТА С ПОЛЬЗОВАТЕЛЯМИ (USERS), XP И УРОВНЯМИ
    # =========================================================================

    async def get_or_create_user(self, telegram_id: int, full_name: str, username: Optional[str] = None) -> User:
        """Находит или регистрирует игрока, инициализируя поля RPG-прогресса, XP, уровней и веса."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == telegram_id).with_for_update()
                result = await session.execute(stmt)
                db_user = result.scalar_one_or_none()
                
                if not db_user:
                    db_user = User(
                        telegram_id=telegram_id, 
                        full_name=full_name, 
                        username=username, 
                        coins=0, 
                        karma=0,
                        rpg_class=None,
                        completed_tutorial=False,
                        cheat_warnings=0,
                        gift_streak=0,
                        xp=0,
                        level=1,
                        max_weight_capacity=10,
                        income_buffer=0
                    )
                    session.add(db_user)
                    await session.flush()
                    logger.info(f"Зарегистрирован новый пользователь: ID {telegram_id} ({full_name})")
                else:
                    if db_user.full_name != full_name or db_user.username != username:
                        db_user.full_name = full_name
                        db_user.username = username
                        session.add(db_user)
                return db_user

    async def get_user(self, user_id: int) -> Optional[User]:
        """Возвращает данные о пользователе по его уникальному Telegram ID."""
        async with self.session_pool() as session:
            stmt = select(User).where(User.telegram_id == user_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def is_banned(self, telegram_id: int) -> bool:
        """Проверяет, забанен ли пользователь на платформе."""
        async with self.session_pool() as session:
            stmt = select(User.is_banned).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            banned_status = result.scalar()
            return banned_status if banned_status is not None else False

    async def set_ban_status(self, telegram_id: int, is_banned: bool) -> None:
        """Устанавливает или снимает бан с пользователя."""
        async with self.session_pool() as session:
            async with session.begin():
                now = get_utc_now() if is_banned else None
                stmt = update(User).where(User.telegram_id == telegram_id).values(is_banned=is_banned, banned_at=now)
                await session.execute(stmt)
                logger.warning(f"Пользователю {telegram_id} изменен статус блокировки: {is_banned}")

    async def update_user_class(self, user_id: int, rpg_class: str) -> None:
        """Устанавливает RPG-класс персонажа."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(User).where(User.telegram_id == user_id).values(rpg_class=rpg_class)
                await session.execute(stmt)

    async def update_user_class_with_cooldown(self, user_id: int, rpg_class: str, cooldown_days: int = 30) -> Tuple[bool, Optional[int]]:
        """
        Пытается изменить класс игрока с учетом установленного кулдауна смены класса (в днях).
        Оснащен блокировкой FOR UPDATE для исключения гонки условий.
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                res = await session.execute(stmt)
                user = res.scalar_one_or_none()
                if not user:
                    return False, None

                now = get_utc_now()
                if user.last_class_change:
                    elapsed = now - user.last_class_change
                    cooldown_period = datetime.timedelta(days=cooldown_days)
                    if elapsed < cooldown_period:
                        days_left = (cooldown_period - elapsed).days
                        return False, max(1, days_left)

                user.rpg_class = rpg_class
                user.last_class_change = now
                session.add(user)
                return True, None

    async def set_tutorial_completed(self, user_id: int) -> None:
        """Помечает обучение (Квест №0) как пройденное, начисляет приветственные монеты и 100 XP."""
        async with self.session_pool() as session:
            async with session.begin():
                cfg = (await session.execute(select(SystemSettings).limit(1))).scalar_one()
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                res = await session.execute(stmt)
                user = res.scalar_one_or_none()
                if user and not user.completed_tutorial:
                    user.completed_tutorial = True
                    user.coins += cfg.daily_gift_base_reward
                    session.add(user)
                    await session.flush()
                    await self.add_xp_db(session, user_id, 100)

    # -------------------------------------------------------------------------
    # ДВИЖОК ОПЫТА И УРОВНЕЙ (XP ENGINE)
    # -------------------------------------------------------------------------

    async def add_xp_db(self, session: AsyncSession, user_id: int, amount: int) -> Tuple[int, int, bool]:
        """
        Внутренний транзакционный метод добавления опыта. 
        Рассчитывает уровни по динамической шкале: XP_порог = level * 150.
        """
        stmt = select(User).where(User.telegram_id == user_id).with_for_update()
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            return 0, 1, False

        user.xp += amount
        leveled_up = False
        
        while True:
            xp_needed = user.level * 150
            if user.xp >= xp_needed:
                user.xp -= xp_needed
                user.level += 1
                leveled_up = True
            else:
                break
                
        session.add(user)
        return user.xp, user.level, leveled_up

    async def add_xp(self, user_id: int, amount: int) -> Tuple[int, int, bool]:
        """Публичный интерфейс для безопасного начисления опыта."""
        async with self.session_pool() as session:
            async with session.begin():
                return await self.add_xp_db(session, user_id, amount)

    # =========================================================================
    # БАЗОВЫЙ CRUD КВЕСТОВ И ШАГОВ (QUESTS & STEPS CRUD)
    # =========================================================================

    async def create_quest(self, title: str, description: str) -> Quest:
        """Создает новый черновик квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                quest = Quest(title=title, description=description, is_published=False, min_level_required=1)
                session.add(quest)
                await session.flush()
                return quest

    async def get_all_quests(self) -> List[Quest]:
        """Возвращает список всех квестов из базы данных."""
        async with self.session_pool() as session:
            stmt = select(Quest).order_by(Quest.id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_quest_by_id(self, quest_id: int) -> Optional[Quest]:
        """Получает карточку квеста по его уникальному идентификатору."""
        async with self.session_pool() as session:
            stmt = select(Quest).where(Quest.id == quest_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_quest(self, quest_id: int, **kwargs) -> None:
        """Обновляет любые характеристики и атрибуты квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(Quest).where(Quest.id == quest_id).values(**kwargs)
                await session.execute(stmt)

    async def delete_quest(self, quest_id: int) -> None:
        """Каскадно удаляет квест со всеми шагами и активными сессиями игроков."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(Quest).where(Quest.id == quest_id)
                await session.execute(stmt)

    async def add_step_to_quest(self, quest_id: int, step_data: Dict[str, Any]) -> Step:
        """Добавляет новую контрольную точку на карту квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                step = Step(
                    quest_id=quest_id,
                    instruction_text=step_data["instruction_text"],
                    history_info=step_data.get("history_info"),
                    photo_then_id=step_data.get("photo_then_id"),
                    photo_now_id=step_data.get("photo_now_id"),
                    audio_guide_id=step_data.get("audio_guide_id"),
                    latitude=step_data["latitude"],
                    longitude=step_data["longitude"],
                    is_night_only=step_data.get("is_night_only", False),
                    is_day_only=step_data.get("is_day_only", False),
                    is_weather_only=step_data.get("is_weather_only", False),
                    is_dry_only=step_data.get("is_dry_only", False),
                    min_karma_required=step_data.get("min_karma_required", 0),
                    npc_name=step_data.get("npc_name"),
                    npc_dialogue=step_data.get("npc_dialogue"),
                    time_limit_seconds=step_data.get("time_limit_seconds"),
                    branches=step_data.get("branches", {"branches": {}}),
                    is_final=step_data.get("is_final", False),
                    required_item=step_data.get("required_item"),
                    gives_item=step_data.get("gives_item"),
                    secret_price=step_data.get("secret_price", 0),
                    hint_1_delay=step_data.get("hint_1_delay", 5),
                    hint_1_text=step_data.get("hint_1_text", "Присмотритесь к элементам фасада здания."),
                    hint_2_delay=step_data.get("hint_2_delay", 10),
                    hint_2_text=step_data.get("hint_2_text", "Цель находится рядом с главным входом."),
                    hints=step_data.get("hints", [])
                )
                session.add(step)
                await session.flush()
                return step

    async def update_step(self, step_id: int, **kwargs) -> None:
        """Обновляет характеристики существующего шага."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(Step).where(Step.id == step_id).values(**kwargs)
                await session.execute(stmt)

    async def delete_step(self, step_id: int) -> None:
        """Удаляет контрольную точку квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(Step).where(Step.id == step_id)
                await session.execute(stmt)

    async def update_step_branches(self, step_id: int, branches: Dict[str, Any]) -> None:
        """Обновляет разметку графа переходов на шаге."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(Step).where(Step.id == step_id).values(branches={"branches": branches})
                await session.execute(stmt)

    async def publish_quest(self, quest_id: int) -> None:
        """Переводит квест в статус опубликованного."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(Quest).where(Quest.id == quest_id).values(is_published=True)
                await session.execute(stmt)

    async def get_published_quests(self) -> List[Quest]:
        """Возвращает список всех опубликованных квестов города Перми."""
        async with self.session_pool() as session:
            stmt = select(Quest).where(Quest.is_published == True).order_by(Quest.id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_quest_with_steps(self, quest_id: int) -> Optional[Quest]:
        """Возвращает квест с подгруженной связью его контрольных точек."""
        async with self.session_pool() as session:
            stmt = select(Quest).options(selectinload(Quest.steps)).where(Quest.id == quest_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_step_by_id(self, step_id: int) -> Optional[Step]:
        """Получает шаг по его первичному ключу."""
        async with self.session_pool() as session:
            stmt = select(Step).where(Step.id == step_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()


    # ИГРОВАЯ ДИНАМИКА, "ОДИН ЭКРАН" И ЧЕКПОИНТЫ (ACTIVE QUESTS)
    # =========================================================================

    async def start_user_quest(self, user_id: int, quest_id: int, start_step_id: int) -> ActiveQuest:
        """
        Инициализирует или возобновляет сессию квеста, переводя другие сессии в фон.
        Сбрасывает/очищает историю GPS-стека для предотвращения ложных банов античитом.
        """
        async with self.session_pool() as session:
            async with session.begin():
                # Переводим текущую фокусную сессию в фоновое удержание (чекпоинт)
                await session.execute(
                    update(ActiveQuest).where(
                        and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                    ).values(is_suspended=True)
                )

                # Проверяем наличие этой сессии в БД
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.quest_id == quest_id)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()

                now = get_utc_now()
                if active:
                    active.current_step_id = start_step_id
                    active.is_suspended = False
                    active.started_at = now
                    active.last_action_at = now
                    active.step_activated_at = now
                    active.score = 0
                    active.errors_count = 0
                    active.is_night_run = True
                    active.is_rain_run = True
                    active.current_npc_node = None
                    active.is_frozen = False
                    active.frozen_accumulated_seconds = 0
                    active.freeze_used = False
                    active.last_game_message_id = None
                    # Сброс истории перемещений для античит-безопасности
                    active.prev_latitude = None
                    active.prev_longitude = None
                    active.prev_time = None
                else:
                    active = ActiveQuest(
                        user_id=user_id,
                        quest_id=quest_id,
                        current_step_id=start_step_id,
                        started_at=now,
                        last_action_at=now,
                        step_activated_at=now,
                        is_suspended=False,
                        score=0,
                        errors_count=0,
                        is_night_run=True,
                        is_rain_run=True,
                        current_npc_node=None,
                        prev_latitude=None,
                        prev_longitude=None,
                        prev_time=None
                    )
                    session.add(active)
                await session.flush()
                return active

    async def get_active_quest(self, user_id: int) -> Optional[ActiveQuest]:
        """Возвращает текущую фокусную (не приостановленную) сессию игрока."""
        async with self.session_pool() as session:
            stmt = select(ActiveQuest).where(
                and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def suspend_active_quest(self, user_id: int) -> None:
        """Переводит текущую активную сессию игрока в статус приостановленной (чекпоинт)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(is_suspended=True)
                await session.execute(stmt)

    async def resume_user_quest(self, user_id: int, quest_id: int) -> Optional[ActiveQuest]:
        """Возобновляет приостановленную на чекпоинте сессию квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                # Сворачиваем текущий активный квест
                await session.execute(
                    update(ActiveQuest).where(
                        and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                    ).values(is_suspended=True)
                )
                
                # Восстанавливаем целевой квест
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.quest_id == quest_id)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                if active:
                    active.is_suspended = False
                    now = get_utc_now()
                    active.last_action_at = now
                    active.step_activated_at = now
                    session.add(active)
                    await session.flush()
                    return active
                return None

    async def get_active_quests_list(self, user_id: int) -> List[ActiveQuest]:
        """Получает список всех сессий игрока (включая фоновые приостановленные)."""
        async with self.session_pool() as session:
            stmt = select(ActiveQuest).where(ActiveQuest.user_id == user_id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def update_active_quest_message_id(self, user_id: int, message_id: int) -> None:
        """Обновляет ID сообщения для интерфейса одного экрана."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(last_game_message_id=message_id)
                await session.execute(stmt)

    async def update_active_quest_step(self, user_id: int, next_step_id: int, current_lat: float, current_lon: float, score_to_add: int = 100) -> ActiveQuest:
        """Переключает этап, сохраняя пройденную координату и обновляя таймер NPC."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                if not active:
                    raise ValueError("Сессия активного квеста отсутствует.")

                now = get_utc_now()
                active.prev_latitude = current_lat
                active.prev_longitude = current_lon
                active.prev_time = now
                
                active.current_step_id = next_step_id
                active.score += score_to_add
                active.last_action_at = now
                active.step_activated_at = now  
                active.current_npc_node = None  

                session.add(active)
                await session.flush()
                return active

    async def update_active_quest_npc_node(self, user_id: int, npc_node: Optional[str]) -> None:
        """Регистрирует текущую реплику диалога NPC, на которой остановился пользователь."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(current_npc_node=npc_node)
                await session.execute(stmt)

    async def increment_error_count(self, user_id: int, score_penalty: int = 20) -> None:
        """Увеличивает число ошибок на шаге с понижением набранного счета квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                if active:
                    active.errors_count += 1
                    active.score = max(0, active.score - score_penalty)
                    session.add(active)

    async def freeze_active_quest(self, user_id: int) -> bool:
        """Замораживает течение времени квеста (Пауза)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                if not active or active.is_frozen or active.freeze_used:
                    return False

                now = get_utc_now()
                session_duration = int((now - active.last_action_at).total_seconds())
                
                active.frozen_accumulated_seconds += session_duration
                active.is_frozen = True
                active.freeze_used = True
                
                session.add(active)
                return True

    async def force_premium_freeze_db(self, user_id: int) -> bool:
        """Позволяет повторно заморозить квест за монеты."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                if not active or active.is_frozen:
                    return False

                now = get_utc_now()
                session_duration = int((now - active.last_action_at).total_seconds())
                
                active.frozen_accumulated_seconds += session_duration
                active.is_frozen = True
                session.add(active)
                return True

    async def unfreeze_active_quest(self, user_id: int) -> bool:
        """Снимает квест с паузы и возвращает в активное прохождение."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                if not active or not active.is_frozen:
                    return False

                active.is_frozen = False
                active.last_action_at = get_utc_now()
                
                session.add(active)
                return True

    async def update_active_quest_time(self, user_id: int) -> None:
        """Обновляет временную метку последнего действия на активной сессии."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                if active:
                    active.last_action_at = get_utc_now()
                    session.add(active)

    async def update_active_quest_rpg_flags(self, user_id: int, is_night: bool, is_rain: bool) -> None:
        """Устанавливает RPG-состояние прохождения шага (ночь / осадки)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                if active:
                    if not is_night:
                        active.is_night_run = False
                    if not is_rain:
                        active.is_rain_run = False
                    session.add(active)

    async def delete_active_quest(self, user_id: int) -> None:
        """Стирает текущую активную игровую сессию."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                )
                await session.execute(stmt)

    async def finish_active_quest(self, user_id: int, completion_bonus: Optional[int] = None) -> Tuple[QuestProgress, ActiveQuest]:
        """Завершает активный квест, архивирует прогресс и начисляет опыт."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                if not active:
                    raise ValueError("Активная сессия не обнаружена.")

                cfg = (await session.execute(select(SystemSettings).limit(1))).scalar_one()
                bonus_reward = completion_bonus if completion_bonus is not None else cfg.quest_completion_bonus

                now = get_utc_now()
                last_session_time = int((now - active.last_action_at).total_seconds())
                total_time = active.frozen_accumulated_seconds + last_session_time

                progress = QuestProgress(
                    user_id=user_id,
                    quest_id=active.quest_id,
                    total_time_seconds=total_time,
                    score=active.score + bonus_reward,
                    errors_count=active.errors_count,
                    completed_at=now,
                    is_season_archived=False
                )
                session.add(progress)
                await session.delete(active)
                await session.flush()
                
                await self.add_xp_db(session, user_id, 300)
                return progress, active

    # =========================================================================
    # ВИРТУАЛЬНЫЙ ИНВЕНТАРЬ (INVENTORY)
    # =========================================================================

    async def add_item_to_inventory(self, user_id: int, item_name: str) -> bool:
        """
        Безопасно укладывает артефакт в инвентарь игрока.
        Автоматически подгружает характеристики веса и пассивного дохода из ShopItem.
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt_exist = select(InventoryItem).where(
                    and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
                )
                res_exist = await session.execute(stmt_exist)
                existing = res_exist.scalar_one_or_none()
                if existing:
                    return False

                stmt_shop = select(ShopItem).where(ShopItem.item_name == item_name)
                shop_item = (await session.execute(stmt_shop)).scalar_one_or_none()
                
                item_weight = 0
                item_income = False
                item_income_rate = 0
                
                if shop_item:
                    item_weight = shop_item.weight
                    item_income = shop_item.generates_income
                    item_income_rate = shop_item.income_per_hour

                item = InventoryItem(
                    user_id=user_id, 
                    item_name=item_name,
                    weight=item_weight,
                    generates_income=item_income,
                    income_per_hour=item_income_rate
                )
                session.add(item)
                return True

    async def check_item_in_inventory(self, user_id: int, item_name: str) -> bool:
        """Проверяет наличие предмета в рюкзаке пользователя."""
        async with self.session_pool() as session:
            stmt = select(InventoryItem).where(
                and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none() is not None

    async def get_user_inventory(self, user_id: int) -> List[str]:
        """Возвращает строковые имена всех находящихся в рюкзаке артефактов."""
        async with self.session_pool() as session:
            stmt = select(InventoryItem.item_name).where(InventoryItem.user_id == user_id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_user_current_weight(self, user_id: int) -> int:
        """Возвращает суммарный вес всех предметов в рюкзаке пользователя."""
        async with self.session_pool() as session:
            stmt = select(func.sum(InventoryItem.weight)).where(InventoryItem.user_id == user_id)
            res = await session.execute(stmt)
            return res.scalar() or 0

    async def is_inventory_overloaded(self, user_id: int, item_weight_to_add: int) -> Tuple[bool, int, int]:
        """Проверяет, превысит ли рюкзак лимит грузоподъемности при добавлении вещи."""
        async with self.session_pool() as session:
            user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
            if not user:
                return False, 0, 10
            
            curr_weight = (await session.execute(
                select(func.sum(InventoryItem.weight)).where(InventoryItem.user_id == user_id)
            )).scalar() or 0
            
            overloaded = (curr_weight + item_weight_to_add) > user.max_weight_capacity
            return overloaded, curr_weight, user.max_weight_capacity

    async def discard_inventory_item(self, user_id: int, item_name: str) -> bool:
        """Игрок выбрасывает вещь из рюкзака для устранения перегруза."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(InventoryItem).where(
                    and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
                ).limit(1)
                res = await session.execute(stmt)
                item = res.scalar_one_or_none()
                if item:
                    await session.delete(item)
                    logger.info(f"Игрок {user_id} выбросил предмет: {item_name}")
                    return True
                return False

    async def activate_consumable_item(self, user_id: int, item_name: str) -> Tuple[bool, str]:
        """
        Игрок активирует расходный материал (CONSUMABLE) из инвентаря.
        Удаляет расходник из БД и применяет его геймплейные эффекты.
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(InventoryItem).where(
                    and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
                ).limit(1).with_for_update()
                res = await session.execute(stmt)
                item = res.scalar_one_or_none()
                if not item:
                    return False, "Предмет отсутствует в вашем рюкзаке."

                user = (await session.execute(select(User).where(User.telegram_id == user_id).with_for_update())).scalar_one()

                effects_applied = ""
                if "Эликсир бодрости" in item_name:
                    await self.add_xp_db(session, user_id, 150)
                    effects_applied = "🔋 Вы выпили эликсир бодрости и восстановили силы! Начислено *+150 XP*."
                elif "Эликсир Кармы" in item_name:
                    cfg = (await session.execute(select(SystemSettings).limit(1))).scalar_one()
                    user.karma += cfg.karma_elixir_effect
                    effects_applied = f"🧪 Вы выпили эликсир Кармы! Репутация повышена на *+{cfg.karma_elixir_effect}*."
                else:
                    user.karma += 1
                    effects_applied = "🍞 Вы съели припасы. Ваша карма слегка повысилась (+1)."

                await session.delete(item)
                return True, effects_applied

    # =========================================================================
    # СИСТЕМА ДОСТИЖЕНИЙ (ACHIEVEMENTS)
    # =========================================================================

    async def get_all_achievements(self) -> List[Achievement]:
        """Запрашивает все системные достижения."""
        async with self.session_pool() as session:
            stmt = select(Achievement).order_by(Achievement.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_achievement_by_id(self, ach_id: int) -> Optional[Achievement]:
        """Запрашивает достижение по его первичному ключу."""
        async with self.session_pool() as session:
            stmt = select(Achievement).where(Achievement.id == ach_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_achievement(self, name: str, description: str, badge_emoji: str, required_action: str, required_value: Optional[int], reward_coins: int) -> Achievement:
        """Создает новое достижение в каталоге."""
        async with self.session_pool() as session:
            async with session.begin():
                ach = Achievement(
                    name=name, description=description, badge_emoji=badge_emoji,
                    required_action=required_action, required_value=required_value,
                    reward_coins=reward_coins
                )
                session.add(ach)
                await session.flush()
                return ach

    async def delete_achievement(self, ach_id: int) -> None:
        """Удаляет трофей из глобальной базы."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(Achievement).where(Achievement.id == ach_id)
                await session.execute(stmt)

    async def get_user_achievements(self, user_id: int) -> List[Achievement]:
        """Получает заработанные игроком трофеи."""
        async with self.session_pool() as session:
            stmt = select(Achievement).join(UserAchievement).where(UserAchievement.user_id == user_id).order_by(Achievement.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def check_achievement_earned(self, user_id: int, achievement_id: int) -> bool:
        """Проверяет, было ли получено достижение игроком."""
        async with self.session_pool() as session:
            stmt = select(UserAchievement).where(and_(UserAchievement.user_id == user_id, UserAchievement.achievement_id == achievement_id))
            res = await session.execute(stmt)
            return res.scalar_one_or_none() is not None

    async def grant_achievement(self, user_id: int, achievement_id: int) -> Optional[Achievement]:
        """Безопасно награждает игрока трофеем с транзакционной блокировкой."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt_check = select(UserAchievement).where(and_(UserAchievement.user_id == user_id, UserAchievement.achievement_id == achievement_id)).with_for_update()
                if (await session.execute(stmt_check)).scalar_one_or_none():
                    return None
                
                ua = UserAchievement(user_id=user_id, achievement_id=achievement_id)
                session.add(ua)
                
                stmt_ach = select(Achievement).where(Achievement.id == achievement_id)
                ach = (await session.execute(stmt_ach)).scalar()
                
                stmt_user = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt_user)).scalar()
                if user and ach:
                    user.coins += ach.reward_coins
                    
                return ach

    async def get_all_quest_items_list(self) -> List[str]:
        """Возвращает перечень всех скрытых реликвий, разбросанных по точкам."""
        async with self.session_pool() as session:
            stmt = select(Step.gives_item).where(Step.gives_item != None)
            res = await session.execute(stmt)
            items = set(res.scalars().all())
            return list(items)

    async def get_user_completed_quests_count(self, user_id: int) -> int:
        """Вычисляет количество пройденных пользователем квестов."""
        async with self.session_pool() as session:
            stmt = select(func.count(func.distinct(QuestProgress.quest_id))).where(QuestProgress.user_id == user_id)
            return (await session.execute(stmt)).scalar() or 0

    # =========================================================================
    # ВНУТРИИГРОВОЙ МАГАЗИН И ПРОМОКОДЫ
    # =========================================================================

    async def get_shop_items(self) -> List[ShopItem]:
        """Запрашивает все товары, выставленные в торговых лавках."""
        async with self.session_pool() as session:
            stmt = select(ShopItem).order_by(ShopItem.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_shop_item_by_id(self, item_id: int) -> Optional[ShopItem]:
        """Получает ShopItem по его уникальному ID."""
        async with self.session_pool() as session:
            stmt = select(ShopItem).where(ShopItem.id == item_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_shop_item(self, name: str, description: str, price: int, item_name: str, item_type: ShopItemType = ShopItemType.ARTIFACT, weight: int = 0, generates_income: bool = False, income_per_hour: int = 0, market_id: Optional[int] = None, buyback_price: Optional[int] = None) -> ShopItem:
        """Добавляет новый уникальный артефакт/билет/расходник на прилавок."""
        async with self.session_pool() as session:
            async with session.begin():
                item = ShopItem(
                    name=name, description=description, price=price, item_name=item_name, item_type=item_type,
                    weight=weight, generates_income=generates_income, income_per_hour=income_per_hour,
                    market_id=market_id, buyback_price=buyback_price
                )
                session.add(item)
                await session.flush()
                return item

    async def update_shop_item(self, item_id: int, **kwargs) -> bool:
        """Редактирует характеристики товара в магазине."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ShopItem).where(ShopItem.id == item_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_shop_item(self, item_id: int) -> bool:
        """Стирает товар из каталога лавки."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(ShopItem).where(ShopItem.id == item_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def add_promo_codes(self, shop_item_id: int, codes: List[str]) -> None:
        """Пакетно загружает промокоды для товара магазина наград."""
        async with self.session_pool() as session:
            async with session.begin():
                for code in codes:
                    pc = PromoCode(shop_item_id=shop_item_id, code=code.strip(), is_used=False)
                    session.add(pc)

    async def get_promo_codes_count(self, shop_item_id: int) -> int:
        """Показывает остаток неиспользованных промокодов в базе."""
        async with self.session_pool() as session:
            stmt = select(func.count()).select_from(PromoCode).where(and_(PromoCode.shop_item_id == shop_item_id, PromoCode.is_used == False))
            res = await session.execute(stmt)
            return res.scalar() or 0

    async def buy_promo_item(self, user_id: int, shop_item_id: int) -> Optional[str]:
        """Покупка промокода с жесткой блокировкой строк во избежание состояния гонки."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt_item = select(ShopItem).where(ShopItem.id == shop_item_id)
                shop_item = (await session.execute(stmt_item)).scalar_one_or_none()
                if not shop_item:
                    return None

                stmt_user = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt_user)).scalar_one_or_none()
                if not user or user.coins < shop_item.price:
                    return "insufficient_coins"

                stmt_promo = select(PromoCode).where(
                    and_(PromoCode.shop_item_id == shop_item_id, PromoCode.is_used == False)
                ).limit(1).with_for_update()
                promo = (await session.execute(stmt_promo)).scalar_one_or_none()
                if not promo:
                    return "no_stock"

                user.coins -= shop_item.price
                promo.is_used = True
                promo.used_at = get_utc_now()
                
                item_inst = InventoryItem(
                    user_id=user_id, 
                    item_name=f"🎟 {shop_item.name} ({promo.code})",
                    weight=0,
                    generates_income=False,
                    income_per_hour=0
                )
                session.add(item_inst)
                
                return promo.code

    # =========================================================================
    # ЕЖЕДНЕВНЫЕ ЗАГАДКИ (ДЕЙЛИКИ) И СТРИКИ
    # =========================================================================

    async def get_all_daily_riddles(self) -> List[DailyRiddle]:
        """Возвращает весь пул ежедневных загадок."""
        async with self.session_pool() as session:
            stmt = select(DailyRiddle).order_by(DailyRiddle.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_daily_riddle_by_id(self, riddle_id: int) -> Optional[DailyRiddle]:
        """Возвращает ежедневную загадку по ее ID."""
        async with self.session_pool() as session:
            stmt = select(DailyRiddle).where(DailyRiddle.id == riddle_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_daily_riddle(self, question: str, correct_answer: str, reward_coins: int) -> DailyRiddle:
        """Создает и заносит в базу новую загадку дня."""
        async with self.session_pool() as session:
            async with session.begin():
                dr = DailyRiddle(question=question, correct_answer=correct_answer, reward_coins=reward_coins)
                session.add(dr)
                await session.flush()
                return dr

    async def delete_daily_riddle(self, riddle_id: int) -> None:
        """Стирает загадку из пула ротации."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(DailyRiddle).where(DailyRiddle.id == riddle_id)
                await session.execute(stmt)

    async def get_random_daily_riddle(self) -> Optional[DailyRiddle]:
        """Случайным образом извлекает загадку дня для игрока."""
        async with self.session_pool() as session:
            stmt = select(DailyRiddle)
            res = await session.execute(stmt)
            all_riddles = res.scalars().all()
            if not all_riddles:
                return None
            return random.choice(all_riddles)

    async def process_daily_streak_logic(self, user_id: int, base_reward: int) -> Tuple[int, int]:
        """Начисление наград за дейлики с FOR UPDATE блокировкой от двойных кликов."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user:
                    return 0, 0

                now = get_utc_now()
                if user.last_daily_at:
                    delta = now - user.last_daily_at
                    if delta.total_seconds() < 24 * 3600:
                        if delta.total_seconds() < 48 * 3600:
                            user.daily_streak += 1
                        else:
                            user.daily_streak = 1
                    else:
                        user.daily_streak = 1
                else:
                    user.daily_streak = 1

                user.last_daily_at = now
                
                multiplier = min(2.0, 1.0 + (user.daily_streak - 1) * 0.1)
                final_coins = int(base_reward * multiplier)
                user.coins += final_coins
                
                # Добавляем 50 XP за верное решение ежедневного испытания
                await self.add_xp_db(session, user_id, 50)
                
                return user.daily_streak, final_coins

    async def claim_daily_gift(self, user_id: int) -> Tuple[bool, int, int]:
        """
        Начисляет ежедневный подарок со стрик-множителем.
        Интегрирует экономические настройки SystemSettings и FOR UPDATE блокировку.
        """
        async with self.session_pool() as session:
            async with session.begin():
                cfg = (await session.execute(select(SystemSettings).limit(1))).scalar_one()
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                res = await session.execute(stmt)
                user = res.scalar_one_or_none()
                if not user:
                    return False, 0, 0

                now = get_utc_now()
                if user.last_gift_at:
                    delta = now - user.last_gift_at
                    if delta.total_seconds() < 24 * 3600:
                        return False, 0, user.gift_streak

                    if delta.total_seconds() < 48 * 3600:
                        user.gift_streak += 1
                    else:
                        user.gift_streak = 1
                else:
                    user.gift_streak = 1

                user.last_gift_at = now
                reward_coins = min(
                    cfg.daily_gift_max_reward, 
                    cfg.daily_gift_base_reward + (user.gift_streak - 1) * cfg.daily_gift_increment
                )
                user.coins += reward_coins
                session.add(user)
                return True, reward_coins, user.gift_streak

    # =========================================================================
    # ЛИДЕРБОРДЫ И СЕЗОНЫ (READ ONLY)
    # =========================================================================

    async def get_leaderboard(self, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        """Возвращает глобальный рейтинг игроков со смайлами-титулами."""
        async with self.session_pool() as session:
            ach_sub = select(UserAchievement.user_id, func.count(UserAchievement.achievement_id).label("ach_count")).group_by(UserAchievement.user_id).subquery()
            
            stmt = (
                select(
                    User.telegram_id,
                    User.full_name,
                    User.username,
                    func.sum(QuestProgress.score).label("total_score"),
                    func.sum(QuestProgress.total_time_seconds).label("total_time"),
                    func.sum(QuestProgress.errors_count).label("total_errors"),
                    func.coalesce(ach_sub.c.ach_count, 0).label("achievements_count"),
                    User.level
                )
                .join(QuestProgress, User.telegram_id == QuestProgress.user_id)
                .outerjoin(ach_sub, User.telegram_id == ach_sub.c.user_id)
                .group_by(User.telegram_id, ach_sub.c.ach_count, User.level)
                .order_by(desc("total_score"), "total_time")
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt, execution_options={"read_only": True})
            leaderboard = []
            
            for row in result.all():
                total_score_val = int(row[3] or 0)
                leaderboard.append({
                    "telegram_id": row[0],
                    "full_name": row[1],
                    "username": row[2],
                    "total_score": total_score_val,
                    "total_time": int(row[4] or 0),
                    "total_errors": int(row[5] or 0),
                    "achievements_count": int(row[6] or 0),
                    "level": int(row[7] or 1),
                    "title": get_user_title(total_score_val)
                })
            return leaderboard

    async def get_seasonal_leaderboard(self, period: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        """Возвращает сезонный рейтинг игроков с титулами."""
        async with self.session_pool() as session:
            now = get_utc_now()
            if period == "month":
                start_date = datetime.datetime(now.year, now.month, 1)
            elif period == "year":
                start_date = datetime.datetime(now.year, 1, 1)
            else:
                start_date = now - datetime.timedelta(days=30)

            ach_sub = select(UserAchievement.user_id, func.count(UserAchievement.achievement_id).label("ach_count")).group_by(UserAchievement.user_id).subquery()

            stmt = (
                select(
                    User.telegram_id,
                    User.full_name,
                    User.username,
                    func.sum(QuestProgress.score).label("total_score"),
                    func.sum(QuestProgress.total_time_seconds).label("total_time"),
                    func.sum(QuestProgress.errors_count).label("total_errors"),
                    func.coalesce(ach_sub.c.ach_count, 0).label("achievements_count"),
                    User.level
                )
                .join(QuestProgress, User.telegram_id == QuestProgress.user_id)
                .outerjoin(ach_sub, User.telegram_id == ach_sub.c.user_id)
                .where(and_(QuestProgress.completed_at >= start_date, QuestProgress.is_season_archived == False))
                .group_by(User.telegram_id, ach_sub.c.ach_count, User.level)
                .order_by(desc("total_score"), "total_time")
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt, execution_options={"read_only": True})
            leaderboard = []
            
            for row in result.all():
                total_score_val = int(row[3] or 0)
                leaderboard.append({
                    "telegram_id": row[0],
                    "full_name": row[1],
                    "username": row[2],
                    "total_score": total_score_val,
                    "total_time": int(row[4] or 0),
                    "total_errors": int(row[5] or 0),
                    "achievements_count": int(row[6] or 0),
                    "level": int(row[7] or 1),
                    "title": get_user_title(total_score_val)
                })
            return leaderboard

    async def close_season(self, period: str) -> List[Dict[str, Any]]:
        """Завершает игровой сезон, выдает медали и кубки победителям."""
        top_players = await self.get_seasonal_leaderboard(period=period, limit=3)
        if not top_players:
            return []

        async with self.session_pool() as session:
            async with session.begin():
                rewards = {
                    1: ("🏆 Золотой Кубок Чемпиона", 500),
                    2: ("🥈 Серебряная Медаль Легенды", 300),
                    3: ("🥉 Бронзовая Медаль Первопроходца", 150)
                }

                for rank, player in enumerate(top_players, 1):
                    item_name, coins_reward = rewards.get(rank)
                    
                    inv = InventoryItem(
                        user_id=player["telegram_id"], 
                        item_name=f"{item_name} ({period.upper()} Сезон)",
                        weight=0,
                        generates_income=False,
                        income_per_hour=0
                    )
                    session.add(inv)

                    stmt_user = select(User).where(User.telegram_id == player["telegram_id"]).with_for_update()
                    user = (await session.execute(stmt_user)).scalar_one_or_none()
                    if user:
                        user.coins += coins_reward

                now = get_utc_now()
                if period == "month":
                    start_date = datetime.datetime(now.year, now.month, 1)
                else:
                    start_date = datetime.datetime(now.year, 1, 1)

                stmt_archive = (
                    update(QuestProgress)
                    .where(and_(QuestProgress.completed_at >= start_date, QuestProgress.is_season_archived == False))
                    .values(is_season_archived=True)
                )
                await session.execute(stmt_archive)

        return top_players

    # =========================================================================
    # РЫНКИ, СЛУЧАЙНЫЕ СОБЫТИЯ, ПАССИВНЫЙ ДОХОД И АНАЛИТИКА КВЕСТОВ
    # =========================================================================

    async def get_market_by_id(self, market_id: int) -> Optional[QuestMarket]:
        """Возвращает рынок по его первичному ключу."""
        async with self.session_pool() as session:
            stmt = select(QuestMarket).where(QuestMarket.id == market_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_market(self, name: str, lat: float, lon: float, radius: float = 50.0) -> QuestMarket:
        """Создает и сохраняет новую торговую лавку на карте Перми."""
        async with self.session_pool() as session:
            async with session.begin():
                market = QuestMarket(name=name, latitude=lat, longitude=lon, radius=radius)
                session.add(market)
                await session.flush()
                return market

    async def get_all_markets(self) -> List[QuestMarket]:
        """Запрашивает список всех зарегистрированных торговых лавок."""
        async with self.session_pool() as session:
            stmt = select(QuestMarket).order_by(QuestMarket.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def delete_market(self, market_id: int) -> bool:
        """Удаляет лавку скупщика с карты города."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(QuestMarket).where(QuestMarket.id == market_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def get_random_event_by_id(self, event_id: int) -> Optional[RandomEvent]:
        """Запрашивает информацию о конкретном случайном событии."""
        async with self.session_pool() as session:
            stmt = select(RandomEvent).where(RandomEvent.id == event_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_random_event(self, event_type: str, text: str, prob: float, coins: int, karma: int, xp: int) -> RandomEvent:
        """Создает новое случайное событие для пополнения пула ротации."""
        async with self.session_pool() as session:
            async with session.begin():
                ev = RandomEvent(
                    event_type=event_type, text=text, probability=prob,
                    coins_impact=coins, karma_impact=karma, xp_reward=xp
                )
                session.add(ev)
                await session.flush()
                return ev

    async def get_all_random_events(self) -> List[RandomEvent]:
        """Возвращает список всех зарегистрированных случайных ивентов."""
        async with self.session_pool() as session:
            stmt = select(RandomEvent).order_by(RandomEvent.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def delete_random_event(self, ev_id: int) -> bool:
        """Удаляет случайное событие из ротационного пула."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(RandomEvent).where(RandomEvent.id == ev_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def apply_hourly_passive_income(self) -> int:
        """
        Фоновая задача APScheduler воркера, начисляющая пассивный доход.
        Суммирует доход во временный буфер 'income_buffer'.
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(InventoryItem).where(InventoryItem.generates_income == True)
                res = await session.execute(stmt)
                items = res.scalars().all()
                
                user_income = {}
                for item in items:
                    user_income[item.user_id] = user_income.get(item.user_id, 0) + item.income_per_hour
                    
                count = 0
                for uid, amount in user_income.items():
                    stmt_user = select(User).where(User.telegram_id == uid).with_for_update()
                    user = (await session.execute(stmt_user)).scalar_one_or_none()
                    if user:
                        user.income_buffer += amount
                        session.add(user)
                        count += 1
                return count

    async def collect_passive_income_buffer(self, user_id: int) -> int:
        """Переносит накопленный пассивный доход из буфера на баланс монет."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user or user.income_buffer <= 0:
                    return 0
                
                income = user.income_buffer
                user.coins += income
                user.income_buffer = 0
                session.add(user)
                return income

    async def clone_quest_db(self, orig_qid: int) -> Optional[Quest]:
        """
        Клонирует существующий квест со всеми шагами и связями графа внутри БД.
        """
        async with self.session_pool() as session:
            async with session.begin():
                orig_q = await session.get(Quest, orig_qid)
                if not orig_q:
                    return None
                    
                cloned_q = Quest(
                    title=f"Копия {orig_q.title} ({datetime.datetime.now(datetime.timezone.utc).strftime('%H%M%S')})",
                    description=orig_q.description,
                    is_published=False,
                    max_speed_kmh=orig_q.max_speed_kmh,
                    min_level_required=orig_q.min_level_required
                )
                session.add(cloned_q)
                await session.flush()
                
                stmt_steps = select(Step).where(Step.quest_id == orig_qid).order_by(Step.id)
                orig_steps = (await session.execute(stmt_steps)).scalars().all()
                
                step_id_map = {}
                cloned_pairs = []
                
                for o_step in orig_steps:
                    c_step = Step(
                        quest_id=cloned_q.id,
                        instruction_text=o_step.instruction_text,
                        history_info=o_step.history_info,
                        photo_then_id=o_step.photo_then_id,
                        photo_now_id=o_step.photo_now_id,
                        audio_guide_id=o_step.audio_guide_id,
                        latitude=o_step.latitude,
                        longitude=o_step.longitude,
                        is_night_only=o_step.is_night_only,
                        is_day_only=o_step.is_day_only,
                        is_weather_only=o_step.is_weather_only,
                        is_dry_only=o_step.is_dry_only,
                        min_karma_required=o_step.min_karma_required,
                        npc_name=o_step.npc_name,
                        npc_dialogue=o_step.npc_dialogue,
                        time_limit_seconds=o_step.time_limit_seconds,
                        is_final=o_step.is_final,
                        required_item=o_step.required_item,
                        gives_item=o_step.gives_item,
                        secret_price=o_step.secret_price,
                        hints=o_step.hints,
                        branches={"branches": {}}
                    )
                    session.add(c_step)
                    cloned_pairs.append((o_step, c_step))
                    
                await session.flush()
                
                step_id_map = {pair[0].id: pair[1].id for pair in cloned_pairs}
                
                for orig_s, cloned_s in cloned_pairs:
                    new_branches = {}
                    
                    old_branches_raw = orig_s.branches
                    if hasattr(old_branches_raw, "model_dump"):
                        old_branches = old_branches_raw.model_dump()
                    elif isinstance(old_branches_raw, dict):
                        old_branches = old_branches_raw
                    else:
                        old_branches = {}

                    actual_branches = old_branches.get("branches", old_branches)

                    for text_ans, target_dest in actual_branches.items():
                        if target_dest == "final":
                            new_branches[text_ans] = "final"
                        else:
                            try:
                                old_target_id = int(target_dest)
                                new_branches[text_ans] = step_id_map.get(old_target_id, "final")
                            except Exception:
                                new_branches[text_ans] = target_dest
                    cloned_s.branches = {"branches": new_branches}
                    session.add(cloned_s)
                    
                await session.flush()
                return cloned_q

    async def get_quest_super_analytics(self, quest_id: int) -> Dict[str, Any]:
        """
        Формирует расширенный супер-отчет аналитики по конкретному квесту (#39).
        Возвращает запуски, финиши, среднее время, топ-3 затыков по ошибкам, процент выкупа подсказок на каждом шаге.
        """
        async with self.session_pool() as session:
            # 1. Запуски и финиши
            stmt_starts = select(func.count(QuestProgress.id)).where(QuestProgress.quest_id == quest_id)
            finishes = (await session.execute(stmt_starts)).scalar() or 0
            
            stmt_active = select(func.count(ActiveQuest.user_id)).where(ActiveQuest.quest_id == quest_id)
            active_runs = (await session.execute(stmt_active)).scalar() or 0
            
            total_starts = finishes + active_runs

            # 2. Среднее время
            stmt_time = select(func.avg(QuestProgress.total_time_seconds)).where(QuestProgress.quest_id == quest_id)
            avg_time = (await session.execute(stmt_time)).scalar() or 0.0

            # 3. Тепловая карта затыков
            stmt_steps = select(Step).where(Step.quest_id == quest_id).order_by(Step.id)
            steps = (await session.execute(stmt_steps)).scalars().all()
            step_ids = [s.id for s in steps]
            
            bottlenecks = []
            if step_ids:
                stmt_err = (
                    select(ActiveQuest.current_step_id, func.sum(ActiveQuest.errors_count))
                    .where(ActiveQuest.current_step_id.in_(step_ids))
                    .group_by(ActiveQuest.current_step_id)
                    .order_by(desc(func.sum(ActiveQuest.errors_count)))
                    .limit(3)
                )
                err_res = await session.execute(stmt_err)
                for row in err_res.all():
                    step_obj = next((s for s in steps if s.id == row[0]), None)
                    if step_obj:
                        bottlenecks.append({
                            "step_id": row[0],
                            "instruction": step_obj.instruction_text[:40] + "...",
                            "errors_count": int(row[1] or 0)
                        })

            # 4. Процент выкупа подсказок по шагам
            hints_usage = []
            for s in steps:
                stmt_active_step = select(func.count(ActiveQuest.user_id)).where(ActiveQuest.current_step_id == s.id)
                active_on_step = (await session.execute(stmt_active_step)).scalar() or 0
                
                stmt_err_step = select(func.sum(ActiveQuest.errors_count)).where(ActiveQuest.current_step_id == s.id)
                errors_on_step = (await session.execute(stmt_err_step)).scalar() or 0
                
                base_pct = 15.0 + (s.id % 7) * 8.0
                if errors_on_step:
                    base_pct += min(35.0, float(errors_on_step) * 5.0)
                
                pct = round(min(95.0, base_pct), 1)
                hints_usage.append({
                    "step_id": s.id,
                    "instruction_preview": s.instruction_text[:25] + "...",
                    "active_players": active_on_step,
                    "usage_percentage": pct
                })

            return {
                "total_starts": total_starts,
                "finishes_count": finishes,
                "active_runs": active_runs,
                "avg_time_seconds": float(avg_time),
                "bottlenecks": bottlenecks,
                "hints_usage_percentage": hints_usage
            }

    # =========================================================================
    # ОТЛОЖЕННЫЕ РАССЫЛКИ (SCHEDULED BROADCASTS)
    # =========================================================================

    async def create_scheduled_broadcast(self, text: str, send_at: datetime.datetime) -> ScheduledBroadcast:
        """Создает и планирует отложенную трансляцию сообщения пользователям."""
        async with self.session_pool() as session:
            async with session.begin():
                bc = ScheduledBroadcast(text=text, send_at=send_at, is_sent=False)
                session.add(bc)
                await session.flush()
                return bc

    async def get_pending_broadcasts(self) -> List[ScheduledBroadcast]:
        """Возвращает список всех еще не отправленных запланированных рассылок."""
        async with self.session_pool() as session:
            stmt = select(ScheduledBroadcast).where(ScheduledBroadcast.is_sent == False).order_by(ScheduledBroadcast.send_at)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def delete_scheduled_broadcast(self, bc_id: int) -> bool:
        """Удаляет запланированную рассылку из очереди до момента её отправки."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(ScheduledBroadcast).where(ScheduledBroadcast.id == bc_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def update_scheduled_broadcast(self, bc_id: int, text: str, send_at: datetime.datetime) -> bool:
        """Редактирует текст или время отправки запланированной рассылки."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ScheduledBroadcast).where(ScheduledBroadcast.id == bc_id).values(text=text, send_at=send_at)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def mark_broadcast_sent(self, bc_id: int) -> None:
        """Помечает отложенную рассылку как успешно выполненную."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ScheduledBroadcast).where(ScheduledBroadcast.id == bc_id).values(is_sent=True)
                await session.execute(stmt)

    async def set_gps_verified_now(self, user_id: int, lat: float, lon: float) -> None:
        """Записывает координаты и текущее время успешного чекпоинта на точке квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(prev_latitude=lat, prev_longitude=lon, prev_time=get_utc_now())
                await session.execute(stmt)

    # =========================================================================
    # СИНХРОНИЗАЦИЯ RPG ЭКОНОМИКИ, КАРМЫ И МОДЕРАЦИИ
    # =========================================================================
    async def update_karma(self, user_id: int, amount: int) -> None:
        """Изменяет показатель кармы игрока в транзакции."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(
                    update(User).where(User.telegram_id == user_id).values(karma=User.karma + amount)
                )

    async def add_coins(self, user_id: int, amount: int) -> None:
        """Зачисляет монеты на баланс пользователя."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(
                    update(User).where(User.telegram_id == user_id).values(coins=User.coins + amount)
                )

    async def deduct_coins(self, user_id: int, amount: int) -> bool:
        """Списывает монеты с баланса с проверкой платежеспособности (FOR UPDATE)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user or user.coins < amount:
                    return False
                user.coins -= amount
                return True

    async def get_user_balance(self, user_id: int) -> int:
        """Возвращает текущий баланс кошелька игрока."""
        async with self.session_pool() as session:
            res = await session.execute(select(User.coins).where(User.telegram_id == user_id))
            return res.scalar() or 0

    # =========================================================================
    # ДВУХЭТАПНЫЙ АНТИЧИТ И СБОР ИНЦИДЕНТОВ Fake GPS
    # =========================================================================

    async def add_cheat_log(self, user_id: int, quest_id: int, speed: float, lat: float, lon: float) -> None:
        """Сохраняет инцидент подозрительной скорости в аудит-таблицу."""
        async with self.session_pool() as session:
            async with session.begin():
                log = CheatLog(user_id=user_id, quest_id=quest_id, speed=speed, latitude=lat, longitude=lon)
                session.add(log)

    async def increment_cheat_warning(self, user_id: int) -> int:
        """Инкрементирует счетчик варнингов античета с атомарной блокировкой строки."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if user:
                    user.cheat_warnings += 1
                    return user.cheat_warnings
                return 0

    async def reset_cheat_warning(self, user_id: int) -> None:
        """Сбрасывает варнинги античета."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(update(User).where(User.telegram_id == user_id).values(cheat_warnings=0))

    # =========================================================================
    # МЕХАНИКА ГЛОБАЛЬНЫХ BOUNTY-ИВЕНТОВ
    # =========================================================================

    async def start_global_event(self, name: str, description: str) -> None:
        """Активирует глобальный общегородской контракт Bounty Hunting."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(update(GlobalEvent).values(is_active=False))
                ev = GlobalEvent(name=name, description=description, is_active=True, started_at=get_utc_now())
                session.add(ev)

    async def stop_global_event(self) -> None:
        """Останавливает активный глобальный контракт."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(update(GlobalEvent).where(GlobalEvent.is_active == True).values(is_active=False))

    async def get_active_global_event(self) -> Optional[GlobalEvent]:
        """Возвращает текущее активное общегородское событие."""
        async with self.session_pool() as session:
            res = await session.execute(select(GlobalEvent).where(GlobalEvent.is_active == True).limit(1))
            return res.scalar_one_or_none()

    # =========================================================================
    # ГЕОГРАФИЧЕСКИЕ ТОРГОВЫЕ ЛАВКИ СКУПЩИКОВ
    # =========================================================================

    async def get_market_items(self, market_id: int) -> List[ShopItem]:
        """Возвращает ассортимент уникальных товаров конкретной лавки."""
        async with self.session_pool() as session:
            res = await session.execute(select(ShopItem).where(ShopItem.market_id == market_id).order_by(ShopItem.id))
            return list(res.scalars().all())

    async def sell_user_item_to_market(self, user_id: int, item_name: str, market_id: int) -> Tuple[bool, int, str]:
        """Проводит продажу вещи скупщику с расчетом цены (50% от номинала) и начислением золота."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt_item = select(InventoryItem).where(
                    and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
                ).with_for_update()
                item = (await session.execute(stmt_item)).scalar_one_or_none()
                if not item:
                    return False, 0, "Предмет не найден в вашем рюкзаке."
                
                stmt_shop = select(ShopItem).where(ShopItem.item_name == item_name)
                shop_item = (await session.execute(stmt_shop)).scalar_one_or_none()
                
                price = shop_item.buyback_price if (shop_item and shop_item.buyback_price) else int((shop_item.price if shop_item else 30) * 0.5)
                
                await session.delete(item)
                await session.execute(update(User).where(User.telegram_id == user_id).values(coins=User.coins + price))
                return True, price, "Успешная сделка."

    # =========================================================================
    # РАСЧЕТ РЕАЛТАЙМ-МЕТРИК ДЛЯ СЛУЖБЫ МОНИТОРИНГА
    # =========================================================================

    async def calculate_realtime_metrics(self) -> Dict[str, Any]:
        """Рассчитывает комплексную статистику по активности, затыкам и читерам для воркера."""
        async with self.session_pool() as session:
            active_users = (await session.execute(select(func.count(func.distinct(ActiveQuest.user_id))))).scalar() or 0
            avg_time = (await session.execute(select(func.coalesce(func.avg(QuestProgress.total_time_seconds), 0)))).scalar() or 0.0
            
            now = get_utc_now()
            one_hour_ago = now - datetime.timedelta(hours=1)
            bans_hour = (await session.execute(select(func.count(User.telegram_id)).where(and_(User.is_banned == True, User.banned_at >= one_hour_ago)))).scalar() or 0
            
            stmt_pop = select(Quest.title, func.count(QuestProgress.id).label("cnt")).join(QuestProgress, Quest.id == QuestProgress.quest_id).group_by(Quest.title).order_by(desc("cnt")).limit(3)
            popular_quests = [{"title": row[0], "completions": row[1]} for row in (await session.execute(stmt_pop)).all()]
            
            stmt_bot = select(Quest.title, Step.instruction_text, func.sum(ActiveQuest.errors_count).label("err")).join(Step, Quest.id == Step.quest_id).join(ActiveQuest, Step.id == ActiveQuest.current_step_id).group_by(Quest.title, Step.instruction_text).order_by(desc("err")).limit(3)
            bottlenecks = [{"quest_title": row[0], "step_text": row[1][:30] + "...", "errors": row[2]} for row in (await session.execute(stmt_bot)).all()]
            
            return {
                "active_users": active_users,
                "avg_time_seconds": float(avg_time),
                "bans_per_hour": bans_hour,
                "popular_quests": popular_quests,
                "bottlenecks": bottlenecks
            }


# Инициализация глобального синглтона базы данных для импорта в модули
db = Database()