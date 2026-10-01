import asyncio
import logging
import sys
import structlog
import redis.asyncio as redis
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, TelegramObject

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import BigInt, String, DateTime


# ==========================================
# 1. НАСТРОЙКИ (Конфигурация)
# ==========================================
class Settings(BaseSettings):
    BOT_TOKEN: str = "ВАШ_ТОКЕН_БОТА"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/anobsus_vpn"
    REDIS_URL: str = "redis://localhost:6379/0"
    ADMIN_IDS: str = "111111111"
    WELCOME_IMAGE_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()


# ==========================================
# 2. БАЗА ДАННЫХ И МОДЕЛИ
# ==========================================
class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(10), nullable=True, default="ru")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    last_activity: Mapped[datetime] = mapped_column(DateTime, nullable=False)

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ==========================================
# 3. РЕПОЗИТОРИЙ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================
class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_user(self, user_id: int, username: str | None, first_name: str, language_code: str | None) -> User:
        stmt = insert(User).values(
            id=user_id,
            username=username,
            first_name=first_name,
            language_code=language_code,
            status="active",
            last_activity=datetime.utcnow(),
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={
                "username": username,
                "first_name": first_name,
                "language_code": language_code,
                "last_activity": datetime.utcnow(),
            },
        ).returning(User)

        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()


# ==========================================
# 4. КЛАВИАТУРЫ
# ==========================================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🟢 Подключиться")],
            [KeyboardButton(text="📱 Прокси Telegram")],
            [KeyboardButton(text="👤 Кабинет"), KeyboardButton(text="🎁 Демо")],
            [KeyboardButton(text="💳 VPN Тарифы"), KeyboardButton(text="📞 Техподдержка")],
            [KeyboardButton(text="📢 Наши новости ↗")],
        ],
        resize_keyboard=True,
    )


# ==========================================
# 5. ЗАЩИТА ОТ СПАМА (Rate Limit Middleware)
# ==========================================
class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, redis_client: redis.Redis, limit: int = 5, period: int = 2):
        self.redis = redis_client
        self.limit = limit
        self.period = period

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        key = f"ratelimit:{user_id}"

        current = await self.redis.get(key)
        if current and int(current) >= self.limit:
            await event.answer("⚠️ Слишком много запросов. Подождите немного.")
            return

        pipe = self.redis.pipeline()
        pipe.incr(key, 1)
        if not current:
            pipe.expire(key, self.period)
        await pipe.execute()

        return await handler(event, data)


# ==========================================
# 6. РОУТЕРЫ И ОБРАБОТЧИКИ (Handlers)
# ==========================================
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        await user_repo.upsert_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            language_code=message.from_user.language_code,
        )

    text = (
        "🔐 **AnobsusVPN**\n\n"
        "Добро пожаловать!\n\n"
        "Быстрое и защищённое подключение к интернету без лишних сложностей.\n\n"
        "⚡ Высокая скорость\n"
        "🛡 Надёжная защита\n"
        "🌍 Глобальный доступ\n"
        "🔒 Конфиденциальность\n\n"
        "Выберите действие ниже:"
    )

    if settings.WELCOME_IMAGE_URL:
        await message.answer_photo(photo=settings.WELCOME_IMAGE_URL, caption=text, reply_markup=get_main_keyboard(), parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()


# ==========================================
# 7. ДИСПЕТЧЕР
# ==========================================
def get_dispatcher(redis_client: redis.Redis) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(RateLimitMiddleware(redis_client))
    dp.include_router(router)
    return dp


# ==========================================
# 8. ЗАПУСК БОТА
# ==========================================
def setup_logging():
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main():
    setup_logging()
    logger = structlog.get_logger()

    logger.info("starting_bot_initialization")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    bot = Bot(token=settings.BOT_TOKEN)
    dp = get_dispatcher(redis_client)

    try:
        logger.info("bot_started_polling")
        await dp.start_polling(bot, session_maker=None)
    finally:
        await redis_client.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped!")
