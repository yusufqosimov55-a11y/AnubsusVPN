import asyncio
import logging
import os
import ssl
import secrets
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

import aiohttp

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func, select, text
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
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ----------------------------- TON payment configuration -----------------------------
TON_WALLET_ADDRESS = os.getenv(
    "TON_WALLET_ADDRESS",
    "UQC1Gh5ZO6r0-_qBOAyWJ1AXuyEzs169ld0D-PJGrCVQ_d3D",
).strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
# Manual bank-card payment details. Put your public payment details in Render.
# Do NOT put secret banking credentials or passwords here.
CARD_PAYMENT_DETAILS = os.getenv("CARD_PAYMENT_DETAILS", "5614 6812 5331 8145").strip()
CBU_RATE_REFRESH_SECONDS = 300  # 5 minutes
CBU_USD_RATE_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/"
TON_PRICE_REFRESH_SECONDS = 300  # 5 minutes
TON_ORDER_TTL_SECONDS = 15 * 60
TON_API_BASE = "https://toncenter.com/api/v3"
COINGECKO_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=the-open-network&vs_currencies=usd&precision=full"
)
# Public Binance market-data fallback. No API key is required for this endpoint.
BINANCE_TON_PRICE_URL = "https://data-api.binance.vision/api/v3/ticker/price?symbol=TONUSDT"

TARIFF_PRICES_USD = {
    7: Decimal("1.00"),
    30: Decimal("3.00"),
    90: Decimal("7.50"),
    365: Decimal("25.00"),
}

TON_USD_PRICE: Decimal | None = None
TON_PRICE_UPDATED_AT: datetime | None = None
TON_PRICE_LOCK = asyncio.Lock()
USD_UZS_RATE: Decimal | None = None
USD_UZS_RATE_UPDATED_AT: datetime | None = None
USD_UZS_RATE_LOCK = asyncio.Lock()

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Fiat/base amount of the order. For TON payments this stores the USD tariff price.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Exact crypto amount with 9-decimal TON precision.
    crypto_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 9), nullable=True)
    # Explicit immutable pricing snapshot for manual card orders.
    usd_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    uzs_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    receipt_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    receipt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="UZS", nullable=False)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tariff_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
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


async def ensure_payment_schema() -> None:
    """Add payment columns to an existing Neon database safely."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS crypto_amount NUMERIC(24, 9)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS usd_amount NUMERIC(24, 2)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS exchange_rate NUMERIC(24, 6)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS uzs_amount NUMERIC(24, 2)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_chat_id BIGINT"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_message_id BIGINT"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_file_id VARCHAR(512)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_type VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS confirmed_by BIGINT"))
        await conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ"))


async def init_db() -> None:
    """Create missing tables on first launch.

    For an established production database, Alembic migrations should be used
    for schema changes. create_all is intentionally kept here so the bot can
    start on a fresh Neon database without a manual SQL step.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

        # Existing Neon databases need these small additive migrations.
        await connection.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id VARCHAR(64)"))
        await connection.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"))
        await connection.execute(text("ALTER TABLE payments ALTER COLUMN amount TYPE NUMERIC(24, 9)"))
        await connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_order_id ON payments(order_id) WHERE order_id IS NOT NULL")
        )


# ----------------------------- CBU USD/UZS rate helpers -----------------------------


