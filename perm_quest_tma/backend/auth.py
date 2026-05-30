import hmac
import hashlib
import json
import logging
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | bool:
    """
    Выполняет криптографическую валидацию строки инициализации (initData) от Telegram Mini App.
    Проверяет HMAC-SHA256 цифровую подпись, сгенерированную на основе BOT_TOKEN.
    
    Возвращает словарь с верифицированными данными пользователя, если подпись совпадает,
    иначе возвращает False. Это гарантирует невозможность подделки запросов игроками.
    """
    try:
        if not init_data:
            return False
            
        # Разбираем строку запроса в словарь параметров
        parsed_data = dict(parse_qsl(init_data))
        
        # Извлекаем хэш подписи, отправленный Telegram клиентом
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            return False
            
        # Формируем строку проверки (параметры сортируются в алфавитном порядке)
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed_data.items())
        )
        
        # Шаг 1: Генерируем секретный ключ на основе константы WebAppData и BOT_TOKEN
        secret_key = hmac.HMAC(
            key=b"WebAppData",
            msg=bot_token.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        
        # Шаг 2: Рассчитываем итоговую контрольную сумму (HMAC-SHA256)
        calculated_hash = hmac.HMAC(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        # Сравниваем рассчитанный хэш с переданным
        if calculated_hash == received_hash:
            user_json = parsed_data.get("user", "{}")
            return json.loads(user_json)
            
        logger.warning("Попытка неавторизованного доступа: Хэш-подписи не совпадают!")
        return False
        
    except Exception as e:
        logger.error(f"Ошибка в процессе валидации initData: {e}", exc_info=True)
        return False