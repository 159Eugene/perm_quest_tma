import asyncio
import logging
from sqlalchemy import text
from tgbot.database.db_api import db
from tgbot.database.models import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("recreate_db")

async def main():
    logger.info("🚀 Запуск процесса полной очистки и пересоздания базы данных...")
    
    try:
        async with db.engine.begin() as conn:
            # Удаляем всю схему public целиком (обходит циклические зависимости FK)
            logger.info("🧹 Удаление схемы public (DROP SCHEMA CASCADE)...")
            await conn.execute(text("DROP SCHEMA public CASCADE;"))
            await conn.execute(text("CREATE SCHEMA public;"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO postgres;"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public;"))
            logger.info("🗑 Схема public успешно пересоздана.")
            
            # Создаем таблицы заново по новой схеме моделей (models.py)
            logger.info("📦 Создание новых таблиц с обновленной структурой...")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("✨ Новые таблицы успешно созданы.")
            
        # Запускаем посев начальных данных (дефолтные настройки баланса, ачивки, товары, дейлики)
        logger.info("🌱 Посев начальных демонстрационных данных...")
        await db.seed_initial_data()
        logger.info("🎉 База данных успешно воссоздана и наполнена!")
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при пересоздании базы данных: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
