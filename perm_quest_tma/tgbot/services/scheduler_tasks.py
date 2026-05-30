import os
import datetime
import logging
import asyncio
from typing import Optional
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete
from tgbot.database.db_api import db
from tgbot.database.models import User, InventoryItem, ScheduledBroadcast, ActiveQuest, Step
from tgbot.config import settings

logger = logging.getLogger(__name__)

# Скрытая глобальная переменная для удержания синглтона бота без сериализации в APScheduler
_bot_instance: Optional[Bot] = None


def get_bot_instance() -> Bot:
    """
    Возвращает текущий рабочий синглтон Bot.
    Позволяет фоновым задачам получать доступ к боту без сериализации инстанса.
    """
    global _bot_instance
    if _bot_instance is None:
        raise ValueError("Глобальный синглтон Bot еще не инициализирован в планировщике фоновых задач!")
    return _bot_instance


async def metrics_monitoring_job() -> None:
    """
    Регулярный мониторинг здоровья системы и активности игроков.
    Оповещает администрацию в специальный чат при аномалиях.
    """
    try:
        if not settings.bot.admin_alerts_chat_id:
            logger.info("Мониторинг метрик запущен, но чат для алертов (ADMIN_ALERTS_CHAT_ID) не настроен.")
            return

        # Получаем инстанс бота из синглтона
        bot = get_bot_instance()

        # Расчет актуальной статистики из базы данных
        m = await db.calculate_realtime_metrics()
        alerts = []

        # Проверка пиковой нагрузки пешеходов на сервере
        if m["active_users"] > settings.bot.alert_active_users_threshold:
            alerts.append(
                f"🏃‍♂️ *Пиковая нагрузка:* Количество активных игроков в сети ({m['active_users']}) "
                f"превысило установленный лимит в {settings.bot.alert_active_users_threshold}!"
            )

        # Проверка аномальной частоты блокировок читеров (защита от подмены координат)
        if m["bans_per_hour"] > settings.bot.alert_cheat_rate_per_hour:
            alerts.append(
                f"🚨 *Атака читеров:* За последний час зафиксировано {m['bans_per_hour']} "
                f"блокировок античита! Рекомендуется проверить логи и активность игроков."
            )

        # Если обнаружены критические отклонения — отправляем экстренный отчет
        if alerts:
            summary_message = "⚠️ *СИСТЕМА АВТОМАТИЧЕСКОГО МОНИТОРИНГА БОТА*\n\n" + "\n".join(alerts)
            try:
                await bot.send_message(
                    chat_id=settings.bot.admin_alerts_chat_id,
                    text=summary_message,
                    parse_mode="Markdown"
                )
                logger.warning("Администраторам отправлено экстренное уведомление о метриках системы.")
            except Exception as e:
                logger.error(f"Не удалось отправить алерт в Telegram: {e}")
    except Exception as e:
        logger.error(f"Ошибка выполнения задачи мониторинга метрик: {e}", exc_info=True)


async def db_backup_job() -> None:
    """
    Выполняет автоматическое резервное копирование базы данных PostgreSQL
    с помощью утилиты pg_dump и сохраняет файлы в ротационный архив.
    Дополнительно производит ротацию дампов (удаляет файлы старше 14 дней).
    """
    backup_dir = "/backups"
    if not os.path.exists(backup_dir):
        try:
            os.makedirs(backup_dir)
        except Exception as e:
            logger.error(f"Не удалось создать директорию для бэкапов: {e}")
            return

    # Генерация уникального имени файла дампа
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"backup_{settings.db.name}_{timestamp}.sql")

    # Установка пароля в окружение для pg_dump в изолированном процессе
    os.environ["PGPASSWORD"] = settings.db.password
    command = [
        "pg_dump", "-h", settings.db.host, "-p", str(settings.db.port),
        "-U", settings.db.user, "-d", settings.db.name, "-F", "c", "-f", backup_file
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            logger.info(f"💾 Создан резервный дамп БД PostgreSQL: {backup_file}")
        else:
            logger.error(f"❌ Ошибка утилиты pg_dump: {stderr.decode('utf-8')}")
    except Exception as e:
        logger.error(f"❌ Системное исключение при создании бэкапа БД: {e}", exc_info=True)
    finally:
        # Удаляем пароль из переменных окружения процесса в целях безопасности
        if "PGPASSWORD" in os.environ:
            del os.environ["PGPASSWORD"]

    # Ротация дампов бэкапов (автоудаление файлов из /backups старше 14 дней)
    try:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        cutoff = now - datetime.timedelta(days=14)
        if os.path.exists(backup_dir):
            for file_name in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, file_name)
                if os.path.isfile(file_path) and file_name.startswith("backup_") and file_name.endswith(".sql"):
                    file_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(file_path), datetime.timezone.utc).replace(tzinfo=None)
                    if file_mtime < cutoff:
                        os.remove(file_path)
                        logger.info(f"🗑️ Ротация бэкапов: успешно удален устаревший дамп {file_path}")
    except Exception as e:
        logger.error(f"Ошибка при ротации файлов бэкапов: {e}", exc_info=True)


