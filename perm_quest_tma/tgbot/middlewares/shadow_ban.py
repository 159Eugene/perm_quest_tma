import logging
from typing import Any, Awaitable, Callable, Dict, Union
from aiogram import BaseMiddleware
from aiogram.types import Update, Message, CallbackQuery, TelegramObject
from redis.asyncio import Redis

from tgbot.database.db_api import db

logger = logging.getLogger(__name__)


class ShadowBanMiddleware(BaseMiddleware):
    """
    Внешний Middleware для защиты платформы от спама (Flood Control)
    и автоматического отсечения заблокированных пользователей.
    
    Поддерживает обработку как Message, так и CallbackQuery типов обновлений.
    """
    def __init__(self, redis: Redis, rate_limit: Union[int, float] = 1.0):
        self.redis = redis
        self.rate_limit = rate_limit
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Поскольку middleware регистрируется на dp.update, событием выступает Update
        if not isinstance(event, Update):
            return await handler(event, data)

        user_id = None
        user_event = None

        # Извлекаем источник события и ID пользователя
        if event.message:
            user_event = event.message
            user_id = event.message.from_user.id
        elif event.callback_query:
            user_event = event.callback_query
            user_id = event.callback_query.from_user.id

        # Если событие не связано с конкретным пользователем, пропускаем его дальше
        if not user_id:
            return await handler(event, data)

        # 1. Проверка блокировки (Shadow Ban) с кэшированием в Redis во избежание перегрузки СУБД
        is_banned = await self._is_user_banned(user_id)
        if is_banned:
            if isinstance(user_event, CallbackQuery):
                try:
                    await user_event.answer("⛔ Ваш аккаунт заблокирован на этой платформе.", show_alert=True)
                except Exception:
                    pass
            # Молча прерываем цепочку обработки для заблокированного пользователя
            return

        # 2. Флуд-контроль (Rate Limiting) на основе Redis
        limit_key = f"flood:{user_id}"
        is_limited = await self.redis.get(limit_key)
        if is_limited:
            if isinstance(user_event, CallbackQuery):
                try:
                    await user_event.answer("⚠️ Пожалуйста, не спамьте кнопками!", show_alert=False)
                except Exception:
                    pass
            # Прерываем обработку флуд-события
            return

        # Устанавливаем кулдаун в Redis на выполнение следующего действия
        await self.redis.setex(limit_key, self.rate_limit, "1")

        return await handler(event, data)

    async def _is_user_banned(self, user_id: int) -> bool:
        """
        Проверяет статус блокировки пользователя.
        Сначала опрашивает Redis, и только при промахе кэша обращается к PostgreSQL.
        """
        cache_key = f"banned:{user_id}"
        try:
            cached_status = await self.redis.get(cache_key)
            if cached_status is not None:
                return cached_status.decode('utf-8') == "1"
        except Exception as e:
            logger.error(f"Ошибка при работе с кэшем бана Redis: {e}")

        # Обращаемся к PostgreSQL
        banned = await db.is_banned(user_id)

        # Сохраняем результат в кэш на 5 минут (300 секунд)
        try:
            await self.redis.setex(cache_key, 300, "1" if banned else "0")
        except Exception as e:
            logger.error(f"Ошибка сохранения кэша бана в Redis: {e}")

        return banned