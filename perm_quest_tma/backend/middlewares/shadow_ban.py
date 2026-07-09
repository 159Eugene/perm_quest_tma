import json
import logging
from urllib.parse import parse_qsl
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from redis.asyncio import Redis

from tgbot.database.db_api import db

logger = logging.getLogger(__name__)

class TMAShadowBanMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis: Redis, rate_limit: int = 1):
        super().__init__(app)
        self.redis = redis
        self.rate_limit = rate_limit

    async def dispatch(self, request: Request, call_next):
        init_data = request.headers.get("X-Tg-Init-Data")
        if not init_data:
            return await call_next(request)

        try:
            parsed_data = dict(parse_qsl(init_data))
            user_json = parsed_data.get("user")
            
            if not user_json:
                return await call_next(request)
            
            user_data = json.loads(user_json)
            user_id = user_data.get("id")
            
            if not user_id:
                return await call_next(request)

            # 1. Проверка блокировки (Shadow Ban) с кэшированием в Redis
            cache_key = f"banned:{user_id}"
            cached_status = await self.redis.get(cache_key)
            
            is_banned = False
            if cached_status is not None:
                is_banned = cached_status.decode('utf-8') == "1"
            else:
                is_banned = await db.is_banned(user_id)
                await self.redis.setex(cache_key, 300, "1" if is_banned else "0")

            if is_banned:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Ваш аккаунт заблокирован на платформе."}
                )

            # 2. Флуд-контроль (Rate Limiting)
            # ИСПРАВЛЕНИЕ: Ограничиваем ТОЛЬКО POST/PUT/DELETE запросы (действия).
            # GET-запросы (загрузка карты, инвентаря) проходят свободно.
            if request.method in ["POST", "PUT", "DELETE"] and "/api/quest/ping-location" not in request.url.path:
                limit_key = f"flood:{user_id}"
                is_limited = await self.redis.get(limit_key)
                
                if is_limited:
                    return JSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={"detail": "Слишком частые действия. Пожалуйста, подождите."}
                    )
                # Блокируем следующие действия на rate_limit секунд
                await self.redis.setex(limit_key, self.rate_limit, "1")

        except Exception as e:
            logger.error(f"Ошибка в TMAShadowBanMiddleware: {e}")
        
        return await call_next(request)