async def check_and_send_broadcasts_job() -> None:
    """
    Периодическая задача проверки и отправки запланированных рассылок (каждую минуту) через синглтон Bot.
    """
    try:
        pending = await db.get_pending_broadcasts()
        if not pending:
            return

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        bot = get_bot_instance()

        # Выбираем активных пользователей для рассылки
        async with db.session_pool() as session:
            result = await session.execute(select(User.telegram_id).where(User.is_banned == False))
            user_ids = result.scalars().all()

        for bc in pending:
            # Если время отправки наступило или прошло
            if bc.send_at <= now:
                logger.info(f"🚀 Запуск отложенной рассылки ID: {bc.id}")
                success_count = 0
                
                for user_id in user_ids:
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"📢 *Запланированное объявление*\n\n{bc.text}",
                            parse_mode="Markdown"
                        )
                        success_count += 1
                        await asyncio.sleep(0.05)  # Защита от флуд-контроля Telegram API
                    except Exception:
                        pass
                        
                await db.mark_broadcast_sent(bc.id)
                logger.info(f"✅ Отложенная рассылка ID {bc.id} завершена. Доставлено: {success_count} пользователям.")

    except Exception as e:
        logger.error(f"Ошибка в работе планировщика отложенных рассылок: {e}", exc_info=True)


async def cleanup_expired_mercenaries_job() -> None:
    """
    Фоновая задача, удаляющая наёмников из инвентаря пользователей по истечении срока действия контракта.
    Настройки времени жизни подгружаются динамически из SystemSettings.
    Отправляет push-уведомление игрокам, чей наёмник покинул рюкзак.
    """
    try:
        bot = get_bot_instance()
        cfg = await db.get_system_settings()
        lifetime_minutes = cfg.merc_lifetime_minutes if cfg else 60

        async with db.session_pool() as session:
            async with session.begin():
                cutoff_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=lifetime_minutes)
                
                # Извлекаем список наёмников, срок которых истёк
                stmt_select = select(InventoryItem).where(
                    InventoryItem.item_name == "🧙‍♂️ Наемник",
                    InventoryItem.acquired_at < cutoff_time
                )
                res_select = await session.execute(stmt_select)
                expired_items = res_select.scalars().all()
                
                if expired_items:
                    user_ids_to_notify = [item.user_id for item in expired_items]
                    
                    # Массово удаляем истекшие записи
                    stmt_delete = delete(InventoryItem).where(
                        InventoryItem.item_name == "🧙‍♂️ Наемник",
                        InventoryItem.acquired_at < cutoff_time
                    )
                    await session.execute(stmt_delete)
                    logger.info(f"🧹 Фоновая очистка: удалено {len(expired_items)} контрактов наёмников.")
                    
                    # Отправляем push-уведомление каждому пострадавшему пользователю
                    for user_id in user_ids_to_notify:
                        try:
                            await bot.send_message(
                                chat_id=user_id,
                                text="🧙‍♂️ *Срок действия контракта наёмника истек!*\n\nОн бесшумно собрал свои вещи и покинул ваш рюкзак.",
                                parse_mode="Markdown"
                            )
                            logger.info(f"Отправлено push-уведомление об истечении наемника пользователю {user_id}")
                        except Exception:
                            pass
    except Exception as e:
        logger.error(f"Ошибка при фоновой очистке просроченных наёмников: {e}", exc_info=True)


