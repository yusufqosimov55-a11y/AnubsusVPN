import asyncio
import logging
import os
import ssl
from datetime import datetime, timezone, timedelta
from pathlib import Path
from decimal import Decimal

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ============================================================
# Telegram VPN bot — UI/logic skeleton
# VPN API and payments are intentionally separated from the menu.
# PostgreSQL/Neon is used for users, subscriptions, payments and promo codes.
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vpn_bot")

BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "assets"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ----------------------------- Pricing -----------------------------
# Prices are displayed and stored in USD.
BALANCE_CURRENCY = "USD"
TARIFF_PRICES = {
    7: Decimal("1.00"),
    30: Decimal("3.00"),
    90: Decimal("7.50"),
    365: Decimal("25.00"),
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Neon/Render can provide either postgres:// or postgresql://.
# asyncpg needs the async SQLAlchemy driver in the URL.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql+psycopg2://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# ----------------------------- Database -----------------------------


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    devices: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referrals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Payment(Base):
    __tablename__ = "payments"

    # Payment amounts are currently denominated in USD.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USD", nullable=False)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tariff_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_activations: Mapped[int] = mapped_column(Integer, nullable=False)
    used_activations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# asyncpg does not accept libpq-style ``sslmode`` as a direct connection
# keyword. Neon commonly puts ``sslmode=require`` (and sometimes
# ``channel_binding=require``) into DATABASE_URL. Remove those URL options
# before SQLAlchemy passes the connection arguments to asyncpg, and enable
# TLS explicitly with an SSL context instead.
db_url = make_url(DATABASE_URL)
db_query = dict(db_url.query)
sslmode = str(db_query.get("sslmode", "")).lower()

if "sslmode" in db_query or "channel_binding" in db_query:
    db_url = db_url.difference_update_query(["sslmode", "channel_binding"])

connect_args = {}

if sslmode in {"require", "verify-ca", "verify-full"} or "channel_binding" in DATABASE_URL:
    connect_args["ssl"] = ssl.create_default_context()

engine = create_async_engine(
    db_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create missing tables on first launch.

    For an established production database, Alembic migrations should be used
    for schema changes. create_all is intentionally kept here so the bot can
    start on a fresh Neon database without a manual SQL step.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


# ----------------------------- UI assets -----------------------------

# Put your own images here later. If a file does not exist, the bot sends text only.
IMAGES = {
    "home": BASE_DIR / "IMG_3880.jpeg",        # Главное меню
    "tariffs": BASE_DIR / "IMG_3879.jpeg",     # Тарифы
    "cabinet": BASE_DIR / "IMG_3885.jpeg",    # Профиль
    "support": BASE_DIR / "IMG_3878.jpeg",    # Техподдержка
    "vpn": BASE_DIR / "IMG_3883.jpeg",        # Подключиться

    # Пока используем существующие фото
    "referral": BASE_DIR / "IMG_3878.jpeg",
    "proxy": BASE_DIR / "IMG_3879.jpeg",
    "instructions": BASE_DIR / "IMG_3883.jpeg",
}

# ----------------------------- Helpers -----------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_user(telegram_user) -> User:
    async with SessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user.id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            session.add(user)
        else:
            user.username = telegram_user.username
            user.first_name = telegram_user.first_name
            user.updated_at = utc_now()

        await session.commit()
        return user


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_active_subscription(
    session: AsyncSession,
    user_id: int,
) -> Subscription | None:
    now = utc_now()

    result = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()

    if subscription and subscription.expires_at <= now:
        subscription.status = "expired"
        await session.commit()
        return None

    return subscription


async def subscription_text(user_id: int) -> str:
    async with SessionLocal() as session:
        user = await get_user(session, user_id)

        if not user:
            return "🔴 <b>Подписка:</b> не активна"

        subscription = await get_active_subscription(session, user.id)

        if not subscription:
            return "🔴 <b>Подписка:</b> не активна"

        until = subscription.expires_at
        left = until - utc_now()

        if left.total_seconds() <= 0:
            return "🔴 <b>Подписка:</b> закончилась"

        days = left.days
        hours = left.seconds // 3600

        return (
            f"🟢 <b>Подписка:</b> активна\n"
            f"⏳ Осталось: <b>{days} дн. {hours} ч.</b>\n"
            f"📅 До: <b>{until.strftime('%d.%m.%Y %H:%M')}</b>"
        )


async def add_subscription_days(telegram_id: int, days: int) -> datetime | None:
    if days <= 0:
        return None

    async with SessionLocal() as session:
        user = await get_user(session, telegram_id)

        if user is None:
            return None

        now = utc_now()

        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
            .with_for_update()
        )
        subscription = result.scalar_one_or_none()

        if subscription and subscription.expires_at > now:
            subscription.expires_at = subscription.expires_at + timedelta(days=days)
            subscription.updated_at = now
        elif subscription:
            subscription.status = "expired"
            subscription.expires_at = now + timedelta(days=days)
            subscription.status = "active"
            subscription.updated_at = now
        else:
            subscription = Subscription(
                user_id=user.id,
                expires_at=now + timedelta(days=days),
                status="active",
            )
            session.add(subscription)

        await session.commit()
        return subscription.expires_at


async def mark_expired_subscriptions() -> None:
    """Update old active rows for statistics/history.

    Access control does not depend on this function: every access checks
    expires_at against the current UTC time directly.
    """
    async with SessionLocal() as session:
        await session.execute(
            Subscription.__table__.update()
            .where(
                Subscription.status == "active",
                Subscription.expires_at <= utc_now(),
            )
            .values(status="expired", updated_at=utc_now())
        )
        await session.commit()


# ----------------------------- Anti-spam -----------------------------

# This is intentionally simple for one Render process. When the bot is scaled
# to multiple instances, move this limiter to Redis.
RATE_LIMIT_WINDOW = 2.0
RATE_LIMIT_MAX_REQUESTS = 8
_rate_limit: dict[int, list[float]] = {}


def allowed(user_id: int) -> bool:
    now = asyncio.get_running_loop().time()
    values = _rate_limit.setdefault(user_id, [])
    values[:] = [value for value in values if now - value < RATE_LIMIT_WINDOW]

    if len(values) >= RATE_LIMIT_MAX_REQUESTS:
        return False

    values.append(now)
    return True


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ----------------------------- Temporary compatibility -----------------------------
# The old demo_users dictionary is intentionally retained so the original
# project structure remains recognizable. Production data is now stored in DB.

demo_users: dict[int, dict] = {}


# ----------------------------- Keyboards -----------------------------


def back_kb(target: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data=target)]
        ]
    )


