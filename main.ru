# ==============================================================================
# STRUCTURE & INSTRUCTIONS
# ==============================================================================
# Структура папок для проекта AnobsusVPN:
# .
# ├── .env
# ├── .dockerignore
# ├── Dockerfile
# ├── docker-compose.yml
# ├── requirements.txt
# └── app/
#     ├── __init__.py
#     ├── main.py
#     ├── config/
#     │   ├── __init__.py
#     │   └── settings.py
#     ├── database/
#     │   ├── __init__.py
#     │   ├── base.py
#     │   └── session.py
#     ├── models/
#     │   ├── __init__.py
#     │   └── user.py
#     ├── repositories/
#     │   ├── __init__.py
#     │   └── user_repo.py
#     ├── keyboards/
#     │   ├── __init__.py
#     │   └── reply.py
#     ├── middlewares/
#     │   ├── __init__.py
#     │   └── rate_limit.py
#     ├── handlers/
#     │   ├── __init__.py
#     │   └── start.py
#     └── bot/
#         ├── __init__.py
#         └── dispatcher.py
#
# Для запуска проекта создайте файлы по путям, указанным в комментариях,
# заполните .env и выполните команду: docker-compose up --build -d
# ==============================================================================


# ==============================================================================
# 1. requirements.txt
# ==============================================================================
"""
aiogram==3.17.0
sqlalchemy==2.0.36
asyncpg==0.30.0
alembic==1.14.1
redis==5.2.1
pydantic==2.10.6
pydantic-settings==2.7.1
aiohttp==3.11.11
python-dotenv==1.0.1
structlog==25.1.0
greenlet==3.1.1
"""


# ==============================================================================
# 2. .env.example (сохранить как .env)
# ==============================================================================
"""
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/anobsus_vpn
REDIS_URL=redis://redis:6379/0
ADMIN_IDS=111111111,222222222
BOT_MODE=polling
WEBHOOK_URL=https://yourdomain.com/webhook
SUPPORT_USERNAME=anobsus_support
NEWS_CHANNEL_URL=https://t.me/anobsus_news
VPN_API_URL=https://vpn.api.internal:8443
VPN_API_KEY=super-secure-vpn-api-key
WELCOME_IMAGE_URL=
"""


# ==============================================================================
# 3. Dockerfile
# ==============================================================================
"""
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "app.main"]
"""


# ==============================================================================
# 4. docker-compose.yml
# ==============================================================================
"""
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: anobsus_vpn
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: always

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    restart: always

  bot:
    build: .
    environment:
      - BOT_TOKEN=${BOT_TOKEN}
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/anobsus_vpn
      - REDIS_URL=redis://redis:6379/0
      - ADMIN_IDS=${ADMIN_IDS}
      - VPN_API_URL=${VPN_API_URL}
      - VPN_API_KEY=${VPN_API_KEY}
    depends_on:
      - postgres
      - redis
    restart: always

volumes:
  pgdata:
"""


# ==============================================================================
# 5. app/config/settings.py
# ==============================================================================
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    DATABASE_URL: str
    REDIS_URL: str
    ADMIN_IDS: str
    BOT_MODE: str = "polling"
    WEBHOOK_URL: str = ""
    SUPPORT_USERNAME: str = "support"
    NEWS_CHANNEL_URL: str = "https://t.me/"
    VPN_API_URL: str
    VPN_API_KEY: str
    WELCOME_IMAGE_URL: str = ""

    @property
    def parsed_admin_ids(self) -> list[int]:
        return [int(admin_id.strip()) for admin_id in self.ADMIN_IDS.split(",") if admin_id.strip()]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()


# ==============================================================================
# 6. app/database/base.py
# ==============================================================================
from datetime import datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import DateTime


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ==============================================================================
# 7. app/database/session.py
# ==============================================================================
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session() -> AsyncSession:
    async with async_session_maker() as session:
        yield session


# ==============================================================================
# 8. app/models/user.py
# ==============================================================================
from datetime import datetime
from sqlalchemy import BigInt, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(10), nullable=True, default="ru")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    last_activity: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ==============================================================================
# 9. app/repositories/user_repo.py
# ==============================================================================
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert


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


# ==============================================================================
# 10. app/keyboards/reply.py
# ==============================================================================
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


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


# ==============================================================================
# 11. app/middlewares/rate_limit.py
# ==============================================================================
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message
import redis.asyncio as redis


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, redis_client: redis.Redis, limit: int = 5, period: int = 2):
        self.redis = redis_client
        self.limit = limit
        self.period = period

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        key = f"ratelimit:{user_id}"

        current = await self.redis.get(key)
        if current and int(current) >= self.limit:
            await event.answer("⚠️ Слишком много запросов. Пожалуйста, подождите немного.")
            return

        pipe = self.redis.pipeline()
        pipe.incr(key, 1)
        if not current:
            pipe.expire(key, self.period)
        await pipe.execute()

        return await handler(event, data)


# ==============================================================================
# 12. app/handlers/start.py
# ==============================================================================
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
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


# ==============================================================================
# 13. app/bot/dispatcher.py
# ==============================================================================
import redis.asyncio as redis
from aiogram import Dispatcher


def get_dispatcher(redis_client: redis.Redis) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(RateLimitMiddleware(redis_client))
    dp.include_router(router)
    return dp


# ==============================================================================
# 14. app/main.py
# ==============================================================================
import asyncio
import logging
import sys
import structlog
import redis.asyncio as redis
from aiogram import Bot


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