async def auto_update_lock_timers_job() -> None:
    """
    Фоновая задача для автоматического обновления таймеров блокировок (кнопок подсказок)
    на активных игровых экранах пользователей в режиме реального времени.
    """
    try:
        # Динамический импорт во избежание циклической зависимости при инициализации бота
        from tgbot.handlers.user_quest import refresh_step_ui
        bot = get_bot_instance()
        
        async with db.session_pool() as session:
            # Нам нужны только активные, незамороженные и неприостановленные сессии
            stmt = select(ActiveQuest).where(
                ActiveQuest.is_suspended == False,
                ActiveQuest.is_frozen == False,
                ActiveQuest.last_game_message_id != None
            )
            result = await session.execute(stmt)
            active_sessions = result.scalars().all()
            
            for active in active_sessions:
                try:
                    # Загружаем шаг, на котором находится пользователь
                    step_stmt = select(Step).where(Step.id == active.current_step_id)
                    step_res = await session.execute(step_stmt)
                    step = step_res.scalar_one_or_none()
                    if not step:
                        continue
                        
                    # Обновляем UI шага, чтобы пересчитать время до подсказок
                    await refresh_step_ui(bot, active.user_id, step, active)
                    await asyncio.sleep(0.05)  # Защита от флуд-контроля Telegram
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Ошибка в работе автообновления таймеров кнопок: {e}", exc_info=True)


async def passive_income_job() -> None:
    """
    Фоновая задача начисления пассивного дохода от элитных артефактов.
    Она запускается раз в час и накапливает монеты во временный буфер.
    """
    try:
        # Заменено process_hourly_passive_income на apply_hourly_passive_income
        count = await db.apply_hourly_passive_income()
        if count > 0:
            logger.info(f"💰 Начислен пассивный доход в буфер для {count} пользователей.")
    except Exception as e:
        logger.error(f"Ошибка в работе начисления пассивного дохода: {e}", exc_info=True)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """
    Регистрирует и настраивает планировщик фоновых процессов приложения.
    Сохраняет ссылку на синглтон Bot в глобальную переменную модуля.
    """
    global _bot_instance
    _bot_instance = bot

    # Ежедневное резервное копирование в 03:00 ночи (чистые аргументы, нет SSLContext!)
    scheduler.add_job(
        db_backup_job, 
        trigger="cron", 
        hour=3, 
        minute=0,
        id="db_daily_backup", 
        replace_existing=True
    )
    
    # Мониторинг метрик каждые 30 минут (чистые аргументы, нет SSLContext!)
    scheduler.add_job(
        metrics_monitoring_job, 
        trigger="interval", 
        minutes=30,
        id="system_metrics_monitoring", 
        replace_existing=True
    )

    # Проверка отложенных рассылок каждую минуту (чистые аргументы, нет SSLContext!)
    scheduler.add_job(
        check_and_send_broadcasts_job,
        trigger="interval",
        minutes=1,
        id="scheduled_broadcasts_checker",
        replace_existing=True
    )

    # Очистка просроченных наёмников каждые 5 минут (чистые аргументы, нет SSLContext!)
    scheduler.add_job(
        cleanup_expired_mercenaries_job,
        trigger="interval",
        minutes=5,
        id="expired_mercenaries_cleaner",
        replace_existing=True
    )
    
    # Автообновление таймеров на кнопках активных игроков каждую минуту (чистые аргументы, нет SSLContext!)
    scheduler.add_job(
        auto_update_lock_timers_job,
        trigger="interval",
        minutes=1,
        id="lock_timers_auto_updater",
        replace_existing=True
    )

    # Ежечасное начисление пассивного дохода от элитных артефактов в буфер (интервал - 1 час)
    scheduler.add_job(
        passive_income_job,
        trigger="interval",
        hours=1,
        id="passive_income_accruer",
        replace_existing=True
    )
    
    logger.info("Планировщик APScheduler успешно настроен: бэкапы, метрики, рассылки, таймеры, наёмники и доходы активны.")