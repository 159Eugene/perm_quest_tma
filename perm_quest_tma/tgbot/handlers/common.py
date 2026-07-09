import logging
from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tgbot.database.db_api import db
from tgbot.config import settings

logger = logging.getLogger(__name__)
common_router = Router()

@common_router.message(CommandStart())
async def cmd_start(message: Message):
    """
    Регистрирует нового пользователя и выдает кнопку для входа в TMA.
    """
    # Регистрируем пользователя (если его еще нет в БД)
    await db.get_or_create_user(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username
    )

    # Строим Inline-кнопку для запуска Mini App
    builder = InlineKeyboardBuilder()
    
    # URL для WebApp лучше вынести в .env (например, WEBAPP_URL)
    # Если его там нет, подставь свой домен: url="https://твой-домен.com/"
    webapp_url = getattr(settings.bot, "webapp_url", "https://t.me/твой_бот/app") 
    
    builder.button(
        text="🗺 Открыть Quest Sity",
        web_app=WebAppInfo(url=webapp_url)
    )

    await message.answer(
        "👋 <b>Добро пожаловать в Perm Quest Platform!</b>\n\n"
        "Мы полностью обновили платформу! Теперь все квесты, радар, инвентарь и сражения с NPC происходят в нашем новом удобном <b>Telegram Mini App</b>.\n\n"
        "Нажмите кнопку ниже, чтобы начать приключение!",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )