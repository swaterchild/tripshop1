"""
Tripov Shop — Telegram-магазин в одном файле.
Python 3.11+
aiogram 3.x
SQLite

Установка:
    python -m pip install -U aiogram tzdata

Запуск:
    1. Переименуйте файл в main.py (необязательно, но удобно).
    2. Заполните блок НАСТРОЙКИ ниже.
    3. Запустите: python main.py

Важно:
- Автовыдачи нет.
- Автоматической проверки оплаты нет.
- Оплата внутри бота не используется.
- При оплате рублями бот показывает номер телефона для перевода через Т-Банк / СБП.
- Оплата товара может проходить рублями через Т-Банк или вручную Stars владельцу,
  если для конкретного товара указана цена в Stars.
- Кнопки Premium в этом варианте нет.
- Раздел «Купить Stars» работает как отдельный склад: у пакета задаются количество Stars
  в одном заказе, цена в рублях, общий остаток Stars и способ выдачи.
- Сейчас Stars можно выдавать подарком; позже можно добавить вариант «на аккаунт» через Fragment.
- После оплаты покупатель отправляет в бота чек или скрин подтверждения.
- Заявка с прикреплённым скрином приходит владельцу в личные сообщения через бота.
- После нажатия владельцем «Выдать» заказ подтверждается, остаток уменьшается,
  покупатель получает уведомление, а заказ сохраняется в статистику.
- Заказы ни в какие каналы не публикуются.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ==========================================================
# НАСТРОЙКИ — ЗАПОЛНИТЕ ПЕРЕД ЗАПУСКОМ
# ==========================================================

BOT_TOKEN = "8854998089:AAHka18Sm5bkUd8zcc96cnFtUKgR9hIe5lk"

# Telegram ID владельца. Узнать можно через @userinfobot.
OWNER_ID = 8197798416

# Заявки на выдачу бот отправляет только владельцу в личные сообщения.
# Владелец должен хотя бы один раз открыть бота и нажать /start.

# Ссылка на канал с отзывами.
REVIEWS_CHANNEL_URL = "https://t.me/tripofobovrep"

# Username администратора без @. Кнопка поддержки откроет личный чат.
SUPPORT_USERNAME = "TripSupport77"

# Ручная оплата рублями переводом по номеру телефона через Т-Банк / СБП.
# Впишите номер телефона, который дал клиент, и имя получателя.
# Номер карты нигде не используется.
T_BANK_PHONE = "+79313716777"
T_BANK_RECIPIENT = "Тимур / Наталья"
T_BANK_NAME = "Т-Банк"

# Обязательный комментарий/подпись к переводу.
PAYMENT_COMMENT = "Трипов"

# Username владельца без @. Сюда покупатель перейдёт для ручной передачи Stars.
STARS_RECEIVER_USERNAME = "TripSupport77"

# Часовой пояс для дат и статистики «за сегодня».
TIMEZONE_NAME = "Europe/Riga"

# Путь к базе данных.
DB_PATH = Path(__file__).with_name("tripov_shop.sqlite3")

# Примерные аккаунты при первом запуске.
# Stars добавляются владельцем командой:
# /newstars 100 | 150₽ | подарком
# После запуска цену и количество можно менять через /admin.
SEED_PRODUCTS = [
    # category, code, name, price_rub, price_stars, stock, visible
    ("accounts", "ru", "🇷🇺 Россия", 60, 60, 48, 1),
    ("accounts", "kz", "🇰🇿 Казахстан", 60, 60, 16, 1),
    ("accounts", "ua", "🇺🇦 Украина", 60, 60, 5, 1),
]


# ==========================================================
# ОБЩИЕ НАСТРОЙКИ
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("shop_bot")

TZ = ZoneInfo(TIMEZONE_NAME)
router = Router()

CATEGORY_TITLES = {
    "accounts": "🛒 Купить аккаунты",
    "stars": "⭐ Купить Stars",
}

ORDER_STATUS_LABELS = {
    "pending": "⏳ Ожидает решения",
    "approved": "✅ Выдан",
    "cancelled": "❌ Отменён",
}

PAYMENT_METHOD_LABELS = {
    "card": "💳 Т-Банк по телефону (рубли)",
    "stars": "⭐ Оплата Telegram Stars",
}

STAR_DELIVERY_LABELS = {
    "gift": "🎁 Подарком",
    "account": "👤 На аккаунт",
    "standard": "—",
}


# ==========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Редактирует сообщение и молча игнорирует повторное нажатие той же кнопки."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return False
        raise


def local_now() -> datetime:
    return datetime.now(TZ)


def today_utc_bounds() -> tuple[str, str]:
    current_date = local_now().date()
    start_local = datetime.combine(current_date, time.min, tzinfo=TZ)
    end_local = datetime.combine(current_date, time.max, tzinfo=TZ)
    return (
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


def stars(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ⭐"


def format_prices(price_rub: int, price_stars: int) -> str:
    parts: list[str] = []
    if price_rub > 0:
        parts.append(money(price_rub))
    if price_stars > 0:
        parts.append(stars(price_stars))
    return " / ".join(parts) if parts else "Бесплатно"


def product_prices(product: sqlite3.Row) -> str:
    return format_prices(int(product["price_rub"]), int(product["price_stars"]))


def order_amount(order: sqlite3.Row) -> str:
    if order["payment_method"] == "stars":
        return stars(int(order["final_price_stars"]))
    return money(int(order["final_price_rub"]))


def parse_prices(value: str) -> tuple[int, int] | None:
    """Понимает 100₽/100звезд, 100/100, 100 ₽ / 100 ⭐ или одну цену в рублях."""
    normalized = value.strip().lower().replace("ё", "е")
    numbers = re.findall(r"\d+", normalized)
    if "/" in normalized:
        if len(numbers) != 2:
            return None
        return int(numbers[0]), int(numbers[1])
    if len(numbers) == 1:
        return int(numbers[0]), 0
    return None


def parse_quantity(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(?:шт\.?|штук[аи]?)?\s*", value.lower())
    return int(match.group(1)) if match else None


def parse_quick_product_line(value: str) -> tuple[str, int, int, int] | None:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) != 3 or not parts[0] or len(parts[0]) > 100:
        return None
    parsed_prices = parse_prices(parts[1])
    stock = parse_quantity(parts[2])
    if parsed_prices is None or stock is None:
        return None
    price_rub, price_stars = parsed_prices
    return parts[0], price_rub, price_stars, stock


def normalize_star_delivery(value: str) -> str | None:
    normalized = value.strip().lower().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized)
    if normalized in {"подарком", "подарок", "gift", "через подарок"}:
        return "gift"
    if normalized in {"на аккаунт", "аккаунт", "fragment", "через fragment", "на акк"}:
        return "account"
    return None


def parse_star_amount(value: str) -> int | None:
    normalized = value.strip().lower().replace("ё", "е")
    match = re.fullmatch(r"\s*(\d+)\s*(?:⭐|stars?|звезд[аы]?|звезд|шт\.?)?\s*", normalized)
    if not match:
        return None
    amount = int(match.group(1))
    return amount if amount > 0 else None


def parse_stars_product_line(value: str) -> tuple[int, int, int, str] | None:
    """
    Короткий формат: 100 | 150₽ | подарком.
    Расширенный формат: 100 | 150₽ | 1000 | подарком,
    где 100 — размер заказа, а 1000 — общий остаток Stars.
    """
    parts = [part.strip() for part in value.split("|")]
    if len(parts) == 3:
        package_amount = parse_star_amount(parts[0])
        prices = parse_prices(parts[1])
        stock = package_amount
        delivery_method = normalize_star_delivery(parts[2])
    elif len(parts) == 4:
        package_amount = parse_star_amount(parts[0])
        prices = parse_prices(parts[1])
        stock = parse_star_amount(parts[2])
        delivery_method = normalize_star_delivery(parts[3])
    else:
        return None
    if package_amount is None or prices is None or stock is None or delivery_method is None:
        return None
    price_rub, _ = prices
    if price_rub <= 0 or stock < package_amount:
        return None
    return package_amount, price_rub, stock, delivery_method


def product_unit_amount(product: sqlite3.Row) -> int:
    try:
        return max(1, int(product["unit_amount"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return 1


def product_available(product: sqlite3.Row) -> int:
    return max(0, int(product["available"]))


def product_can_buy(product: sqlite3.Row) -> bool:
    return product_available(product) >= product_unit_amount(product)


def delivery_label(value: str | None) -> str:
    return STAR_DELIVERY_LABELS.get(value or "standard", html.escape(value or "—"))


def stock_label(product: sqlite3.Row, value: int | None = None) -> str:
    amount = product_available(product) if value is None else max(0, int(value))
    if product["category"] == "stars":
        return stars(amount)
    return f"{amount} шт."


def safe_username(username: str | None, user_id: int) -> str:
    if username:
        return f"@{html.escape(username)}"
    return f"<code>{user_id}</code>"


def valid_tme_url(url: str) -> bool:
    return bool(re.fullmatch(r"https://t\.me/[A-Za-z0-9_+\-/]+", url.strip()))


def category_title(category: str) -> str:
    return CATEGORY_TITLES.get(category, f"📁 {html.escape(category)}")


def main_menu_keyboard(show_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛒 Купить аккаунты"), KeyboardButton(text="⭐ Купить Stars")],
        [KeyboardButton(text="📝 Отзывы"), KeyboardButton(text="👨‍💻 Поддержка")],
    ]
    if show_admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел",
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Склад и товары", callback_data="admin:stock")
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="📜 История заказов", callback_data="admin:history:0")
    builder.button(text="🎟 Промокоды", callback_data="admin:promos")
    builder.button(text="📣 Рассылка", callback_data="admin:broadcast")
    builder.adjust(1)
    return builder.as_markup()


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ==========================================================
# БАЗА ДАННЫХ
# ==========================================================


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_blocked INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL,
                    price_rub INTEGER NOT NULL DEFAULT 0 CHECK(price_rub >= 0),
                    price_stars INTEGER NOT NULL DEFAULT 0 CHECK(price_stars >= 0),
                    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
                    unit_amount INTEGER NOT NULL DEFAULT 1 CHECK(unit_amount > 0),
                    delivery_method TEXT NOT NULL DEFAULT 'standard',
                    is_visible INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 1 AND 100),
                    expires_on TEXT,
                    max_uses INTEGER,
                    uses INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    product_id INTEGER NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    base_price_rub INTEGER NOT NULL,
                    base_price_stars INTEGER NOT NULL DEFAULT 0,
                    discount_percent INTEGER NOT NULL DEFAULT 0,
                    final_price_rub INTEGER NOT NULL,
                    final_price_stars INTEGER NOT NULL DEFAULT 0,
                    promo_code TEXT,
                    payment_method TEXT NOT NULL,
                    stock_units INTEGER NOT NULL DEFAULT 1,
                    delivery_method TEXT NOT NULL DEFAULT 'standard',
                    receipt_file_id TEXT NOT NULL,
                    receipt_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    processed_by INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(product_id) REFERENCES products(id)
                );

                CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
                CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
                """
            )

            # Мягкая миграция старой SQLite-базы без удаления данных.
            product_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(products)")}
            if "price_stars" not in product_columns:
                self.conn.execute("ALTER TABLE products ADD COLUMN price_stars INTEGER NOT NULL DEFAULT 0")
                self.conn.execute("UPDATE products SET price_stars = price_rub WHERE price_stars = 0")
            if "unit_amount" not in product_columns:
                self.conn.execute("ALTER TABLE products ADD COLUMN unit_amount INTEGER NOT NULL DEFAULT 1")
            if "delivery_method" not in product_columns:
                self.conn.execute(
                    "ALTER TABLE products ADD COLUMN delivery_method TEXT NOT NULL DEFAULT 'standard'"
                )

            order_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(orders)")}
            if "base_price_stars" not in order_columns:
                self.conn.execute("ALTER TABLE orders ADD COLUMN base_price_stars INTEGER NOT NULL DEFAULT 0")
            if "final_price_stars" not in order_columns:
                self.conn.execute("ALTER TABLE orders ADD COLUMN final_price_stars INTEGER NOT NULL DEFAULT 0")
            if "stock_units" not in order_columns:
                self.conn.execute("ALTER TABLE orders ADD COLUMN stock_units INTEGER NOT NULL DEFAULT 1")
            if "delivery_method" not in order_columns:
                self.conn.execute(
                    "ALTER TABLE orders ADD COLUMN delivery_method TEXT NOT NULL DEFAULT 'standard'"
                )

            now = utc_now_iso()
            for category, code, name, price_rub, price_stars, stock, visible in SEED_PRODUCTS:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO products
                    (category, code, name, price_rub, price_stars, stock, reserved, is_visible, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (category, code.lower(), name, price_rub, price_stars, stock, visible, now, now),
                )
            self.conn.commit()

    async def upsert_user(self, message: Message) -> None:
        user = message.from_user
        if not user:
            return
        now = utc_now_iso()
        async with self.lock:
            self.conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (user.id, user.username, user.first_name, now, now),
            )
            self.conn.commit()

    async def get_products(self, category: str | None = None, include_hidden: bool = False) -> list[sqlite3.Row]:
        conditions: list[str] = []
        params: list[Any] = []
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if not include_hidden:
            conditions.append("is_visible = 1")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT *, MAX(stock - reserved, 0) AS available
            FROM products
            {where}
            ORDER BY category, id
        """
        async with self.lock:
            return list(self.conn.execute(query, params).fetchall())

    async def get_product(self, product_id: int) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT *, MAX(stock - reserved, 0) AS available FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()

    async def get_product_by_code(self, code: str) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT *, MAX(stock - reserved, 0) AS available FROM products WHERE code = ? COLLATE NOCASE",
                (code.strip(),),
            ).fetchone()

    async def add_product(
        self,
        category: str,
        code: str,
        name: str,
        price_rub: int,
        price_stars: int,
        stock: int,
    ) -> tuple[bool, str]:
        now = utc_now_iso()
        async with self.lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO products
                    (category, code, name, price_rub, price_stars, stock, reserved, is_visible, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                    """,
                    (category.lower(), code.lower(), name, price_rub, price_stars, stock, now, now),
                )
                self.conn.commit()
                return True, "Товар добавлен."
            except sqlite3.IntegrityError:
                return False, "Товар с таким кодом уже существует."

    async def quick_add_product(
        self,
        name: str,
        price_rub: int,
        price_stars: int,
        stock_to_add: int,
    ) -> tuple[bool, str]:
        """Создаёт товар accounts или пополняет товар с тем же названием."""
        if min(price_rub, price_stars, stock_to_add) < 0:
            return False, "Цена и количество не могут быть отрицательными."
        now = utc_now_iso()
        async with self.lock:
            existing = self.conn.execute(
                "SELECT * FROM products WHERE category = 'accounts' AND name = ? COLLATE NOCASE",
                (name.strip(),),
            ).fetchone()
            if existing:
                self.conn.execute(
                    """
                    UPDATE products
                    SET price_rub = ?, price_stars = ?, stock = stock + ?,
                        is_visible = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (price_rub, price_stars, stock_to_add, now, existing["id"]),
                )
                self.conn.commit()
                return True, f"Товар обновлён. Теперь на складе: {existing['stock'] + stock_to_add} шт."

            code = "quick_" + local_now().strftime("%Y%m%d%H%M%S%f")
            self.conn.execute(
                """
                INSERT INTO products
                (category, code, name, price_rub, price_stars, stock, reserved, is_visible, created_at, updated_at)
                VALUES ('accounts', ?, ?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (code, name.strip(), price_rub, price_stars, stock_to_add, now, now),
            )
            self.conn.commit()
            return True, f"Новый товар добавлен на склад: {stock_to_add} шт."

    async def quick_add_stars_product(
        self,
        package_amount: int,
        price_rub: int,
        stock_to_add: int,
        delivery_method: str,
    ) -> tuple[bool, str]:
        """Создаёт пакет Stars или пополняет совпадающий пакет."""
        if package_amount <= 0 or price_rub <= 0 or stock_to_add <= 0:
            return False, "Количество Stars, цена и остаток должны быть больше нуля."
        if stock_to_add < package_amount:
            return False, "Остаток Stars не может быть меньше размера одного пакета."
        if delivery_method not in {"gift", "account"}:
            return False, "Неизвестный способ выдачи Stars."

        now = utc_now_iso()
        async with self.lock:
            existing = self.conn.execute(
                """
                SELECT * FROM products
                WHERE category = 'stars' AND unit_amount = ? AND delivery_method = ?
                ORDER BY id LIMIT 1
                """,
                (package_amount, delivery_method),
            ).fetchone()
            if existing:
                new_stock = int(existing["stock"]) + stock_to_add
                self.conn.execute(
                    """
                    UPDATE products
                    SET name = ?, price_rub = ?, price_stars = 0, stock = ?,
                        is_visible = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        f"{package_amount} Telegram Stars",
                        price_rub,
                        new_stock,
                        now,
                        existing["id"],
                    ),
                )
                self.conn.commit()
                return True, (
                    f"Пакет обновлён: {package_amount} Stars за {money(price_rub)}. "
                    f"Общий остаток: {stars(new_stock)}. Выдача: {delivery_label(delivery_method)}."
                )

            suffix = "gift" if delivery_method == "gift" else "account"
            base_code = f"stars_{package_amount}_{suffix}"
            code = base_code
            counter = 2
            while self.conn.execute(
                "SELECT 1 FROM products WHERE code = ? COLLATE NOCASE", (code,)
            ).fetchone():
                code = f"{base_code}_{counter}"
                counter += 1

            self.conn.execute(
                """
                INSERT INTO products
                (category, code, name, price_rub, price_stars, stock, reserved,
                 unit_amount, delivery_method, is_visible, created_at, updated_at)
                VALUES ('stars', ?, ?, ?, 0, ?, 0, ?, ?, 1, ?, ?)
                """,
                (
                    code,
                    f"{package_amount} Telegram Stars",
                    price_rub,
                    stock_to_add,
                    package_amount,
                    delivery_method,
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return True, (
                f"Пакет Stars добавлен. Код: {code}. Размер: {stars(package_amount)}, "
                f"цена: {money(price_rub)}, остаток: {stars(stock_to_add)}, "
                f"выдача: {delivery_label(delivery_method)}."
            )

    async def change_stock(self, product_id: int, mode: str, value: int) -> tuple[bool, str]:
        async with self.lock:
            row = self.conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if not row:
                return False, "Товар не найден."

            if mode == "add":
                new_stock = row["stock"] + value
            elif mode == "subtract":
                new_stock = row["stock"] - value
            elif mode == "set":
                new_stock = value
            else:
                return False, "Неизвестная операция."

            if new_stock < row["reserved"]:
                return False, f"Нельзя установить меньше зарезервированного количества: {row['reserved']}."
            if new_stock < 0:
                return False, "Количество не может быть отрицательным."

            self.conn.execute(
                "UPDATE products SET stock = ?, updated_at = ? WHERE id = ?",
                (new_stock, utc_now_iso(), product_id),
            )
            self.conn.commit()
            if row["category"] == "stars":
                return True, f"Новый остаток: {stars(new_stock)}."
            return True, f"Новое количество: {new_stock} шт."

    async def set_price(self, product_id: int, price_rub: int, price_stars: int = 0) -> tuple[bool, str]:
        if price_rub < 0 or price_stars < 0:
            return False, "Цена не может быть отрицательной."
        async with self.lock:
            cursor = self.conn.execute(
                "UPDATE products SET price_rub = ?, price_stars = ?, updated_at = ? WHERE id = ?",
                (price_rub, price_stars, utc_now_iso(), product_id),
            )
            self.conn.commit()
            if cursor.rowcount == 0:
                return False, "Товар не найден."
            return True, f"Новая цена: {format_prices(price_rub, price_stars)}."

    async def toggle_product_visibility(self, product_id: int) -> tuple[bool, str]:
        async with self.lock:
            row = self.conn.execute("SELECT is_visible FROM products WHERE id = ?", (product_id,)).fetchone()
            if not row:
                return False, "Товар не найден."
            new_value = 0 if row["is_visible"] else 1
            self.conn.execute(
                "UPDATE products SET is_visible = ?, updated_at = ? WHERE id = ?",
                (new_value, utc_now_iso(), product_id),
            )
            self.conn.commit()
            return True, "Товар показан в каталоге." if new_value else "Товар скрыт из каталога."

    async def validate_promo(self, code: str) -> tuple[bool, str, sqlite3.Row | None]:
        normalized = code.strip().upper()
        async with self.lock:
            promo = self.conn.execute(
                "SELECT * FROM promo_codes WHERE code = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()

        if not promo:
            return False, "Промокод не найден.", None
        if not promo["is_active"]:
            return False, "Промокод отключён.", None
        if promo["expires_on"]:
            try:
                if date.fromisoformat(promo["expires_on"]) < local_now().date():
                    return False, "Срок действия промокода истёк.", None
            except ValueError:
                return False, "У промокода некорректный срок действия.", None
        if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
            return False, "Лимит использований промокода исчерпан.", None
        return True, "Промокод применён.", promo

    async def create_promo(
        self,
        code: str,
        discount_percent: int,
        expires_on: str | None,
        max_uses: int | None,
    ) -> tuple[bool, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", code):
            return False, "Код: 2–32 символа, латиница, цифры, _ или -."
        if not 1 <= discount_percent <= 100:
            return False, "Скидка должна быть от 1 до 100%."
        if max_uses is not None and max_uses <= 0:
            return False, "Лимит использований должен быть больше нуля."
        if expires_on:
            try:
                expiration = date.fromisoformat(expires_on)
            except ValueError:
                return False, "Дата должна быть в формате ГГГГ-ММ-ДД."
            if expiration < local_now().date():
                return False, "Дата окончания уже прошла."

        async with self.lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO promo_codes
                    (code, discount_percent, expires_on, max_uses, uses, is_active, created_at)
                    VALUES (?, ?, ?, ?, 0, 1, ?)
                    """,
                    (code.upper(), discount_percent, expires_on, max_uses, utc_now_iso()),
                )
                self.conn.commit()
                return True, "Промокод создан."
            except sqlite3.IntegrityError:
                return False, "Промокод с таким названием уже существует."

    async def get_promos(self) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM promo_codes ORDER BY is_active DESC, id DESC LIMIT 30"
                ).fetchall()
            )

    async def toggle_promo(self, promo_id: int) -> tuple[bool, str]:
        async with self.lock:
            promo = self.conn.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,)).fetchone()
            if not promo:
                return False, "Промокод не найден."
            new_value = 0 if promo["is_active"] else 1
            self.conn.execute("UPDATE promo_codes SET is_active = ? WHERE id = ?", (new_value, promo_id))
            self.conn.commit()
            return True, "Промокод включён." if new_value else "Промокод отключён."

    async def create_pending_order(
        self,
        user_id: int,
        username: str | None,
        product_id: int,
        payment_method: str,
        receipt_file_id: str,
        receipt_type: str,
        promo_code: str | None,
    ) -> tuple[bool, str, sqlite3.Row | None]:
        """Создаёт заявку и резервирует нужное число складских единиц."""
        async with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")

                product = self.conn.execute(
                    "SELECT *, (stock - reserved) AS available FROM products WHERE id = ?",
                    (product_id,),
                ).fetchone()
                if not product or not product["is_visible"]:
                    self.conn.rollback()
                    return False, "Товар больше недоступен.", None

                units = max(1, int(product["unit_amount"]))
                if int(product["available"]) < units:
                    self.conn.rollback()
                    return False, "Товар закончился или остатка недостаточно.", None

                discount = 0
                normalized_promo: str | None = None
                if promo_code:
                    normalized_promo = promo_code.strip().upper()
                    promo = self.conn.execute(
                        "SELECT * FROM promo_codes WHERE code = ? COLLATE NOCASE",
                        (normalized_promo,),
                    ).fetchone()
                    if not promo or not promo["is_active"]:
                        self.conn.rollback()
                        return False, "Промокод больше недоступен.", None
                    if promo["expires_on"] and date.fromisoformat(promo["expires_on"]) < local_now().date():
                        self.conn.rollback()
                        return False, "Срок действия промокода истёк.", None
                    if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
                        self.conn.rollback()
                        return False, "Лимит промокода исчерпан.", None
                    discount = promo["discount_percent"]
                    self.conn.execute(
                        "UPDATE promo_codes SET uses = uses + 1 WHERE id = ?",
                        (promo["id"],),
                    )

                final_price_rub = max(0, round(product["price_rub"] * (100 - discount) / 100))
                final_price_stars = max(0, round(product["price_stars"] * (100 - discount) / 100))
                if payment_method == "card" and product["price_rub"] <= 0:
                    self.conn.rollback()
                    return False, "Для товара недоступна оплата рублями.", None
                if payment_method == "stars" and product["price_stars"] <= 0:
                    self.conn.rollback()
                    return False, "Для товара недоступна оплата Stars.", None
                if product["category"] == "stars" and payment_method != "card":
                    self.conn.rollback()
                    return False, "Покупка Stars оплачивается рублями через Т-Банк.", None

                now = utc_now_iso()
                cursor = self.conn.execute(
                    """
                    INSERT INTO orders (
                        user_id, username, product_id, product_code, product_name, category,
                        base_price_rub, base_price_stars, discount_percent,
                        final_price_rub, final_price_stars, promo_code,
                        payment_method, stock_units, delivery_method,
                        receipt_file_id, receipt_type, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        user_id,
                        username,
                        product["id"],
                        product["code"],
                        product["name"],
                        product["category"],
                        product["price_rub"],
                        product["price_stars"],
                        discount,
                        final_price_rub,
                        final_price_stars,
                        normalized_promo,
                        payment_method,
                        units,
                        product["delivery_method"],
                        receipt_file_id,
                        receipt_type,
                        now,
                    ),
                )
                order_id = cursor.lastrowid
                self.conn.execute(
                    "UPDATE products SET reserved = reserved + ?, updated_at = ? WHERE id = ?",
                    (units, now, product_id),
                )
                self.conn.commit()

                order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                return True, "Заявка создана.", order
            except Exception:
                self.conn.rollback()
                logger.exception("Не удалось создать заказ")
                return False, "Ошибка базы данных при создании заказа.", None

    async def approve_order(self, order_id: int, admin_id: int) -> tuple[str, sqlite3.Row | None]:
        async with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if not order:
                    self.conn.rollback()
                    return "not_found", None
                if order["status"] != "pending":
                    self.conn.rollback()
                    return order["status"], order

                product = self.conn.execute("SELECT * FROM products WHERE id = ?", (order["product_id"],)).fetchone()
                if not product:
                    self.conn.rollback()
                    return "product_missing", order
                units = max(1, int(order["stock_units"]))
                if product["stock"] < units or product["reserved"] < units:
                    self.conn.rollback()
                    return "no_stock", order

                now = utc_now_iso()
                self.conn.execute(
                    """
                    UPDATE products
                    SET stock = stock - ?,
                        reserved = MAX(reserved - ?, 0),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (units, units, now, order["product_id"]),
                )
                self.conn.execute(
                    """
                    UPDATE orders
                    SET status = 'approved', processed_at = ?, processed_by = ?
                    WHERE id = ?
                    """,
                    (now, admin_id, order_id),
                )
                self.conn.commit()
                updated = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                return "approved", updated
            except Exception:
                self.conn.rollback()
                logger.exception("Не удалось подтвердить заказ %s", order_id)
                return "error", None

    async def cancel_order(self, order_id: int, admin_id: int) -> tuple[str, sqlite3.Row | None]:
        async with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if not order:
                    self.conn.rollback()
                    return "not_found", None
                if order["status"] != "pending":
                    self.conn.rollback()
                    return order["status"], order

                now = utc_now_iso()
                self.conn.execute(
                    """
                    UPDATE products
                    SET reserved = MAX(reserved - ?, 0), updated_at = ?
                    WHERE id = ?
                    """,
                    (max(1, int(order["stock_units"])), now, order["product_id"]),
                )
                if order["promo_code"]:
                    self.conn.execute(
                        """
                        UPDATE promo_codes
                        SET uses = MAX(uses - 1, 0)
                        WHERE code = ? COLLATE NOCASE
                        """,
                        (order["promo_code"],),
                    )
                self.conn.execute(
                    """
                    UPDATE orders
                    SET status = 'cancelled', processed_at = ?, processed_by = ?
                    WHERE id = ?
                    """,
                    (now, admin_id, order_id),
                )
                self.conn.commit()
                updated = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                return "cancelled", updated
            except Exception:
                self.conn.rollback()
                logger.exception("Не удалось отменить заказ %s", order_id)
                return "error", None

    async def get_order(self, order_id: int) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            )

    async def get_statistics(self) -> dict[str, Any]:
        start, end = today_utc_bounds()
        async with self.lock:
            today = self.conn.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(CASE WHEN payment_method = 'card' THEN final_price_rub ELSE 0 END), 0) AS revenue_rub,
                       COALESCE(SUM(CASE WHEN payment_method = 'stars' THEN final_price_stars ELSE 0 END), 0) AS revenue_stars
                FROM orders
                WHERE status = 'approved' AND processed_at BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()
            total = self.conn.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(CASE WHEN payment_method = 'card' THEN final_price_rub ELSE 0 END), 0) AS revenue_rub,
                       COALESCE(SUM(CASE WHEN payment_method = 'stars' THEN final_price_stars ELSE 0 END), 0) AS revenue_stars
                FROM orders WHERE status = 'approved'
                """
            ).fetchone()
            pending = self.conn.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE status = 'pending'"
            ).fetchone()
            top = list(
                self.conn.execute(
                    """
                    SELECT product_name, COUNT(*) AS sold,
                           SUM(CASE WHEN payment_method = 'card' THEN final_price_rub ELSE 0 END) AS revenue_rub,
                           SUM(CASE WHEN payment_method = 'stars' THEN final_price_stars ELSE 0 END) AS revenue_stars
                    FROM orders
                    WHERE status = 'approved'
                    GROUP BY product_id, product_name
                    ORDER BY sold DESC, revenue_rub DESC, revenue_stars DESC
                    LIMIT 5
                    """
                ).fetchall()
            )
        return {
            "today_count": today["count"],
            "today_revenue_rub": today["revenue_rub"],
            "today_revenue_stars": today["revenue_stars"],
            "total_count": total["count"],
            "total_revenue_rub": total["revenue_rub"],
            "total_revenue_stars": total["revenue_stars"],
            "pending_count": pending["count"],
            "top": top,
        }

    async def get_order_history(self, page: int, page_size: int = 10) -> tuple[list[sqlite3.Row], int]:
        offset = max(page, 0) * page_size
        async with self.lock:
            total = self.conn.execute("SELECT COUNT(*) AS count FROM orders").fetchone()["count"]
            rows = list(
                self.conn.execute(
                    "SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
            )
        return rows, total

    async def get_all_user_ids(self) -> list[int]:
        async with self.lock:
            return [
                row["user_id"]
                for row in self.conn.execute(
                    "SELECT user_id FROM users WHERE is_blocked = 0 ORDER BY user_id"
                ).fetchall()
            ]

    async def mark_user_blocked(self, user_id: int, blocked: bool = True) -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE users SET is_blocked = ? WHERE user_id = ?",
                (1 if blocked else 0, user_id),
            )
            self.conn.commit()


# ==========================================================
# СОСТОЯНИЯ FSM
# ==========================================================


class PurchaseStates(StatesGroup):
    waiting_promo = State()
    waiting_receipt = State()


class AdminStates(StatesGroup):
    waiting_stock_value = State()
    waiting_price = State()
    waiting_new_product = State()
    waiting_new_promo = State()
    waiting_broadcast = State()
    waiting_broadcast_confirmation = State()


# ==========================================================
# ФОРМАТИРОВАНИЕ СООБЩЕНИЙ
# ==========================================================


def build_order_caption(order: sqlite3.Row, status: str | None = None) -> str:
    actual_status = status or order["status"]
    username = safe_username(order["username"], order["user_id"])
    lines = [
        f"<b>🧾 Заявка на выдачу #{order['id']}</b>",
        "",
        f"• Username покупателя: {username}",
        f"• Что купил: <b>{html.escape(order['product_name'])}</b>",
        f"• Цена: <b>{order_amount(order)}</b>",
        f"• Способ оплаты: {PAYMENT_METHOD_LABELS.get(order['payment_method'], html.escape(order['payment_method']))}",
    ]
    if order["category"] == "stars":
        lines.extend(
            [
                f"• Количество Stars: <b>{stars(int(order['stock_units']))}</b>",
                f"• Способ выдачи: <b>{delivery_label(order['delivery_method'])}</b>",
            ]
        )
    lines.extend(
        [
            "• Скрин оплаты: прикреплён к заявке",
            f"• Дата и время: {format_datetime(order['created_at'])}",
        ]
    )
    if order["discount_percent"]:
        lines.append(
            f"• Промокод: <code>{html.escape(order['promo_code'] or '')}</code> "
            f"(скидка {order['discount_percent']}%)"
        )
    if actual_status != "pending":
        lines.append(f"• Статус: {ORDER_STATUS_LABELS.get(actual_status, html.escape(actual_status))}")
    return "\n".join(lines)


def order_admin_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выдать", callback_data=f"order:approve:{order_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"order:cancel:{order_id}"),
            ]
        ]
    )


def product_manage_keyboard(product: sqlite3.Row) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить", callback_data=f"stock:change:add:{product['id']}")
    builder.button(text="➖ Списать", callback_data=f"stock:change:subtract:{product['id']}")
    builder.button(text="✏️ Установить количество", callback_data=f"stock:change:set:{product['id']}")
    builder.button(text="💰 Изменить цену", callback_data=f"stock:price:{product['id']}")
    visibility_text = "🙈 Скрыть" if product["is_visible"] else "👁 Показать"
    builder.button(text=visibility_text, callback_data=f"stock:toggle:{product['id']}")
    builder.button(text="⬅️ К складу", callback_data="admin:stock")
    builder.adjust(2, 1, 1, 1, 1)
    return builder.as_markup()


def product_admin_text(product: sqlite3.Row) -> str:
    lines = [
        f"<b>⚙️ {html.escape(product['name'])}</b>",
        "",
        f"Код: <code>{html.escape(product['code'])}</code>",
        f"Категория: <code>{html.escape(product['category'])}</code>",
        f"Цена: <b>{product_prices(product)}</b>",
    ]
    if product["category"] == "stars":
        lines.extend(
            [
                f"Размер одного заказа: <b>{stars(product_unit_amount(product))}</b>",
                f"Способ выдачи: <b>{delivery_label(product['delivery_method'])}</b>",
            ]
        )
    lines.extend(
        [
            f"Всего на складе: <b>{stock_label(product, product['stock'])}</b>",
            f"Зарезервировано заявками: <b>{stock_label(product, product['reserved'])}</b>",
            f"Доступно для покупки: <b>{stock_label(product, product['available'])}</b>",
            f"Видимость: {'показывается' if product['is_visible'] else 'скрыт'}",
        ]
    )
    return "\n".join(lines)


# ==========================================================
# ЭКЗЕМПЛЯРЫ БОТА И БАЗЫ
# ==========================================================


db = Database(DB_PATH)
bot: Bot
dp = Dispatcher()
dp.include_router(router)


# ==========================================================
# КАТАЛОГ И ПОКУПКА
# ==========================================================


async def show_catalog(target: Message, category: str, edit: bool = False) -> None:
    products = await db.get_products(category=category)
    title = category_title(category)

    if not products:
        text = (
            f"<b>{title}</b>\n\n"
            "Сейчас в этом разделе нет товаров. Администратор сможет добавить их через /admin."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data=f"catalog:{category}")]]
        )
    else:
        blocks: list[str] = [f"<b>{title}</b>"]
        builder = InlineKeyboardBuilder()
        for product in products:
            available = product_available(product)
            can_buy = product_can_buy(product)
            if category == "stars":
                product_lines = [
                    f"<b>⭐ {product_unit_amount(product)} Stars</b>",
                    f"Цена: <b>{money(int(product['price_rub']))}</b>",
                    f"В наличии: <b>{stars(available)}</b>",
                    f"Выдача: <b>{delivery_label(product['delivery_method'])}</b>",
                ]
            else:
                availability = f"{available} шт." if available > 0 else "Нет в наличии"
                product_lines = [
                    f"<b>{html.escape(product['name'])}</b>",
                    f"Цена: <b>{product_prices(product)}</b>",
                    f"В наличии: <b>{availability}</b>",
                ]
            blocks.append("\n".join(product_lines))
            if can_buy:
                button_name = (
                    f"🛒 Купить {product_unit_amount(product)} ⭐"
                    if category == "stars"
                    else f"🛒 Купить — {product['name']}"
                )
                builder.button(text=button_name, callback_data=f"buy:{product['id']}")
            else:
                builder.button(text="❌ Нет в наличии", callback_data="noop:out_of_stock")
        builder.button(text="🔄 Обновить", callback_data=f"catalog:{category}")
        builder.adjust(1)
        text = "\n\n".join(blocks)
        keyboard = builder.as_markup()

    if edit:
        try:
            await safe_edit_text(target, text, reply_markup=keyboard)
        except TelegramBadRequest:
            await target.answer(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def show_payment_methods(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    product = await db.get_product(int(data["product_id"]))
    if not product or not product_can_buy(product) or not product["is_visible"]:
        await state.clear()
        await message.answer("❌ Товар уже закончился или был скрыт.", reply_markup=main_menu_keyboard())
        return

    promo_code = data.get("promo_code")
    discount = 0
    if promo_code:
        valid, _, promo = await db.validate_promo(promo_code)
        if valid and promo:
            discount = promo["discount_percent"]
        else:
            await state.update_data(promo_code=None, discount_percent=0)
            promo_code = None

    final_price_rub = max(0, round(product["price_rub"] * (100 - discount) / 100))
    final_price_stars = max(0, round(product["price_stars"] * (100 - discount) / 100))
    await state.update_data(
        final_price_rub=final_price_rub,
        final_price_stars=final_price_stars,
        discount_percent=discount,
    )

    payment_price = money(final_price_rub) if product["category"] == "stars" else format_prices(final_price_rub, final_price_stars)
    text = [
        "<b>Оформление заказа</b>",
        "",
        f"📦 Товар: <b>{html.escape(product['name'])}</b>",
        f"💰 К оплате: <b>{payment_price}</b>",
    ]
    if product["category"] == "stars":
        text.extend(
            [
                f"⭐ Количество: <b>{stars(product_unit_amount(product))}</b>",
                f"🚚 Выдача: <b>{delivery_label(product['delivery_method'])}</b>",
            ]
        )
    if promo_code:
        text.append(f"🎟 Промокод: <code>{html.escape(promo_code)}</code> (−{discount}%)")
    text.extend(["", "Выберите способ оплаты:"])

    payment_rows: list[list[InlineKeyboardButton]] = []
    if final_price_rub > 0:
        payment_rows.append([InlineKeyboardButton(text=f"💳 Т-Банк — {money(final_price_rub)}", callback_data="pay:card")])
    if product["category"] != "stars" and final_price_stars > 0:
        payment_rows.append([InlineKeyboardButton(text=f"⭐ Stars — {stars(final_price_stars)}", callback_data="pay:stars")])
    payment_rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="purchase:cancel")])
    await message.answer("\n".join(text), reply_markup=InlineKeyboardMarkup(inline_keyboard=payment_rows))


@router.message(CommandStart())
async def command_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.upsert_user(message)
    text = (
        "<b>Добро пожаловать в Tripov Shop!</b> 👋\n\n"
        "Выберите аккаунт или пакет Telegram Stars, оплатите заказ и отправьте чек. "
        "После проверки администратор подтвердит заявку и выполнит выдачу вручную.\n\n"
        "По вопросам используйте кнопку «👨‍💻 Поддержка»."
    )
    if message.from_user and is_owner(message.from_user.id):
        text += "\n\n⚙️ Управление: /admin\n📖 Справка по админ-функциям: /help"
    await message.answer(
        text,
        reply_markup=main_menu_keyboard(bool(message.from_user and is_owner(message.from_user.id))),
    )


@router.message(Command("cancel"))
async def command_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
    else:
        await message.answer("Нет активного действия.", reply_markup=main_menu_keyboard())


@router.message(Command("myorders"))
async def command_my_orders(message: Message) -> None:
    await db.upsert_user(message)
    if not message.from_user:
        return
    orders = await db.get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    lines = ["<b>Ваши последние заказы</b>"]
    for order in orders:
        lines.append(
            f"\n<b>#{order['id']}</b> · {html.escape(order['product_name'])}\n"
            f"{order_amount(order)} · {ORDER_STATUS_LABELS.get(order['status'], order['status'])}\n"
            f"{format_datetime(order['created_at'])}"
        )
    await message.answer("\n".join(lines))


@router.message(F.text == "🛒 Купить аккаунты")
async def menu_accounts(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.upsert_user(message)
    await show_catalog(message, "accounts")


@router.message(F.text == "⭐ Купить Stars")
async def menu_stars(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.upsert_user(message)
    await show_catalog(message, "stars")


@router.message(F.text == "📝 Отзывы")
async def menu_reviews(message: Message) -> None:
    await db.upsert_user(message)
    url = REVIEWS_CHANNEL_URL if valid_tme_url(REVIEWS_CHANNEL_URL) else "https://t.me/telegram"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📝 Открыть отзывы", url=url)]]
    )
    await message.answer("Отзывы о Tripov Shop находятся в отдельном Telegram-канале.", reply_markup=keyboard)


@router.message(F.text == "👨‍💻 Поддержка")
async def menu_support(message: Message) -> None:
    await db.upsert_user(message)
    username = SUPPORT_USERNAME.lstrip("@").strip()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💻 Написать администратору", url=f"https://t.me/{username}")]
        ]
    )
    await message.answer("Нажмите кнопку ниже, чтобы написать в поддержку Tripov Shop.", reply_markup=keyboard)


@router.callback_query(F.data.startswith("catalog:"))
async def callback_catalog(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    category = callback.data.split(":", 1)[1]
    if callback.message:
        await show_catalog(callback.message, category, edit=True)


@router.callback_query(F.data == "noop:out_of_stock")
async def callback_out_of_stock(callback: CallbackQuery) -> None:
    await callback.answer("Товар закончился.", show_alert=True)


@router.callback_query(F.data.startswith("buy:"))
async def callback_buy(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    product_id = int(callback.data.split(":", 1)[1])
    product = await db.get_product(product_id)
    if not product or not product["is_visible"] or not product_can_buy(product):
        await callback.answer("Товар уже закончился.", show_alert=True)
        return

    await callback.answer()
    await state.clear()
    await state.update_data(product_id=product_id, promo_code=None, discount_percent=0)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:enter")],
            [InlineKeyboardButton(text="➡️ Продолжить без промокода", callback_data="promo:skip")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="purchase:cancel")],
        ]
    )
    await callback.message.answer(
        f"Вы выбрали <b>{html.escape(product['name'])}</b> за <b>{product_prices(product)}</b>.\n\n"
        "Есть промокод?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "promo:enter")
async def callback_enter_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if "product_id" not in data:
        await callback.answer("Начните покупку заново.", show_alert=True)
        return
    await state.set_state(PurchaseStates.waiting_promo)
    if callback.message:
        await callback.message.answer("Введите промокод одним сообщением. Для отмены: /cancel")


@router.message(PurchaseStates.waiting_promo, F.text)
async def process_promo(message: Message, state: FSMContext) -> None:
    code = message.text.strip().upper()
    valid, info, promo = await db.validate_promo(code)
    if not valid or not promo:
        await message.answer(f"❌ {html.escape(info)}\nВведите другой код или нажмите /cancel.")
        return
    await state.update_data(promo_code=promo["code"], discount_percent=promo["discount_percent"])
    await state.set_state(None)
    await message.answer(f"✅ Промокод применён. Скидка: <b>{promo['discount_percent']}%</b>.")
    await show_payment_methods(message, state)


@router.callback_query(F.data == "promo:skip")
async def callback_skip_promo(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "product_id" not in data:
        await callback.answer("Начните покупку заново.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(promo_code=None, discount_percent=0)
    if callback.message:
        await show_payment_methods(callback.message, state)


@router.callback_query(F.data.startswith("pay:"))
async def callback_payment_method(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return
    method = callback.data.split(":", 1)[1]
    if method not in PAYMENT_METHOD_LABELS:
        await callback.answer("Неизвестный способ оплаты.", show_alert=True)
        return
    data = await state.get_data()
    if "product_id" not in data:
        await callback.answer("Начните покупку заново.", show_alert=True)
        return

    product = await db.get_product(int(data["product_id"]))
    if not product or not product_can_buy(product):
        await state.clear()
        await callback.answer("Товар закончился.", show_alert=True)
        return

    final_price_rub = int(data.get("final_price_rub", product["price_rub"]))
    final_price_stars = int(data.get("final_price_stars", product["price_stars"]))
    if method == "card" and final_price_rub <= 0:
        await callback.answer("Оплата на карту для этого товара недоступна.", show_alert=True)
        return
    if method == "stars" and final_price_stars <= 0:
        await callback.answer("Оплата Stars для этого товара недоступна.", show_alert=True)
        return
    if product["category"] == "stars" and method != "card":
        await callback.answer("Покупка Stars оплачивается рублями через Т-Банк.", show_alert=True)
        return

    await callback.answer()
    if method == "card":
        phone = T_BANK_PHONE.strip()
        recipient = T_BANK_RECIPIENT.strip()
        bank_name = T_BANK_NAME.strip() or "Т-Банк"
        if not phone or phone == "+7XXXXXXXXXX":
            await callback.answer(
                "Номер телефона для оплаты не настроен владельцем.",
                show_alert=True,
            )
            return
        payment_text = (
            "<b>💳 Оплата рублями по номеру телефона</b>\n\n"
            f"Сумма: <b>{money(final_price_rub)}</b>\n"
            f"Банк: <b>{html.escape(bank_name)}</b>\n"
            f"Номер телефона: <code>{html.escape(phone)}</code>\n"
            f"Получатель: <b>{html.escape(recipient or 'уточните перед переводом')}</b>\n"
            f"Комментарий к переводу: <b>{html.escape(PAYMENT_COMMENT)}</b>\n\n"
            "Переведите точную сумму по номеру телефона через Т-Банк/СБП. "
            "Перед оплатой проверьте имя получателя и обязательно укажите комментарий, "
            "затем нажмите кнопку ниже и отправьте чек."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📎 Отправить чек Т-Банка", callback_data="receipt:start")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="purchase:cancel")],
            ]
        )
    else:
        receiver = STARS_RECEIVER_USERNAME.strip().lstrip("@")
        if not receiver:
            await callback.answer("Username владельца для Stars не настроен.", show_alert=True)
            return
        payment_text = (
            "<b>⭐ Ручная оплата Telegram Stars</b>\n\n"
            f"Сумма: <b>{stars(final_price_stars)}</b>\n\n"
            f"Нажмите кнопку ниже и отправьте <b>{stars(final_price_stars)}</b> владельцу "
            f"@{html.escape(receiver)}.\n\n"
            "Оплата проходит не внутри бота. После передачи Stars вернитесь сюда и "
            "отправьте скриншот подтверждения."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"⭐ Перейти к @{receiver}", url=f"https://t.me/{receiver}")],
                [InlineKeyboardButton(text="📎 Отправить подтверждение", callback_data="receipt:start")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="purchase:cancel")],
            ]
        )

    await state.update_data(payment_method=method)
    await callback.message.answer(payment_text, reply_markup=keyboard)


@router.callback_query(F.data == "receipt:start")
async def callback_receipt_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "payment_method" not in data or "product_id" not in data:
        await callback.answer("Начните покупку заново.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(PurchaseStates.waiting_receipt)
    if callback.message:
        method = data.get("payment_method")
        if method == "stars":
            prompt = (
                "Отправьте <b>скриншот подтверждения передачи Stars владельцу</b> "
                "одним сообщением.\n\nМожно отправить изображение как фото или документ."
            )
        else:
            prompt = (
                "Отправьте <b>чек перевода через Т-Банк</b> одним сообщением.\n\n"
                "Можно отправить изображение как фото или документ."
            )
        await callback.message.answer(prompt)


@router.callback_query(F.data == "purchase:cancel")
async def callback_purchase_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Покупка отменена.")
    await state.clear()
    if callback.message:
        await callback.message.answer("Покупка отменена.", reply_markup=main_menu_keyboard())


@router.message(PurchaseStates.waiting_receipt)
async def process_receipt(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    receipt_file_id: str | None = None
    receipt_type: str | None = None
    if message.photo:
        receipt_file_id = message.photo[-1].file_id
        receipt_type = "photo"
    elif message.document:
        receipt_file_id = message.document.file_id
        receipt_type = "document"

    if not receipt_file_id or not receipt_type:
        data = await state.get_data()
        if data.get("payment_method") == "stars":
            await message.answer("❌ Отправьте скриншот подтверждения как фото или документ.")
        else:
            await message.answer("❌ Отправьте чек Т-Банка как фото или документ.")
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    payment_method = data.get("payment_method")
    if not product_id or payment_method not in PAYMENT_METHOD_LABELS:
        await state.clear()
        await message.answer("Сессия покупки устарела. Начните заказ заново.", reply_markup=main_menu_keyboard())
        return

    await db.upsert_user(message)
    success, info, order = await db.create_pending_order(
        user_id=message.from_user.id,
        username=message.from_user.username,
        product_id=int(product_id),
        payment_method=payment_method,
        receipt_file_id=receipt_file_id,
        receipt_type=receipt_type,
        promo_code=data.get("promo_code"),
    )
    await state.clear()

    if not success or not order:
        await message.answer(f"❌ {html.escape(info)}", reply_markup=main_menu_keyboard())
        return

    caption = build_order_caption(order)
    keyboard = order_admin_keyboard(order["id"])

    try:
        if receipt_type == "photo":
            await bot.send_photo(OWNER_ID, receipt_file_id, caption=caption, reply_markup=keyboard)
        else:
            await bot.send_document(OWNER_ID, receipt_file_id, caption=caption, reply_markup=keyboard)
    except Exception:
        logger.exception("Не удалось отправить заказ владельцу")
        # Освобождаем резерв, потому что владелец не получил заявку.
        await db.cancel_order(order["id"], OWNER_ID)
        await message.answer(
            "❌ Не удалось передать заявку администратору. Заказ отменён, попробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.answer(
        f"✅ Чек отправлен администратору Tripov Shop.\n\nНомер заявки: <b>#{order['id']}</b>. "
        "После ручной проверки вам придёт уведомление.",
        reply_markup=main_menu_keyboard(),
    )


# ==========================================================
# ОБРАБОТКА ЗАКАЗОВ ВЛАДЕЛЬЦЕМ
# ==========================================================


async def edit_admin_order_message(callback: CallbackQuery, order: sqlite3.Row) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_caption(caption=build_order_caption(order), reply_markup=None)
    except TelegramBadRequest:
        try:
            await callback.message.edit_text(build_order_caption(order), reply_markup=None)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("order:approve:"))
async def callback_approve_order(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.rsplit(":", 1)[1])
    result, order = await db.approve_order(order_id, callback.from_user.id)

    if result == "approved" and order:
        await edit_admin_order_message(callback, order)
        try:
            await bot.send_message(
                order["user_id"],
                f"✅ <b>Заказ #{order['id']} подтверждён!</b>\n\n"
                f"Товар: {html.escape(order['product_name'])}\n"
                f"Сумма: {order_amount(order)}\n\n"
                "Администратор Tripov Shop подтвердил оплату и свяжется с вами для ручной выдачи товара.",
            )
        except Exception:
            logger.exception("Не удалось уведомить покупателя заказа #%s", order_id)

        await callback.answer(
            "Заказ подтверждён, остаток уменьшен и покупатель уведомлён.",
            show_alert=True,
        )
        return

    messages = {
        "approved": "Этот заказ уже был подтверждён.",
        "cancelled": "Этот заказ уже отменён.",
        "not_found": "Заказ не найден.",
        "product_missing": "Товар удалён из базы.",
        "no_stock": "Нельзя подтвердить: проблема с остатком или резервом.",
        "error": "Ошибка базы данных.",
    }
    await callback.answer(messages.get(result, "Не удалось обработать заказ."), show_alert=True)


@router.callback_query(F.data.startswith("order:cancel:"))
async def callback_cancel_order(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.rsplit(":", 1)[1])
    result, order = await db.cancel_order(order_id, callback.from_user.id)

    if result == "cancelled" and order:
        await edit_admin_order_message(callback, order)
        try:
            await bot.send_message(
                order["user_id"],
                f"❌ <b>Заказ #{order['id']} отменён.</b>\n\n"
                "Оплата не была подтверждена. По вопросам обратитесь в поддержку Tripov Shop.",
            )
        except Exception:
            logger.exception("Не удалось уведомить покупателя об отмене #%s", order_id)
        await callback.answer("Заказ отменён, резерв возвращён.", show_alert=True)
        return

    messages = {
        "approved": "Этот заказ уже подтверждён.",
        "cancelled": "Этот заказ уже отменён.",
        "not_found": "Заказ не найден.",
        "error": "Ошибка базы данных.",
    }
    await callback.answer(messages.get(result, "Не удалось отменить заказ."), show_alert=True)


# ==========================================================
# АДМИН-ПАНЕЛЬ
# ==========================================================


@router.message(Command("help"))
async def command_help(message: Message) -> None:
    """Показывает покупателю краткую помощь, а владельцу — полную админ-шпаргалку."""
    await db.upsert_user(message)

    if message.from_user and is_owner(message.from_user.id):
        await message.answer(
            "<b>📖 Админ-справка Tripov Shop</b>\n\n"
            "<b>Основное управление</b>\n"
            "• <code>/admin</code> — открыть админ-панель.\n"
            "• <code>/help</code> — показать эту справку.\n"
            "• <code>/cancel</code> — отменить текущее действие.\n\n"
            "<b>Добавление аккаунтов</b>\n"
            "• <code>/new +7 | 100₽/100звезд | 1 шт</code>\n"
            "Создаёт новый товар. Если название уже существует, бот пополнит остаток и обновит цены.\n\n"
            "<b>Добавление Telegram Stars</b>\n"
            "• <code>/newstars 100 | 150₽ | подарком</code>\n"
            "Создаёт один пакет на 100 ⭐.\n"
            "• <code>/newstars 100 | 150₽ | 1000 | подарком</code>\n"
            "Один заказ = 100 ⭐, общий остаток = 1000 ⭐.\n"
            "Способ выдачи: <code>подарком</code> или <code>на аккаунт</code>.\n\n"
            "<b>Быстрое управление складом</b>\n"
            "• <code>/add код 20</code> — добавить 20 единиц к остатку.\n"
            "• <code>/set код 100</code> — установить остаток ровно 100.\n"
            "• <code>/price код 100₽/100звезд</code> — изменить цену.\n\n"
            "Код товара показывается в разделе «📦 Склад и товары». Для Stars количество в командах склада указывается в звёздах.\n\n"
            "<b>Кнопки админ-панели</b>\n"
            "• Склад — добавить, списать, установить количество, изменить цену или скрыть товар.\n"
            "• Статистика — заказы, выручка и популярные товары.\n"
            "• История — все заявки и их статусы.\n"
            "• Промокоды — скидка, срок действия и лимит использований.\n"
            "• Рассылка — сообщение всем пользователям.\n\n"
            "Заявка покупателя приходит вам в личные сообщения бота. После проверки нажмите «✅ Выдать» или «❌ Отменить»."
        )
        return

    await message.answer(
        "<b>ℹ️ Как оформить заказ</b>\n\n"
        "1. Выберите аккаунт или пакет Stars.\n"
        "2. Оплатите по реквизитам Т-Банка.\n"
        f"3. Укажите комментарий <b>{html.escape(PAYMENT_COMMENT)}</b>.\n"
        "4. Отправьте чек в бота и дождитесь ручного подтверждения.\n\n"
        f"Помощь: @{html.escape(SUPPORT_USERNAME.lstrip('@'))}"
    )


@router.message(Command("admin"))
async def command_admin(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return
    await state.clear()
    await message.answer("<b>⚙️ Админ-панель Tripov Shop</b>", reply_markup=admin_menu_keyboard())


@router.message(F.text == "⚙️ Админ-панель")
async def menu_admin(message: Message, state: FSMContext) -> None:
    await command_admin(message, state)


@router.callback_query(F.data == "admin:menu")
async def callback_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        await safe_edit_text(callback.message, "<b>⚙️ Админ-панель Tripov Shop</b>", reply_markup=admin_menu_keyboard())


async def render_stock(message: Message, edit: bool = True) -> None:
    products = await db.get_products(include_hidden=True)
    lines = ["<b>📦 Склад Tripov Shop</b>", ""]
    builder = InlineKeyboardBuilder()

    if not products:
        lines.append("Товаров пока нет.")
    else:
        for product in products:
            visible = "👁" if product["is_visible"] else "🙈"
            lines.extend(
                [
                    f"{visible} <b>{html.escape(product['name'])}</b> "
                    f"(<code>{html.escape(product['code'])}</code>)",
                    f"Категория: <code>{html.escape(product['category'])}</code> · "
                    f"Цена: {product_prices(product)}",
                ]
            )
            if product["category"] == "stars":
                lines.append(
                    f"Пакет: {stars(product_unit_amount(product))} · "
                    f"Выдача: {delivery_label(product['delivery_method'])}"
                )
            lines.extend(
                [
                    f"Всего: {stock_label(product, product['stock'])} · "
                    f"Резерв: {stock_label(product, product['reserved'])} · "
                    f"Доступно: <b>{stock_label(product, product['available'])}</b>",
                    "",
                ]
            )
            builder.button(
                text=f"⚙️ {product['name']} ({stock_label(product, product['available'])})",
                callback_data=f"stock:product:{product['id']}",
            )

    builder.button(text="➕ Добавить новый товар", callback_data="stock:new")
    builder.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    builder.adjust(1)
    output_text = "\n".join(lines)

    if edit:
        try:
            await safe_edit_text(message, output_text, reply_markup=builder.as_markup())
        except TelegramBadRequest:
            await message.answer(output_text, reply_markup=builder.as_markup())
    else:
        await message.answer(output_text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:stock")
async def callback_admin_stock(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        await render_stock(callback.message)


@router.callback_query(F.data.startswith("stock:product:"))
async def callback_stock_product(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    product_id = int(callback.data.rsplit(":", 1)[1])
    product = await db.get_product(product_id)
    if not product or not callback.message:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await safe_edit_text(
        callback.message,
        product_admin_text(product),
        reply_markup=product_manage_keyboard(product),
    )


@router.callback_query(F.data.startswith("stock:change:"))
async def callback_stock_change(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    _, _, mode, product_id_raw = callback.data.split(":")
    product_id = int(product_id_raw)
    product = await db.get_product(product_id)
    if not product or not callback.message:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    unit_word = "Stars" if product["category"] == "stars" else "единиц товара"
    prompts = {
        "add": f"Введите, сколько {unit_word} добавить:",
        "subtract": f"Введите, сколько {unit_word} списать:",
        "set": f"Введите точное количество {unit_word} на складе:",
    }
    await state.set_state(AdminStates.waiting_stock_value)
    await state.update_data(stock_mode=mode, stock_product_id=product_id)
    await callback.message.answer(
        f"<b>{html.escape(product['name'])}</b>\n{prompts[mode]}\n\nДля отмены: /cancel"
    )


@router.message(AdminStates.waiting_stock_value, F.text)
async def process_stock_value(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    try:
        value = int(message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое неотрицательное число.")
        return

    data = await state.get_data()
    success, info = await db.change_stock(
        int(data["stock_product_id"]),
        str(data["stock_mode"]),
        value,
    )
    await state.clear()
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("stock:price:"))
async def callback_stock_price(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    product_id = int(callback.data.rsplit(":", 1)[1])
    product = await db.get_product(product_id)
    if not product or not callback.message:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_price)
    await state.update_data(price_product_id=product_id)
    await callback.message.answer(
        f"Текущая цена <b>{html.escape(product['name'])}</b>: {product_prices(product)}.\n"
        "Введите цены в формате <code>100₽/100звезд</code>.\n"
        "Можно указать только рубли: <code>100</code>. Для отмены: /cancel"
    )


@router.message(AdminStates.waiting_price, F.text)
async def process_price(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    parsed = parse_prices(message.text)
    if parsed is None:
        await message.answer("Введите цену, например: <code>100₽/100звезд</code>.")
        return
    price_rub, price_stars = parsed
    data = await state.get_data()
    success, info = await db.set_price(int(data["price_product_id"]), price_rub, price_stars)
    await state.clear()
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("stock:toggle:"))
async def callback_stock_toggle(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    success, info = await db.toggle_product_visibility(product_id)
    await callback.answer(info, show_alert=True)
    product = await db.get_product(product_id)
    if success and product and callback.message:
        await safe_edit_text(
            callback.message,
            product_admin_text(product),
            reply_markup=product_manage_keyboard(product),
        )


@router.callback_query(F.data == "stock:new")
async def callback_new_product(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_new_product)
    if callback.message:
        await callback.message.answer(
            "<b>Быстрое добавление товара</b>\n\n"
            "<b>Аккаунт:</b>\n"
            "<code>+7 | 100₽/100звезд | 1 шт</code>\n\n"
            "<b>Пакет Stars:</b>\n"
            "<code>100 | 150₽ | подарком</code>\n"
            "Так добавится один пакет на 100 Stars.\n"
            "Для общего остатка: <code>100 | 150₽ | 1000 | подарком</code>.\n"
            "Первое число — Stars в одном заказе, 1000 — общий остаток.\n"
            "Позже можно указать способ <code>на аккаунт</code>.\n\n"
            "Прямая команда для Stars:\n"
            "<code>/newstars 100 | 150₽ | подарком</code>\n\n"
            "Расширенный обычный формат:\n"
            "<code>категория | код | название | 100₽/100звезд | количество</code>\n"
            "Для отмены: /cancel"
        )


@router.message(AdminStates.waiting_new_product, F.text)
async def process_new_product(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return

    stars_quick = parse_stars_product_line(message.text)
    if stars_quick:
        package_amount, price_rub, stock, delivery_method = stars_quick
        success, info = await db.quick_add_stars_product(
            package_amount, price_rub, stock, delivery_method
        )
        if success:
            await state.clear()
        await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())
        return

    quick = parse_quick_product_line(message.text)
    if quick:
        name, price_rub, price_stars, stock = quick
        success, info = await db.quick_add_product(name, price_rub, price_stars, stock)
        if success:
            await state.clear()
        await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())
        return

    parts = [part.strip() for part in message.text.split("|")]
    if len(parts) != 5:
        await message.answer("Используйте формат: <code>+7 | 100₽/100звезд | 1 шт</code>")
        return
    category, code, name, prices_raw, stock_raw = parts
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,32}", category):
        await message.answer("Категория: 2–32 символа, латиница, цифры, _ или -.")
        return
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,32}", code):
        await message.answer("Код товара: 2–32 символа, латиница, цифры, _ или -.")
        return
    if not name or len(name) > 100:
        await message.answer("Название должно содержать от 1 до 100 символов.")
        return
    parsed_prices = parse_prices(prices_raw)
    stock = parse_quantity(stock_raw)
    if parsed_prices is None or stock is None:
        await message.answer("Пример цен и количества: <code>100₽/100звезд | 1 шт</code>")
        return
    price_rub, price_stars = parsed_prices

    success, info = await db.add_product(category, code, name, price_rub, price_stars, stock)
    if success:
        await state.clear()
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


@router.message(Command("newstars"))
async def command_new_stars_product(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) != 2:
        await message.answer(
            "Использование:\n<code>/newstars 100 | 150₽ | подарком</code>"
        )
        return
    parsed = parse_stars_product_line(payload[1])
    if not parsed:
        await message.answer(
            "Неверный формат. Пример:\n"
            "<code>/newstars 100 | 150₽ | подарком</code>\n\n"
            "Короткий формат создаёт один пакет. Для общего остатка используйте: "
            "<code>/newstars 100 | 150₽ | 1000 | подарком</code>.\n"
            "Способ: <code>подарком</code> или <code>на аккаунт</code>."
        )
        return
    package_amount, price_rub, stock, delivery_method = parsed
    success, info = await db.quick_add_stars_product(
        package_amount, price_rub, stock, delivery_method
    )
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


@router.message(Command("new"))
async def command_new_product(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) == 1:
        await state.set_state(AdminStates.waiting_new_product)
        await message.answer(
            "Отправьте аккаунт так:\n<code>+7 | 100₽/100звезд | 1 шт</code>\n\nДля Stars: <code>/newstars 100 | 150₽ | подарком</code>"
        )
        return
    quick = parse_quick_product_line(payload[1])
    if not quick:
        await message.answer("Формат команды: <code>/new +7 | 100₽/100звезд | 1 шт</code>")
        return
    name, price_rub, price_stars, stock = quick
    success, info = await db.quick_add_product(name, price_rub, price_stars, stock)
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


# ==========================================================
# КОМАНДЫ СКЛАДА
# ==========================================================


async def resolve_product_for_command(message: Message, command_name: str) -> tuple[sqlite3.Row | None, int | None]:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer(f"Использование: <code>/{command_name} код число</code>")
        return None, None
    product = await db.get_product_by_code(parts[1])
    if not product:
        await message.answer("Товар с таким кодом не найден.")
        return None, None
    try:
        value = int(parts[2])
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("Число должно быть целым и неотрицательным.")
        return None, None
    return product, value


@router.message(Command("add"))
async def command_add_stock(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    product, value = await resolve_product_for_command(message, "add")
    if product is None or value is None:
        return
    success, info = await db.change_stock(product["id"], "add", value)
    await message.answer(("✅ " if success else "❌ ") + html.escape(info))


@router.message(Command("set"))
async def command_set_stock(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    product, value = await resolve_product_for_command(message, "set")
    if product is None or value is None:
        return
    success, info = await db.change_stock(product["id"], "set", value)
    await message.answer(("✅ " if success else "❌ ") + html.escape(info))


@router.message(Command("price"))
async def command_set_price(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Использование: <code>/price код 100₽/100звезд</code>")
        return
    product = await db.get_product_by_code(parts[1])
    if not product:
        await message.answer("Товар с таким кодом не найден.")
        return
    parsed = parse_prices(parts[2])
    if parsed is None:
        await message.answer("Цена должна быть в формате <code>100₽/100звезд</code>.")
        return
    price_rub, price_stars = parsed
    success, info = await db.set_price(product["id"], price_rub, price_stars)
    await message.answer(("✅ " if success else "❌ ") + html.escape(info))


# ==========================================================
# СТАТИСТИКА И ИСТОРИЯ
# ==========================================================


@router.callback_query(F.data == "admin:stats")
async def callback_admin_stats(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    stats = await db.get_statistics()
    lines = [
        "<b>📊 Статистика</b>",
        "",
        f"⏳ Заявок на проверке: <b>{stats['pending_count']}</b>",
        f"🛒 Заказов сегодня: <b>{stats['today_count']}</b>",
        f"💰 Выручка сегодня: <b>{money(stats['today_revenue_rub'])}</b> / <b>{stars(stats['today_revenue_stars'])}</b>",
        f"📦 Заказов всего: <b>{stats['total_count']}</b>",
        f"💵 Выручка за всё время: <b>{money(stats['total_revenue_rub'])}</b> / <b>{stars(stats['total_revenue_stars'])}</b>",
        "",
        "<b>Самые продаваемые товары:</b>",
    ]
    if stats["top"]:
        for index, row in enumerate(stats["top"], start=1):
            lines.append(
                f"{index}. {html.escape(row['product_name'])} — {row['sold']} шт. "
                f"({money(row['revenue_rub'])} / {stars(row['revenue_stars'])})"
            )
    else:
        lines.append("Пока нет подтверждённых заказов.")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats")],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")],
        ]
    )
    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin:history:"))
async def callback_admin_history(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    page_size = 10
    orders, total = await db.get_order_history(page, page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page >= total_pages:
        page = total_pages - 1
        orders, total = await db.get_order_history(page, page_size)

    lines = [f"<b>📜 История заказов — страница {page + 1}/{total_pages}</b>", ""]
    if not orders:
        lines.append("Заказов пока нет.")
    else:
        for order in orders:
            lines.append(
                f"<b>#{order['id']}</b> · {ORDER_STATUS_LABELS.get(order['status'], order['status'])}\n"
                f"👤 {safe_username(order['username'], order['user_id'])}\n"
                f"📦 {html.escape(order['product_name'])} · {order_amount(order)}\n"
                f"🕒 {format_datetime(order['created_at'])}\n"
            )

    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="⬅️ Назад", callback_data=f"admin:history:{page - 1}")
    if page + 1 < total_pages:
        builder.button(text="Вперёд ➡️", callback_data=f"admin:history:{page + 1}")
    builder.button(text="🔄 Обновить", callback_data=f"admin:history:{page}")
    builder.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    builder.adjust(2, 1, 1)

    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=builder.as_markup())


# ==========================================================
# ПРОМОКОДЫ
# ==========================================================


async def render_promos(message: Message) -> None:
    promos = await db.get_promos()
    lines = ["<b>🎟 Промокоды</b>", ""]
    builder = InlineKeyboardBuilder()

    if not promos:
        lines.append("Промокодов пока нет.")
    else:
        for promo in promos:
            status = "✅" if promo["is_active"] else "❌"
            limit = promo["max_uses"] if promo["max_uses"] is not None else "∞"
            expires = promo["expires_on"] or "без срока"
            lines.append(
                f"{status} <code>{html.escape(promo['code'])}</code> — "
                f"{promo['discount_percent']}%\n"
                f"Использовано: {promo['uses']}/{limit} · До: {expires}\n"
            )
            builder.button(
                text=f"{'Отключить' if promo['is_active'] else 'Включить'} {promo['code']}",
                callback_data=f"promo_admin:toggle:{promo['id']}",
            )

    builder.button(text="➕ Создать промокод", callback_data="promo_admin:new")
    builder.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    builder.adjust(1)
    try:
        await safe_edit_text(message, "\n".join(lines), reply_markup=builder.as_markup())
    except TelegramBadRequest:
        await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:promos")
async def callback_admin_promos(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        await render_promos(callback.message)


@router.callback_query(F.data == "promo_admin:new")
async def callback_new_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_new_promo)
    if callback.message:
        await callback.message.answer(
            "<b>Создание промокода</b>\n\n"
            "Отправьте одной строкой:\n"
            "<code>КОД | СКИДКА | ДАТА | ЛИМИТ</code>\n\n"
            "Пример:\n"
            "<code>SUMMER5 | 5 | 2026-08-31 | 100</code>\n\n"
            "Без срока и без лимита:\n"
            "<code>WELCOME | 10 | - | -</code>\n\n"
            "Для отмены: /cancel"
        )


@router.message(AdminStates.waiting_new_promo, F.text)
async def process_new_promo(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    parts = [part.strip() for part in message.text.split("|")]
    if len(parts) != 4:
        await message.answer("Нужно 4 поля: код | скидка | дата | лимит")
        return
    code, discount_raw, expires_raw, max_uses_raw = parts
    try:
        discount = int(discount_raw)
    except ValueError:
        await message.answer("Скидка должна быть целым числом.")
        return
    expires = None if expires_raw == "-" else expires_raw
    if max_uses_raw == "-":
        max_uses = None
    else:
        try:
            max_uses = int(max_uses_raw)
        except ValueError:
            await message.answer("Лимит должен быть целым числом или символом -.")
            return

    success, info = await db.create_promo(code, discount, expires, max_uses)
    if success:
        await state.clear()
    await message.answer(("✅ " if success else "❌ ") + html.escape(info), reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("promo_admin:toggle:"))
async def callback_toggle_promo(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    promo_id = int(callback.data.rsplit(":", 1)[1])
    success, info = await db.toggle_promo(promo_id)
    await callback.answer(info, show_alert=True)
    if success and callback.message:
        await render_promos(callback.message)


# ==========================================================
# РАССЫЛКА
# ==========================================================


@router.callback_query(F.data == "admin:broadcast")
async def callback_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_broadcast)
    if callback.message:
        await callback.message.answer(
            "📣 Отправьте сообщение для рассылки.\n\n"
            "Можно отправить текст, фото, видео, документ или другое сообщение. "
            "Бот скопирует его всем пользователям.\n\nДля отмены: /cancel"
        )


@router.message(AdminStates.waiting_broadcast)
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id,
    )
    await state.set_state(AdminStates.waiting_broadcast_confirmation)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Начать рассылку", callback_data="broadcast:confirm")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="broadcast:cancel")],
        ]
    )
    await message.answer("Отправить это сообщение всем пользователям?", reply_markup=keyboard)


@router.callback_query(F.data == "broadcast:cancel")
async def callback_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Рассылка отменена.")
    await state.clear()
    if callback.message:
        await safe_edit_text(callback.message, "Рассылка отменена.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "broadcast:confirm")
async def callback_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    current_state = await state.get_state()
    if current_state != AdminStates.waiting_broadcast_confirmation.state:
        await callback.answer("Данные рассылки устарели.", show_alert=True)
        return

    await callback.answer()
    data = await state.get_data()
    source_chat_id = int(data["broadcast_chat_id"])
    source_message_id = int(data["broadcast_message_id"])
    await state.clear()

    if callback.message:
        await safe_edit_text(callback.message, "📣 Рассылка запущена…")

    users = await db.get_all_user_ids()
    sent = 0
    failed = 0

    for user_id in users:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent += 1
            await asyncio.sleep(0.04)
        except TelegramRetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + 0.5)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
            await db.mark_user_blocked(user_id, True)
        except Exception:
            failed += 1
            logger.exception("Ошибка рассылки пользователю %s", user_id)

    result_text = (
        "<b>📣 Рассылка завершена</b>\n\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>\n"
        f"👥 Всего получателей: <b>{len(users)}</b>"
    )
    if callback.message:
        await callback.message.answer(result_text, reply_markup=admin_menu_keyboard())


# ==========================================================
# НЕИЗВЕСТНЫЕ СООБЩЕНИЯ
# ==========================================================


@router.message()
async def fallback_message(message: Message) -> None:
    await db.upsert_user(message)
    await message.answer(
        "Используйте кнопки главного меню.",
        reply_markup=main_menu_keyboard(bool(message.from_user and is_owner(message.from_user.id))),
    )


# ==========================================================
# ЗАПУСК
# ==========================================================


async def set_commands() -> None:
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь и админ-справка"),
        BotCommand(command="myorders", description="Мои заказы"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
        BotCommand(command="admin", description="Админ-панель (владелец)"),
        BotCommand(command="new", description="Добавить аккаунт (владелец)"),
        BotCommand(command="newstars", description="Добавить пакет Stars (владелец)"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    global bot

    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_БОТА":
        raise RuntimeError("Заполните BOT_TOKEN в блоке настроек.")
    if OWNER_ID <= 0:
        logger.warning("OWNER_ID не настроен.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    await db.initialize()
    await set_commands()
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Бот запущен. База данных: %s", DB_PATH)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        db.conn.close()


if __name__ == "__main__":
    asyncio.run(main())
