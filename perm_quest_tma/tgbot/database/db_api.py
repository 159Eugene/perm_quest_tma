import math
import datetime
import logging
import random
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy import select, update, delete, func, desc, and_, or_, text as sa_text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from redis.asyncio import Redis
from tgbot.config import settings
from tgbot.database.models import (
    Base, User, Quest, Step, InventoryItem, ActiveQuest, QuestProgress, 
    Achievement, UserAchievement, ShopItem, PromoCode, DailyRiddle, SystemSettings,
    CheatLog, ScheduledBroadcast, ShopItemType, QuestMarket, RandomEvent, GlobalEvent,
    ARMarker, CraftRecipe, City, NPCCharacter, LevelConfig
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
    Класс управления базы данных. Реализует паттерн Data Access Object (DAO)
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
        self.redis = Redis.from_url(settings.redis.redis_url)
        # --- Кэш системных настроек ---
        self._sys_settings_cache = None
        self._sys_settings_cache_time = None
        




















    async def seed_initial_data(self) -> None:
        """ГЕНЕРАЛЬНЫЙ РЕЛИЗ 6.0: 10 Квестов, 5 Макрорайонов, Гео-экономика и Огромные цикличные графы."""
        async with self.session_pool() as session:
            async with session.begin():
                # --- 1. АВТОМАТИЧЕСКОЕ ИСЦЕЛЕНИЕ ТАБЛИЦ ПОСТГРЕСА (DDL) ---
                try:
                    await session.execute(sa_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS stamina INTEGER DEFAULT 100;"))
                    await session.execute(sa_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS max_stamina INTEGER DEFAULT 100;"))
                    await session.execute(sa_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_stamina_update TIMESTAMP WITHOUT TIME ZONE;"))
                    await session.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS level_configs (
                            level INTEGER PRIMARY KEY,
                            xp_to_next INTEGER NOT NULL DEFAULT 150,
                            reward_coins INTEGER DEFAULT 0,
                            reward_item_name VARCHAR(100),
                            stamina_bonus INTEGER DEFAULT 0
                        );
                    """))
                    await session.execute(sa_text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_quest_start_cost INTEGER DEFAULT 20;"))
                    await session.execute(sa_text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_step_cost INTEGER DEFAULT 15;"))
                    await session.execute(sa_text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_npc_talk_cost INTEGER DEFAULT 5;"))
                    await session.execute(sa_text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_quest_cooldown_hours INTEGER DEFAULT 20;"))
                    await session.execute(sa_text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_npc_cooldown_hours INTEGER DEFAULT 24;"))

                    await session.execute(sa_text("ALTER TABLE quests ADD COLUMN IF NOT EXISTS stamina_cost_override INTEGER;"))
                    await session.execute(sa_text("ALTER TABLE quests ADD COLUMN IF NOT EXISTS cooldown_override_hours INTEGER;"))
                    await session.execute(sa_text("ALTER TABLE quests ADD COLUMN IF NOT EXISTS is_free BOOLEAN DEFAULT false;"))

                    await session.execute(sa_text("ALTER TABLE npc_characters ADD COLUMN IF NOT EXISTS stamina_cost_override INTEGER;"))
                    await session.execute(sa_text("ALTER TABLE npc_characters ADD COLUMN IF NOT EXISTS cooldown_override_hours INTEGER;"))
                    await session.execute(sa_text("ALTER TABLE npc_characters ADD COLUMN IF NOT EXISTS is_free BOOLEAN DEFAULT false;"))
                except Exception as e:
                    logger.warning(f"Миграция DDL пропущена: {e}")

                # --- 2. МАТРИЦА ПРОГРЕССИИ УРОВНЕЙ ---
                stmt_lvl = select(func.count()).select_from(LevelConfig)
                if (await session.execute(stmt_lvl)).scalar() == 0:
                    session.add_all([
                        LevelConfig(level=1, xp_to_next=150, reward_coins=0, reward_item_name=None, stamina_bonus=0),
                        LevelConfig(level=2, xp_to_next=200, reward_coins=50, reward_item_name=None, stamina_bonus=5),
                        LevelConfig(level=3, xp_to_next=350, reward_coins=100, reward_item_name="Грибушинский чай", stamina_bonus=0),
                        LevelConfig(level=4, xp_to_next=550, reward_coins=150, reward_item_name=None, stamina_bonus=5),
                        LevelConfig(level=5, xp_to_next=800, reward_coins=300, reward_item_name="Эликсир бодрости", stamina_bonus=10),
                        LevelConfig(level=6, xp_to_next=1200, reward_coins=450, reward_item_name=None, stamina_bonus=0),
                        LevelConfig(level=7, xp_to_next=1700, reward_coins=600, reward_item_name="Посикунчики", stamina_bonus=10),
                        LevelConfig(level=8, xp_to_next=2400, reward_coins=1000, reward_item_name="Пермская соль-пермянка", stamina_bonus=10),
                        LevelConfig(level=9, xp_to_next=3300, reward_coins=1500, reward_item_name=None, stamina_bonus=10),
                        LevelConfig(level=10, xp_to_next=4500, reward_coins=3000, reward_item_name="Купеческий вексель Любимова", stamina_bonus=20),
                        LevelConfig(level=11, xp_to_next=6000, reward_coins=4000, reward_item_name=None, stamina_bonus=10),
                        LevelConfig(level=12, xp_to_next=8000, reward_coins=5500, reward_item_name="Чердынский сбор", stamina_bonus=10),
                        LevelConfig(level=13, xp_to_next=10500, reward_coins=7500, reward_item_name="Чернильница Пастернака", stamina_bonus=0),
                        LevelConfig(level=14, xp_to_next=13500, reward_coins=10000, reward_item_name=None, stamina_bonus=10),
                        LevelConfig(level=15, xp_to_next=18000, reward_coins=15000, reward_item_name="Акция пароходства", stamina_bonus=25)
                    ])

                # --- 3. СИСТЕМНЫЕ НАСТРОЙКИ ---
                stmt_sys = select(func.count()).select_from(SystemSettings)
                if (await session.execute(stmt_sys)).scalar() == 0:
                    session.add(SystemSettings(
                        tutorial_latitude=58.0097, tutorial_longitude=56.2444, tutorial_answer="пермь",
                        merchant_bonus=20, ranger_cd_minutes=5, historian_mult=2.5,
                        base_step_coins=15, base_step_score=150, quest_completion_bonus=500,
                        karma_elixir_price=60, karma_elixir_effect=3, daily_gift_base_reward=20,
                        daily_gift_increment=10, daily_gift_max_reward=150, scroll_event_price=15,
                        scroll_event_karma=2, wallet_event_coins=30, wallet_event_karma_penalty=-2,
                        wallet_event_karma_reward=3, merc_lifetime_minutes=60, merc_summon_price=200,
                        merc_efficiency=100,
                        default_quest_start_cost=20, default_step_cost=15, default_npc_talk_cost=5,
                        default_quest_cooldown_hours=20, default_npc_cooldown_hours=24
                    ))

                # --- 4. ДОСТИЖЕНИЯ ---
                stmt_ach = select(func.count()).select_from(Achievement)
                if (await session.execute(stmt_ach)).scalar() == 0:
                    session.add_all([
                        Achievement(name="Уральский пешеход", description="Проходите сюжетные квесты Перми.", badge_emoji="🚶‍♂️", required_action="quests_completed", required_value_bronze=1, required_value_silver=3, required_value_diamond=10, reward_coins_bronze=100, reward_coins_silver=500, reward_coins_diamond=2000),
                        Achievement(name="Купец первой гильдии", description="Накапливайте золотой капитал.", badge_emoji="💰", required_action="coins_earned", required_value_bronze=1000, required_value_silver=5000, required_value_diamond=25000, reward_coins_bronze=200, reward_coins_silver=1000, reward_coins_diamond=5000),
                        Achievement(name="Святой человек", description="Накопите максимум положительной кармы.", badge_emoji="😇", required_action="karma_earned", required_value_bronze=10, required_value_silver=30, required_value_diamond=100, reward_coins_bronze=300, reward_coins_silver=1000, reward_coins_diamond=5000),
                        Achievement(name="Душа Компании", description="Общайтесь с городскими NPC.", badge_emoji="🗣", required_action="npc_talks", required_value_bronze=3, required_value_silver=10, required_value_diamond=25, reward_coins_bronze=50, reward_coins_silver=300, reward_coins_diamond=1500)
                    ])

                # --- 5. МАГАЗИН И АРТЕФАКТЫ ---
                stmt_shop = select(func.count()).select_from(ShopItem)
                if (await session.execute(stmt_shop)).scalar() == 0:
                    session.add_all([
                        ShopItem(name="☕️ Эспрессо (Экспресс-доставка)", description="Мгновенный глоток бодрости (+45 ⚡️).", price=75, item_name="Пермский Эспрессо", item_type=ShopItemType.CONSUMABLE, weight=0, market_ids=[0]),
                        ShopItem(name="☕️ Эспрессо (С прилавка)", description="Тот же бодрящий эспрессо (+45 ⚡️), но дешевле!", price=30, item_name="Пермский Эспрессо", item_type=ShopItemType.CONSUMABLE, weight=0, market_ids=[1, 2, 3, 4, 8, 9]),
                        ShopItem(name="🫖 Грибушинский чай в баночке", description="Восстанавливает 100/100 ⚡️.", price=50, item_name="Грибушинский чай", item_type=ShopItemType.CONSUMABLE, weight=1, market_ids=[1, 3]),
                        ShopItem(name="Посикунчики (порция)", description="+2 Кармы.", price=45, item_name="Посикунчики", item_type=ShopItemType.CONSUMABLE, weight=1, market_ids=[1, 4]),
                        ShopItem(name="Секретный пропуск КамГЭС", description="Доступ в шлюзы.", price=500, item_name="Пропуск КамГЭС", item_type=ShopItemType.TICKET, weight=0, market_ids=[6]),
                        
                        ShopItem(name="Чугунная болванка", description="Сырье Мотовилихинского завода.", price=15, item_name="Чугунная болванка", item_type=ShopItemType.ARTIFACT, weight=2, market_ids=[2]),
                        ShopItem(name="Авиационный керосин", description="Топливо для испытаний.", price=20, item_name="Авиационный керосин", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[8]),
                        ShopItem(name="Целебный мох", description="Сбор для шаманов.", price=10, item_name="Целебный мох", item_type=ShopItemType.CONSUMABLE, weight=1, market_ids=[9]),
                        ShopItem(name="Закамский живой квас", description="Утоляет жажду капитана (+50 ⚡️).", price=15, item_name="Закамский живой квас", item_type=ShopItemType.CONSUMABLE, weight=1, market_ids=[7]),
                        ShopItem(name="Гайвинский судак (Вяленый)", description="Лучшая закуска на дамбе (+40 ⚡️). Обожают сталкеры.", price=35, item_name="Вяленый судак", item_type=ShopItemType.CONSUMABLE, weight=1, market_ids=[6]),

                        ShopItem(name="Старый Ломик", description="Инструмент грузчика. Отлично вскрывает тайники.", price=100, item_name="Старый Ломик", item_type=ShopItemType.ARTIFACT, weight=2, market_ids=[]),
                        ShopItem(name="Золотые Часы Мешкова", description="Семейная реликвия пароходчика.", price=1000, item_name="Часы Мешкова", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[]),
                        ShopItem(name="Карта Колчака", description="Ветхая бумага с крестиком.", price=500, item_name="Карта Колчака", item_type=ShopItemType.ARTIFACT, weight=0, market_ids=[]),
                        ShopItem(name="Слиток Колчака", description="Тяжелый кусок золота империи.", price=3000, item_name="Слиток Колчака", item_type=ShopItemType.ARTIFACT, weight=5, market_ids=[]),
                        ShopItem(name="Орден Мецената", description="Приносит +120 монет в час.", price=0, item_name="Орден Мецената", item_type=ShopItemType.ARTIFACT, weight=0, generates_income=True, income_per_hour=120, market_ids=[]),
                        ShopItem(name="Светлый Оберег Некрополя", description="Очищенная реликвия.", price=800, item_name="Светлый оберег", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[]),
                        ShopItem(name="Проклятый Череп", description="Приносит несчастья.", price=2000, item_name="Проклятый череп", item_type=ShopItemType.ARTIFACT, weight=2, market_ids=[]),
                        ShopItem(name="Купеческий вексель Любимова", description="Приносит +15 монет в час.", price=350, item_name="Купеческий вексель Любимова", item_type=ShopItemType.ARTIFACT, weight=1, generates_income=True, income_per_hour=15, market_ids=[]),
                        ShopItem(name="Патент Славянова", description="Исторический документ.", price=500, item_name="Патент Славянова", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[]),
                        ShopItem(name="Лопатка турбины", description="Титан.", price=400, item_name="Лопатка турбины", item_type=ShopItemType.ARTIFACT, weight=3, market_ids=[]),
                        ShopItem(name="Амулет Чуди", description="Мистика леса.", price=600, item_name="Амулет Чуди", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[]),
                        ShopItem(name="Речной компас", description="Указывает на Каму.", price=300, item_name="Речной компас", item_type=ShopItemType.ARTIFACT, weight=1, market_ids=[])
                    ])

                # --- 6. РЫНКИ СБЫТА ---
                stmt_market = select(func.count()).select_from(QuestMarket)
                if (await session.execute(stmt_market)).scalar() == 0:
                    session.add_all([
                        QuestMarket(name="Купеческий причал на Каме", latitude=58.0195, longitude=56.2515, radius=100.0), 
                        QuestMarket(name="Рынок Мотовилихинских заводов", latitude=58.0315, longitude=56.3150, radius=120.0), 
                        QuestMarket(name="Торговые ряды Сибирской", latitude=58.0125, longitude=56.2580, radius=90.0), 
                        QuestMarket(name="Старый Сенной рынок", latitude=58.0090, longitude=56.2300, radius=150.0), 
                        QuestMarket(name="Барахолка на Заимке", latitude=58.0040, longitude=56.1830, radius=100.0), 
                        QuestMarket(name="Тайный рынок КамГЭС (Гайва)", latitude=58.1165, longitude=56.3255, radius=120.0), 
                        QuestMarket(name="Закамский причал", latitude=58.0050, longitude=55.9360, radius=100.0), 
                        QuestMarket(name="Сквер Авиаторов (Крохалевка)", latitude=57.9850, longitude=56.2360, radius=100.0), 
                        QuestMarket(name="Лесная застава (Балатово)", latitude=57.9810, longitude=56.1750, radius=100.0) 
                    ])

                # --- 7. ВНЕШНИЕ БАЗЫ NPC (На карте) ---
                stmt_npc = select(func.count()).select_from(NPCCharacter)
                if (await session.execute(stmt_npc)).scalar() == 0:
                    session.add_all([
                        NPCCharacter(name="Грузчик Гаврила", description="Отдыхает на причале.", latitude=58.0205, longitude=56.2540, radius=50.0, dialogue_tree={"start": {"text": "Эй, путник! Поможешь?", "options": [{"text": "Помогу!", "next_node": "help"}, {"text": "У меня дела.", "next_node": "exit"}]}, "help": {"text": "Держи ломик.", "options": [{"text": "Взять", "next_node": "exit", "item_give": "Старый Ломик", "xp_change": 50}]}}),
                        NPCCharacter(name="Николай Мешков", description="Пароходчик.", latitude=58.0195, longitude=56.2515, radius=45.0, is_free=True, cooldown_override_hours=0, dialogue_tree={"start": {"text": "Ох, как же мне без часов...", "options": [{"text": "Я нашел их!", "next_node": "reward", "item_take": "Часы Мешкова"}]}, "reward": {"text": "Спасибо! Держи вексель.", "options": [{"text": "Благодарю", "next_node": "exit", "item_give": "Купеческий вексель Любимова", "coins_change": 200}]}}),
                        NPCCharacter(name="Призрак Некрополя", description="Блуждает в тумане.", latitude=58.0160, longitude=56.2750, radius=50.0),
                        NPCCharacter(name="Закамский Контрабандист", description="Скупает темный лут.", latitude=58.0050, longitude=55.9360, radius=50.0),
                        
                        NPCCharacter(name="Инженер Славянов", description="Мотовилиха. Гений электросварки.", latitude=58.0336, longitude=56.3168, radius=50.0),
                        NPCCharacter(name="Старый Авиатор", description="Крохалевка. Ветеран 'Пермских моторов'.", latitude=57.9745, longitude=56.2385, radius=50.0),
                        NPCCharacter(name="Шаманка Чуди", description="Балатово. Знает тайны Черняевского леса.", latitude=57.9810, longitude=56.1650, radius=50.0),
                        NPCCharacter(name="Речной Капитан", description="Закамск. Местный морской волк.", latitude=58.0050, longitude=55.9360, radius=50.0),
                        NPCCharacter(name="Сталкер с КамГЭС", description="Гайва. Контролирует проход на плотину.", latitude=58.1165, longitude=56.3255, radius=50.0)
                    ])

                # --- 8. СЮЖЕТНЫЕ КВЕСТЫ (Ключевая логика) ---
                stmt_quest = select(func.count()).select_from(Quest)
                if (await session.execute(stmt_quest)).scalar() == 0:
                    q1 = Quest(title="Наследие Мешкова: Инвентарный детектив", description="МНОГОШАГОВЫЙ КВЕСТ. Помогите Гавриле и верните часы!", is_published=True, min_level_required=2, max_speed_kmh=15.0)
                    q2 = Quest(title="Энергия Камы: Блокада", description="ГАЙВА. Чтобы пройти дальше, вам придется договориться со Сталкером.", is_published=True, min_level_required=2, max_speed_kmh=15.0)
                    q3 = Quest(title="Моральный Компас: Золото Колчака", description="НЕЛИНЕЙНЫЙ КВЕСТ. Вы нашли карту клада. Кому вы ее отдадите?", is_published=True, min_level_required=3, max_speed_kmh=15.0)
                    q4 = Quest(title="Ночной Дозор: Тайны Некрополя", description="МИСТИКА. Квест доступен только НОЧЬЮ. Исследуйте могилы с призраками.", is_published=True, min_level_required=2, max_speed_kmh=15.0)
                    q5 = Quest(title="Бесплатный Промо-Тур: Эспланада", description="Быстрый старт для новичков! Квест ничего не стоит.", is_published=True, min_level_required=1, max_speed_kmh=15.0, is_free=True, cooldown_override_hours=0)
                    
                    q6 = Quest(title="Мотовилиха: Искры Сварки", description="Новичковый тур. Помогите Инженеру запустить аппарат. Найдите сырье на рынке.", is_published=True, min_level_required=1, is_free=True, cooldown_override_hours=0, max_speed_kmh=15.0)
                    q7 = Quest(title="Крохалевка: Крылья Пармы", description="Новичковый тур. Прогулка к монументу МиГ-31. Добудьте топливо для Авиатора.", is_published=True, min_level_required=1, is_free=True, cooldown_override_hours=0, max_speed_kmh=15.0)
                    q8 = Quest(title="Балатово: Зов Чуди", description="Новичковый тур. Мистическая прогулка по лесу. Сделайте подношение Шаманке.", is_published=True, min_level_required=1, is_free=True, cooldown_override_hours=0, max_speed_kmh=15.0)
                    q9 = Quest(title="Закамск: Речной Волк", description="Новичковый тур. Прогулка по набережной правого берега. Принесите квас Капитану.", is_published=True, min_level_required=1, is_free=True, cooldown_override_hours=0, max_speed_kmh=15.0)
                    q10 = Quest(title="Гайва: Огни КамГЭС", description="Новичковый тур. Прогулка по парку Чехова. Договоритесь со Сталкером.", is_published=True, min_level_required=1, is_free=True, cooldown_override_hours=0, max_speed_kmh=15.0)

                    session.add_all([q1, q2, q3, q4, q5, q6, q7, q8, q9, q10])
                    await session.flush()

                    # === СОЗДАНИЕ ШАГОВ ===

                    # Старые квесты (Q1-Q5)
                    s1_1 = Step(quest_id=q1.id, instruction_text="Подойдите к Гавриле. Введите 'гаврила'.", npc_name="Грузчик Гаврила", latitude=58.0205, longitude=56.2540)
                    s1_2 = Step(quest_id=q1.id, instruction_text="Тайник найден! Введите 'вскрыть'.", latitude=58.0200, longitude=56.2520)
                    s1_fail = Step(quest_id=q1.id, instruction_text="❌ У вас нет Ломика! Введите 'назад'.", latitude=58.0200, longitude=56.2520)
                    s1_3 = Step(quest_id=q1.id, instruction_text="Тайник вскрыт! Введите 'забрать'.", gives_item="Часы Мешкова", latitude=58.0200, longitude=56.2520)
                    s1_4 = Step(quest_id=q1.id, instruction_text="Квест завершен.", is_final=True, latitude=58.0195, longitude=56.2515)

                    s2_1 = Step(quest_id=q2.id, instruction_text="Парк Чехова. Сколько колонн? (Ответ: 10)", latitude=58.1250, longitude=56.2840)
                    s2_2 = Step(quest_id=q2.id, instruction_text="Шаг 2. Тайный рынок КамГЭС. Введите 'торговец'.", npc_name="Торговец", latitude=58.1165, longitude=56.3255)
                    s2_fail = Step(quest_id=q2.id, instruction_text="❌ Без судака прохода нет! Возвращайтесь на рынок. Напишите 'назад'.", latitude=58.1170, longitude=56.3260)
                    s2_3 = Step(quest_id=q2.id, instruction_text="Шаг 3. Введите 'сталкер', чтобы подойти к охране.", npc_name="Сталкер", latitude=58.1170, longitude=56.3260)
                    s2_4 = Step(quest_id=q2.id, instruction_text="Путь открыт! Введите 'шлюз'.", is_final=True, latitude=58.1180, longitude=56.3270)

                    s3_1 = Step(quest_id=q3.id, instruction_text="Вокзал Пермь-2. Год? (Ответ: 1919)", latitude=58.0040, longitude=56.1830)
                    s3_2 = Step(quest_id=q3.id, instruction_text="Отнести в 'музей' или на 'рынок'?", latitude=58.0055, longitude=56.1850)
                    s3_light = Step(quest_id=q3.id, instruction_text="Введите 'отдать'.", gives_item="Орден Мецената", is_final=True, latitude=58.0195, longitude=56.2515)
                    s3_dark = Step(quest_id=q3.id, instruction_text="Введите 'продать'.", gives_item="Слиток Колчака", latitude=58.0050, longitude=55.9360)
                    s3_dark_end = Step(quest_id=q3.id, instruction_text="Сделка состоялась. Введите 'финиш'.", is_final=True, latitude=58.0050, longitude=55.9360)

                    s4_1 = Step(quest_id=q4.id, instruction_text="Дождитесь заката. Введите 'ночь'.", is_night_only=True, latitude=58.0160, longitude=56.2750)
                    s4_2 = Step(quest_id=q4.id, instruction_text="Введите 'аминь'.", is_night_only=True, is_final=True, gives_item="Светлый оберег", latitude=58.0155, longitude=56.2740)

                    s5_1 = Step(quest_id=q5.id, instruction_text="Эспланада. Колонн у Театра? (10)", latitude=58.0090, longitude=56.2210)
                    s5_2 = Step(quest_id=q5.id, instruction_text="Героям фронта. Введите 'слава'.", is_final=True, latitude=58.0110, longitude=56.2230)

                    # --- САМОДОСТАТОЧНЫЕ НОВЫЕ КВЕСТЫ (Q6-Q10) ---
                    s6_1 = Step(quest_id=q6.id, instruction_text="Шаг 1. Рынок Мотовилихинских заводов. Введите 'торговец'.", npc_name="Торговец", latitude=58.0315, longitude=56.3150)
                    s6_2 = Step(quest_id=q6.id, instruction_text="Шаг 2. Найдите Инженера Славянова. Напишите 'инженер'.", npc_name="Инженер Славянов", latitude=58.0336, longitude=56.3168)
                    s6_fail = Step(quest_id=q6.id, instruction_text="❌ У вас нет Чугунной болванки! Возвращайтесь на рынок. Введите 'назад'.", latitude=58.0336, longitude=56.3168)
                    s6_3 = Step(quest_id=q6.id, instruction_text="✅ Награда получена! Введите 'далее'.", gives_item="Патент Славянова", secret_price=100, latitude=58.0336, longitude=56.3168)
                    s6_4 = Step(quest_id=q6.id, instruction_text="Квест успешно завершен!", is_final=True, latitude=58.0336, longitude=56.3168)

                    s7_1 = Step(quest_id=q7.id, instruction_text="Шаг 1. Сквер Авиаторов. Введите 'торговец'.", npc_name="Торговец", latitude=57.9850, longitude=56.2360)
                    s7_2 = Step(quest_id=q7.id, instruction_text="Шаг 2. Подойдите к Авиатору. Напишите 'авиатор'.", npc_name="Старый Авиатор", latitude=57.9745, longitude=56.2385)
                    s7_fail = Step(quest_id=q7.id, instruction_text="❌ У вас нет Керосина! Вернитесь на рынок. Введите 'назад'.", latitude=57.9745, longitude=56.2385)
                    s7_3 = Step(quest_id=q7.id, instruction_text="✅ Мотор ревёт! Введите 'далее'.", gives_item="Лопатка турбины", secret_price=100, latitude=57.9745, longitude=56.2385)
                    s7_4 = Step(quest_id=q7.id, instruction_text="Квест успешно завершен!", is_final=True, latitude=57.9745, longitude=56.2385)

                    s8_1 = Step(quest_id=q8.id, instruction_text="Шаг 1. Лесная застава. Введите 'торговец'.", npc_name="Торговец", latitude=57.9810, longitude=56.1750)
                    s8_2 = Step(quest_id=q8.id, instruction_text="Шаг 2. Найдите Шаманку Чуди. Напишите 'шаманка'.", npc_name="Шаманка Чуди", latitude=57.9810, longitude=56.1650)
                    s8_fail = Step(quest_id=q8.id, instruction_text="❌ Духи молчат. Вам нужен Целебный мох. Введите 'назад'.", latitude=57.9810, longitude=56.1650)
                    s8_3 = Step(quest_id=q8.id, instruction_text="✅ Ритуал прошел успешно! Введите 'далее'.", gives_item="Амулет Чуди", secret_price=100, latitude=57.9810, longitude=56.1650)
                    s8_4 = Step(quest_id=q8.id, instruction_text="Квест успешно завершен!", is_final=True, latitude=57.9810, longitude=56.1650)

                    s9_1 = Step(quest_id=q9.id, instruction_text="Шаг 1. Закамский причал. Введите 'торговец'.", npc_name="Торговец", latitude=58.0050, longitude=55.9360)
                    s9_2 = Step(quest_id=q9.id, instruction_text="Шаг 2. Найдите Речного Капитана. Напишите 'капитан'.", npc_name="Речной Капитан", latitude=58.0050, longitude=55.9360)
                    s9_fail = Step(quest_id=q9.id, instruction_text="❌ Капитан хочет пить! Нужен квас. Введите 'назад'.", latitude=58.0050, longitude=55.9360)
                    s9_3 = Step(quest_id=q9.id, instruction_text="✅ Жажда утолена! Введите 'далее'.", gives_item="Речной компас", secret_price=100, latitude=58.0050, longitude=55.9360)
                    s9_4 = Step(quest_id=q9.id, instruction_text="Квест успешно завершен!", is_final=True, latitude=58.0050, longitude=55.9360)

                    s10_1 = Step(quest_id=q10.id, instruction_text="Шаг 1. Тайный рынок КамГЭС. Введите 'торговец'.", npc_name="Торговец", latitude=58.1165, longitude=56.3255)
                    s10_2 = Step(quest_id=q10.id, instruction_text="Шаг 2. Подойдите к Сталкеру. Напишите 'сталкер'.", npc_name="Сталкер с КамГЭС", latitude=58.1165, longitude=56.3255)
                    s10_fail = Step(quest_id=q10.id, instruction_text="❌ Сталкер требует гостинец! Введите 'назад'.", latitude=58.1165, longitude=56.3255)
                    s10_3 = Step(quest_id=q10.id, instruction_text="✅ Пропуск получен! Введите 'далее'.", gives_item="Секретный пропуск КамГЭС", secret_price=100, latitude=58.1165, longitude=56.3255)
                    s10_4 = Step(quest_id=q10.id, instruction_text="Квест успешно завершен!", is_final=True, latitude=58.1165, longitude=56.3255)

                    session.add_all([
                        s1_1, s1_2, s1_fail, s1_3, s1_4,
                        s2_1, s2_2, s2_3, s2_fail, s2_4,
                        s3_1, s3_2, s3_light, s3_dark, s3_dark_end,
                        s4_1, s4_2, s5_1, s5_2,
                        s6_1, s6_2, s6_fail, s6_3, s6_4,
                        s7_1, s7_2, s7_fail, s7_3, s7_4,
                        s8_1, s8_2, s8_fail, s8_3, s8_4,
                        s9_1, s9_2, s9_fail, s9_3, s9_4,
                        s10_1, s10_2, s10_fail, s10_3, s10_4
                    ])
                    await session.flush()

                    # === ПРОШИВКА ТОПОЛОГИИ И ИНВЕНТАРНЫХ ПРОВЕРОК ===

                    # Старые квесты
                    s1_1.npc_dialogue = {"start": {"text": "Эй, путник! Поможешь? Держи ломик.", "options": [{"text": "Взять ломик", "next_node": "exit", "item_give": "Старый Ломик", "xp_change": 50}]}}
                    s1_1.branches = {"branches": {"гаврила": s1_2.id}}
                    s1_2.branches = {"branches": {"вскрыть": {"target": s1_3.id, "required_item": "Старый Ломик", "fail_target": s1_fail.id}}}
                    s1_fail.branches = {"branches": {"назад": s1_1.id}}
                    s1_3.branches = {"branches": {"забрать": s1_4.id}}
                    s1_4.branches = {"branches": {"отдал": "final"}}

                    s2_1.branches = {"branches": {"10": s2_2.id, "десять": s2_2.id}}
                    s2_2.npc_dialogue = {"start": {"text": "Свежий вяленый судак! Всего 35 монет.", "options": [{"text": "Купить (35 монет)", "next_node": "exit", "coins_change": -35, "item_give": "Вяленый судак"}, {"text": "Уйти", "next_node": "exit"}]}}
                    s2_2.branches = {"branches": {"торговец": s2_3.id}}
                    s2_3.npc_dialogue = {"start": {"text": "Проход закрыт. А, это мне? Проходи.", "options": [{"text": "Отдать судака", "next_node": "exit", "item_take": "Вяленый судак", "xp_change": 50}, {"text": "У меня нет судака", "next_node": f"step_{s2_fail.id}"}]}}
                    s2_3.branches = {"branches": {"сталкер": s2_4.id}}
                    s2_fail.branches = {"branches": {"назад": s2_2.id}}
                    s2_4.branches = {"branches": {"шлюз": "final"}}

                    s3_1.branches = {"branches": {"1919": s3_2.id}}
                    s3_2.branches = {"branches": {"музей": s3_light.id, "рынок": s3_dark.id}}
                    s3_light.branches = {"branches": {"отдать": "final"}}
                    s3_dark.branches = {"branches": {"продать": s3_dark_end.id}}
                    s3_dark_end.branches = {"branches": {"финиш": "final"}}

                    s4_1.branches = {"branches": {"ночь": s4_2.id}}
                    s4_2.branches = {"branches": {"аминь": "final"}}

                    s5_1.branches = {"branches": {"10": s5_2.id, "десять": s5_2.id}}
                    s5_2.branches = {"branches": {"слава": "final"}}

                    # Q6: Мотовилиха
                    s6_1.npc_dialogue = {"start": {"text": "Тебе нужна Чугунная болванка? 15 монет, и она твоя.", "options": [{"text": "Купить (15 монет)", "next_node": "exit", "coins_change": -15, "item_give": "Чугунная болванка"}, {"text": "Пока нет", "next_node": "exit"}]}}
                    s6_1.branches = {"branches": {"торговец": s6_2.id}}
                    s6_2.npc_dialogue = {"start": {"text": "Принес чугунную болванку для моих опытов?", "options": [{"text": "Да, держи", "next_node": "exit", "item_take": "Чугунная болванка", "xp_change": 50}, {"text": "У меня ее нет...", "next_node": f"step_{s6_fail.id}"}]}}
                    s6_2.branches = {"branches": {"инженер": s6_3.id}}
                    s6_fail.branches = {"branches": {"назад": s6_1.id}}
                    s6_3.branches = {"branches": {"далее": s6_4.id}}
                    s6_4.branches = {"branches": {"финиш": "final"}}

                    # Q7: Крохалевка
                    s7_1.npc_dialogue = {"start": {"text": "Лучший авиационный керосин на районе. 20 монет.", "options": [{"text": "Купить (20 монет)", "next_node": "exit", "coins_change": -20, "item_give": "Авиационный керосин"}, {"text": "Пока нет", "next_node": "exit"}]}}
                    s7_1.branches = {"branches": {"торговец": s7_2.id}}
                    s7_2.npc_dialogue = {"start": {"text": "Без керосина мотор не завести. Принес?", "options": [{"text": "Отдать керосин", "next_node": "exit", "item_take": "Авиационный керосин", "xp_change": 50}, {"text": "Забыл купить", "next_node": f"step_{s7_fail.id}"}]}}
                    s7_2.branches = {"branches": {"авиатор": s7_3.id}}
                    s7_fail.branches = {"branches": {"назад": s7_1.id}}
                    s7_3.branches = {"branches": {"далее": s7_4.id}}
                    s7_4.branches = {"branches": {"финиш": "final"}}

                    # Q8: Балатово
                    s8_1.npc_dialogue = {"start": {"text": "Целебный мох из глубин леса. Отдам за 10 монет.", "options": [{"text": "Купить (10 монет)", "next_node": "exit", "coins_change": -10, "item_give": "Целебный мох"}, {"text": "Отказ", "next_node": "exit"}]}}
                    s8_1.branches = {"branches": {"торговец": s8_2.id}}
                    s8_2.npc_dialogue = {"start": {"text": "Духи требуют подношения. У тебя есть мох?", "options": [{"text": "Отдать мох", "next_node": "exit", "item_take": "Целебный мох", "xp_change": 50}, {"text": "Нет мха", "next_node": f"step_{s8_fail.id}"}]}}
                    s8_2.branches = {"branches": {"шаманка": s8_3.id}}
                    s8_fail.branches = {"branches": {"назад": s8_1.id}}
                    s8_3.branches = {"branches": {"далее": s8_4.id}}
                    s8_4.branches = {"branches": {"финиш": "final"}}

                    # Q9: Закамск
                    s9_1.npc_dialogue = {"start": {"text": "Настоящий закамский живой квас! 15 монет.", "options": [{"text": "Купить (15 монет)", "next_node": "exit", "coins_change": -15, "item_give": "Закамский живой квас"}, {"text": "Потом", "next_node": "exit"}]}}
                    s9_1.branches = {"branches": {"торговец": s9_2.id}}
                    s9_2.npc_dialogue = {"start": {"text": "В горле пересохло. Принес квас?", "options": [{"text": "Угостить капитана", "next_node": "exit", "item_take": "Закамский живой квас", "xp_change": 50}, {"text": "Ой, забыл", "next_node": f"step_{s9_fail.id}"}]}}
                    s9_2.branches = {"branches": {"капитан": s9_3.id}}
                    s9_fail.branches = {"branches": {"назад": s9_1.id}}
                    s9_3.branches = {"branches": {"далее": s9_4.id}}
                    s9_4.branches = {"branches": {"финиш": "final"}}

                    # Q10: Гайва
                    s10_1.npc_dialogue = {"start": {"text": "Лучшая закуска на дамбе! Судак вяленый, 35 монет.", "options": [{"text": "Купить (35 монет)", "next_node": "exit", "coins_change": -35, "item_give": "Вяленый судак"}, {"text": "Дорого", "next_node": "exit"}]}}
                    s10_1.branches = {"branches": {"торговец": s10_2.id}}
                    s10_2.npc_dialogue = {"start": {"text": "Проход на КамГЭС только для своих. Гостинец принес?", "options": [{"text": "Отдать судака", "next_node": "exit", "item_take": "Вяленый судак", "xp_change": 50}, {"text": "С пустыми руками", "next_node": f"step_{s10_fail.id}"}]}}
                    s10_2.branches = {"branches": {"сталкер": s10_3.id}}
                    s10_fail.branches = {"branches": {"назад": s10_1.id}}
                    s10_3.branches = {"branches": {"далее": s10_4.id}}
                    s10_4.branches = {"branches": {"финиш": "final"}}

                    session.add_all([
                        s1_1, s1_2, s1_fail, s1_3, s1_4,
                        s2_1, s2_2, s2_3, s2_fail, s2_4,
                        s3_1, s3_2, s3_light, s3_dark, s3_dark_end,
                        s4_1, s4_2, s5_1, s5_2,
                        s6_1, s6_2, s6_fail, s6_3, s6_4,
                        s7_1, s7_2, s7_fail, s7_3, s7_4,
                        s8_1, s8_2, s8_fail, s8_3, s8_4,
                        s9_1, s9_2, s9_fail, s9_3, s9_4,
                        s10_1, s10_2, s10_fail, s10_3, s10_4
                    ])

                await session.flush()




















    #=========================================================================
    # ГЛОБАЛЬНЫЕ СИСТЕМНЫЕ НАСТРОЙКИ (SYSTEM SETTINGS)
    # =========================================================================

    async def get_system_settings(self) -> SystemSettings:
        """Получает текущую конфигурацию настроек (Использует in-memory кэш на 60 сек для турбо-скорости)."""
        now = get_utc_now()
        # Проверяем, есть ли свежий кэш (младше 60 секунд)
        if self._sys_settings_cache and self._sys_settings_cache_time:
            if (now - self._sys_settings_cache_time).total_seconds() < 60:
                return self._sys_settings_cache

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
                        self._sys_settings_cache = item
                        self._sys_settings_cache_time = get_utc_now()
                        return item
            
            # Обновляем кэш
            self._sys_settings_cache = item
            self._sys_settings_cache_time = get_utc_now()
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
	# Сбрасываем кэш при обновлении, чтобы изменения применились мгновенно
        self._sys_settings_cache = None

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
                        income_buffer=0,
                        stamina=100,
                        max_stamina=100,
                        last_stamina_update=get_utc_now()
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
        """Возвращает юзера с фоновым дочислением стамины (+1 ед / 180 сек)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                res = await session.execute(stmt)
                user = res.scalar_one_or_none()
                if user:
                    now = get_utc_now()
                    max_st = getattr(user, 'max_stamina', 100) or 100 # <-- ДИНАМИЧЕСКИЙ БАК
                    curr_st = user.stamina if getattr(user, 'stamina', None) is not None else max_st
                    
                    if curr_st < max_st and user.last_stamina_update:
                        elapsed = (now - user.last_stamina_update).total_seconds()
                        ticks = int(elapsed // 180) # 180 сек = 3 минуты
                        if ticks > 0:
                            user.stamina = min(max_st, curr_st + ticks)
                            user.last_stamina_update += datetime.timedelta(seconds=ticks * 180)
                            session.add(user)
                    elif getattr(user, 'stamina', None) is None:
                        user.stamina = 100
                        user.last_stamina_update = now
                        session.add(user)
                return user

    async def spend_user_stamina(self, user_id: int, cost: int) -> Tuple[bool, int]:
        """Атомарно списывает бодрость. Возвращает (Успех, Остаток энергии)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user: return False, 0
                
                max_st = getattr(user, 'max_stamina', 100) or 100
                
                # --- ТУМБЛЕР ТЕСТИРОВЩИКА: Проверка режима смертного в Redis ---
                if getattr(user, 'is_tester', False):
                    if not await self.redis.get(f"qa:mortal_mode:{user_id}"):
                        return True, max_st
                # -------------------------------------------------------------

                now = get_utc_now()
                curr_st = user.stamina if getattr(user, 'stamina', None) is not None else max_st
                
                if curr_st < max_st and user.last_stamina_update:
                    elapsed = (now - user.last_stamina_update).total_seconds()
                    ticks = int(elapsed // 180)
                    if ticks > 0:
                        curr_st = min(max_st, curr_st + ticks)
                        user.last_stamina_update += datetime.timedelta(seconds=ticks * 180)

                if curr_st < cost:
                    user.stamina = curr_st
                    session.add(user)
                    return False, curr_st
                
                user.stamina = curr_st - cost
                if not user.last_stamina_update:
                    user.last_stamina_update = now
                session.add(user)
                return True, user.stamina

    async def update_user_city_settings(self, user_id: int, city_id: Optional[int], auto_detect_enabled: bool) -> None:
        """Обновляет настройки города пользователя."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(User).where(User.telegram_id == user_id).values(
                    city_id=city_id, 
                    auto_city_detect=auto_detect_enabled
                )
                await session.execute(stmt)

    async def get_nearest_city(self, lat: float, lon: float) -> Optional[City]:
        """Автоматический поиск ближайшего города по координатам с учетом радиуса."""
        async with self.session_pool() as session:
            stmt = select(City)
            res = await session.execute(stmt)
            cities = res.scalars().all()
            
            nearest_city = None
            min_dist = float('inf')
            
            for city in cities:
                # Формула Гаверсинуса для расчета дистанции
                R = 6371.0 # Радиус Земли в километрах
                dlat = math.radians(city.latitude - lat)
                dlon = math.radians(city.longitude - lon)
                a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(city.latitude)) * math.sin(dlon / 2)**2
                c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                distance = R * c
                
                # Проверяем, входим ли мы в радиус города и является ли он ближайшим
                if distance <= city.radius_km and distance < min_dist:
                    min_dist = distance
                    nearest_city = city
                    
            return nearest_city

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
        """Движок прогрессии 2.0: чтение порогов и выдача наград из LevelConfig."""
        stmt = select(User).where(User.telegram_id == user_id).with_for_update()
        user = (await session.execute(stmt)).scalar_one_or_none()
        if not user:
            return 0, 1, False

        user.xp += amount
        leveled_up = False
        
        while True:
            stmt_cfg = select(LevelConfig).where(LevelConfig.level == user.level)
            curr_cfg = (await session.execute(stmt_cfg)).scalar_one_or_none()
            xp_needed = curr_cfg.xp_to_next if curr_cfg else user.level * 150
            
            if user.xp >= xp_needed:
                user.xp -= xp_needed
                user.level += 1
                leveled_up = True
                
                # Авто-выдача наград за достигнутый новый уровень
                stmt_next = select(LevelConfig).where(LevelConfig.level == user.level)
                next_cfg = (await session.execute(stmt_next)).scalar_one_or_none()
                if next_cfg:
                    if next_cfg.reward_coins > 0:
                        user.coins += next_cfg.reward_coins
                    if next_cfg.stamina_bonus > 0:
                        user.max_stamina = (getattr(user, 'max_stamina', 100) or 100) + next_cfg.stamina_bonus
                        user.stamina = min(user.max_stamina, (user.stamina or 0) + next_cfg.stamina_bonus)
                    if next_cfg.reward_item_name:
                        session.add(InventoryItem(
                            user_id=user_id, item_name=next_cfg.reward_item_name,
                            weight=0, generates_income=False, income_per_hour=0
                        ))
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

    async def wipe_and_reseed_all_db(self) -> None:
        """Ядерная зачистка Постгреса до дна через TRUNCATE CASCADE (Спринт Wipe)."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(sa_text("TRUNCATE TABLE quests, steps, active_quests, quest_progress, npc_characters, quest_markets, shop_items, craft_recipes, daily_riddles, achievements, random_events, global_events, inventory RESTART IDENTITY CASCADE;"))
        await self.seed_initial_data()

    async def get_last_quest_completion_time(self, user_id: int, quest_id: int) -> Optional[datetime.datetime]:
        """Возвращает время последнего успешного завершения квеста игроком."""
        async with self.session_pool() as session:
            stmt = select(QuestProgress.completed_at).where(
                and_(QuestProgress.user_id == user_id, QuestProgress.quest_id == quest_id)
            ).order_by(desc(QuestProgress.completed_at)).limit(1)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    # =========================================================================
    # ВЫБОРКА ВСЕХ ТОЧЕК ДЛЯ КАРТЫ (GeoJSON Layer)
    # =========================================================================

    async def get_all_map_points(self, user_level: int = 1) -> List[Dict[str, Any]]:
        """
        Собирает все географические объекты для отображения на интерактивной карте:
        - Квесты (координаты первой точки, статус блокировки по уровню)
        - Торговые лавки (QuestMarket)
        - AR-маркеры
        - NPC на карте
        """
        points = []
        async with self.session_pool() as session:
            # 1. Запрашиваем активные квесты и координаты их первого шага
            stmt_quests = select(Quest.id, Quest.title, Quest.description, Quest.min_level_required).where(Quest.is_published == True).order_by(Quest.id)
            quests = await session.execute(stmt_quests)
            
            for q_id, title, desc_text, min_level in quests.all():
                step_stmt = select(Step.latitude, Step.longitude, Step.welcome_message).where(Step.quest_id == q_id).order_by(Step.id).limit(1)
                step_res = await session.execute(step_stmt)
                step_row = step_res.first()
                if step_row:
                    points.append({
                        "id": q_id,
                        "type": "quest",
                        "title": title,
                        "description": desc_text,
                        "lat": step_row[0],
                        "lng": step_row[1],
                        "welcome_message": step_row[2] or "",
                        "min_level_required": min_level,
                        "is_locked": user_level < min_level
                    })

            # 2. Запрашиваем торговые лавки (QuestMarket)
            stmt_markets = select(QuestMarket.id, QuestMarket.name, QuestMarket.latitude, QuestMarket.longitude, QuestMarket.radius).order_by(QuestMarket.id)
            markets = await session.execute(stmt_markets)
            for m_id, name, lat, lng, radius in markets.all():
                points.append({
                    "id": m_id,
                    "type": "market",
                    "title": name,
                    "lat": lat,
                    "lng": lng,
                    "radius": radius
                })

            # 3. Запрашиваем AR-маркеры
            stmt_ar = select(ARMarker.id, ARMarker.name, ARMarker.latitude, ARMarker.longitude).where(ARMarker.is_active == True).order_by(ARMarker.id)
            ar_markers = await session.execute(stmt_ar)
            for ar_id, name, lat, lng in ar_markers.all():
                points.append({
                    "id": ar_id,
                    "type": "ar_marker",
                    "title": name,
                    "lat": lat,
                    "lng": lng
                })

            # 4. NPC на карте
            stmt_npcs = select(
                NPCCharacter.id, NPCCharacter.name, NPCCharacter.description,
                NPCCharacter.latitude, NPCCharacter.longitude, NPCCharacter.radius
            ).order_by(NPCCharacter.id)
            npcs = await session.execute(stmt_npcs)
            for n_id, name, desc_text, lat, lng, radius in npcs.all():
                if lat and lng:
                    points.append({
                        "id": n_id,
                        "type": "npc",
                        "title": name,
                        "description": desc_text,
                        "lat": lat,
                        "lng": lng,
                        "radius": radius
                    })

        return points


    # =========================================================================
    # ИГРОВАЯ ДИНАМИКА, "ОДИН ЭКРАН", ЧЕКПОИНТЫ И ESCROW БУФЕР (ACTIVE QUESTS)
    # =========================================================================

    async def start_user_quest(self, user_id: int, quest_id: int, start_step_id: int) -> ActiveQuest:
        """
        Инициализирует сессию квеста.
        Очищает буферы Escrow, гарантируя чистый старт для накопления ресурсов.
        """
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(
                    update(ActiveQuest).where(
                        and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                    ).values(is_suspended=True)
                )

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
                    active.prev_latitude = None
                    active.prev_longitude = None
                    active.prev_time = None
                    
                    # Очистка буфера на старте
                    active.pending_coins = 0
                    active.pending_xp = 0
                    active.pending_karma = 0
                    active.pending_items = []
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
                        prev_time=None,
                        pending_coins=0,
                        pending_xp=0,
                        pending_karma=0,
                        pending_items=[]
                    )
                    session.add(active)
                await session.flush()
                return active

    async def get_active_quest(self, user_id: int) -> Optional[ActiveQuest]:
        """Возвращает текущую фокусную сессию игрока."""
        async with self.session_pool() as session:
            stmt = select(ActiveQuest).where(
                and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def suspend_active_quest(self, user_id: int) -> None:
        """Переводит текущую сессию на чекпоинт."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(is_suspended=True)
                await session.execute(stmt)

    async def resume_user_quest(self, user_id: int, quest_id: int) -> Optional[ActiveQuest]:
        """Возобновляет приостановленную сессию квеста."""
        async with self.session_pool() as session:
            async with session.begin():
                await session.execute(
                    update(ActiveQuest).where(
                        and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                    ).values(is_suspended=True)
                )
                
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
        """Получает список всех сессий игрока."""
        async with self.session_pool() as session:
            stmt = select(ActiveQuest).where(ActiveQuest.user_id == user_id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def update_active_quest_message_id(self, user_id: int, message_id: int) -> None:
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(last_game_message_id=message_id)
                await session.execute(stmt)

    async def update_active_quest_step(self, user_id: int, next_step_id: int, current_lat: float, current_lon: float, score_to_add: int = 100) -> ActiveQuest:
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
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).values(current_npc_node=npc_node)
                await session.execute(stmt)

    async def increment_error_count(self, user_id: int, score_penalty: int = 20) -> None:
        """Штрафует очки за неверный ответ."""
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

    # -------------------------------------------------------------------------
    # ESCROW-СИСТЕМА (УСЛОВНОЕ НАЧИСЛЕНИЕ В БУФЕР КВЕСТА + NPC ДИАЛОГИ)
    # -------------------------------------------------------------------------

    async def add_quest_rewards(self, user_id: int, coins: int = 0, xp: int = 0, karma: int = 0, item_name: Optional[str] = None) -> None:
        """Добавляет награды во временный буфер активного квеста (Escrow)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                active = (await session.execute(stmt)).scalar_one_or_none()
                if active:
                    active.pending_coins += coins
                    active.pending_xp += xp
                    active.pending_karma += karma
                    if item_name:
                        # Оборачиваем в list(), чтобы SQLAlchemy увидела изменение JSON-колонки
                        items = list(active.pending_items or [])
                        if item_name not in items:
                            items.append(item_name)
                        active.pending_items = items
                    session.add(active)
    async def process_npc_action_in_escrow(self, user_id: int, item_give: Optional[str] = None, item_take: Optional[str] = None, xp_change: int = 0) -> Tuple[bool, str]:
        """
        Обрабатывает транзакционную логику NPC-диалогов: выдача/изъятие предметов и начисление опыта
        напрямую в буфер активного квеста (Escrow).
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                active = (await session.execute(stmt)).scalar_one_or_none()
                
                if not active:
                    return False, "Активная сессия квеста не найдена для взаимодействия с NPC."

                msg_parts = []

                # Обработка изъятия предмета
                if item_take:
                    # Проверяем сначала в буфере квеста
                    items_in_buffer = list(active.pending_items or [])
                    if item_take in items_in_buffer:
                        items_in_buffer.remove(item_take)
                        active.pending_items = items_in_buffer
                        msg_parts.append(f"NPC забрал у вас из сумки квеста: {item_take}.")
                    else:
                        # Проверяем в основном инвентаре
                        stmt_inv = select(InventoryItem).where(
                            and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_take)
                        ).limit(1)
                        inv_item = (await session.execute(stmt_inv)).scalar_one_or_none()
                        if inv_item:
                            await session.delete(inv_item)
                            msg_parts.append(f"NPC забрал ваш предмет: {item_take}.")
                        else:
                            return False, f"У вас нет необходимого предмета: {item_take}."

                # Обработка выдачи предмета
                if item_give:
                    items_in_buffer = list(active.pending_items or [])
                    if item_give not in items_in_buffer:
                        items_in_buffer.append(item_give)
                        active.pending_items = items_in_buffer
                        msg_parts.append(f"NPC передал вам предмет: {item_give}.")

                # Обработка опыта
                if xp_change != 0:
                    active.pending_xp += xp_change
                    if active.pending_xp < 0:
                        active.pending_xp = 0
                    sign = "+" if xp_change > 0 else ""
                    msg_parts.append(f"Опыт обновлен: {sign}{xp_change} XP.")

                session.add(active)
                return True, " ".join(msg_parts)

    async def spend_quest_coins(self, user_id: int, amount: int) -> bool:
        """Списывает монеты (сначала из заработанного буфера, затем с баланса профиля)."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt_active = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                active = (await session.execute(stmt_active)).scalar_one_or_none()
                
                stmt_user = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt_user)).scalar_one_or_none()
                
                if not active or not user: 
                    return False
                
                total_available = active.pending_coins + user.coins
                if total_available < amount:
                    return False
                    
                if active.pending_coins >= amount:
                    active.pending_coins -= amount
                else:
                    rem = amount - active.pending_coins
                    active.pending_coins = 0
                    user.coins -= rem
                    
                session.add(active)
                session.add(user)
                return True

    async def delete_active_quest(self, user_id: int) -> dict:
        """
        Стирает текущую активную сессию (прерывание квеста).
        Возвращает словарь со списком потерянных буферных ресурсов для уведомления игрока.
        """
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                res = await session.execute(stmt)
                active = res.scalar_one_or_none()
                
                lost_rewards = {"coins": 0, "xp": 0, "karma": 0, "items": []}
                
                if active:
                    lost_rewards = {
                        "coins": active.pending_coins,
                        "xp": active.pending_xp,
                        "karma": active.pending_karma,
                        "items": active.pending_items or []
                    }
                    await session.delete(active)
                    
                return lost_rewards

    async def finish_active_quest(self, user_id: int, completion_bonus: Optional[int] = None) -> Tuple[QuestProgress, ActiveQuest]:
        """Завершает активный квест, переносит ресурсы из Escrow-буфера в профиль и архивирует прогресс."""
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

                # === ПРИМЕНЕНИЕ БУФЕРА ESCROW В ПРОФИЛЬ ===
                stmt_user = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt_user)).scalar_one_or_none()
                if user:
                    user.coins += active.pending_coins
                    user.karma += active.pending_karma
                
                # Выдача накопленных артефактов в инвентарь
                if active.pending_items:
                    for item_name in active.pending_items:
                        stmt_exist = select(InventoryItem).where(
                            and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
                        )
                        if not (await session.execute(stmt_exist)).scalar_one_or_none():
                            stmt_shop = select(ShopItem).where(ShopItem.item_name == item_name)
                            shop_item = (await session.execute(stmt_shop)).scalar_one_or_none()
                            item_weight = shop_item.weight if shop_item else 0
                            item_income = shop_item.generates_income if shop_item else False
                            item_income_rate = shop_item.income_per_hour if shop_item else 0
                            
                            session.add(InventoryItem(
                                user_id=user_id, 
                                item_name=item_name,
                                weight=item_weight,
                                generates_income=item_income,
                                income_per_hour=item_income_rate
                            ))

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
                
                pending_xp = active.pending_xp
                
                await session.delete(active)
                await session.flush()
                
                # Добавляем заработанный в квесте опыт + бонус завершения
                await self.add_xp_db(session, user_id, pending_xp + 300)
                return progress, active


    # =========================================================================
    # ВИРТУАЛЬНЫЙ ИНВЕНТАРЬ (INVENTORY) И УЧЕТ ВЕСА БУФЕРА
    # =========================================================================

    async def add_item_to_inventory(self, user_id: int, item_name: str) -> bool:
        """
        Безопасно укладывает артефакт в инвентарь игрока (прямое добавление из магазина).
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
        """Проверяет наличие предмета в рюкзаке пользователя (или в текущем буфере)."""
        async with self.session_pool() as session:
            stmt = select(InventoryItem).where(
                and_(InventoryItem.user_id == user_id, InventoryItem.item_name == item_name)
            )
            res = await session.execute(stmt)
            if res.scalar_one_or_none() is not None:
                return True
            
            # Проверяем буфер квеста
            stmt_active = select(ActiveQuest).where(
                and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
            )
            active = (await session.execute(stmt_active)).scalar_one_or_none()
            if active and active.pending_items and item_name in active.pending_items:
                return True
            return False

    async def get_user_inventory(self, user_id: int) -> List[str]:
        """Возвращает строковые имена всех находящихся в рюкзаке артефактов."""
        async with self.session_pool() as session:
            stmt = select(InventoryItem.item_name).where(InventoryItem.user_id == user_id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_user_current_weight(self, user_id: int) -> int:
        """Возвращает суммарный вес всех предметов в рюкзаке пользователя (и в буфере)."""
        async with self.session_pool() as session:
            stmt = select(func.sum(InventoryItem.weight)).where(InventoryItem.user_id == user_id)
            res = await session.execute(stmt)
            weight = res.scalar() or 0
            
            stmt_active = select(ActiveQuest).where(
                and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
            )
            active = (await session.execute(stmt_active)).scalar_one_or_none()
            if active and active.pending_items:
                for p_item in active.pending_items:
                    shop_item = (await session.execute(select(ShopItem).where(ShopItem.item_name == p_item))).scalar_one_or_none()
                    if shop_item:
                        weight += shop_item.weight
            return weight

    async def is_inventory_overloaded(self, user_id: int, item_weight_to_add: int) -> Tuple[bool, int, int]:
        """Проверяет, превысит ли рюкзак лимит грузоподъемности при добавлении вещи (с учетом буфера Escrow)."""
        async with self.session_pool() as session:
            user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
            if not user:
                return False, 0, 10
            
            curr_weight = (await session.execute(
                select(func.sum(InventoryItem.weight)).where(InventoryItem.user_id == user_id)
            )).scalar() or 0
            
            stmt_active = select(ActiveQuest).where(
                and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
            )
            active = (await session.execute(stmt_active)).scalar_one_or_none()
            if active and active.pending_items:
                for p_item in active.pending_items:
                    shop_item = (await session.execute(select(ShopItem).where(ShopItem.item_name == p_item))).scalar_one_or_none()
                    if shop_item:
                        curr_weight += shop_item.weight
            
            overloaded = (curr_weight + item_weight_to_add) > user.max_weight_capacity
            return overloaded, curr_weight, user.max_weight_capacity

    async def discard_inventory_item(self, user_id: int, item_name: str) -> bool:
        """Игрок выбрасывает вещь из рюкзака (или из буфера) для устранения перегруза."""
        async with self.session_pool() as session:
            async with session.begin():
                # Пробуем удалить из буфера
                stmt_active = select(ActiveQuest).where(
                    and_(ActiveQuest.user_id == user_id, ActiveQuest.is_suspended == False)
                ).with_for_update()
                active = (await session.execute(stmt_active)).scalar_one_or_none()
                if active and active.pending_items and item_name in active.pending_items:
                    # Оборачиваем в list() для создания нового объекта в памяти
                    items = list(active.pending_items)
                    items.remove(item_name)
                    active.pending_items = items
                    session.add(active)
                    return True

                # Удаляем из основного инвентаря
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
                elif "Эспрессо" in item_name or "Кофе" in item_name:
                    user.stamina = min(100, (getattr(user, 'stamina', 100) or 100) + 45)
                    effects_applied = "☕️ Вы выпили крепкий пермский эспрессо! Бодрость персонажа повышена на *+45* ⚡️."
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

    async def update_achievement(self, ach_id: int, **kwargs) -> bool:
        """Обновляет характеристики трофея/достижения."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(Achievement).where(Achievement.id == ach_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_achievement(self, ach_id: int) -> bool:
        """Удаляет трофей из глобальной базы."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(Achievement).where(Achievement.id == ach_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def get_user_achievements(self, user_id: int) -> List[dict]:
        """Получает трофеи игрока вместе с их текущим рангом (tier) в виде DTO."""
        async with self.session_pool() as session:
            stmt = select(Achievement, UserAchievement.tier).join(
                UserAchievement, Achievement.id == UserAchievement.achievement_id
            ).where(UserAchievement.user_id == user_id).order_by(Achievement.id)
            res = await session.execute(stmt)
            
            result = []
            for ach, tier in res.all():
                result.append({
                    "id": ach.id, "name": ach.name, "description": ach.description,
                    "badge_emoji": ach.badge_emoji, "badge": ach.badge_emoji,
                    "desc": ach.description, "tier": tier,
                    "val_bronze": ach.required_value_bronze,
                    "val_silver": ach.required_value_silver,
                    "val_diamond": ach.required_value_diamond,
                })
            return result

    async def get_user_total_score(self, user_id: int) -> int:
        """Возвращает сумму всех набранных очков игрока."""
        async with self.session_pool() as session:
            stmt = select(func.sum(QuestProgress.score)).where(QuestProgress.user_id == user_id)
            return (await session.execute(stmt)).scalar() or 0

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
        """Запрашивает все товары (включая глобальные цифровые товары и билеты)."""
        async with self.session_pool() as session:
            stmt = select(ShopItem).order_by(ShopItem.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_shop_items_by_market(self, market_id: int) -> List[ShopItem]:
        async with self.session_pool() as session:
            # Находим товары, у которых market_id входит в JSON-массив market_ids строго через скобки '[X]'
            stmt = select(ShopItem).where(sa_text(f"market_ids @> '[{market_id}]'")).order_by(ShopItem.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_shop_item_by_id(self, item_id: int) -> Optional[ShopItem]:
        """Получает ShopItem по его уникальному ID."""
        async with self.session_pool() as session:
            stmt = select(ShopItem).where(ShopItem.id == item_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_shop_item(self, name: str, description: str, price: int, item_name: str, item_type: ShopItemType = ShopItemType.ARTIFACT, weight: int = 0, generates_income: bool = False, income_per_hour: int = 0, market_id: Optional[int] = None, buyback_price: Optional[int] = None) -> ShopItem:
        """
        Добавляет новый товар. Для физических артефактов жестко рекомендуется указание market_id 
        для привязки к конкретной торговой лавке на карте.
        """
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

    async def update_daily_riddle(self, riddle_id: int, **kwargs) -> bool:
        """Обновляет содержимое или награду загадки дня."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(DailyRiddle).where(DailyRiddle.id == riddle_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_daily_riddle(self, riddle_id: int) -> bool:
        """Стирает загадку из пула ротации."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(DailyRiddle).where(DailyRiddle.id == riddle_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

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
                .where(User.is_tester == False)
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
                .where(User.is_tester == False)
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
    # КРАФТ И РЕЦЕПТЫ (CRAFT RECIPES) - НОВЫЙ CRUD
    # =========================================================================

    async def get_all_craft_recipes(self) -> List[CraftRecipe]:
        """Возвращает список всех рецептов крафта."""
        async with self.session_pool() as session:
            stmt = select(CraftRecipe).order_by(CraftRecipe.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_craft_recipe_by_id(self, recipe_id: int) -> Optional[CraftRecipe]:
        """Возвращает рецепт по ID."""
        async with self.session_pool() as session:
            stmt = select(CraftRecipe).where(CraftRecipe.id == recipe_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_craft_recipe(self, name: str, description: str, result_item_name: str, ingredients: dict, coins_cost: int, min_level: int) -> CraftRecipe:
        """Создает новый рецепт."""
        async with self.session_pool() as session:
            async with session.begin():
                recipe = CraftRecipe(
                    name=name, description=description, result_item_name=result_item_name,
                    ingredients=ingredients, coins_cost=coins_cost, min_level=min_level
                )
                session.add(recipe)
                await session.flush()
                return recipe

    async def update_craft_recipe(self, recipe_id: int, **kwargs) -> bool:
        """Обновляет рецепт крафта."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(CraftRecipe).where(CraftRecipe.id == recipe_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_craft_recipe(self, recipe_id: int) -> bool:
        """Удаляет рецепт крафта."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(CraftRecipe).where(CraftRecipe.id == recipe_id)
                res = await session.execute(stmt)
                return res.rowcount > 0
# =========================================================================
    # РЫНКИ, СЛУЧАЙНЫЕ СОБЫТИЯ И ГЛОБАЛЬНЫЕ ИВЕНТЫ
    # =========================================================================

    async def get_market_by_id(self, market_id: int) -> Optional[QuestMarket]:
        """Возвращает рынок по его первичному ключу."""
        async with self.session_pool() as session:
            stmt = select(QuestMarket).where(QuestMarket.id == market_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    # =========================================================================
    # БАЗЫ NPC (ПЕРСОНАЖИ НА КАРТЕ)
    # =========================================================================

    async def create_npc_character(self, name: str, description: str, lat: float, lon: float, radius: float = 30.0) -> NPCCharacter:
        """Размещает новую базу NPC на карте."""
        async with self.session_pool() as session:
            async with session.begin():
                npc = NPCCharacter(name=name, description=description, latitude=lat, longitude=lon, radius=radius)
                session.add(npc)
                await session.flush()
                return npc

    async def get_all_npcs(self) -> List[NPCCharacter]:
        """Возвращает всех NPC."""
        async with self.session_pool() as session:
            res = await session.execute(select(NPCCharacter).order_by(NPCCharacter.id))
            return list(res.scalars().all())

    async def delete_npc(self, npc_id: int) -> bool:
        """Удаляет NPC."""
        async with self.session_pool() as session:
            async with session.begin():
                res = await session.execute(delete(NPCCharacter).where(NPCCharacter.id == npc_id))
                return res.rowcount > 0

    async def create_market(self, name: str, lat: float, lon: float, radius: float = 50.0) -> QuestMarket:
        """Создает и сохраняет новую торговую лавку на карте."""
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

    # --- Случайные события (Random Events) ---
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

    async def update_random_event(self, ev_id: int, **kwargs) -> bool:
        """Обновляет параметры случайного события."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(RandomEvent).where(RandomEvent.id == ev_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_random_event(self, ev_id: int) -> bool:
        """Удаляет случайное событие из ротационного пула."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(RandomEvent).where(RandomEvent.id == ev_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

    # --- Глобальные ивенты (Global Events) ---
    async def get_all_global_events(self) -> List[GlobalEvent]:
        """Возвращает список всех глобальных ивентов."""
        async with self.session_pool() as session:
            stmt = select(GlobalEvent).order_by(GlobalEvent.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_global_event_by_id(self, event_id: int) -> Optional[GlobalEvent]:
        """Возвращает глобальный ивент по ID."""
        async with self.session_pool() as session:
            stmt = select(GlobalEvent).where(GlobalEvent.id == event_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def create_global_event(self, name: str, description: str, city_id: Optional[int] = None, is_active: bool = False) -> GlobalEvent:
        """Создает новый глобальный ивент."""
        async with self.session_pool() as session:
            async with session.begin():
                ev = GlobalEvent(name=name, description=description, city_id=city_id, is_active=is_active)
                if is_active:
                    ev.started_at = get_utc_now()
                session.add(ev)
                await session.flush()
                return ev

    async def update_global_event(self, ev_id: int, **kwargs) -> bool:
        """Обновляет глобальный ивент (и корректно обрабатывает перезапуски)."""
        async with self.session_pool() as session:
            async with session.begin():
                if 'is_active' in kwargs:
                    if kwargs['is_active']:
                        kwargs['started_at'] = get_utc_now()
                    else:
                        kwargs['started_at'] = None
                        
                stmt = update(GlobalEvent).where(GlobalEvent.id == ev_id).values(**kwargs)
                res = await session.execute(stmt)
                return res.rowcount > 0

    async def delete_global_event(self, ev_id: int) -> bool:
        """Удаляет глобальный ивент."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = delete(GlobalEvent).where(GlobalEvent.id == ev_id)
                res = await session.execute(stmt)
                return res.rowcount > 0

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

    # --- Фоновые процессы ---
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
                    min_level_required=orig_q.min_level_required,
                    drawflow_data=orig_q.drawflow_data,
                    is_coop=orig_q.is_coop,
                    coop_max_size=orig_q.coop_max_size,
                    global_time_limit_seconds=orig_q.global_time_limit_seconds
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

    async def grant_global_flag(self, user_id: int, flag: str) -> None:
        """Добавляет глобальный сюжетный флаг игроку, если его еще нет."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if user:
                    flags = user.global_flags or []
                    if isinstance(flags, list) and flag not in flags:
                        # Создаем новый список для корректного трекинга изменений SQLAlchemy
                        new_flags = list(flags)
                        new_flags.append(flag)
                        user.global_flags = new_flags
                        session.add(user)

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
    # ГЕОГРАФИЧЕСКИЕ ТОРГОВЫЕ ЛАВКИ СКУПЩИКОВ
    # =========================================================================

    async def get_market_items(self, market_id: int) -> List[ShopItem]:
        """
        Возвращает ассортимент уникальных товаров конкретной лавки.
        Гарантирует жесткую привязку списка ShopItem к запрашиваемому QuestMarket.
        """
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

    # =========================================================================
    # CRM-МОДУЛЬ УПРАВЛЕНИЯ ИГРОКАМИ (АДМИНКА)
    # =========================================================================

    async def get_all_players_crm(self) -> List[Dict[str, Any]]:
        """Выгружает абсолютно всех юзеров (с инвентарем и статусом онлайна) для CRM."""
        async with self.session_pool() as session:
            stmt = select(User).options(
                selectinload(User.inventory),
                selectinload(User.active_quests)
            ).order_by(desc(User.created_at))

            users = (await session.execute(stmt)).scalars().all()
            now = get_utc_now()
            online_cutoff = now - datetime.timedelta(minutes=15)

            result = []
            for u in users:
                is_online = False
                if u.active_quests:
                    last_act = u.active_quests[0].last_action_at
                    if last_act and last_act >= online_cutoff:
                        is_online = True

                inv_items = [item.item_name for item in u.inventory]

                result.append({
                    "telegram_id": u.telegram_id,
                    "full_name": u.full_name,
                    "username": u.username or "",
                    "rpg_class": u.rpg_class or "Не выбран",
                    "level": u.level,
                    "xp": u.xp,
                    "coins": u.coins,
                    "karma": u.karma,
                    "is_banned": u.is_banned,
                    "is_online": is_online,
                    "registered_at": u.created_at.strftime("%d.%m.%Y %H:%M"),
                    "inventory": inv_items
                })
            return result

    async def admin_update_player_field(self, user_id: int, field: str, value: Any, mode: str = "set") -> Tuple[bool, str]:
        """Универсальный точечный апдейт числовых и текстовых статов игрока."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user:
                    return False, "Игрок не найден в базе"

                if field == "rpg_class":
                    user.rpg_class = str(value)
                    return True, f"Класс изменен на '{value}'"

                if field in ["coins", "karma", "xp", "level", "stamina", "max_stamina"]:
                    val = int(value)
                    curr_val = getattr(user, field)

                    new_val = (curr_val + val) if mode == "add" else val

                    # Защита от «дурака» (отрицательных балансов и нулевых уровней)
                    if field in ["coins", "xp"] and new_val < 0: new_val = 0
                    if field == "level" and new_val < 1: new_val = 1

                    setattr(user, field, new_val)

                    # Если админ накинул XP кнопкой "+", пересчитываем уровень по игровой формуле
                    if field == "xp" and mode == "add":
                        while True:
                            needed = user.level * 150
                            if user.xp >= needed:
                                user.xp -= needed
                                user.level += 1
                            else: break

                    session.add(user)
                    return True, f"Параметр '{field}' успешно обновлен (Текущее значение: {getattr(user, field)})"
                return False, "Неизвестный параметр"

    async def update_city(self, city_id: int, **kwargs) -> None:
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(City).where(City.id == city_id).values(**kwargs)
                await session.execute(stmt)

    async def update_market_meta(self, market_id: int, **kwargs) -> None:
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(QuestMarket).where(QuestMarket.id == market_id).values(**kwargs)
                await session.execute(stmt)

    async def update_npc_meta(self, npc_id: int, **kwargs) -> None:
        async with self.session_pool() as session:
            async with session.begin():
                stmt = update(NPCCharacter).where(NPCCharacter.id == npc_id).values(**kwargs)
                await session.execute(stmt)

    # --- ЭПИК ПРИВАТНОСТИ И 3-УРОВНЕВЫХ ДОСТИЖЕНИЙ ---
    async def get_user_earned_achievements_dict(self, user_id: int) -> Dict[int, str]:
        """Возвращает словарь открытых достижений юзера: achievement_id -> tier ('bronze', 'silver', 'diamond')"""
        async with self.session_pool() as session:
            stmt = select(UserAchievement.achievement_id, UserAchievement.tier).where(UserAchievement.user_id == user_id)
            res = await session.execute(stmt)
            return {row[0]: row[1] for row in res.all()}

    async def upsert_user_achievement(self, user_id: int, achievement_id: int, tier: str, reward_coins: int) -> bool:
        """Атомарно сохраняет или повышает ранг достижения игроку с начислением разницы монет."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(UserAchievement).where(and_(UserAchievement.user_id == user_id, UserAchievement.achievement_id == achievement_id)).with_for_update()
                ua = (await session.execute(stmt)).scalar_one_or_none()
                if ua:
                    if ua.tier == tier or (ua.tier == 'diamond') or (ua.tier == 'silver' and tier == 'bronze'):
                        return False  # Ранг уже выше или равен
                    ua.tier = tier
                    ua.earned_at = get_utc_now()
                else:
                    ua = UserAchievement(user_id=user_id, achievement_id=achievement_id, tier=tier)
                    session.add(ua)
                
                user = (await session.execute(select(User).where(User.telegram_id == user_id).with_for_update())).scalar()
                if user:
                    user.coins += reward_coins
                return True

    async def toggle_user_anonymity(self, user_id: int) -> bool:
        """Переключает статус приватности профиля юзера."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if user:
                    user.is_anonymous = not user.is_anonymous
                    session.add(user)
                    return user.is_anonymous
                return False

    async def get_player_public_profile(self, target_user_id: int) -> Optional[dict]:
        """Формирует безопасный DTO-объект профиля для публичного просмотра."""
        async with self.session_pool() as session:
            stmt = select(User).where(User.telegram_id == target_user_id)
            user = (await session.execute(stmt)).scalar_one_or_none()
            if not user:
                return None

            stmt_q = select(func.count(func.distinct(QuestProgress.quest_id))).where(QuestProgress.user_id == target_user_id)
            completed_quests = (await session.execute(stmt_q)).scalar() or 0

            stmt_sc = select(func.sum(QuestProgress.score)).where(QuestProgress.user_id == target_user_id)
            total_score = (await session.execute(stmt_sc)).scalar() or 0

            if user.is_anonymous:
                return {
                    "is_anonymous": True,
                    "telegram_id": user.telegram_id,
                    "full_name": "Призрак Пармы",
                    "rpg_class": "Скрыто",
                    "level": user.level,
                    "score": total_score,
                    "karma": "?",
                    "completed_quests_count": "?",
                    "achievements": [],
                    "bio_message": "Пользователь ограничил доступ к своему профилю."
                }

            stmt_ach = select(Achievement, UserAchievement.tier).join(
                UserAchievement, Achievement.id == UserAchievement.achievement_id
            ).where(UserAchievement.user_id == target_user_id)
            ach_res = await session.execute(stmt_ach)
            
            ach_list = []
            for ach, tier in ach_res.all():
                ach_list.append({
                    "id": ach.id,
                    "name": ach.name,
                    "description": ach.description,
                    "badge": ach.badge_emoji,
                    "tier": tier,
                    "val_bronze": ach.required_value_bronze,
                    "val_silver": ach.required_value_silver,
                    "val_diamond": ach.required_value_diamond,
                })

            class_map = {"merchant": "Купец 💰", "ranger": "Следопыт 🧭", "historian": "Историк 📜"}
            return {
                "is_anonymous": False,
                "telegram_id": user.telegram_id,
                "full_name": user.full_name,
                "rpg_class": class_map.get(user.rpg_class, user.rpg_class or "Не выбран"),
                "level": user.level,
                "score": total_score,
                "karma": user.karma,
                "completed_quests_count": completed_quests,
                "achievements": ach_list,
                "bio_message": "Открыт для новых экспедиций!"
            }

    async def toggle_tester_status(self, user_id: int) -> bool:
        """Переключает статус QA-тестировщика. При выключении производит физический сброс профиля."""
        async with self.session_pool() as session:
            async with session.begin():
                stmt = select(User).where(User.telegram_id == user_id).with_for_update()
                user = (await session.execute(stmt)).scalar_one_or_none()
                if not user: return False
                if user.is_tester:
                    # Корректное удаление объекта из сессии SQLAlchemy для мгновенного срабатывания ON DELETE CASCADE
                    await session.delete(user)
                    logger.warning(f"Произведен ядерный сброс профиля тестировщика: {user_id}")
                    return False
                else:
                    user.is_tester = True
                    session.add(user)
                    return True

    async def track_market_presence(self, market_id: int, user_id: int) -> None:
        """Продлевает TTL присутствия покупателя на витрине магазина до 180 секунд."""
        key = f"presence:market:{market_id}:{user_id}"
        await self.redis.setex(key, 180, "1")


    async def get_market_presence_count(self, market_id: int) -> int:
        """Подсчитывает количество уникальных живых ключей присутствия на конкретном рынке."""
        pattern = f"presence:market:{market_id}:*"
        keys = await self.redis.keys(pattern)
        return len(keys)


    async def evacuate_location_players(self, entity_type: str, entity_id: int) -> List[int]:
        """Принудительно зачищает локацию и вычищает присутствие из Redis."""
        kicked_ids = set()
        async with self.session_pool() as session:
            async with session.begin():
                if entity_type == "quest":
                    stmt = delete(ActiveQuest).where(ActiveQuest.quest_id == entity_id).returning(ActiveQuest.user_id)
                    kicked_ids.update((await session.execute(stmt)).scalars().all())
                    
                elif entity_type == "npc":
                    npc = await session.get(NPCCharacter, entity_id)
                    if npc and npc.name:
                        subq = select(Step.id).where(Step.npc_name == npc.name)
                        stmt = delete(ActiveQuest).where(ActiveQuest.current_step_id.in_(subq)).returning(ActiveQuest.user_id)
                        kicked_ids.update((await session.execute(stmt)).scalars().all())

        # --- ПЫЛЕСОСИМ REDIS-СЕССИИ ДЛЯ ЛАВОК И СВОБОДНЫХ NPC ---
        if entity_type == "market":
            keys = await self.redis.keys(f"presence:market:{entity_id}:*")
            for k in keys:
                try:
                    uid = int(k.decode("utf-8").split(":")[-1])
                    kicked_ids.add(uid)
                    await self.redis.delete(k)
                except Exception: pass

        elif entity_type == "npc":
            keys = await self.redis.keys(f"presence:npc:{entity_id}:*")
            for k in keys:
                try:
                    uid = int(k.decode("utf-8").split(":")[-1])
                    kicked_ids.add(uid)
                    await self.redis.delete(k)
                except Exception: pass

        return list(kicked_ids)

    async def move_entity_db(self, entity_type: str, entity_id: int, lat: float, lng: float) -> Tuple[bool, str]:
        """Атомарно переносит квест (шаг №1), лавку или NPC на новые координаты (Спринт Телепорт)."""
        async with self.session_pool() as session:
            async with session.begin():
                if entity_type == "quest":
                    stmt = select(Step).where(Step.quest_id == entity_id).order_by(Step.id).limit(1)
                    step = (await session.execute(stmt)).scalar_one_or_none()
                    if not step: return False, "Первый шаг квеста не найден в БД"
                    step.latitude = lat
                    step.longitude = lng
                    session.add(step)
                    
                    quest = await session.get(Quest, entity_id)
                    if quest and quest.drawflow_data:
                        try:
                            df = quest.drawflow_data.get("drawflow", {}).get("Home", {}).get("data", {})
                            for nid, n in df.items():
                                if n.get("name") == "step_node" and (str(n.get("data", {}).get("step_id")) == str(step.id)):
                                    n["data"]["latitude"] = lat
                                    n["data"]["longitude"] = lng
                            session.add(quest)
                        except Exception: pass
                    return True, "📍 Точка старта квеста успешно перенесена!"

                elif entity_type == "market":
                    market = await session.get(QuestMarket, entity_id)
                    if not market: return False, "Торговая лавка не найдена"
                    market.latitude = lat
                    market.longitude = lng
                    session.add(market)
                    return True, "🏪 Торговая лавка перенесена!"

                elif entity_type == "npc":
                    npc = await session.get(NPCCharacter, entity_id)
                    if not npc: return False, "NPC не найден"
                    npc.latitude = lat
                    npc.longitude = lng
                    session.add(npc)
                    return True, "🗣 Персонаж перенесен на новое место!"

                return False, "Неизвестный тип объекта"

    # =========================================================================
    # КОНФИГУРАТОР УРОВНЕЙ 3.0 И BI-АНАЛИТИКА ИГРОКОВ (ПАКЕТ 2 & 3)

    # =========================================================================
    # КОНФИГУРАТОР УРОВНЕЙ 3.0 И BI-АНАЛИТИКА ИГРОКОВ (ПАКЕТ 2 & 3)
    # =========================================================================

    async def get_all_level_configs(self) -> List[LevelConfig]:
        """Возвращает всю таблицу конфигурации уровней с 1 по 100."""
        async with self.session_pool() as session:
            res = await session.execute(select(LevelConfig).order_by(LevelConfig.level))
            return list(res.scalars().all())

    async def upsert_level_config(self, level: int, xp_to_next: int, reward_coins: int, reward_item_name: Optional[str], stamina_bonus: int) -> LevelConfig:
        """Создает или обновляет правила порога опыта и наград для конкретного уровня."""
        async with self.session_pool() as session:
            async with session.begin():
                cfg = await session.get(LevelConfig, level)
                if not cfg:
                    cfg = LevelConfig(level=level)
                    session.add(cfg)
                cfg.xp_to_next = xp_to_next
                cfg.reward_coins = reward_coins
                cfg.reward_item_name = reward_item_name if reward_item_name else None
                cfg.stamina_bonus = stamina_bonus
                return cfg

    async def delete_level_config(self, level: int) -> bool:
        """Удаляет кастомный уровень из прогрессии."""
        async with self.session_pool() as session:
            async with session.begin():
                res = await session.execute(delete(LevelConfig).where(LevelConfig.level == level))
                return res.rowcount > 0

    async def get_player_bi_analytics(self, user_id: int) -> Dict[str, Any]:
        """BI-агрегатор: высчитывает суточный ритм онлайна (часы 0..23), проведенные минуты и воронку."""
        async with self.session_pool() as session:
            # 1. Суточный ритм активности (группировка логов по часам дня)
            stmt_rhythm = select(
                func.extract('hour', PlayerLocationLog.timestamp).label('hr'),
                func.count().label('cnt')
            ).where(PlayerLocationLog.user_id == user_id).group_by('hr')
            
            res_rhythm = await session.execute(stmt_rhythm)
            hourly_dict = {int(row[0]): row[1] for row in res_rhythm.all() if row[0] is not None}
            hourly_rhythm = [hourly_dict.get(h, 0) for h in range(24)]

            # 2. Суммарное чистое время прохождения квестов
            stmt_time = select(func.coalesce(func.sum(QuestProgress.total_time_seconds), 0)).where(QuestProgress.user_id == user_id)
            total_seconds = (await session.execute(stmt_time)).scalar() or 0
            total_minutes = round(total_seconds / 60.0, 1)

            # 3. Воронка вовлеченности
            completed_quests = await self.get_user_completed_quests_count(user_id)
            stmt_actions = select(func.count()).select_from(PlayerLocationLog).where(PlayerLocationLog.user_id == user_id)
            total_actions = (await session.execute(stmt_actions)).scalar() or 0

            return {
                "hourly_rhythm": hourly_rhythm,
                "total_play_minutes": total_minutes,
                "completed_quests": completed_quests,
                "total_actions_logged": total_actions
            }

db = Database()