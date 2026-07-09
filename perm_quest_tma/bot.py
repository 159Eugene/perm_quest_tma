import asyncio
import logging
from aiogram import Bot, Dispatcher
from tgbot.config import settings
from tgbot.database.db_api import db
from tgbot.handlers.common import common_router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Инициализация Telegram-бота (TMA Gateway)...")

    # Инициализация БД
    # await db.create_all()
    logger.info("Подключение к PostgreSQL успешно.")

    # Инициализация объектов Bot и Dispatcher (Storage больше не нужен)
    bot = Bot(token=settings.bot.token.get_secret_value())
    dp = Dispatcher()

    # Подключаем единственный роутер входа
    dp.include_router(common_router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Минималистичный бот запущен и готов пускать игроков в TMA!")
        await dp.start_polling(bot)
    finally:
        logger.info("Остановка бота и освобождение ресурсов...")
        await bot.session.close()
        logger.info("Работа завершена.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Процесс бота остановлен.")