def home_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔐 Подключить VPN", callback_data="vpn"),
            InlineKeyboardButton(text="💳 Тарифы", callback_data="tariffs"),
        ],
        [
            InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet"),
            InlineKeyboardButton(text="🌍 Серверы", callback_data="servers"),
        ],
        [
            InlineKeyboardButton(text="🎁 Пробный период", callback_data="trial"),
            InlineKeyboardButton(text="👥 Пригласить друга", callback_data="referral"),
        ],
        [
            InlineKeyboardButton(text="🛠 Поддержка", callback_data="support"),
            InlineKeyboardButton(text="📚 Помощь", callback_data="help"),
        ],
        [InlineKeyboardButton(text="📢 Новости", callback_data="news")],
    ]

    if user_id is not None and is_admin(user_id):
        rows.append([
            InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin")
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def vpn_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Получить доступ", callback_data="get_access")],
            [InlineKeyboardButton(text="📱 Мои устройства", callback_data="devices")],
            [InlineKeyboardButton(text="🌍 Выбрать сервер", callback_data="servers")],
            [InlineKeyboardButton(text="📖 Как подключиться", callback_data="instructions")],
            [InlineKeyboardButton(text="🔄 Обновить доступ", callback_data="renew")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )


def tariffs_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="7 дней — $1.00", callback_data="buy_7"),
                InlineKeyboardButton(text="1 месяц — $3.00", callback_data="buy_30"),
            ],
            [
                InlineKeyboardButton(text="3 месяца — $7.50", callback_data="buy_90"),
                InlineKeyboardButton(text="1 год — $25.00", callback_data="buy_365"),
            ],
            [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )


def cabinet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Мой VPN", callback_data="vpn")],
            [InlineKeyboardButton(text="💳 Купить / продлить", callback_data="tariffs")],
            [InlineKeyboardButton(text="📱 Устройства", callback_data="devices")],
            [InlineKeyboardButton(text="💰 Платежи", callback_data="payments")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )


def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Не подключается VPN", callback_data="support_connect")],
            [InlineKeyboardButton(text="💳 Проблема с оплатой", callback_data="support_payment")],
            [InlineKeyboardButton(text="📱 Проблема с устройством", callback_data="support_device")],
            [InlineKeyboardButton(text="💬 Написать оператору", callback_data="support_operator")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )


