import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.redis import RedisJobStore
from tgbot.config import settings
from tgbot.database.db_api import db
from tgbot.handlers.admin_constructor import admin_router
from tgbot.handlers.user_quest import user_quest_router
from tgbot.handlers.common import common_router
from tgbot.middlewares.shadow_ban import ShadowBanMiddleware
from tgbot.services.scheduler_tasks import setup_scheduler

# Настройка продвинутого логирования с выводом в консоль и в файл для мониторинга RPG-платформы
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("info.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Инициализация Telegram-бота квестов Перми...")

    # 1. Асинхронная инициализация базы данных PostgreSQL (создание и проверка таблиц)
    await db.create_all()
    logger.info("Успешное подключение к PostgreSQL и синхронизация таблиц.")

    # 2. Настройка Redis для хранения состояний FSM с ограничением пула соединений
    redis = Redis.from_url(
        settings.redis.redis_url,
        max_connections=20,
        retry_on_timeout=True
    )
    storage = RedisStorage(redis=redis)

    # 3. Настройка APScheduler с Redis-бэкендом для фоновых задач
    job_stores = {
        'default': RedisJobStore(
            host=settings.redis.host,
            port=settings.redis.port,
            db=1
        )
    }
    scheduler = AsyncIOScheduler(jobstores=job_stores)

    # 4. Инициализация объектов Bot и Dispatcher (FSM привязан к Redis)
    bot = Bot(token=settings.bot.token.get_secret_value())
    dp = Dispatcher(storage=storage)

    # 5. Регистрация внешнего middleware для защиты от флуда и спама (ShadowBan)
    dp.update.outer_middleware(ShadowBanMiddleware(redis=redis, rate_limit=1))

    # Передача шедулера, бота и кэш-клиента Redis во все хендлеры через контекст aiogram
    dp["scheduler"] = scheduler
    dp["bot"] = bot
    dp["redis"] = redis

    # 6. Подключение игровых и административных модулей (роутеров)
    dp.include_router(admin_router)
    dp.include_router(user_quest_router)
    dp.include_router(common_router)

    # 7. Инициализация и запуск планировщика фоновых задач с передачей Bot
    setup_scheduler(scheduler, bot)
    scheduler.start()
    logger.info("Планировщик APScheduler успешно запущен.")

    # 8. Запуск бесконечного цикла опроса обновлений Telegram (Polling)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот успешно запущен и готов принимать сообщения игроков!")
        # Передаем redis и scheduler в качестве workflow data для гарантированной доставки в хендлеры
        await dp.start_polling(bot, redis=redis, scheduler=scheduler)
    finally:
        # Корректное закрытие всех соединений при завершении процесса
        logger.info("Остановка бота и освобождение ресурсов...")
        await bot.session.close()
        await redis.close()
        scheduler.shutdown()
        logger.info("Все соединения закрыты. Работа завершена.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Процесс бота принудительно остановлен пользователем.")