async def fetch_usd_uzs_rate() -> Decimal:
    """Fetch the official USD/UZS rate from CBU."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers={"accept": "application/json"}) as http:
        async with http.get(CBU_USD_RATE_URL) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("Ccy", "")).upper() != "USD":
            continue
        raw_rate = row.get("Rate")
        if raw_rate is None:
            continue
        rate = Decimal(str(raw_rate).replace(",", "."))
        if rate > 0:
            return rate.quantize(Decimal("0.000001"))

    raise ValueError("CBU USD rate not found in response")


async def refresh_usd_uzs_rate(force: bool = False) -> Decimal | None:
    """Refresh CBU rate every five minutes; keep last successful value on errors."""
    global USD_UZS_RATE, USD_UZS_RATE_UPDATED_AT

    now = utc_now()
    if (
        not force
        and USD_UZS_RATE is not None
        and USD_UZS_RATE_UPDATED_AT is not None
        and (now - USD_UZS_RATE_UPDATED_AT).total_seconds() < CBU_RATE_REFRESH_SECONDS
    ):
        return USD_UZS_RATE

    async with USD_UZS_RATE_LOCK:
        now = utc_now()
        if (
            not force
            and USD_UZS_RATE is not None
            and USD_UZS_RATE_UPDATED_AT is not None
            and (now - USD_UZS_RATE_UPDATED_AT).total_seconds() < CBU_RATE_REFRESH_SECONDS
        ):
            return USD_UZS_RATE

        try:
            rate = await fetch_usd_uzs_rate()
            USD_UZS_RATE = rate
            USD_UZS_RATE_UPDATED_AT = now
            log.info("CBU USD/UZS rate updated: %s", rate)
        except Exception:
            log.exception("Failed to refresh CBU USD/UZS rate")
            if USD_UZS_RATE is not None:
                log.warning("Using last successful CBU USD/UZS rate: %s", USD_UZS_RATE)
            else:
                log.error("No previous CBU USD/UZS rate is available")

        return USD_UZS_RATE


async def usd_uzs_rate_loop() -> None:
    while True:
        try:
            await asyncio.sleep(CBU_RATE_REFRESH_SECONDS)
            await refresh_usd_uzs_rate(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error in CBU rate loop")


# ----------------------------- TON payment helpers -----------------------------


def format_ton(value: Decimal) -> str:
    return f"{Decimal(value):.9f}".rstrip("0").rstrip(".")


def tariff_price_usd(days: int) -> Decimal | None:
    return TARIFF_PRICES_USD.get(days)


async def fetch_ton_usd_price() -> Decimal:
    """Fetch TON/USD from CoinGecko, with Binance public market data as fallback."""
    timeout = aiohttp.ClientTimeout(total=15)

    # 1) CoinGecko. A Demo/Pro key is optional in configuration; if it fails,
    # fall back to the public Binance market-data endpoint below.
    headers = {"accept": "application/json"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
            async with http.get(COINGECKO_PRICE_URL) as response:
                if response.status < 400:
                    data = await response.json()
                    raw_price = data.get("the-open-network", {}).get("usd")
                    if raw_price is not None:
                        price = Decimal(str(raw_price))
                        if price > 0:
                            return price
                else:
                    body = await response.text()
                    log.warning("CoinGecko price request failed: HTTP %s: %s", response.status, body[:300])
    except Exception:
        log.exception("CoinGecko price request failed")

    # 2) Binance public market data: TON/USDT is used as a close USD proxy.
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(BINANCE_TON_PRICE_URL) as response:
                response.raise_for_status()
                data = await response.json()
        raw_price = data.get("price")
        price = Decimal(str(raw_price))
        if price <= 0:
            raise RuntimeError("Invalid Binance TON/USDT price")
        return price
    except Exception:
        log.exception("Binance TON/USDT price request failed")
        raise RuntimeError("Не удалось получить актуальный курс TON/USD")


async def refresh_ton_price(force: bool = False) -> Decimal | None:
    global TON_USD_PRICE, TON_PRICE_UPDATED_AT

    async with TON_PRICE_LOCK:
        now = utc_now()
        if (
            not force
            and TON_USD_PRICE is not None
            and TON_PRICE_UPDATED_AT is not None
            and (now - TON_PRICE_UPDATED_AT).total_seconds() < TON_PRICE_REFRESH_SECONDS
        ):
            return TON_USD_PRICE

        try:
            price = await fetch_ton_usd_price()
        except Exception:
            log.exception("Failed to refresh TON/USD price")
            return TON_USD_PRICE

        TON_USD_PRICE = price
        TON_PRICE_UPDATED_AT = now
        log.info("TON/USD price updated: %s", price)
        return price


async def ton_price_loop() -> None:
    while True:
        try:
            await refresh_ton_price(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TON price refresh loop failed")
        await asyncio.sleep(TON_PRICE_REFRESH_SECONDS)


async def create_ton_payment(telegram_id: int, tariff_days: int) -> Payment | None:
    price_usd = tariff_price_usd(tariff_days)
    if price_usd is None:
        return None

    ton_price = await refresh_ton_price()
    if ton_price is None:
        return None

    base_amount = (price_usd / ton_price).quantize(Decimal("0.000000001"))
    now = utc_now()
    expires_at = now + timedelta(seconds=TON_ORDER_TTL_SECONDS)
    order_id = "TON-" + secrets.token_hex(5).upper()

    async with SessionLocal() as session:
        user = await get_user(session, telegram_id)
        if user is None:
            return None

        result = await session.execute(
            select(Payment.crypto_amount).where(
                Payment.currency == "TON",
                Payment.status == "pending",
                Payment.expires_at > now,
            )
        )
        used_amounts = {Decimal(str(row[0])) for row in result.all() if row[0] is not None}

        # A tiny nanotons offset makes simultaneous orders distinguishable.
        for _ in range(20):
            offset = Decimal(secrets.randbelow(999) + 1) / Decimal("1000000000")
            amount = (base_amount + offset).quantize(Decimal("0.000000001"))
            if amount not in used_amounts:
                break
        else:
            return None

        payment = Payment(
            user_id=user.id,
            amount=price_usd,
            crypto_amount=amount,
            currency="TON",
            method="TON",
            provider="TON Center + CoinGecko/Binance",
            order_id=order_id,
            tariff_days=tariff_days,
            status="pending",
            expires_at=expires_at,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)
        return payment


def ton_payment_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Я оплатил", callback_data=f"ton_check_{payment_id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="tariffs")],
        ]
    )


def extract_message_text(message_content: dict | None) -> str:
    if not isinstance(message_content, dict):
        return ""
    decoded = message_content.get("decoded")
    if isinstance(decoded, dict):
        for key in ("text", "comment", "value"):
            value = decoded.get(key)
            if isinstance(value, str):
                return value.strip()
    return ""


async def find_ton_transaction(payment: Payment) -> str | None:
    if not payment.expires_at:
        return None

    start_utime = int(payment.created_at.timestamp()) - 30
    end_utime = int(min(payment.expires_at, utc_now()).timestamp()) + 30
    params = {
        "account": TON_WALLET_ADDRESS,
        "start_utime": start_utime,
        "end_utime": end_utime,
        "limit": 100,
        "sort": "desc",
    }
    headers = {"accept": "application/json"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
        async with http.get(f"{TON_API_BASE}/transactions", params=params) as response:
            response.raise_for_status()
            data = await response.json()

    expected_nanotons = int(Decimal(payment.crypto_amount or 0) * Decimal("1000000000"))

    async with SessionLocal() as session:
        for tx in data.get("transactions", []):
            tx_hash = tx.get("hash")
            in_msg = tx.get("in_msg") or {}
            try:
                value = int(in_msg.get("value"))
            except (TypeError, ValueError):
                continue

            if value != expected_nanotons:
                continue

            # TON Center was queried for our receiving account, so the
            # transaction is already scoped to this wallet. If decoded text
            # is available, require our order ID when a comment is present.
            content_text = extract_message_text(in_msg.get("message_content"))
            if content_text and payment.order_id and payment.order_id not in content_text:
                continue

            used = await session.execute(
                select(Payment.id).where(
                    Payment.transaction_id == tx_hash,
                    Payment.status == "paid",
                ).limit(1)
            )
            if used.scalar_one_or_none() is not None:
                continue

            return tx_hash

    return None


async def confirm_ton_payment(payment_id: int, telegram_id: int) -> tuple[bool, str]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if payment is None or payment.currency != "TON":
            return False, "Платёж не найден."

        user = await session.get(User, payment.user_id)
        if user is None or user.telegram_id != telegram_id:
            return False, "Платёж не принадлежит вашему аккаунту."

        if payment.status == "paid":
            return True, "Платёж уже подтверждён."

        if payment.expires_at and payment.expires_at < utc_now():
            payment.status = "expired"
            await session.commit()
            return False, "Срок действия заказа истёк. Создайте новый заказ."

        found_hash = await find_ton_transaction(payment)
        if not found_hash:
            return False, "❌ Транзакция не найдена. Проверьте перевод и попробуйте ещё раз через несколько секунд."

        duplicate = await session.execute(
            select(Payment.id).where(
                Payment.transaction_id == found_hash,
                Payment.status == "paid",
                Payment.id != payment_id,
            ).limit(1)
        )
        if duplicate.scalar_one_or_none() is not None:
            return False, "Эта транзакция уже была использована для другого заказа."

        now = utc_now()
        payment.transaction_id = found_hash
        payment.status = "paid"
        payment.paid_at = now

        # Activate/extend subscription in the same DB transaction as payment
        # confirmation, so one transaction cannot grant days twice.
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
        days = int(payment.tariff_days or 0)

        if subscription and subscription.expires_at > now:
            subscription.expires_at = subscription.expires_at + timedelta(days=days)
            subscription.updated_at = now
        elif subscription:
            subscription.expires_at = now + timedelta(days=days)
            subscription.status = "active"
            subscription.updated_at = now
        else:
            session.add(
                Subscription(
                    user_id=user.id,
                    expires_at=now + timedelta(days=days),
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )

        await session.commit()
        return True, "✅ Оплата подтверждена! Подписка активирована."


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


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    """Support operator only. Financial/admin-owner actions never use this check."""
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

    if user_id is not None and is_owner(user_id):
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


def payment_methods_kb(days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 TON", callback_data=f"pay_ton_{days}")],
            [InlineKeyboardButton(text="💳 Uzcard / Humo", callback_data=f"pay_card_{days}")],
            [InlineKeyboardButton(text="↩️ Назад к тарифам", callback_data="tariffs")],
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
    card_receipt = State()
    support_message = State()


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

Оплата сейчас доступна в TON.
Курс TON/USD автоматически обновляется каждые 5 минут.
"""