# ----------------------------- Admin keyboards -----------------------------


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика и аналитика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🔎 Поиск и управление пользователями", callback_data="admin_users")],
            [InlineKeyboardButton(text="📢 Массовая рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🎁 Ручная выдача дней / бонусов", callback_data="admin_bonus")],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_promo")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )


# ----------------------------- FSM states -----------------------------


class UserStates(StatesGroup):
    promo = State()


class AdminStates(StatesGroup):
    search = State()
    broadcast = State()
    bonus_user = State()
    bonus_days = State()
    promo_code = State()
    promo_days = State()
    promo_uses = State()


# ----------------------------- Message helpers -----------------------------


async def show_screen(
    message: Message,
    *,
    key: str,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    image = IMAGES.get(key)
    if image and image.exists():
        await message.answer_photo(
            photo=FSInputFile(image),
            caption=text,
            reply_markup=keyboard,
        )
    else:
        await message.answer(text, reply_markup=keyboard)


async def edit_screen(
    callback: CallbackQuery,
    *,
    key: str,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    # Telegram cannot turn an existing text message into a photo message.
    # For the first version we delete the old message and send the new screen.
    try:
        await callback.message.delete()
    except Exception:
        pass

    await show_screen(callback.message, key=key, text=text, keyboard=keyboard)
    await callback.answer()


# ----------------------------- Screens -----------------------------

HOME_TEXT = """
<b>🚀 Добро пожаловать!</b>

Быстрый и удобный VPN для повседневного использования.

🔒 Защищённое соединение
⚡ Стабильные серверы
📱 Поддержка нескольких устройств
🛠 Помощь прямо в Telegram

Выберите нужный раздел ниже:
"""

VPN_TEXT = """
<b>🔐 Мой VPN</b>

{status}

Здесь можно получить доступ, посмотреть устройства,
выбрать сервер или открыть инструкцию по подключению.

<i>Сейчас работает демонстрационный режим.
Реальный VPN API подключим следующим этапом.</i>
"""

TARIFFS_TEXT = """
<b>💳 Тарифы</b>

Выберите срок подписки:

🟢 7 дней — <b>$1.00</b>
🔵 1 месяц — <b>$3.00</b>
🟣 3 месяца — <b>$7.50</b>
🟠 1 год — <b>$25.00</b>

Цены указаны в долларах США.
Выберите срок подписки ниже.
"""

CABINET_TEXT = """
<b>👤 Личный кабинет</b>

🆔 ID: <code>{user_id}</code>
{status}

📱 Устройства: <b>{devices}</b>
🎁 Приглашено друзей: <b>{referrals}</b>
💰 Баланс: <b>${balance}</b>
"""

SUPPORT_TEXT = """
<b>🛠 Поддержка</b>

Опишите проблему или выберите подходящий раздел.
Не отправляйте пароль, токены или платёжные данные.

Среднее время ответа оператора зависит от нагрузки.
"""

HELP_TEXT = """
<b>📚 Помощь</b>

<b>Как подключиться?</b>
1. Откройте «Мой VPN».
2. Получите доступ.
3. Выберите сервер.
4. Установите приложение по инструкции.
5. Импортируйте конфигурацию.

Если что-то не работает — откройте «Поддержка».
"""

SERVERS_TEXT = """
<b>🌍 Серверы</b>

Доступные направления:

🇩🇪 Германия — подготовка
🇳🇱 Нидерланды — подготовка
🇫🇮 Финляндия — подготовка

После подключения реального VPN API статус
серверов будет показываться автоматически.
"""

REFERRAL_TEXT = """
<b>👥 Партнёрская программа</b>

Приглашайте друзей по своей ссылке.

🔗 Ваша ссылка:
<code>https://t.me/ВАШ_БОТ?start=ref_{user_id}</code>

🎁 Условия и бонусы можно настроить после подключения
платежей и реальной системы подписок.
"""

PROXY_TEXT = """
<b>🌐 Telegram Proxy</b>

Здесь можно будет получить актуальные параметры
прокси для Telegram.

Пока раздел работает как интерфейс-заглушка.
"""

INSTRUCTIONS_TEXT = """
<b>📖 Подключение VPN</b>

1️⃣ Установите поддерживаемое VPN-приложение.
2️⃣ Получите конфигурацию в разделе «Мой VPN».
3️⃣ Импортируйте её в приложение.
4️⃣ Включите соединение.
5️⃣ Проверьте статус.

После подключения VPN API сюда можно добавить
автоматическую выдачу конфигурации.
"""


# ----------------------------- Handlers -----------------------------

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(message.from_user)
    await show_screen(
        message,
        key="home",
        text=HOME_TEXT,
        keyboard=home_kb(message.from_user.id),
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(callback.from_user)
    await edit_screen(
        callback,
        key="home",
        text=HOME_TEXT,
        keyboard=home_kb(callback.from_user.id),
    )


@dp.callback_query(F.data == "vpn")
async def vpn(callback: CallbackQuery) -> None:
    await ensure_user(callback.from_user)
    text = VPN_TEXT.format(status=await subscription_text(callback.from_user.id))
    await edit_screen(callback, key="vpn", text=text, keyboard=vpn_kb())


@dp.callback_query(F.data == "tariffs")
async def tariffs(callback: CallbackQuery) -> None:
    await ensure_user(callback.from_user)
    await edit_screen(callback, key="tariffs", text=TARIFFS_TEXT, keyboard=tariffs_kb())


@dp.callback_query(F.data == "cabinet")
async def cabinet(callback: CallbackQuery) -> None:
    user = await ensure_user(callback.from_user)

    async with SessionLocal() as session:
        db_user = await get_user(session, callback.from_user.id)
        data = db_user

    text = CABINET_TEXT.format(
        user_id=callback.from_user.id,
        status=await subscription_text(callback.from_user.id),
        devices=data.devices if data else 0,
        referrals=data.referrals if data else 0,
        balance=data.balance if data else 0,
    )
    await edit_screen(callback, key="cabinet", text=text, keyboard=cabinet_kb())


@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="support", text=SUPPORT_TEXT, keyboard=support_kb())


@dp.callback_query(F.data == "help")
async def help_screen(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="instructions", text=HELP_TEXT, keyboard=back_kb())


@dp.callback_query(F.data == "servers")
async def servers(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇩🇪 Германия", callback_data="server_de")],
            [InlineKeyboardButton(text="🇳🇱 Нидерланды", callback_data="server_nl")],
            [InlineKeyboardButton(text="🇫🇮 Финляндия", callback_data="server_fi")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
        ]
    )
    await edit_screen(callback, key="vpn", text=SERVERS_TEXT, keyboard=kb)


@dp.callback_query(F.data.startswith("server_"))
async def server_selected(callback: CallbackQuery) -> None:
    code = callback.data.split("_", 1)[1]
    names = {"de": "🇩🇪 Германия", "nl": "🇳🇱 Нидерланды", "fi": "🇫🇮 Финляндия"}
    name = names.get(code, "Неизвестный сервер")
    text = (
        f"<b>{name}</b>\n\n"
        "🟡 Сервер пока не подключён к VPN API.\n\n"
        "Когда инфраструктура будет готова, здесь будут "
        "реальный статус, нагрузка, пинг и кнопка выбора."
    )
    await edit_screen(callback, key="vpn", text=text, keyboard=back_kb("servers"))


@dp.callback_query(F.data == "referral")
async def referral(callback: CallbackQuery) -> None:
    text = REFERRAL_TEXT.format(user_id=callback.from_user.id)
    await edit_screen(callback, key="referral", text=text, keyboard=back_kb())


@dp.callback_query(F.data == "proxy")
async def proxy(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="proxy", text=PROXY_TEXT, keyboard=back_kb())


@dp.callback_query(F.data == "instructions")
async def instructions(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="instructions", text=INSTRUCTIONS_TEXT, keyboard=back_kb("vpn"))


@dp.callback_query(F.data == "devices")
async def devices(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)

    text = (
        "<b>📱 Мои устройства</b>\n\n"
        f"Подключено: <b>{user.devices if user else 0}</b>\n\n"
        "Лимит устройств будет зависеть от тарифа. "
        "После подключения VPN API здесь появится управление "
        "активными устройствами."
    )
    await edit_screen(callback, key="cabinet", text=text, keyboard=back_kb("cabinet"))


@dp.callback_query(F.data == "payments")
async def payments(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        rows = []

        if user:
            result = await session.execute(
                select(Payment)
                .where(Payment.user_id == user.id)
                .order_by(Payment.created_at.desc())
                .limit(10)
            )
            rows = result.scalars().all()

    if not rows:
        text = (
            "<b>💰 История платежей</b>\n\n"
            "Пока платежей нет.\n\n"
            "После подключения платёжной системы здесь будут "
            "дата, сумма, тариф и статус каждой операции."
        )
    else:
        lines = ["<b>💰 История платежей</b>", ""]
        for payment in rows:
            date_text = payment.created_at.strftime("%d.%m.%Y %H:%M")
            status_text = "✅ Оплачен" if payment.status == "paid" else "⏳ Ожидает"
            lines.append(
                f"{date_text} — <b>{payment.amount} {payment.currency}</b> — {status_text}"
            )
        text = "\n".join(lines)

    await edit_screen(callback, key="cabinet", text=text, keyboard=back_kb("cabinet"))


@dp.callback_query(F.data == "trial")
async def trial(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)

    if user and user.trial_used:
        text = "<b>🎁 Пробный период</b>\n\nВы уже использовали пробный период."
    else:
        text = (
            "<b>🎁 Пробный период</b>\n\n"
            "В демонстрационном режиме пробный доступ не выдаётся.\n"
            "После подключения VPN API здесь будет автоматическая выдача."
        )
    await edit_screen(callback, key="vpn", text=text, keyboard=back_kb())


@dp.callback_query(F.data == "get_access")
async def get_access(callback: CallbackQuery) -> None:
    await edit_screen(
        callback,
        key="vpn",
        text=(
            "<b>🔐 Получение доступа</b>\n\n"
            "Сейчас VPN API ещё не подключён.\n\n"
            "На следующем этапе эта кнопка будет создавать "
            "персональный VPN-доступ и отправлять конфигурацию пользователю."
        ),
        keyboard=back_kb("vpn"),
    )


@dp.callback_query(F.data == "renew")
async def renew(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="tariffs", text=TARIFFS_TEXT, keyboard=tariffs_kb())


@dp.callback_query(F.data.startswith("buy_"))
async def buy_tariff(callback: CallbackQuery) -> None:
    period = callback.data.split("_", 1)[1]
    labels = {"7": "7 дней", "30": "1 месяц", "90": "3 месяца", "365": "1 год"}
    label = labels.get(period, period)
    days = int(period) if period.isdigit() else 0
    price = TARIFF_PRICES.get(days)
    price_text = f"${price:.2f}" if price is not None else "уточняется"

    # The payment provider is deliberately not faked here.
    await edit_screen(
        callback,
        key="tariffs",
        text=(
            f"<b>💳 Выбран тариф: {label}</b>\n"
            f"💵 Стоимость: <b>{price_text}</b>\n\n"
            "Платёжная система пока не подключена.\n\n"
            "На следующем этапе здесь будет реальная оплата, "
            "проверка платежа и автоматическая активация подписки."
        ),
        keyboard=back_kb("tariffs"),
    )


# ----------------------------- User promo code -----------------------------

@dp.callback_query(F.data == "promo")
async def promo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.promo)
    await callback.message.answer(
        "<b>🎟 Промокод</b>\n\n"
        "Отправьте промокод отдельным сообщением.\n"
        "Для отмены отправьте: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(UserStates.promo)
async def promo_entered(message: Message, state: FSMContext) -> None:
    if not allowed(message.from_user.id):
        return await message.answer("⏳ Слишком много запросов. Подождите немного.")

    raw_code = (message.text or "").strip()

    if raw_code.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Промокод отменён.", reply_markup=home_kb(message.from_user.id))

    if not raw_code:
        return await message.answer("❌ Отправьте промокод текстом.")

    code = " ".join(raw_code.split()).upper()

    await ensure_user(message.from_user)

    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)

        if not user:
            await state.clear()
            return await message.answer("❌ Пользователь не найден. Отправьте /start ещё раз.")

        result = await session.execute(
            select(PromoCode)
            .where(func.upper(PromoCode.code) == code)
            .with_for_update()
        )
        promo_code = result.scalar_one_or_none()

        if not promo_code:
            return await message.answer("❌ Промокод не найден.")

        if not promo_code.active or promo_code.used_activations >= promo_code.max_activations:
            return await message.answer("❌ Этот промокод больше недоступен.")

        existing = await session.execute(
            select(PromoRedemption).where(
                PromoRedemption.promo_id == promo_code.id,
                PromoRedemption.user_id == user.id,
            )
        )

        if existing.scalar_one_or_none():
            return await message.answer("❌ Вы уже использовали этот промокод.")

        now = utc_now()
        sub_result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
            .with_for_update()
        )
        subscription = sub_result.scalar_one_or_none()

        if subscription and subscription.expires_at > now:
            subscription.expires_at = subscription.expires_at + timedelta(days=promo_code.days)
            subscription.updated_at = now
        elif subscription:
            subscription.status = "active"
            subscription.expires_at = now + timedelta(days=promo_code.days)
            subscription.updated_at = now
        else:
            session.add(
                Subscription(
                    user_id=user.id,
                    expires_at=now + timedelta(days=promo_code.days),
                    status="active",
                )
            )

        session.add(
            PromoRedemption(
                promo_id=promo_code.id,
                user_id=user.id,
            )
        )

        promo_code.used_activations += 1

        if promo_code.used_activations >= promo_code.max_activations:
            promo_code.active = False
            promo_code.deactivated_at = now

        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Промокод активирован!</b>\n\n"
        f"🎁 Начислено: <b>{promo_code.days} дней</b> подписки."
    )


