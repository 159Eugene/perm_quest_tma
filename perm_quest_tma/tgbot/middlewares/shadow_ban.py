import logging
import urllib.parse
import json
from typing import Any, Awaitable, Callable, Dict, Union, Optional

# Импорты для Telegram-бота
from aiogram import BaseMiddleware as AiogramBaseMiddleware
from aiogram.types import Update, CallbackQuery, TelegramObject
from redis.asyncio import Redis

import asyncio  # <-- ДОБАВИЛИ ДЛЯ ПАУЗ TARPITTING
# Импорты для FastAPI (Starlette)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from tgbot.database.db_api import db

logger = logging.getLogger(__name__)

# =====================================================================
# 1. MIDDLEWARE ДЛЯ TELEGRAM-БОТА (Aiogram)
# =====================================================================
class ShadowBanMiddleware(AiogramBaseMiddleware):
    """
    Внешний Middleware для защиты платформы от спама (Flood Control)
    и автоматического отсечения заблокированных пользователей в Боте.
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
        if not isinstance(event, Update):
            return await handler(event, data)

        user_id = None
        user_event = None

        if event.message:
            user_event = event.message
            user_id = event.message.from_user.id
        elif event.callback_query:
            user_event = event.callback_query
            user_id = event.callback_query.from_user.id

        if not user_id:
            return await handler(event, data)

        # 1. Проверка блокировки
        is_banned = await self._is_user_banned(user_id)
        if is_banned:
            if isinstance(user_event, CallbackQuery):
                try:
                    await user_event.answer("⛔ Ваш аккаунт заблокирован на этой платформе.", show_alert=True)
                except Exception: pass
            return

        # 2. Флуд-контроль Бота
        limit_key = f"flood:bot:{user_id}"
        is_limited = await self.redis.get(limit_key)
        if is_limited:
            if isinstance(user_event, CallbackQuery):
                try:
                    await user_event.answer("⚠️ Пожалуйста, не спамьте кнопками!", show_alert=False)
                except Exception: pass
            return

        await self.redis.setex(limit_key, int(max(1, self.rate_limit)), "1")
        return await handler(event, data)

    async def _is_user_banned(self, user_id: int) -> bool:
        cache_key = f"banned:{user_id}"
        try:
            cached_status = await self.redis.get(cache_key)
            if cached_status is not None:
                return cached_status.decode('utf-8') == "1"
        except Exception: pass

        banned = await db.is_banned(user_id)
        try:
            await self.redis.setex(cache_key, 300, "1" if banned else "0")
        except Exception: pass

        return banned







# =====================================================================
# 2. MIDDLEWARE ДЛЯ FASTAPI / TMA (Starlette ASGI)
# =====================================================================
class TMAShadowBanMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis: Redis, rate_limit: Union[int, float] = 10):
        super().__init__(app)
        self.redis = redis
        self.rate_limit = rate_limit

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        path = request.url.path.rstrip("/")

        # 1. ЗЕЛЕНЫЙ КОРИДОР (Фоновый опрос и чистая телеметрия — мгновенный пропуск)
        if path in [
            "/api/presence/heartbeat", 
            "/api/quest/ping-location",
            "/api/quest/active",
            "/api/map/points",
            "/api/profile",
            "/api/items/catalog",
            "/api/cities"
        ] or path.startswith("/api/media/"):
            return await call_next(request)

        auth_header = request.headers.get("X-Tg-Init-Data") or request.headers.get("x-tg-init-data")
        if not auth_header:
            return await call_next(request)

        user_id = self._extract_user_id(auth_header)
        if not user_id:
            return await call_next(request)

        # 2. МНОГОУРОВНЕВАЯ СИСТЕМА DDOS (Паттерн Tarpit / Вязкая смола)
        limit_key = f"flood:api:{user_id}"
        current_requests = await self.redis.incr(limit_key)
        if current_requests == 1:
            await self.redis.expire(limit_key, 2)

        # Математика прогрессивного притормаживания по ТЗ:
        if current_requests > 20:
            await asyncio.sleep(10.0)
        elif current_requests > 9:
            penalty = min(8.0, float(current_requests - 9) * 0.8)
            await asyncio.sleep(penalty)
        elif current_requests > 4:
            await asyncio.sleep(0.5)

        # 3. КРАСНЫЙ КОРИДОР (Эксклюзивный мьютекс на мутирующие экшены)
        is_mutating_action = (
            path in [
                "/api/quest/verify-location",
                "/api/quest/submit-answer",
                "/api/quest/exit",
                "/api/riddle/solve",
            ]
            or path.startswith("/api/quest/start/")
            or path.startswith("/api/quest/npc-choice/")
            or path.startswith("/api/npc/")
            or path.startswith("/api/shop/buy/")
            or path.startswith("/api/craft/")
            or path.startswith("/api/inventory/use/")
            or path.startswith("/api/inventory/discard/")
        )

        if is_mutating_action:
            action_key = f"action_lock:{user_id}"
            # Блокируем повторное нажатие кнопок юзером ровно на 2 секунды. Отдаем 429 для гашения дубля на UI!
            acquired = await self.redis.set(action_key, "1", nx=True, ex=2)
            if not acquired:
                return JSONResponse(
                    status_code=429,
                    content={"status": "error", "message": "Запрос уже обрабатывается..."}
                )

        return await call_next(request)

    def _extract_user_id(self, init_data_str: str) -> Optional[int]:
        try:
            parsed = urllib.parse.parse_qs(init_data_str)
            if "user" in parsed:
                user_obj = json.loads(parsed["user"][0])
                return int(user_obj.get("id"))
        except Exception: pass
        return None

        
        