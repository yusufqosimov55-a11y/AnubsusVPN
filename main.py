import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================
# Telegram VPN bot — UI/logic skeleton
# VPN API, payments and production DB are intentionally separated
# so they can be connected later without rebuilding the menu.
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# ----------------------------- UI assets -----------------------------

# Put your own images here later. If a file does not exist, the bot sends text only.
IMAGES = {
    "home": ASSETS / "home.jpg",
    "tariffs": ASSETS / "tariffs.jpg",
    "cabinet": ASSETS / "cabinet.jpg",
    "support": ASSETS / "support.jpg",
    "vpn": ASSETS / "vpn.jpg",
    "referral": ASSETS / "referral.jpg",
    "proxy": ASSETS / "proxy.jpg",
    "instructions": ASSETS / "instructions.jpg",
}

# ----------------------------- Temporary demo data -----------------------------
# This is deliberately simple for the first stage.
# Tomorrow this can be replaced by PostgreSQL + VPN API without changing the UI.

demo_users: dict[int, dict] = {}


def user_data(user_id: int) -> dict:
    return demo_users.setdefault(
        user_id,
        {
            "subscription_until": None,
            "devices": 0,
            "referrals": 0,
            "balance": 0,
            "trial_used": False,
        },
    )


def subscription_text(user_id: int) -> str:
    data = user_data(user_id)
    until = data["subscription_until"]

    if not until:
        return "🔴 <b>Подписка:</b> не активна"

    if until <= datetime.now(timezone.utc):
        return "🔴 <b>Подписка:</b> закончилась"

    left = until - datetime.now(timezone.utc)
    days = left.days
    hours = left.seconds // 3600
    return (
        f"🟢 <b>Подписка:</b> активна\n"
        f"⏳ Осталось: <b>{days} дн. {hours} ч.</b>\n"
        f"📅 До: <b>{until.strftime('%d.%m.%Y %H:%M')}</b>"
    )


# ----------------------------- Keyboards -----------------------------

def back_kb(target: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data=target)]
        ]
    )


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
    )


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
                InlineKeyboardButton(text="7 дней", callback_data="buy_7"),
                InlineKeyboardButton(text="1 месяц", callback_data="buy_30"),
            ],
            [
                InlineKeyboardButton(text="3 месяца", callback_data="buy_90"),
                InlineKeyboardButton(text="1 год", callback_data="buy_365"),
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

🟢 7 дней — тестовый вариант
🔵 1 месяц — стандартный тариф
🟣 3 месяца — длительный доступ
🟠 1 год — годовая подписка

Стоимость и платёжную систему можно изменить
в одном месте конфигурации.
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
async def start(message: Message) -> None:
    user_data(message.from_user.id)
    await show_screen(
        message,
        key="home",
        text=HOME_TEXT,
        keyboard=home_kb(),
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="home", text=HOME_TEXT, keyboard=home_kb())


@dp.callback_query(F.data == "vpn")
async def vpn(callback: CallbackQuery) -> None:
    text = VPN_TEXT.format(status=subscription_text(callback.from_user.id))
    await edit_screen(callback, key="vpn", text=text, keyboard=vpn_kb())


@dp.callback_query(F.data == "tariffs")
async def tariffs(callback: CallbackQuery) -> None:
    await edit_screen(callback, key="tariffs", text=TARIFFS_TEXT, keyboard=tariffs_kb())


@dp.callback_query(F.data == "cabinet")
async def cabinet(callback: CallbackQuery) -> None:
    data = user_data(callback.from_user.id)
    text = CABINET_TEXT.format(
        user_id=callback.from_user.id,
        status=subscription_text(callback.from_user.id),
        devices=data["devices"],
        referrals=data["referrals"],
        balance=data["balance"],
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
    data = user_data(callback.from_user.id)
    text = (
        "<b>📱 Мои устройства</b>\n\n"
        f"Подключено: <b>{data['devices']}</b>\n\n"
        "Лимит устройств будет зависеть от тарифа. "
        "После подключения VPN API здесь появится управление "
        "активными устройствами."
    )
    await edit_screen(callback, key="cabinet", text=text, keyboard=back_kb("cabinet"))


@dp.callback_query(F.data == "payments")
async def payments(callback: CallbackQuery) -> None:
    text = (
        "<b>💰 История платежей</b>\n\n"
        "Пока платежей нет.\n\n"
        "После подключения платёжной системы здесь будут "
        "дата, сумма, тариф и статус каждой операции."
    )
    await edit_screen(callback, key="cabinet", text=text, keyboard=back_kb("cabinet"))


@dp.callback_query(F.data == "trial")
async def trial(callback: CallbackQuery) -> None:
    data = user_data(callback.from_user.id)
    if data["trial_used"]:
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

    await edit_screen(
        callback,
        key="tariffs",
        text=(
            f"<b>💳 Выбран тариф: {label}</b>\n\n"
            "Платёжная система пока не подключена.\n\n"
            "На следующем этапе здесь будет реальная оплата, "
            "проверка платежа и автоматическая активация подписки."
        ),
        keyboard=back_kb("tariffs"),
    )


@dp.callback_query(F.data == "promo")
async def promo(callback: CallbackQuery) -> None:
    await edit_screen(
        callback,
        key="tariffs",
        text=(
            "<b>🎟 Промокод</b>\n\n"
            "Отправьте промокод отдельным сообщением.\n"
            "Проверка промокодов подключится вместе с базой данных."
        ),
        keyboard=back_kb("tariffs"),
    )


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


# ----------------------------- Fallback -----------------------------

@dp.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Используйте кнопки меню ниже или отправьте /start.",
        reply_markup=home_kb(),
    )


async def main() -> None:
    log.info("VPN bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