# ----------------------------- News -----------------------------

@dp.callback_query(F.data == "news")
async def news(callback: CallbackQuery) -> None:
    await edit_screen(
        callback,
        key="home",
        text=(
            "<b>📢 Новости</b>\n\n"
            "Здесь будут последние новости сервиса, "
            "обслуживание серверов и важные уведомления."
        ),
        keyboard=back_kb(),
    )


# ----------------------------- Support -----------------------------

@dp.callback_query(F.data.startswith("support_"))
async def support_topic(callback: CallbackQuery) -> None:
    topic = callback.data.replace("support_", "")
    titles = {
        "connect": "🔑 Не подключается VPN",
        "payment": "💳 Проблема с оплатой",
        "device": "📱 Проблема с устройством",
        "operator": "💬 Оператор",
    }
    title = titles.get(topic, "Поддержка")

    if topic == "operator":
        text = (
            "<b>💬 Оператор</b>\n\n"
            "В рабочей версии здесь будет кнопка для обращения "
            "в поддержку или пересылка сообщения администратору."
        )
    else:
        text = (
            f"<b>{title}</b>\n\n"
            "Опишите проблему одним сообщением.\n"
            "В рабочей версии обращение будет сохранено в БД "
            "и передано оператору."
        )

    await edit_screen(callback, key="support", text=text, keyboard=back_kb("support"))


