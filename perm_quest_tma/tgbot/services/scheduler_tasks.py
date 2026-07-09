import os
import datetime
import logging
import asyncio
from typing import Optional
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete

from tgbot.database.db_api import db
from tgbot.database.models import User, InventoryItem, ActiveQuest, Step
from tgbot.config import settings

logger = logging.getLogger(__name__)

_bot_instance: Optional[Bot] = None

def set_bot_instance(bot: Bot):
    """Устанавливает глобальный инстанс бота для использования в фоновых задачах."""
    global _bot_instance
    _bot_instance = bot

def get_bot_instance() -> Bot:
    if _bot_instance is None:
        raise ValueError("Глобальный синглтон Bot еще не инициализирован!")
    return _bot_instance

async def metrics_monitoring_job() -> None:
    try:
        if not getattr(settings.bot, 'admin_alerts_chat_id', None):
            return

        bot = get_bot_instance()
        m = await db.calculate_realtime_metrics()
        alerts = []

        if m["active_users"] > getattr(settings.bot, 'alert_active_users_threshold', 500):
            alerts.append(f"🏃‍♂️ *Пиковая нагрузка:* Активных игроков {m['active_users']}!")

        if m["bans_per_hour"] > getattr(settings.bot, 'alert_cheat_rate_per_hour', 20):
            alerts.append(f"🚨 *Атака читеров:* За час забанено {m['bans_per_hour']} игроков!")

        if alerts:
            summary = "⚠️ *АВТОМАТИЧЕСКИЙ МОНИТОРИНГ*\n\n" + "\n".join(alerts)
            await bot.send_message(settings.bot.admin_alerts_chat_id, summary, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка мониторинга: {e}")

async def db_backup_job() -> None:
    backup_dir = "/backups"
    if not os.path.exists(backup_dir):
        try: os.makedirs(backup_dir)
        except: return

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"backup_{settings.db.name}_{timestamp}.sql")

    os.environ["PGPASSWORD"] = settings.db.password
    command = [
        "pg_dump", "-h", settings.db.host, "-p", str(settings.db.port),
        "-U", settings.db.user, "-d", settings.db.name, "-F", "c", "-f", backup_file
    ]

    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        if process.returncode == 0:
            logger.info(f"💾 Создан дамп БД: {backup_file}")
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")
    finally:
        if "PGPASSWORD" in os.environ:
            del os.environ["PGPASSWORD"]

    # Ротация дампов (14 дней)
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=14)
        for f_name in os.listdir(backup_dir):
            f_path = os.path.join(backup_dir, f_name)
            if os.path.isfile(f_path) and f_name.endswith(".sql"):
                if datetime.datetime.fromtimestamp(os.path.getmtime(f_path)) < cutoff:
                    os.remove(f_path)
    except Exception:
        pass

async def check_and_send_broadcasts_job() -> None:
    try:
        pending = await db.get_pending_broadcasts()
        if not pending: return

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        bot = get_bot_instance()

        async with db.session_pool() as session:
            user_ids = (await session.execute(select(User.telegram_id).where(User.is_banned == False))).scalars().all()

        for bc in pending:
            if bc.send_at <= now:
                success_count = 0
                for uid in user_ids:
                    try:
                        await bot.send_message(uid, f"📢 *Объявление*\n\n{bc.text}", parse_mode="Markdown")
                        success_count += 1
                        await asyncio.sleep(0.05)
                    except: pass
                await db.mark_broadcast_sent(bc.id)
                logger.info(f"✅ Рассылка {bc.id} завершена. Доставлено: {success_count}.")
    except Exception as e:
        logger.error(f"Ошибка рассылок: {e}")

async def cleanup_expired_mercenaries_job() -> None:
    try:
        bot = get_bot_instance()
        cfg = await db.get_system_settings()
        lifetime = cfg.merc_lifetime_minutes if cfg else 60

        async with db.session_pool() as session:
            async with session.begin():
                cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=lifetime)
                expired = (await session.execute(select(InventoryItem).where(InventoryItem.item_name == "🧙‍♂️ Наемник", InventoryItem.acquired_at < cutoff))).scalars().all()
                
                if expired:
                    uids = [i.user_id for i in expired]
                    await session.execute(delete(InventoryItem).where(InventoryItem.item_name == "🧙‍♂️ Наемник", InventoryItem.acquired_at < cutoff))
                    
                    for uid in uids:
                        try:
                            await bot.send_message(uid, "🧙‍♂️ *Контракт наёмника истек!*\nОн покинул ваш рюкзак.", parse_mode="Markdown")
                        except: pass
    except Exception as e:
        logger.error(f"Ошибка очистки наемников: {e}")

async def passive_income_job() -> None:
    try:
        count = await db.apply_hourly_passive_income()
        if count > 0: logger.info(f"💰 Начислен пассивный доход для {count} юзеров.")
    except Exception as e:
        logger.error(f"Ошибка пассивного дохода: {e}")

def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    set_bot_instance(bot)
    scheduler.add_job(db_backup_job, trigger="cron", hour=3, minute=0, id="db_daily_backup", replace_existing=True)
    scheduler.add_job(metrics_monitoring_job, trigger="interval", minutes=30, id="metrics_monitoring", replace_existing=True)
    scheduler.add_job(check_and_send_broadcasts_job, trigger="interval", minutes=1, id="broadcasts_checker", replace_existing=True)
    scheduler.add_job(cleanup_expired_mercenaries_job, trigger="interval", minutes=5, id="mercenaries_cleaner", replace_existing=True)
    scheduler.add_job(passive_income_job, trigger="interval", hours=1, id="passive_income", replace_existing=True)
    logger.info("APScheduler успешно настроен.")