CABINET_TEXT = """
<b>👤 Личный кабинет</b>

🆔 ID: <code>{user_id}</code>
{status}

📱 Устройства: <b>{devices}</b>
🎁 Приглашено друзей: <b>{referrals}</b>
💰 Баланс: <b>{balance} сум</b>
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
            status_map = {
                "paid": "✅ Оплачен",
                "rejected": "❌ Отклонён",
                "expired": "⌛ Истёк",
                "pending": "⏳ Ожидает",
            }
            status_text = status_map.get(payment.status, payment.status)
            if payment.currency == "TON":
                amount_text = format_ton(Decimal(payment.crypto_amount or 0))
            elif payment.currency == "UZS" and payment.uzs_amount is not None:
                amount_text = f"{Decimal(payment.uzs_amount):.0f}"
            else:
                amount_text = f"{Decimal(payment.amount):.2f}"
            lines.append(
                f"{date_text} — <b>{amount_text} {payment.currency}</b> — {status_text}"
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
    days = int(period)
    labels = {"7": "7 дней", "30": "1 месяц", "90": "3 месяца", "365": "1 год"}
    label = labels.get(period, period)
    price_usd = tariff_price_usd(days)

    if price_usd is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    await edit_screen(
        callback,
        key="tariffs",
        text=(
            "<b>💳 Выберите способ оплаты</b>\n\n"
            f"Тариф: <b>{label}</b>\n"
            f"Стоимость: <b>${price_usd:.2f}</b>\n\n"
            "Выберите удобный способ оплаты:"
        ),
        keyboard=payment_methods_kb(days),
    )


@dp.callback_query(F.data.startswith("pay_ton_"))
async def pay_ton(callback: CallbackQuery) -> None:
    try:
        days = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректный тариф", show_alert=True)
        return

    price_usd = tariff_price_usd(days)
    if price_usd is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    payment = await create_ton_payment(callback.from_user.id, days)
    if payment is None:
        await callback.answer(
            "Не удалось получить курс TON. Попробуйте через несколько секунд.",
            show_alert=True,
        )
        return

    ton_price = TON_USD_PRICE or Decimal("0")
    expires_text = payment.expires_at.strftime("%H:%M:%S") if payment.expires_at else "—"
    text = (
        f"<b>💎 Оплата TON</b>\n\n"
        f"Тариф: <b>{days} дней</b>\n"
        f"Цена: <b>${price_usd:.2f}</b>\n"
        f"Курс: <b>1 TON ≈ ${ton_price:.4f}</b>\n"
        f"К оплате: <code>{format_ton(payment.crypto_amount or 0)} TON</code>\n\n"
        f"💎 Кошелёк получателя:\n<code>{TON_WALLET_ADDRESS}</code>\n\n"
        f"🆔 Заказ: <code>{payment.order_id}</code>\n"
        f"⏱ Оплатить до: <b>{expires_text}</b>\n\n"
        "Переведите точную сумму TON на указанный кошелёк.\n"
        "В комментарии к переводу желательно указать ID заказа.\n\n"
        "После перевода нажмите «💎 Я оплатил»."
    )
    await edit_screen(callback, key="tariffs", text=text, keyboard=ton_payment_kb(payment.id))


@dp.callback_query(F.data.startswith("pay_card_"))
async def pay_card(callback: CallbackQuery) -> None:
    try:
        days = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректный тариф", show_alert=True)
        return

    price_usd = tariff_price_usd(days)
    if price_usd is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    if not CARD_PAYMENT_DETAILS:
        await callback.answer("Реквизиты карты ещё не настроены владельцем.", show_alert=True)
        return

    rate = await refresh_usd_uzs_rate()
    if rate is None:
        await callback.answer(
            "Не удалось получить курс ЦБ. Попробуйте ещё раз через несколько секунд.",
            show_alert=True,
        )
        return

    # The rate and total are calculated once and stored with this order.
    uzs_amount = (price_usd * rate).quantize(Decimal("1"))

    order_id = "CARD-" + secrets.token_hex(5).upper()
    expires_at = utc_now() + timedelta(minutes=30)

    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        payment = Payment(
            user_id=user.id,
            amount=price_usd,  # legacy/base USD amount; kept for compatibility
            usd_amount=price_usd,
            exchange_rate=rate,
            uzs_amount=uzs_amount,
            currency="UZS",
            method="Uzcard/Humo",
            provider="Manual card verification",
            order_id=order_id,
            tariff_days=days,
            status="pending",
            expires_at=expires_at,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)

    expires_text = expires_at.strftime("%H:%M:%S")
    text = (
        "<b>💳 ОПЛАТА UZCARD / HUMO</b>\n\n"
        f"Тариф: <b>{days} дней</b>\n"
        f"Цена тарифа: <b>${price_usd:.2f}</b>\n"
        f"Курс ЦБ: <b>1 USD = {rate:.2f} UZS</b>\n"
        f"К оплате: <b>{uzs_amount:.0f} UZS</b>\n"
        f"🆔 Заказ: <code>{order_id}</code>\n"
        f"⏱ Оплатить до: <b>{expires_text}</b>\n\n"
        "<b>💳 НОМЕР КАРТЫ ДЛЯ ОПЛАТЫ:</b>\n"
        f"<pre>{CARD_PAYMENT_DETAILS}</pre>\n\n"
        "<b>⚠️ ДЕНЬГИ ПОСТУПЯТ ТОЛЬКО ПОСЛЕ ПРОВЕРКИ ВЛАДЕЛЬЦЕМ.</b>\n"
        "<b>📎 ЧЕК ОБЯЗАТЕЛЕН.</b>\n"
        "<b>❗ БЕЗ ЧЕКА ПЛАТЁЖ МОЖЕТ БЫТЬ НЕ ЗАСЧИТАН.</b>\n"
        "<b>❗ ЕСЛИ ЗАКАЗ ИСТЕЧЁТ, СОЗДАЙТЕ НОВЫЙ ЗАКАЗ.</b>\n\n"
        "После перевода нажмите кнопку ниже и отправьте фото/скриншот или документ с чеком.\n"
        "Владелец проверит поступление денег и после подтверждения выдаст подписку."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📎 Я оплатил — отправить чек", callback_data=f"card_receipt_{payment.id}")],
            [InlineKeyboardButton(text="↩️ Выбрать другой способ", callback_data=f"buy_{days}")],
        ]
    )
    await edit_screen(callback, key="tariffs", text=text, keyboard=keyboard)


@dp.callback_query(F.data.startswith("card_receipt_"))
async def card_receipt_start(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        payment_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ", show_alert=True)
        return

    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        user = await get_user(session, callback.from_user.id)
        if payment is None or user is None or payment.user_id != user.id or payment.method != "Uzcard/Humo":
            await callback.answer("Заказ не найден", show_alert=True)
            return
        if payment.status != "pending":
            await callback.answer("Этот заказ уже обработан или закрыт.", show_alert=True)
            return
        if payment.expires_at and payment.expires_at < utc_now():
            payment.status = "expired"
            await session.commit()
            await callback.answer("Срок заказа истёк. Создайте новый заказ.", show_alert=True)
            return

    await state.set_state(UserStates.card_receipt)
    await state.update_data(card_payment_id=payment_id)
    await callback.message.answer(
        "📎 <b>ОТПРАВЬТЕ ЧЕК ОБ ОПЛАТЕ</b>\n\n"
        "<b>ЧЕК ОБЯЗАТЕЛЕН — БЕЗ НЕГО ПЛАТЁЖ НЕ ПЕРЕДАЁТСЯ НА ПРОВЕРКУ.</b>\n\n"
        "Отправьте фото/скриншот чека или документ.\n"
        "После получения чек будет передан владельцу.\n"
        "<b>ДЕНЬГИ ЗАСЧИТЫВАЮТСЯ ТОЛЬКО ПОСЛЕ РУЧНОЙ ПРОВЕРКИ ВЛАДЕЛЬЦЕМ.</b>\n\n"
        "Если передумали — отправьте: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(UserStates.card_receipt)
async def card_receipt_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    payment_id = int(data.get("card_payment_id", 0) or 0)

    if message.text and message.text.strip().upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("❌ Отправка чека отменена.", reply_markup=home_kb(message.from_user.id))

    if not message.photo and not message.document:
        return await message.answer(
            "❗ <b>ЧЕК ОБЯЗАТЕЛЕН.</b>\n\n"
            "Отправьте именно фото/скриншот чека или документ.\n"
            "Текст, голосовые и другие сообщения не принимаются как подтверждение оплаты."
        )

    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        user = await get_user(session, message.from_user.id)
        if payment is None or user is None or payment.user_id != user.id:
            await state.clear()
            return await message.answer("❌ Заказ не найден. Создайте новый заказ.", reply_markup=home_kb(message.from_user.id))
        if payment.method != "Uzcard/Humo" or payment.status != "pending":
            await state.clear()
            return await message.answer("❌ Этот заказ уже обработан или закрыт.", reply_markup=home_kb(message.from_user.id))
        if payment.expires_at and payment.expires_at < utc_now():
            payment.status = "expired"
            await session.commit()
            await state.clear()
            return await message.answer("⏱ Срок заказа истёк. Создайте новый заказ.", reply_markup=home_kb(message.from_user.id))

        receipt_type = "photo" if message.photo else "document"
        receipt_file_id = message.photo[-1].file_id if message.photo else message.document.file_id

        # Persist receipt metadata so the owner can audit the exact payment.
        payment.receipt_chat_id = message.chat.id
        payment.receipt_message_id = message.message_id
        payment.receipt_file_id = receipt_file_id
        payment.receipt_type = receipt_type
        await session.commit()

        username = f"@{user.username}" if user.username else "нет username"
        rate_text = f"{payment.exchange_rate:.2f}" if payment.exchange_rate is not None else "—"
        uzs_text = f"{payment.uzs_amount:.0f}" if payment.uzs_amount is not None else "—"
        usd_text = f"{payment.usd_amount:.2f}" if payment.usd_amount is not None else f"{payment.amount:.2f}"
        created_text = payment.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")

        owner_text = (
            "<b>💳 НОВЫЙ ПЛАТЁЖ UZCARD / HUMO</b>\n\n"
            f"🆔 Заказ: <code>{payment.order_id}</code>\n"
            f"👤 Пользователь: <code>{user.telegram_id}</code> ({username})\n"
            f"📦 Тариф: <b>{payment.tariff_days} дней</b>\n"
            f"💵 USD: <b>${usd_text}</b>\n"
            f"📈 Зафиксированный курс: <b>1 USD = {rate_text} UZS</b>\n"
            f"💰 К оплате: <b>{uzs_text} UZS</b>\n"
            "💳 Метод: <b>Uzcard / Humo</b>\n"
            f"🕐 Создан: <b>{created_text} UTC</b>\n"
            "📌 Статус: <b>pending</b>\n\n"
            "Проверьте поступление средств и выберите действие."
        )
        owner_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Оплата получена", callback_data=f"card_approve_{payment.id}"),
                    InlineKeyboardButton(text="❌ Не поступила", callback_data=f"card_reject_{payment.id}"),
                ]
            ]
        )

    if OWNER_ID <= 0:
        await state.clear()
        return await message.answer("⚠️ OWNER_ID не настроен. Обратитесь в поддержку.")

    try:
        await bot.send_message(OWNER_ID, owner_text, reply_markup=owner_keyboard)
        await bot.copy_message(OWNER_ID, message.chat.id, message.message_id)
    except Exception:
        log.exception("Failed to send card receipt to owner")
        return await message.answer("⚠️ Не удалось передать чек владельцу. Попробуйте отправить его ещё раз.")

    await state.clear()
    await message.answer(
        "✅ Чек отправлен на проверку владельцу.\n\n"
        "Ожидайте ручной проверки. После подтверждения подписка будет выдана автоматически.",
        reply_markup=home_kb(message.from_user.id),
    )


async def approve_manual_card_payment(payment_id: int, owner_id: int) -> tuple[bool, str, int | None]:
    if not is_owner(owner_id):
        return False, "Доступ запрещён.", None

    async with SessionLocal() as session:
        result = await session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if payment is None or payment.method != "Uzcard/Humo":
            return False, "Платёж не найден.", None

        if payment.status == "paid":
            user = await session.get(User, payment.user_id)
            return True, "Платёж уже был подтверждён. Повторная выдача подписки не выполнялась.", user.telegram_id if user else None

        if payment.status != "pending":
            return False, f"Платёж имеет статус: {payment.status}.", None

        if payment.expires_at and payment.expires_at < utc_now():
            payment.status = "expired"
            await session.commit()
            return False, "Срок заказа истёк.", None

        user = await session.get(User, payment.user_id)
        if user is None:
            return False, "Пользователь не найден.", None

        now = utc_now()
        days = int(payment.tariff_days or 0)
        if days <= 0 or payment.uzs_amount is None or payment.exchange_rate is None or payment.usd_amount is None:
            return False, "У платежа отсутствует сохранённая сумма заказа.", None

        payment.status = "paid"
        payment.paid_at = now
        payment.confirmed_by = owner_id
        payment.confirmed_at = now
        payment.transaction_id = "MANUAL-CARD-" + secrets.token_hex(8).upper()

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
            subscription.expires_at += timedelta(days=days)
            subscription.updated_at = now
        elif subscription:
            subscription.expires_at = now + timedelta(days=days)
            subscription.status = "active"
            subscription.updated_at = now
        else:
            session.add(Subscription(
                user_id=user.id,
                expires_at=now + timedelta(days=days),
                status="active",
                created_at=now,
                updated_at=now,
            ))

        await session.commit()
        return True, f"Оплата подтверждена. Выдано: {days} дней.", user.telegram_id


@dp.callback_query(F.data.startswith("card_approve_"))
async def card_approve(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        payment_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректный платёж.", show_alert=True)
        return

    ok, result_text, telegram_id = await approve_manual_card_payment(payment_id, callback.from_user.id)
    await callback.answer("Готово" if ok else "Ошибка", show_alert=not ok)
    if not ok:
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        log.exception("Failed to remove card payment owner buttons")

    await callback.message.answer(f"✅ {result_text}")
    if telegram_id:
        try:
            await bot.send_message(
                telegram_id,
                "<b>✅ Оплата подтверждена!</b>\n\n"
                "Ваша подписка успешно активирована/продлена.\n"
                "Откройте «Мой VPN», чтобы продолжить.",
                reply_markup=home_kb(telegram_id),
            )
        except Exception:
            log.exception("Failed to notify user about manual card payment")


@dp.callback_query(F.data.startswith("card_reject_"))
async def card_reject(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        payment_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректный платёж.", show_alert=True)
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if payment is None or payment.method != "Uzcard/Humo":
            await callback.answer("Платёж не найден.", show_alert=True)
            return

        if payment.status == "paid":
            await callback.answer("Платёж уже подтверждён.", show_alert=True)
            return
        if payment.status == "rejected":
            await callback.answer("Платёж уже отклонён.", show_alert=True)
            return
        if payment.status != "pending":
            await callback.answer(f"Платёж имеет статус: {payment.status}.", show_alert=True)
            return

        now = utc_now()
        payment.status = "rejected"
        payment.confirmed_by = callback.from_user.id
        payment.confirmed_at = now
        await session.commit()

        user = await session.get(User, payment.user_id)
        telegram_id = user.telegram_id if user else None

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        log.exception("Failed to remove rejected payment buttons")
    await callback.answer("Отклонено")
    await callback.message.answer("❌ Платёж отмечен как не поступивший.")

    if telegram_id:
        try:
            await bot.send_message(
                telegram_id,
                "<b>❌ Оплата пока не подтверждена</b>\n\n"
                "Владелец не подтвердил поступление средств. Если проблема сохраняется, обратитесь в поддержку.",
                reply_markup=home_kb(telegram_id),
            )
        except Exception:
            log.exception("Failed to notify user about rejected manual card payment")


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
async def support_topic(callback: CallbackQuery, state: FSMContext) -> None:
    topic = callback.data.replace("support_", "", 1)
    titles = {
        "connect": "🔑 Не подключается VPN",
        "payment": "💳 Проблема с оплатой",
        "device": "📱 Проблема с устройством",
        "operator": "💬 Оператор",
    }
    title = titles.get(topic, "Поддержка")

    await state.set_state(UserStates.support_message)
    await state.update_data(support_topic=topic)
    await callback.message.answer(
        f"<b>{title}</b>\n\n"
        "Опишите проблему одним сообщением — обращение будет передано оператору поддержки.\n\n"
        "⚠️ Не отправляйте чек, номер карты, сумму платежа, пароли или токены.\n"
        "Для отмены: <code>ОТМЕНА</code>"
    )
    await callback.answer()


@dp.message(UserStates.support_message)
async def support_message_received(message: Message, state: FSMContext) -> None:
    if message.text and message.text.strip().upper() == "ОТМЕНА":
        await state.clear()
        return await message.answer("Обращение отменено.", reply_markup=home_kb(message.from_user.id))

    if message.photo or message.document or message.video or message.voice or message.audio:
        return await message.answer(
            "❗ Для поддержки отправьте текстовое описание проблемы.\n"
            "Чеки и платёжные документы через поддержку не пересылаются."
        )

    if ADMIN_ID <= 0:
        await state.clear()
        return await message.answer(
            "⚠️ Оператор поддержки пока не настроен. Попробуйте позже.",
            reply_markup=home_kb(message.from_user.id),
        )

    data = await state.get_data()
    topic = data.get("support_topic", "operator")
    topic_names = {
        "connect": "Не подключается VPN",
        "payment": "Проблема с оплатой",
        "device": "Проблема с устройством",
        "operator": "Общее обращение",
    }
    topic_name = topic_names.get(topic, "Общее обращение")

    raw_text = (message.text or message.caption or "").strip()
    # Do not pass financial/card data to the support operator.
    import re
    sanitized = re.sub(r"(?<!\d)\d[\d\s-]{10,18}\d(?!\d)", "[ДАННЫЕ СКРЫТЫ]", raw_text)
    sanitized = re.sub(r"(?i)(?:\$|USD|UZS|сум)\s*[\d.,]+", "[СУММА СКРЫТА]", sanitized)
    sanitized = re.sub(r"(?i)[\d.,]+\s*(?:\$|USD|UZS|сум)", "[СУММА СКРЫТА]", sanitized)
    sanitized = sanitized[:3500] if sanitized else "Пользователь отправил пустое сообщение."

    username = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    operator_text = (
        "<b>🛠 Новое обращение в поддержку</b>\n\n"
        f"📌 Тема: <b>{topic_name}</b>\n"
        f"👤 ID: <code>{message.from_user.id}</code>\n"
        f"👤 Username: <b>{username}</b>\n\n"
        f"<b>Сообщение:</b>\n{sanitized}"
    )

    try:
        await bot.send_message(ADMIN_ID, operator_text)
    except Exception:
        log.exception("Failed to send support request to ADMIN_ID")
        return await message.answer(
            "⚠️ Не удалось передать обращение оператору. Попробуйте ещё раз.",
        )

    await state.clear()
    await message.answer(
        "✅ Обращение передано оператору поддержки.\n"
        "Ожидайте ответа.",
        reply_markup=home_kb(message.from_user.id),
    )


# ----------------------------- Admin panel -----------------------------

@dp.callback_query(F.data == "admin")
async def admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()

    if not is_owner(callback.from_user.id):
        return await callback.answer("Нет доступа.", show_alert=True)

    await edit_screen(
        callback,
        key="support",
        text="<b>👑 Панель владельца</b>\n\nВыберите раздел:",
        keyboard=admin_kb(),
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
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

        uzs_paid_count = await session.execute(
            select(func.count(Payment.id)).where(
                Payment.status == "paid",
                Payment.method == "Uzcard/Humo",
                Payment.currency == "UZS",
            )
        )
        uzs_paid_count = uzs_paid_count.scalar() or 0

        uzs_paid_sum = await session.execute(
            select(func.coalesce(func.sum(Payment.uzs_amount), 0)).where(
                Payment.status == "paid",
                Payment.method == "Uzcard/Humo",
                Payment.currency == "UZS",
            )
        )
        uzs_paid_sum = uzs_paid_sum.scalar() or Decimal("0")

        uzs_pending_count = await session.execute(
            select(func.count(Payment.id)).where(
                Payment.status == "pending",
                Payment.method == "Uzcard/Humo",
                Payment.currency == "UZS",
            )
        )
        uzs_pending_count = uzs_pending_count.scalar() or 0

        ton_paid_count = await session.execute(
            select(func.count(Payment.id)).where(
                Payment.status == "paid",
                Payment.method == "TON",
            )
        )
        ton_paid_count = ton_paid_count.scalar() or 0

        ton_paid_sum = await session.execute(
            select(func.coalesce(func.sum(Payment.crypto_amount), 0)).where(
                Payment.status == "paid",
                Payment.method == "TON",
            )
        )
        ton_paid_sum = ton_paid_sum.scalar() or Decimal("0")

        ton_pending_count = await session.execute(
            select(func.count(Payment.id)).where(
                Payment.status == "pending",
                Payment.method == "TON",
            )
        )
        ton_pending_count = ton_pending_count.scalar() or 0

    text = (
        "<b>📊 Финансовая статистика владельца</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🟢 Активных подписок: <b>{active_subscriptions}</b>\n\n"
        "<b>💳 UZCARD / HUMO</b>\n"
        f"✅ Подтверждено: <b>{uzs_paid_count}</b>\n"
        f"💰 Сумма: <b>{uzs_paid_sum:.0f} UZS</b>\n"
        f"⏳ Ожидают: <b>{uzs_pending_count}</b>\n\n"
        "<b>💎 TON</b>\n"
        f"✅ Подтверждено: <b>{ton_paid_count}</b>\n"
        f"💎 Сумма: <b>{format_ton(ton_paid_sum)} TON</b>\n"
        f"⏳ Ожидают: <b>{ton_pending_count}</b>"
    )

    await edit_screen(callback, key="support", text=text, keyboard=admin_kb())


@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
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
    if not is_owner(message.from_user.id):
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
        f"💰 Баланс: <b>{user.balance} сум</b>",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("admin_user_days:"))
async def admin_user_days(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
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
    if not is_owner(callback.from_user.id):
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
    if not is_owner(callback.from_user.id):
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
    if not is_owner(message.from_user.id):
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
    if not is_owner(message.from_user.id):
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
    if not is_owner(callback.from_user.id):
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
    if not is_owner(message.from_user.id):
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
    if not is_owner(callback.from_user.id):
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
    if not is_owner(message.from_user.id):
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
    if not is_owner(message.from_user.id):
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
    if not is_owner(message.from_user.id):
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


@dp.callback_query(F.data.startswith("ton_check_"))
async def check_ton_payment(callback: CallbackQuery) -> None:
    try:
        payment_id = int(callback.data.split("_", 2)[2])
    except (ValueError, IndexError):
        await callback.answer("Некорректный заказ", show_alert=True)
        return

    await callback.answer("🔎 Проверяю блокчейн...")
    ok, message = await confirm_ton_payment(payment_id, callback.from_user.id)

    if ok:
        await edit_screen(
            callback,
            key="tariffs",
            text=f"<b>💎 Оплата TON</b>\n\n{message}\n\nОткройте «Мой VPN», чтобы продолжить.",
            keyboard=back_kb("tariffs"),
        )
    else:
        await edit_screen(
            callback,
            key="tariffs",
            text=f"<b>💎 Проверка оплаты</b>\n\n{message}",
            keyboard=back_kb("tariffs"),
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
    await ensure_payment_schema()
    await refresh_ton_price(force=True)
    await refresh_usd_uzs_rate(force=True)
    ton_task = asyncio.create_task(ton_price_loop())
    cbu_task = asyncio.create_task(usd_uzs_rate_loop())

    log.info("VPN bot started")
    try:
        await dp.start_polling(bot)
    finally:
        ton_task.cancel()
        cbu_task.cancel()
        await asyncio.gather(ton_task, cbu_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