# ----------------------------- Admin panel -----------------------------

@dp.callback_query(F.data == "admin")
async def admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()

    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await edit_screen(
        callback,
        key="support",
        text="<b>👑 Админ-панель</b>\n\nВыберите раздел:",
        keyboard=admin_kb(),
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await mark_expired_subscriptions()

    async with SessionLocal() as session:
        total_result = await session.execute(select(func.count(User.id)))
        total_users = total_result.scalar() or 0

        active_result = await session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == "active",
                Subscription.expires_at > utc_now(),
            )
        )
        active_subscriptions = active_result.scalar() or 0

        revenue_result = await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == "paid"
            )
        )
        revenue = revenue_result.scalar() or 0

        paid_result = await session.execute(
            select(func.count(Payment.id)).where(Payment.status == "paid")
        )
        paid_payments = paid_result.scalar() or 0

    text = (
        "<b>📊 Статистика и аналитика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🟢 Активных подписок: <b>{active_subscriptions}</b>\n"
        f"💰 Оплачено: <b>${revenue}</b>\n"
        f"💳 Успешных платежей: <b>{paid_payments}</b>"
    )

    await edit_screen(callback, key="support", text=text, keyboard=admin_kb())


@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await state.set_state(AdminStates.search)
    await callback.message.answer(
        "🔎 <b>Поиск пользователя</b>\n\n"
        "Отправьте Telegram ID или @username.\n"
        "Для отмены отправьте: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(AdminStates.search)
async def admin_search(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    query_text = (message.text or "").strip()

    if query_text.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Поиск отменён.", reply_markup=admin_kb())

    if not query_text:
        return await message.answer("❌ Отправьте Telegram ID или @username.")

    async with SessionLocal() as session:
        if query_text.startswith("@"):
            username = query_text[1:].strip().lower()
            result = await session.execute(
                select(User).where(func.lower(User.username) == username)
            )
        elif query_text.isdigit():
            result = await session.execute(
                select(User).where(User.telegram_id == int(query_text))
            )
        else:
            result = await session.execute(
                select(User).where(func.lower(User.username) == query_text.lower())
            )

        user = result.scalar_one_or_none()

        if not user:
            await state.clear()
            return await message.answer(
                "❌ Пользователь не найден.",
                reply_markup=admin_kb(),
            )

        subscription = await get_active_subscription(session, user.id)

    await state.clear()

    if subscription:
        subscription_info = (
            "🟢 <b>Подписка активна</b>\n"
            f"📅 До: <b>{subscription.expires_at.strftime('%d.%m.%Y %H:%M:%S')}</b>"
        )
    else:
        subscription_info = "🔴 <b>Подписка не активна</b>"

    username_text = f"@{user.username}" if user.username else "нет username"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🎁 Выдать дни",
                callback_data=f"admin_user_days:{user.telegram_id}",
            )],
            [InlineKeyboardButton(
                text="🔐 Выдать доступ",
                callback_data=f"admin_user_access:{user.telegram_id}",
            )],
            [InlineKeyboardButton(text="↩️ Админ-панель", callback_data="admin")],
        ]
    )

    await message.answer(
        "<b>👤 Пользователь</b>\n\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"👤 Username: <b>{username_text}</b>\n"
        f"📛 Имя: <b>{user.first_name or 'нет'}</b>\n\n"
        f"{subscription_info}\n\n"
        f"📱 Устройства: <b>{user.devices}</b>\n"
        f"🎁 Приглашено друзей: <b>{user.referrals}</b>\n"
        f"💰 Баланс: <b>${user.balance}</b>",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("admin_user_days:"))
