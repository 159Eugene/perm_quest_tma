# СТАЛО (Добавлен import time и убран дубликат проверки хэша):
import hmac
import hashlib
import json
import logging
import time
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | bool:
    """
    Выполняет криптографическую валидацию строки инициализации (initData) от Telegram Mini App.
    Проверяет HMAC-SHA256 цифровую подпись, сгенерированную на основе BOT_TOKEN.
    """
    try:
        if not init_data:
            return False
            
        parsed_data = dict(parse_qsl(init_data))
        
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            return False

        # --- ЗАЩИТА ОТ REPLAY-АТАК (Устаревшие токены старше 24 часов) ---
        auth_date = int(parsed_data.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            logger.warning("Отклонено: Истек срок годности initData (Возможна Replay Attack)")
            return False
            
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed_data.items())
        )
        
        secret_key = hmac.HMAC(
            key=b"WebAppData",
            msg=bot_token.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        
        calculated_hash = hmac.HMAC(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        if calculated_hash == received_hash:
            user_json = parsed_data.get("user", "{}")
            return json.loads(user_json)
            
        logger.warning("Попытка неавторизованного доступа: Хэш-подписи не совпадают!")
        return False
        
    except Exception as e:
        logger.error(f"Ошибка в процессе валидации initData: {e}", exc_info=True)
        return False