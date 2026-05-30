"""
Веб-админпанель на базе SQLAdmin для управления платформой Perm Quest.
Полностью русифицированный интерфейс.
Доступна по адресу: https://questsity.ru/admin/
"""
import os
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from tgbot.database.models import (
    User, Quest, Step, ActiveQuest, QuestProgress,
    InventoryItem, Achievement, UserAchievement,
    ShopItem, PromoCode, DailyRiddle, CheatLog,
    ScheduledBroadcast, QuestMarket, RandomEvent,
    GlobalEvent, SystemSettings, City, Season,
    CoopSession, CoopMember, PvPDuel, PvPQuestion,
    Guild, GuildMember, CraftRecipe, Challenge,
    UserChallenge, QuestReview, PhotoReport,
    SupportTicket, GemTransaction, ARMarker, ARScanLog
)

import logging as _log

# ---------------------------------------------------------------------------
# АВТОРИЗАЦИЯ
# ---------------------------------------------------------------------------

ADMIN_LOGIN = os.environ.get("ADMIN_PANEL_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PANEL_PASSWORD", "changeme_2026")
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "super-secret-key-change-in-production")

_log.getLogger(__name__).info(f"Admin panel configured: login='{ADMIN_LOGIN}'")


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")
        if username == ADMIN_LOGIN and password == ADMIN_PASSWORD:
            request.session.update({"authenticated": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return request.session.get("authenticated", False)


# ---------------------------------------------------------------------------
# МОДЕЛИ АДМИНПАНЕЛИ (полная русификация)
# ---------------------------------------------------------------------------

class UserAdmin(ModelView, model=User):
    name = "Игрок"
    name_plural = "Игроки"
    icon = "fa-solid fa-users"
    column_list = [User.telegram_id, User.full_name, User.username, User.level, User.xp, User.coins, User.gems, User.karma, User.rpg_class, User.is_banned, User.current_city_id, User.guild_id, User.created_at]
    column_searchable_list = [User.full_name, User.username, User.telegram_id]
    column_sortable_list = [User.level, User.coins, User.gems, User.karma, User.created_at]
    column_default_sort = ("created_at", True)
    column_labels = {
        User.telegram_id: "Telegram ID", User.full_name: "Имя", User.username: "Username",
        User.level: "Уровень", User.xp: "Опыт", User.coins: "Монеты", User.gems: "Гемы",
        User.karma: "Карма", User.rpg_class: "Класс", User.is_banned: "Забанен",
        User.current_city_id: "Город", User.guild_id: "Гильдия", User.created_at: "Дата регистрации"
    }


class CityAdmin(ModelView, model=City):
    name = "Город"
    name_plural = "Города"
    icon = "fa-solid fa-city"
    column_list = [City.id, City.name, City.slug, City.latitude, City.longitude, City.radius_km, City.timezone, City.is_active]
    column_searchable_list = [City.name, City.slug]
    column_labels = {
        City.id: "ID", City.name: "Название", City.slug: "Slug",
        City.latitude: "Широта", City.longitude: "Долгота",
        City.radius_km: "Радиус (км)", City.timezone: "Часовой пояс", City.is_active: "Активен"
    }
    form_args = {
        "name": {"description": "Полное название города (например: Пермь)"},
        "slug": {"description": "URL-идентификатор латиницей (например: perm)"},
        "latitude": {"description": "Широта центра города (например: 58.0097)"},
        "longitude": {"description": "Долгота центра города (например: 56.2444)"},
        "radius_km": {"description": "Радиус покрытия города в километрах"},
        "timezone": {"description": "Часовой пояс (например: Asia/Yekaterinburg)"},
    }


class QuestAdmin(ModelView, model=Quest):
    name = "Квест"
    name_plural = "Квесты"
    icon = "fa-solid fa-map-location-dot"
    column_list = [Quest.id, Quest.title, Quest.is_published, Quest.city_id, Quest.season_id, Quest.is_coop, Quest.min_level_required, Quest.max_speed_kmh, Quest.created_at]
    column_searchable_list = [Quest.title]
    column_sortable_list = [Quest.id, Quest.title, Quest.created_at]
    column_labels = {
        Quest.id: "ID", Quest.title: "Название", Quest.is_published: "Опубликован",
        Quest.city_id: "Город", Quest.season_id: "Сезон", Quest.is_coop: "Кооп",
        Quest.min_level_required: "Мин. уровень", Quest.max_speed_kmh: "Макс. скорость (км/ч)",
        Quest.created_at: "Создан"
    }


class StepAdmin(ModelView, model=Step):
    name = "Шаг квеста"
    name_plural = "Шаги квестов"
    icon = "fa-solid fa-location-dot"
    column_list = [Step.id, Step.quest_id, Step.instruction_text, Step.latitude, Step.longitude, Step.is_final, Step.npc_name]
    column_searchable_list = [Step.instruction_text, Step.npc_name]
    column_labels = {
        Step.id: "ID", Step.quest_id: "Квест", Step.instruction_text: "Задание",
        Step.latitude: "Широта", Step.longitude: "Долгота",
        Step.is_final: "Финальный", Step.npc_name: "NPC"
    }


class SeasonAdmin(ModelView, model=Season):
    name = "Сезон"
    name_plural = "Сезоны"
    icon = "fa-solid fa-calendar-days"
    column_list = [Season.id, Season.name, Season.city_id, Season.starts_at, Season.ends_at, Season.is_active, Season.bonus_xp_multiplier, Season.bonus_coins_multiplier]
    column_labels = {
        Season.id: "ID", Season.name: "Название", Season.city_id: "Город",
        Season.starts_at: "Начало", Season.ends_at: "Конец", Season.is_active: "Активен",
        Season.bonus_xp_multiplier: "Множитель XP", Season.bonus_coins_multiplier: "Множитель монет"
    }


class GuildAdmin(ModelView, model=Guild):
    name = "Гильдия"
    name_plural = "Гильдии"
    icon = "fa-solid fa-shield-halved"
    column_list = [Guild.id, Guild.name, Guild.leader_id, Guild.city_id, Guild.level, Guild.total_xp, Guild.max_members, Guild.emblem_emoji]
    column_searchable_list = [Guild.name]
    column_labels = {
        Guild.id: "ID", Guild.name: "Название", Guild.leader_id: "Лидер",
        Guild.city_id: "Город", Guild.level: "Уровень", Guild.total_xp: "Общий XP",
        Guild.max_members: "Макс. участников", Guild.emblem_emoji: "Эмблема"
    }


class GuildMemberAdmin(ModelView, model=GuildMember):
    name = "Участник гильдии"
    name_plural = "Участники гильдий"
    icon = "fa-solid fa-user-shield"
    column_list = [GuildMember.id, GuildMember.guild_id, GuildMember.user_id, GuildMember.role, GuildMember.contribution_xp, GuildMember.joined_at]
    column_labels = {
        GuildMember.id: "ID", GuildMember.guild_id: "Гильдия", GuildMember.user_id: "Игрок",
        GuildMember.role: "Роль", GuildMember.contribution_xp: "Вклад XP", GuildMember.joined_at: "Вступил"
    }


class PvPDuelAdmin(ModelView, model=PvPDuel):
    name = "PvP Дуэль"
    name_plural = "PvP Дуэли"
    icon = "fa-solid fa-swords"
    column_list = [PvPDuel.id, PvPDuel.city_id, PvPDuel.challenger_id, PvPDuel.opponent_id, PvPDuel.status, PvPDuel.challenger_score, PvPDuel.opponent_score, PvPDuel.winner_id, PvPDuel.bet_coins]
    column_labels = {
        PvPDuel.id: "ID", PvPDuel.city_id: "Город", PvPDuel.challenger_id: "Вызывающий",
        PvPDuel.opponent_id: "Противник", PvPDuel.status: "Статус",
        PvPDuel.challenger_score: "Очки вызывающего", PvPDuel.opponent_score: "Очки противника",
        PvPDuel.winner_id: "Победитель", PvPDuel.bet_coins: "Ставка"
    }


class PvPQuestionAdmin(ModelView, model=PvPQuestion):
    name = "Вопрос PvP"
    name_plural = "Вопросы PvP"
    icon = "fa-solid fa-circle-question"
    column_list = [PvPQuestion.id, PvPQuestion.city_id, PvPQuestion.question, PvPQuestion.correct_answer, PvPQuestion.difficulty]
    column_searchable_list = [PvPQuestion.question]
    column_labels = {
        PvPQuestion.id: "ID", PvPQuestion.city_id: "Город", PvPQuestion.question: "Вопрос",
        PvPQuestion.correct_answer: "Правильный ответ", PvPQuestion.difficulty: "Сложность"
    }


class CoopSessionAdmin(ModelView, model=CoopSession):
    name = "Кооп сессия"
    name_plural = "Кооп сессии"
    icon = "fa-solid fa-people-group"
    column_list = [CoopSession.id, CoopSession.quest_id, CoopSession.leader_id, CoopSession.invite_code, CoopSession.max_players, CoopSession.is_active]
    column_labels = {
        CoopSession.id: "ID", CoopSession.quest_id: "Квест", CoopSession.leader_id: "Лидер",
        CoopSession.invite_code: "Код приглашения", CoopSession.max_players: "Макс. игроков", CoopSession.is_active: "Активна"
    }


class CraftRecipeAdmin(ModelView, model=CraftRecipe):
    name = "Рецепт крафта"
    name_plural = "Рецепты крафта"
    icon = "fa-solid fa-hammer"
    column_list = [CraftRecipe.id, CraftRecipe.name, CraftRecipe.result_item_name, CraftRecipe.coins_cost, CraftRecipe.min_level, CraftRecipe.city_id]
    column_searchable_list = [CraftRecipe.name]
    column_labels = {
        CraftRecipe.id: "ID", CraftRecipe.name: "Название", CraftRecipe.result_item_name: "Результат",
        CraftRecipe.coins_cost: "Стоимость", CraftRecipe.min_level: "Мин. уровень", CraftRecipe.city_id: "Город"
    }


class ChallengeAdmin(ModelView, model=Challenge):
    name = "Челлендж"
    name_plural = "Челленджи"
    icon = "fa-solid fa-bullseye"
    column_list = [Challenge.id, Challenge.title, Challenge.challenge_type, Challenge.target_action, Challenge.target_value, Challenge.reward_coins, Challenge.reward_xp, Challenge.reward_gems, Challenge.city_id, Challenge.is_active]
    column_searchable_list = [Challenge.title]
    column_labels = {
        Challenge.id: "ID", Challenge.title: "Название", Challenge.challenge_type: "Тип",
        Challenge.target_action: "Действие", Challenge.target_value: "Цель",
        Challenge.reward_coins: "Награда монет", Challenge.reward_xp: "Награда XP",
        Challenge.reward_gems: "Награда гемов", Challenge.city_id: "Город", Challenge.is_active: "Активен"
    }


class QuestReviewAdmin(ModelView, model=QuestReview):
    name = "Отзыв"
    name_plural = "Отзывы на квесты"
    icon = "fa-solid fa-star"
    column_list = [QuestReview.id, QuestReview.user_id, QuestReview.quest_id, QuestReview.rating, QuestReview.comment, QuestReview.created_at]
    column_labels = {
        QuestReview.id: "ID", QuestReview.user_id: "Игрок", QuestReview.quest_id: "Квест",
        QuestReview.rating: "Оценка", QuestReview.comment: "Комментарий", QuestReview.created_at: "Дата"
    }


class PhotoReportAdmin(ModelView, model=PhotoReport):
    name = "Фото-отчёт"
    name_plural = "Фото-отчёты"
    icon = "fa-solid fa-camera"
    column_list = [PhotoReport.id, PhotoReport.user_id, PhotoReport.quest_id, PhotoReport.caption, PhotoReport.created_at]
    column_labels = {
        PhotoReport.id: "ID", PhotoReport.user_id: "Игрок", PhotoReport.quest_id: "Квест",
        PhotoReport.caption: "Подпись", PhotoReport.created_at: "Дата"
    }


class SupportTicketAdmin(ModelView, model=SupportTicket):
    name = "Тикет поддержки"
    name_plural = "Тикеты поддержки"
    icon = "fa-solid fa-headset"
    column_list = [SupportTicket.id, SupportTicket.user_id, SupportTicket.subject, SupportTicket.status, SupportTicket.created_at, SupportTicket.resolved_at]
    column_searchable_list = [SupportTicket.subject]
    column_labels = {
        SupportTicket.id: "ID", SupportTicket.user_id: "Игрок", SupportTicket.subject: "Тема",
        SupportTicket.status: "Статус", SupportTicket.created_at: "Создан", SupportTicket.resolved_at: "Решён"
    }


class GemTransactionAdmin(ModelView, model=GemTransaction):
    name = "Транзакция гемов"
    name_plural = "Транзакции гемов"
    icon = "fa-solid fa-gem"
    can_create = False
    can_edit = False
    column_list = [GemTransaction.id, GemTransaction.user_id, GemTransaction.amount, GemTransaction.transaction_type, GemTransaction.description, GemTransaction.created_at]
    column_labels = {
        GemTransaction.id: "ID", GemTransaction.user_id: "Игрок", GemTransaction.amount: "Сумма",
        GemTransaction.transaction_type: "Тип", GemTransaction.description: "Описание", GemTransaction.created_at: "Дата"
    }


class ARMarkerAdmin(ModelView, model=ARMarker):
    name = "AR-маркер"
    name_plural = "AR-маркеры"
    icon = "fa-solid fa-qrcode"
    column_list = [ARMarker.id, ARMarker.city_id, ARMarker.code, ARMarker.name, ARMarker.reward_type, ARMarker.reward_value, ARMarker.is_active]
    column_searchable_list = [ARMarker.name, ARMarker.code]
    column_labels = {
        ARMarker.id: "ID", ARMarker.city_id: "Город", ARMarker.code: "Код",
        ARMarker.name: "Название", ARMarker.reward_type: "Тип награды",
        ARMarker.reward_value: "Значение награды", ARMarker.is_active: "Активен"
    }


class ActiveQuestAdmin(ModelView, model=ActiveQuest):
    name = "Активная сессия"
    name_plural = "Активные сессии"
    icon = "fa-solid fa-play"
    column_list = [ActiveQuest.user_id, ActiveQuest.quest_id, ActiveQuest.current_step_id, ActiveQuest.score, ActiveQuest.errors_count, ActiveQuest.is_suspended, ActiveQuest.is_frozen]
    column_labels = {
        ActiveQuest.user_id: "Игрок", ActiveQuest.quest_id: "Квест",
        ActiveQuest.current_step_id: "Текущий шаг", ActiveQuest.score: "Очки",
        ActiveQuest.errors_count: "Ошибки", ActiveQuest.is_suspended: "Приостановлен", ActiveQuest.is_frozen: "Заморожен"
    }


class QuestProgressAdmin(ModelView, model=QuestProgress):
    name = "Прохождение"
    name_plural = "Прохождения"
    icon = "fa-solid fa-trophy"
    column_list = [QuestProgress.id, QuestProgress.user_id, QuestProgress.quest_id, QuestProgress.score, QuestProgress.total_time_seconds, QuestProgress.errors_count, QuestProgress.completed_at]
    column_default_sort = ("completed_at", True)
    column_labels = {
        QuestProgress.id: "ID", QuestProgress.user_id: "Игрок", QuestProgress.quest_id: "Квест",
        QuestProgress.score: "Очки", QuestProgress.total_time_seconds: "Время (сек)",
        QuestProgress.errors_count: "Ошибки", QuestProgress.completed_at: "Завершён"
    }


class InventoryItemAdmin(ModelView, model=InventoryItem):
    name = "Предмет"
    name_plural = "Инвентарь"
    icon = "fa-solid fa-box"
    column_list = [InventoryItem.id, InventoryItem.user_id, InventoryItem.item_name, InventoryItem.weight, InventoryItem.generates_income, InventoryItem.income_per_hour]
    column_searchable_list = [InventoryItem.item_name]
    column_labels = {
        InventoryItem.id: "ID", InventoryItem.user_id: "Игрок", InventoryItem.item_name: "Предмет",
        InventoryItem.weight: "Вес", InventoryItem.generates_income: "Доход", InventoryItem.income_per_hour: "Доход/час"
    }


class ShopItemAdmin(ModelView, model=ShopItem):
    name = "Товар"
    name_plural = "Магазин"
    icon = "fa-solid fa-shop"
    column_list = [ShopItem.id, ShopItem.name, ShopItem.price, ShopItem.item_type, ShopItem.weight, ShopItem.generates_income, ShopItem.market_id]
    column_searchable_list = [ShopItem.name]
    column_labels = {
        ShopItem.id: "ID", ShopItem.name: "Название", ShopItem.price: "Цена",
        ShopItem.item_type: "Тип", ShopItem.weight: "Вес",
        ShopItem.generates_income: "Пассивный доход", ShopItem.market_id: "Лавка"
    }


class AchievementAdmin(ModelView, model=Achievement):
    name = "Достижение"
    name_plural = "Достижения"
    icon = "fa-solid fa-medal"
    column_list = [Achievement.id, Achievement.name, Achievement.badge_emoji, Achievement.required_action, Achievement.reward_coins]
    column_searchable_list = [Achievement.name]
    column_labels = {
        Achievement.id: "ID", Achievement.name: "Название", Achievement.badge_emoji: "Эмблема",
        Achievement.required_action: "Действие", Achievement.reward_coins: "Награда"
    }


class DailyRiddleAdmin(ModelView, model=DailyRiddle):
    name = "Загадка дня"
    name_plural = "Загадки дня"
    icon = "fa-solid fa-puzzle-piece"
    column_list = [DailyRiddle.id, DailyRiddle.question, DailyRiddle.correct_answer, DailyRiddle.reward_coins]
    column_searchable_list = [DailyRiddle.question]
    column_labels = {
        DailyRiddle.id: "ID", DailyRiddle.question: "Вопрос",
        DailyRiddle.correct_answer: "Ответ", DailyRiddle.reward_coins: "Награда"
    }


class CheatLogAdmin(ModelView, model=CheatLog):
    name = "Лог античита"
    name_plural = "Логи античита"
    icon = "fa-solid fa-triangle-exclamation"
    can_create = False
    can_edit = False
    column_list = [CheatLog.id, CheatLog.user_id, CheatLog.quest_id, CheatLog.speed, CheatLog.created_at]
    column_default_sort = ("created_at", True)
    column_labels = {
        CheatLog.id: "ID", CheatLog.user_id: "Игрок", CheatLog.quest_id: "Квест",
        CheatLog.speed: "Скорость (м/с)", CheatLog.created_at: "Дата"
    }


class ScheduledBroadcastAdmin(ModelView, model=ScheduledBroadcast):
    name = "Рассылка"
    name_plural = "Рассылки"
    icon = "fa-solid fa-paper-plane"
    column_list = [ScheduledBroadcast.id, ScheduledBroadcast.text, ScheduledBroadcast.send_at, ScheduledBroadcast.is_sent]
    column_labels = {
        ScheduledBroadcast.id: "ID", ScheduledBroadcast.text: "Текст",
        ScheduledBroadcast.send_at: "Время отправки", ScheduledBroadcast.is_sent: "Отправлено"
    }


class QuestMarketAdmin(ModelView, model=QuestMarket):
    name = "Торговая лавка"
    name_plural = "Торговые лавки"
    icon = "fa-solid fa-store"
    column_list = [QuestMarket.id, QuestMarket.name, QuestMarket.latitude, QuestMarket.longitude, QuestMarket.radius]
    column_searchable_list = [QuestMarket.name]
    column_labels = {
        QuestMarket.id: "ID", QuestMarket.name: "Название",
        QuestMarket.latitude: "Широта", QuestMarket.longitude: "Долгота", QuestMarket.radius: "Радиус (м)"
    }


class RandomEventAdmin(ModelView, model=RandomEvent):
    name = "Случайное событие"
    name_plural = "Случайные события"
    icon = "fa-solid fa-dice"
    column_list = [RandomEvent.id, RandomEvent.event_type, RandomEvent.text, RandomEvent.probability, RandomEvent.coins_impact, RandomEvent.karma_impact]
    column_labels = {
        RandomEvent.id: "ID", RandomEvent.event_type: "Тип", RandomEvent.text: "Текст",
        RandomEvent.probability: "Вероятность (%)", RandomEvent.coins_impact: "Монеты", RandomEvent.karma_impact: "Карма"
    }


class GlobalEventAdmin(ModelView, model=GlobalEvent):
    name = "Глобальный ивент"
    name_plural = "Глобальные ивенты"
    icon = "fa-solid fa-globe"
    column_list = [GlobalEvent.id, GlobalEvent.name, GlobalEvent.city_id, GlobalEvent.is_active, GlobalEvent.started_at]
    column_searchable_list = [GlobalEvent.name]
    column_labels = {
        GlobalEvent.id: "ID", GlobalEvent.name: "Название", GlobalEvent.city_id: "Город",
        GlobalEvent.is_active: "Активен", GlobalEvent.started_at: "Начало"
    }


class SystemSettingsAdmin(ModelView, model=SystemSettings):
    name = "Настройки"
    name_plural = "Системные настройки"
    icon = "fa-solid fa-gear"
    can_create = False
    can_delete = False
    column_list = [SystemSettings.id, SystemSettings.tutorial_answer, SystemSettings.merchant_bonus, SystemSettings.ranger_cd_minutes, SystemSettings.historian_mult, SystemSettings.base_step_coins, SystemSettings.quest_completion_bonus]
    column_labels = {
        SystemSettings.id: "ID", SystemSettings.tutorial_answer: "Слово обучения",
        SystemSettings.merchant_bonus: "Бонус купца (%)", SystemSettings.ranger_cd_minutes: "КД следопыта (мин)",
        SystemSettings.historian_mult: "Множитель историка", SystemSettings.base_step_coins: "Монет за шаг",
        SystemSettings.quest_completion_bonus: "Бонус за квест"
    }


# ---------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ
# ---------------------------------------------------------------------------

def setup_admin(app, engine):
    """Подключает SQLAdmin к FastAPI."""
    import os as _os
    authentication_backend = AdminAuth(secret_key=ADMIN_SECRET_KEY)
    
    # Путь к кастомным шаблонам с русификацией и картой
    templates_dir = _os.path.join(_os.path.dirname(__file__), "templates")

    admin = Admin(
        app=app,
        engine=engine,
        authentication_backend=authentication_backend,
        title="🗺 Perm Quest — Админпанель",
        base_url="/admin",
        templates_dir=templates_dir
    )

    # Основные
    admin.add_view(CityAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(QuestAdmin)
    admin.add_view(StepAdmin)
    admin.add_view(SeasonAdmin)
    # Мультиплеер
    admin.add_view(GuildAdmin)
    admin.add_view(GuildMemberAdmin)
    admin.add_view(CoopSessionAdmin)
    admin.add_view(PvPDuelAdmin)
    admin.add_view(PvPQuestionAdmin)
    # Геймплей
    admin.add_view(CraftRecipeAdmin)
    admin.add_view(ChallengeAdmin)
    admin.add_view(ARMarkerAdmin)
    admin.add_view(RandomEventAdmin)
    admin.add_view(GlobalEventAdmin)
    # Экономика
    admin.add_view(ShopItemAdmin)
    admin.add_view(InventoryItemAdmin)
    admin.add_view(GemTransactionAdmin)
    admin.add_view(QuestMarketAdmin)
    # Прогресс
    admin.add_view(ActiveQuestAdmin)
    admin.add_view(QuestProgressAdmin)
    admin.add_view(AchievementAdmin)
    # Контент
    admin.add_view(DailyRiddleAdmin)
    admin.add_view(QuestReviewAdmin)
    admin.add_view(PhotoReportAdmin)
    # Система
    admin.add_view(SupportTicketAdmin)
    admin.add_view(ScheduledBroadcastAdmin)
    admin.add_view(CheatLogAdmin)
    admin.add_view(SystemSettingsAdmin)

    return admin