async def admin_user_days(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    telegram_id = callback.data.split(":", 1)[1]
    await state.update_data(target_user_id=telegram_id)
    await state.set_state(AdminStates.bonus_days)

    await callback.message.answer(
        "🎁 <b>Выдача дней</b>\n\n"
        f"Пользователь: <code>{telegram_id}</code>\n\n"
        "Сколько дней выдать?\n"
        "Введите число от 1 до 3650.\n\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_user_access:"))
async def admin_user_access(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    telegram_id = callback.data.split(":", 1)[1]
    await state.update_data(target_user_id=telegram_id)
    await state.set_state(AdminStates.bonus_days)

    await callback.message.answer(
        "🔐 <b>Выдача доступа</b>\n\n"
        f"Пользователь: <code>{telegram_id}</code>\n\n"
        "На сколько дней выдать доступ?\n"
        "Введите число от 1 до 3650.\n\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_bonus")
async def admin_bonus(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await state.set_state(AdminStates.bonus_user)
    await callback.message.answer(
        "🎁 <b>Ручная выдача дней / бонусов</b>\n\n"
        "Отправьте Telegram ID пользователя.\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(AdminStates.bonus_user)
async def admin_bonus_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    value = (message.text or "").strip()

    if value.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Выдача бонуса отменена.", reply_markup=admin_kb())

    if not value.isdigit():
        return await message.answer("❌ Telegram ID должен состоять только из цифр.")

    target_id = int(value)

    async with SessionLocal() as session:
        user = await get_user(session, target_id)

    if not user:
        return await message.answer("❌ Пользователь с таким Telegram ID не найден.")

    await state.update_data(target_user_id=str(target_id))
    await state.set_state(AdminStates.bonus_days)
    await message.answer(
        f"👤 Пользователь найден: <code>{target_id}</code>\n\n"
        "Сколько дней выдать?\n"
        "Введите число от 1 до 3650.\n\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )


@dp.message(AdminStates.bonus_days)
async def admin_bonus_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    value = (message.text or "").strip()

    if value.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Выдача бонуса отменена.", reply_markup=admin_kb())

    if not value.isdigit():
        return await message.answer("❌ Введите целое число дней от 1 до 3650.")

    days = int(value)

    if days < 1 or days > 3650:
        return await message.answer("❌ Количество дней должно быть от 1 до 3650.")

    data = await state.get_data()
    target_user_id = data.get("target_user_id")

    if not target_user_id or not str(target_user_id).isdigit():
        await state.clear()
        return await message.answer("❌ Пользователь не выбран. Откройте раздел заново.", reply_markup=admin_kb())

    target_id = int(target_user_id)
    expires_at = await add_subscription_days(target_id, days)

    await state.clear()

    if expires_at is None:
        return await message.answer("❌ Не удалось выдать доступ: пользователь не найден.", reply_markup=admin_kb())

    await message.answer(
        "✅ <b>Доступ выдан</b>\n\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"🎁 Начислено: <b>{days} дней</b>\n"
        f"📅 Подписка до: <b>{expires_at.strftime('%d.%m.%Y %H:%M:%S')}</b>",
        reply_markup=admin_kb(),
    )


# ----------------------------- Admin broadcast -----------------------------

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await state.set_state(AdminStates.broadcast)
    await callback.message.answer(
        "📢 <b>Массовая рассылка</b>\n\n"
        "Отправьте сообщение, которое нужно разослать всем пользователям.\n"
        "Можно отправить текст или сообщение с фото.\n\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(AdminStates.broadcast)
async def admin_broadcast_message(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if (message.text or "").strip().upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Рассылка отменена.", reply_markup=admin_kb())

    await state.clear()

    async with SessionLocal() as session:
        result = await session.execute(select(User.telegram_id))
        telegram_ids = [row[0] for row in result.all()]

    sent = 0
    failed = 0

    for telegram_id in telegram_ids:
        try:
            await message.copy_to(chat_id=telegram_id)
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("Broadcast failed for %s: %s", telegram_id, exc)

        # Keep a small pause so a large database does not immediately flood Telegram.
        await asyncio.sleep(0.10)

    await message.answer(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"⚠️ Не доставлено: <b>{failed}</b>",
        reply_markup=admin_kb(),
    )


# ----------------------------- Admin promo creation -----------------------------

@dp.callback_query(F.data == "admin_promo")
async def admin_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await state.set_state(AdminStates.promo_code)
    await callback.message.answer(
        "🎟 <b>Создание промокода</b>\n\n"
        "Введите промокод.\n"
        "Например: <code>АРБУЗ</code>\n\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(AdminStates.promo_code)
async def admin_promo_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    code = " ".join((message.text or "").split()).strip().upper()

    if code == "ОТМЕНА":
        await state.clear()
        return await message.answer("Создание промокода отменено.", reply_markup=admin_kb())

    if not code:
        return await message.answer("❌ Введите промокод текстом.")

    if len(code) > 100:
        return await message.answer("❌ Промокод слишком длинный. Максимум 100 символов.")

    async with SessionLocal() as session:
        result = await session.execute(
            select(PromoCode).where(func.upper(PromoCode.code) == code)
        )
        exists = result.scalar_one_or_none()

    if exists:
        return await message.answer("❌ Такой промокод уже существует. Введите другой.")

    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.promo_days)

    await message.answer(
        f"🎟 Код: <code>{code}</code>\n\n"
        "На сколько дней подписки будет промокод?\n\n"
        "Введите число от 1 до 3650."
    )


@dp.message(AdminStates.promo_days)
async def admin_promo_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    value = (message.text or "").strip()

    if value.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Создание промокода отменено.", reply_markup=admin_kb())

    if not value.isdigit():
        return await message.answer("❌ Введите целое число от 1 до 3650.")

    days = int(value)

    if days < 1 or days > 3650:
        return await message.answer("❌ Количество дней должно быть от 1 до 3650.")

    await state.update_data(promo_days=days)
    await state.set_state(AdminStates.promo_uses)

    await message.answer(
        f"🎁 Дней: <b>{days}</b>\n\n"
        "Сколько раз промокод можно активировать?\n\n"
        "Например: <b>1</b> — только один человек."
    )


@dp.message(AdminStates.promo_uses)
async def admin_promo_uses(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    value = (message.text or "").strip()

    if value.upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Создание промокода отменено.", reply_markup=admin_kb())

    if not value.isdigit():
        return await message.answer("❌ Введите целое число активаций от 1 до 1 000 000.")

    uses = int(value)

    if uses < 1 or uses > 1_000_000:
        return await message.answer("❌ Количество активаций должно быть от 1 до 1 000 000.")

    data = await state.get_data()
    code = data.get("promo_code")
    days = int(data.get("promo_days", 0))

    if not code or days <= 0:
        await state.clear()
        return await message.answer("❌ Данные промокода потеряны. Создайте его заново.", reply_markup=admin_kb())

    async with SessionLocal() as session:
        result = await session.execute(
            select(PromoCode).where(func.upper(PromoCode.code) == code)
        )
        exists = result.scalar_one_or_none()

        if exists:
            await state.clear()
            return await message.answer("❌ Такой промокод уже существует.", reply_markup=admin_kb())

        promo_code = PromoCode(
            code=code,
            days=days,
            max_activations=uses,
            used_activations=0,
            active=True,
        )
        session.add(promo_code)
        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Промокод создан</b>\n\n"
        f"🎟 <code>{code}</code>\n"
        f"🎁 {days} дней\n"
        f"🔢 {uses} активаций\n"
        "🟢 Активен",
        reply_markup=admin_kb(),
    )


# ----------------------------- Fallback -----------------------------

@dp.message()
async def fallback(message: Message) -> None:
    if not allowed(message.from_user.id):
        return await message.answer("⏳ Слишком много запросов. Подождите немного.")

    await ensure_user(message.from_user)
    await message.answer(
        "Используйте кнопки меню ниже или отправьте /start.",
        reply_markup=home_kb(message.from_user.id),
    )


# ----------------------------- Main -----------------------------

async def main() -> None:
    await init_db()
    log.info("VPN bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
