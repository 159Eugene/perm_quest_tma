from typing import Union
from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from tgbot.config import settings

class IsAdmin(BaseFilter):
    """
    Кастомный фильтр aiogram v3 для проверки прав администратора.
    Сверяет уникальный Telegram ID пользователя со списком ADMIN_IDS,
    загруженным в конфигурационный файл settings.
    """
    async def __call__(self, obj: Union[Message, CallbackQuery]) -> bool:
        # Безопасное извлечение ID отправителя события
        user_id = obj.from_user.id if obj.from_user else None
        if not user_id:
            return False
            
        return user_id in settings.bot.admin_ids