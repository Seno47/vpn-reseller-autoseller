from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from reseller_autoseller.db import Database
from reseller_autoseller.digiseller_client import (
    DigisellerApiError,
    sale_event_from_unique_code,
)
from reseller_autoseller.marketplaces import SUPPORTED_MARKETPLACES
from reseller_autoseller.runtime_config import RuntimeConfig, SETTING_BY_KEY, SETTING_DEFINITIONS
from reseller_autoseller.services import (
    ACTION_LABELS,
    DEFAULT_ACTION_TEMPLATES,
    DELIVERY_TEMPLATE_VARIABLES,
    DeliveryService,
    TEMPLATE_CATEGORIES,
    TEMPLATE_GROUPS,
    delivery_template_key,
)
from reseller_autoseller.statistics import STATS_PERIODS, build_sales_statistics
from reseller_autoseller.xyra_client import XyraNetClient


UNIQUE_CODE_RE = re.compile(r"^[A-Za-z0-9]{16}$")


class MappingState(StatesGroup):
    waiting_payload = State()
    waiting_template = State()


class UserState(StatesGroup):
    waiting_payload = State()


class SettingState(StatesGroup):
    waiting_value = State()


def main_menu(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
            InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
        ],
        [
            InlineKeyboardButton(text="🧾 Товары", callback_data="menu:products"),
            InlineKeyboardButton(text="📦 Продажи", callback_data="menu:sales"),
        ],
        [
            InlineKeyboardButton(text="📈 Статистика", callback_data="stats:period:30d"),
        ],
        [
            InlineKeyboardButton(text="🧭 Тарифы", callback_data="menu:tariffs"),
            InlineKeyboardButton(text="➕ Маппинг", callback_data="map:add"),
        ],
        [
            InlineKeyboardButton(text="📝 Шаблоны", callback_data="menu:templates"),
            InlineKeyboardButton(text="👥 Доступы", callback_data="menu:users"),
        ],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")]])


def user_id(message: Message | CallbackQuery) -> int | None:
    source = message.from_user
    return source.id if source else None


def format_product_key(row: dict) -> str:
    key = f"{row['marketplace']}:{row['external_product_id']}"
    if row.get("external_variant_id"):
        key += f":{row['external_variant_id']}"
    return key


def format_tariff_label(row: dict[str, Any]) -> str:
    code = str(row.get("code") or "")
    family = str(row.get("family_code") or code.split("_")[0] or "tariff").upper()
    period = f"{row.get('duration_days')} дн" if row.get("duration_days") else str(row.get("period_key") or "")
    ip = f"{row.get('ip_limit')} IP" if row.get("ip_limit") else ""
    traffic = "безлимит" if row.get("is_unlimited_traffic") else ""
    price = f"{row.get('api_price_rub')} ₽" if row.get("api_price_rub") else ""
    parts = [part for part in (family, period, ip, traffic, price) if part]
    return " • ".join(parts) + f" ({code})"


SETTING_GROUPS = [
    ("🔌 XyraNet", ["xyranet_api_base_url", "xyranet_api_key", "xyranet_timeout_seconds"]),
    ("🛒 Маркетплейсы", ["marketplace_webhook_secret", "digiseller_seller_id", "digiseller_api_key", "ggsel_seller_id", "ggsel_api_key"]),
    ("🤖 Telegram", ["enable_telegram", "telegram_bot_token"]),
    ("🛡 Веб-панель", ["app_base_url", "admin_username", "admin_password"]),
]

SETTING_ICONS = {
    "app_base_url": "🌐",
    "xyranet_api_base_url": "🔗",
    "xyranet_api_key": "🔑",
    "xyranet_timeout_seconds": "⏱",
    "marketplace_webhook_secret": "🪝",
    "digiseller_seller_id": "🏪",
    "digiseller_api_key": "🔑",
    "ggsel_seller_id": "🛍",
    "ggsel_api_key": "🔑",
    "enable_telegram": "🤖",
    "telegram_bot_token": "🔐",
    "admin_username": "👤",
    "admin_password": "🔒",
}


def setting_button_label(item: dict[str, Any]) -> str:
    icon = SETTING_ICONS.get(str(item["key"]), "⚙️")
    restart = " 🔄" if item["restart_required"] else ""
    return f"{icon} {item['label']}{restart}"


def setting_value_label(runtime: RuntimeConfig, item: dict[str, Any]) -> str:
    if item["sensitive"]:
        return "задано" if item["configured"] else "пусто"
    if item["kind"] == "boolean":
        return "вкл" if runtime.get_bool(item["key"]) else "выкл"
    return str(item["value"])


def parse_mapping_payload(text: str) -> dict[str, str]:
    body = text.strip()
    if body.startswith("/map"):
        parts = body.split(maxsplit=1)
        body = parts[1] if len(parts) == 2 else ""

    if "|" in body:
        parts = [part.strip() for part in body.split("|")]
        if len(parts) == 4:
            marketplace, product_id, tariff_code, title = parts
            variant_id = ""
        elif len(parts) >= 5:
            marketplace, product_id, variant_id, tariff_code = parts[:4]
            title = " | ".join(parts[4:]).strip()
        else:
            raise ValueError("Недостаточно полей")
    else:
        parts = body.split(maxsplit=3)
        if len(parts) == 3:
            marketplace, product_id, tariff_code = parts
            variant_id = ""
            title = product_id
        elif len(parts) >= 4:
            marketplace, product_id, tariff_code, title = parts
            variant_id = ""
        else:
            raise ValueError("Недостаточно полей")

    if variant_id == "-":
        variant_id = ""
    if marketplace.strip().lower() not in SUPPORTED_MARKETPLACES:
        raise ValueError("Поддерживаются только plati, digiseller и ggsel")
    return {
        "marketplace": marketplace.strip().lower(),
        "external_product_id": product_id.strip(),
        "external_variant_id": variant_id.strip(),
        "tariff_code": tariff_code.strip().lower(),
        "title": title.strip() or product_id.strip(),
        "enabled": True,
    }


def template_help_text() -> str:
    lines = ["Доступные переменные:"]
    for key, label in DELIVERY_TEMPLATE_VARIABLES.items():
        lines.append(f"<code>{{{key}}}</code> — {escape(label)}")
    lines.append("\nСообщение <code>-</code>, <code>default</code> или <code>стандарт</code> сбросит шаблон.")
    return "\n".join(lines)


def money_text(value: dict[str, Any] | None, currency: str = "₽") -> str:
    text = str((value or {}).get("text") or "0")
    return f"{text} {currency}" if currency else text


def revenue_text(row: dict[str, Any]) -> str:
    items = row.get("revenue") or []
    if not items:
        return "0 ₽"
    return ", ".join(f"{item.get('text', '0')} {item.get('currency', 'RUB')}" for item in items)


def action_label(action: str) -> str:
    return {
        "create": "Покупка",
        "renew": "Продление",
        "reissue": "Перевыпуск",
        "traffic": "LTE-трафик",
        "ip_limit": "IP-лимит",
    }.get(action, action)


def statistics_keyboard(active_period: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in STATS_PERIODS.items():
        prefix = "✅ " if key == active_period else ""
        row.append(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"stats:period:{key}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def statistics_text(data: dict[str, Any]) -> str:
    totals = data["totals"]
    lines = [
        f"📈 <b>Статистика: {escape(str(data['period']['label']))}</b>",
        f"🧾 Продаж: <b>{totals['sales_count']}</b> ({totals['delivered_count']} выдано, {totals['pending_count']} ждёт)",
        f"💵 Сумма продаж: <b>{escape(revenue_text(totals))}</b>",
        f"💸 Расход XyraNet: <b>{escape(money_text(totals['expense_rub']))}</b>",
        f"📊 Прибыль: <b>{escape(money_text(totals['profit_rub']))}</b>",
    ]
    if totals.get("margin_percent") is not None:
        lines[-1] += f" · маржа {totals['margin_percent']}%"
    lines.append(f"🧮 Средний чек: <b>{escape(money_text(totals['avg_order_rub']))}</b>")

    if data.get("marketplaces"):
        lines.append("\n<b>Площадки</b>")
        for item in data["marketplaces"][:4]:
            lines.append(f"• {escape(str(item['label']))}: {item['sales_count']} · {escape(revenue_text(item))}")

    if data.get("actions"):
        lines.append("\n<b>Действия</b>")
        for item in data["actions"][:5]:
            lines.append(f"• {escape(action_label(str(item['key'])))}: {item['sales_count']} · {escape(revenue_text(item))}")

    if data.get("tariffs"):
        lines.append("\n<b>Топ тарифов</b>")
        for item in data["tariffs"][:5]:
            lines.append(f"• <code>{escape(str(item['label']))}</code>: {item['sales_count']} · {escape(revenue_text(item))}")
    return "\n".join(lines)


def parse_user_payload(text: str) -> tuple[int, str]:
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        raise ValueError("Первым должен быть Telegram ID")
    return int(parts[0]), parts[1].strip() if len(parts) == 2 else ""


async def answer_or_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


def build_dispatcher(
    *,
    db: Database,
    xyranet: XyraNetClient,
    digiseller: Any,
    runtime: RuntimeConfig,
    restart_bot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> Dispatcher:
    router = Router()
    delivery_service = DeliveryService(db=db, xyranet=xyranet, free_reissue_enabled=lambda: runtime.get_bool("free_reissue_enabled"))

    def is_admin(telegram_id: int | None) -> bool:
        return bool(telegram_id and runtime.is_bot_admin(telegram_id))

    def is_owner(telegram_id: int | None) -> bool:
        return bool(telegram_id and runtime.is_env_admin(telegram_id))

    async def deny_message(message: Message) -> None:
        await message.answer("🔒 Доступ только для администраторов.")

    async def deny_callback(callback: CallbackQuery) -> None:
        await callback.answer("🔒 Нет доступа", show_alert=True)

    async def send_home(message: Message) -> None:
        uid = user_id(message)
        if not is_admin(uid):
            await message.answer(
                "🔑 Пришлите сюда 16-символьный код покупки Plati.Market/Digiseller. "
                "Я проверю оплату и выдам VPN-доступ."
            )
            return
        await message.answer(
            "✨ <b>XyraNet Reseller Autoseller</b>\nВыберите действие:",
            reply_markup=main_menu(is_owner(uid)),
        )

    @router.message(Command("start", "menu"))
    async def start(message: Message) -> None:
        await send_home(message)

    @router.callback_query(F.data == "menu:home")
    async def home(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await answer_or_edit(
            callback,
            "✨ <b>XyraNet Reseller Autoseller</b>\nВыберите действие:",
            main_menu(is_owner(uid)),
        )

    @router.callback_query(F.data == "menu:status")
    async def status(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        products = db.list_products()
        sales = db.list_sales(limit=5)
        await answer_or_edit(
            callback,
            "📊 <b>Статус</b>\n"
            f"🧾 Маппингов: <b>{len(products)}</b>\n"
            f"📦 Последних продаж в базе: <b>{len(sales)}</b>\n"
            f"👥 Telegram-админов: <b>{len(runtime.bot_admin_ids())}</b>",
            back_menu(),
        )

    @router.callback_query(F.data == "menu:balance")
    async def balance(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        try:
            data = await xyranet.summary()
            text = (
                "💰 <b>Wholesale</b>\n"
                f"Баланс: <b>{escape(str(data.get('balance')))} {escape(str(data.get('currency', 'RUB')))}</b>\n"
                f"Оборот API: <b>{escape(str(data.get('api_spent_total')))}</b>\n"
                f"Покупок: <b>{escape(str(data.get('api_purchase_count')))}</b>"
            )
        except Exception as exc:
            text = f"⚠️ Не смог получить баланс: <code>{escape(str(exc))}</code>"
        await answer_or_edit(callback, text, back_menu())

    @router.callback_query(F.data == "menu:tariffs")
    async def tariffs(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        try:
            rows = await xyranet.tariffs()
            lines = ["🧭 <b>Доступные тарифы</b>"]
            for item in rows[:30]:
                lines.append(f"• {escape(format_tariff_label(item))}")
            text = "\n".join(lines)
        except Exception as exc:
            text = f"⚠️ Не смог получить тарифы: <code>{escape(str(exc))}</code>"
        await answer_or_edit(callback, text, back_menu())

    @router.callback_query(F.data == "menu:products")
    async def products(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = db.list_products()
        if not rows:
            await answer_or_edit(callback, "🧾 Маппингов пока нет.", back_menu())
            return
        lines = ["🧾 <b>Товары</b>"]
        buttons = []
        for item in rows[:20]:
            state = "✅ вкл" if int(item["enabled"]) else "⏸ выкл"
            lines.append(
                f"#{item['id']} {escape(format_product_key(item))} → "
                f"<code>{escape(item['tariff_code'])}</code> ({state})"
            )
            next_state = "0" if int(item["enabled"]) else "1"
            buttons.append([InlineKeyboardButton(text=f"#{item['id']} {state}", callback_data=f"product:toggle:{item['id']}:{next_state}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("product:toggle:"))
    async def toggle_product(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        _, _, product_id, enabled = (callback.data or "").split(":")
        db.set_product_enabled(int(product_id), enabled == "1")
        await callback.answer("✅ Готово")
        await products(callback)

    @router.callback_query(F.data == "menu:templates")
    async def templates(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        buttons = [[InlineKeyboardButton(text=group["label"], callback_data=f"template:group:{group['key']}")] for group in TEMPLATE_GROUPS]
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "📝 <b>Шаблоны</b>\nВыберите вид действия.", InlineKeyboardMarkup(inline_keyboard=buttons))
        return
        lines = ["📝 <b>Шаблоны по виду действия</b>"]
        buttons = []
        current_category = ""
        for action, label in ACTION_LABELS.items():
            category = TEMPLATE_CATEGORIES.get(action, "Прочее")
            if category != current_category:
                current_category = category
                lines.append(f"\n<b>{escape(category)}</b>")
            state = "свой" if (db.get_setting(delivery_template_key(action)) or "").strip() else "стандарт"
            lines.append(f"• {escape(label)} — {state}")
            buttons.append([InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"template:edit:{action}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("template:group:"))
    async def template_group(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        group_key = (callback.data or "").split(":")[-1]
        group = next((item for item in TEMPLATE_GROUPS if item["key"] == group_key), None)
        if not group:
            await callback.answer("Вид действия не найден", show_alert=True)
            return
        lines = [f"📝 <b>{escape(group['label'])}</b>"]
        buttons = []
        command_action = str(group.get("command_action") or "")
        if command_action:
            service = DeliveryService(db=db, xyranet=xyranet)
            lines.append(f"Команда: <code>{escape(service.expected_command(command_action))}</code>")
        for stage in group["stages"]:
            key = str(stage["key"])
            state = "свой" if (db.get_setting(delivery_template_key(key)) or "").strip() else "стандарт"
            lines.append(f"• {escape(stage['label'])} — {state}")
            buttons.append([InlineKeyboardButton(text=f"✏️ {stage['label']}", callback_data=f"template:edit:{key}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:templates")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("template:edit:"))
    async def edit_template(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        action = (callback.data or "").split(":")[-1]
        if action not in DEFAULT_ACTION_TEMPLATES:
            await callback.answer("Вид действия не найден", show_alert=True)
            return
        await state.set_state(MappingState.waiting_template)
        await state.update_data(action=action)
        current = (db.get_setting(delivery_template_key(action)) or "").strip() or DEFAULT_ACTION_TEMPLATES[action]
        await answer_or_edit(
            callback,
            f"✏️ <b>Шаблон: {escape(ACTION_LABELS[action])}</b>\n\n"
            f"{template_help_text()}\n\n"
            "Текущий шаблон:\n"
            f"<pre>{escape(current)}</pre>",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="♻️ Сбросить на стандартный", callback_data=f"template:reset:{action}")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:templates")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("template:reset:"))
    async def reset_template(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        action = (callback.data or "").split(":")[-1]
        if action not in DEFAULT_ACTION_TEMPLATES:
            await callback.answer("Вид действия не найден", show_alert=True)
            return
        db.set_setting(delivery_template_key(action), "")
        await callback.answer("✅ Шаблон сброшен")
        await templates(callback)

    @router.callback_query(F.data == "menu:sales")
    async def sales(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = db.list_sales(limit=10)
        if not rows:
            await answer_or_edit(callback, "📦 Продаж пока нет.", back_menu())
            return
        lines = ["📦 <b>Последние продажи</b>"]
        for item in rows:
            mark = "✅" if item.get("xyranet_order_id") else "⏳"
            key = f"{item['marketplace']}:{item['external_product_id']}"
            if item.get("external_variant_id"):
                key += f":{item['external_variant_id']}"
            lines.append(f"{mark} {escape(item['external_order_id'])} — {escape(key)}")
        await answer_or_edit(callback, "\n".join(lines), back_menu())

    @router.callback_query(F.data.startswith("stats:period:"))
    async def statistics(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        period = (callback.data or "").split(":")[-1]
        data = build_sales_statistics(db.list_sales_for_statistics(), period=period)
        await answer_or_edit(callback, statistics_text(data), statistics_keyboard(str(data["period"]["key"])))

    @router.callback_query(F.data == "map:add")
    async def map_add(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await state.set_state(MappingState.waiting_payload)
        await answer_or_edit(
            callback,
            "➕ <b>Новый маппинг</b>\n"
            "Отправьте строку:\n"
            "<code>plati | 123456 | lite_monthly | Lite 1 month</code>\n\n"
            "Для варианта/кнопки GGsel:\n"
            "<code>ggsel | offer-9 | button-lite | lite_monthly | Lite 1 month</code>",
            back_menu(),
        )

    @router.message(Command("map"))
    async def map_command(message: Message) -> None:
        uid = user_id(message)
        if not is_admin(uid):
            await deny_message(message)
            return
        await save_mapping_from_text(message, message.text or "")

    @router.message(MappingState.waiting_payload)
    async def map_payload(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        await save_mapping_from_text(message, message.text or "")
        await state.clear()

    @router.message(MappingState.waiting_template)
    async def template_payload(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        data = await state.get_data()
        action = str(data.get("action") or "")
        if action not in ACTION_LABELS:
            await state.clear()
            await message.answer("⚠️ Вид действия не найден.", reply_markup=main_menu(is_owner(uid)))
            return
        text = message.text or ""
        template = "" if text.strip().lower() in {"-", "default", "стандарт"} else text.strip()
        db.set_setting(delivery_template_key(action), template)
        await state.clear()
        await message.answer(
            "✅ Шаблон сохранён\n"
            f"{escape(ACTION_LABELS[action])}\n"
            f"{'Используется свой шаблон.' if template else 'Используется стандартный шаблон.'}",
            reply_markup=main_menu(is_owner(uid)),
        )

    async def save_mapping_from_text(message: Message, text: str) -> None:
        try:
            product = db.upsert_product(parse_mapping_payload(text))
        except ValueError as exc:
            await message.answer(f"⚠️ Не разобрал маппинг: {escape(str(exc))}")
            return
        await message.answer(
            "✅ Маппинг сохранён\n"
            f"{escape(format_product_key(product))}\n"
            f"🧭 Тариф: <code>{escape(product['tariff_code'])}</code>",
            reply_markup=main_menu(is_owner(user_id(message))),
        )

    @router.callback_query(F.data == "menu:users")
    async def users(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = runtime.list_bot_users()
        lines = ["👥 <b>Доступ к боту</b>"]
        buttons = []
        owner = is_owner(uid)
        for row in rows:
            locked = " 🔒 env" if row["locked"] else ""
            state = "✅ вкл" if row["enabled"] else "⏸ выкл"
            label = f" — {escape(row['label'])}" if row.get("label") else ""
            lines.append(f"{row['telegram_id']}{label} ({state}{locked})")
            if owner and not row["locked"]:
                buttons.append(
                    [InlineKeyboardButton(text=f"🗑 Убрать {row['telegram_id']}", callback_data=f"user:delete:{row['telegram_id']}")]
                )
        if owner:
            buttons.append([InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="user:add")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "user:add")
    async def user_add(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        await state.set_state(UserState.waiting_payload)
        await answer_or_edit(callback, "➕ Отправьте Telegram ID и подпись, например:\n<code>123456789 Иван</code>", back_menu())

    @router.message(UserState.waiting_payload)
    async def user_payload(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        if not is_owner(uid):
            await state.clear()
            await deny_message(message)
            return
        try:
            telegram_id, label = parse_user_payload(message.text or "")
        except ValueError as exc:
            await message.answer(f"⚠️ Не разобрал пользователя: {escape(str(exc))}")
            return
        if runtime.is_env_admin(telegram_id):
            await message.answer("🔒 Этот пользователь уже указан в env и не удаляется.")
        else:
            db.upsert_bot_user(telegram_id, label, added_by=uid)
            await message.answer("✅ Пользователь добавлен.", reply_markup=main_menu(True))
        await state.clear()

    @router.callback_query(F.data.startswith("user:delete:"))
    async def user_delete(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        telegram_id = int((callback.data or "").split(":")[-1])
        if runtime.is_env_admin(telegram_id):
            await callback.answer("🔒 ENV-админа нельзя удалить", show_alert=True)
            return
        db.delete_bot_user(telegram_id)
        await callback.answer("🗑 Удалён")
        await users(callback)

    @router.callback_query(F.data == "menu:settings")
    async def settings_menu(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        lines = ["⚙️ <b>Настройки</b>"]
        buttons = []
        settings_by_key = {item["key"]: item for item in runtime.setting_payload()}
        for group_title, keys in SETTING_GROUPS:
            lines.append(f"\n<b>{escape(group_title)}</b>")
            group_buttons: list[InlineKeyboardButton] = []
            for key in keys:
                item = settings_by_key.get(key)
                if not item:
                    continue
                value = setting_value_label(runtime, item)
                lines.append(f"{escape(setting_button_label(item))}: <code>{escape(value)}</code>")
                group_buttons.append(InlineKeyboardButton(text=setting_button_label(item), callback_data=f"setting:edit:{item['key']}"))
                if len(group_buttons) == 2:
                    buttons.append(group_buttons)
                    group_buttons = []
            if group_buttons:
                buttons.append(group_buttons)
        if restart_bot:
            buttons.append([InlineKeyboardButton(text="🔄 Перезапустить Telegram-бота", callback_data="bot:restart")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))
        return
        for item in runtime.setting_payload():
            value = "задано" if item["sensitive"] and item["configured"] else item["value"]
            if item["kind"] == "boolean":
                value = "✅ вкл" if runtime.get_bool(item["key"]) else "⏸ выкл"
            restart = " 🔄" if item["restart_required"] else ""
            lines.append(f"{escape(item['label'])}: <code>{escape(str(value))}</code>{restart}")
            buttons.append([InlineKeyboardButton(text=item["label"], callback_data=f"setting:edit:{item['key']}")])
        if restart_bot:
            buttons.append([InlineKeyboardButton(text="🔄 Перезапустить Telegram-бота", callback_data="bot:restart")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "bot:restart")
    async def bot_restart(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        if not restart_bot:
            await callback.answer("⚠️ Рестарт недоступен", show_alert=True)
            return
        await callback.answer("🔄 Перезапускаю Telegram-бота")
        if callback.message:
            await callback.message.edit_text("🔄 Telegram-бот перезапускается.")
        asyncio.create_task(restart_bot())

    @router.callback_query(F.data.startswith("setting:edit:"))
    async def setting_edit(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        key = (callback.data or "").removeprefix("setting:edit:")
        definition = SETTING_BY_KEY[key]
        if definition.kind == "boolean":
            runtime.set_value(key, not runtime.get_bool(key))
            await callback.answer("✅ Переключено")
            await settings_menu(callback)
            return
        await state.set_state(SettingState.waiting_value)
        await state.update_data(setting_key=key)
        await answer_or_edit(callback, f"✏️ Отправьте новое значение для <b>{escape(definition.label)}</b>.", back_menu())

    @router.message(SettingState.waiting_value)
    async def setting_value(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        if not is_owner(uid):
            await state.clear()
            await deny_message(message)
            return
        data = await state.get_data()
        key = str(data.get("setting_key") or "")
        try:
            runtime.set_value(key, message.text or "")
        except ValueError as exc:
            await message.answer(f"⚠️ Не сохранил настройку: {escape(str(exc))}")
            return
        definition = SETTING_BY_KEY[key]
        note = "\n🔄 Нажмите «Перезапустить Telegram-бота», чтобы применить настройку." if definition.restart_required else ""
        await message.answer(f"✅ Настройка сохранена.{note}", reply_markup=main_menu(True))
        await state.clear()

    @router.message(F.text)
    async def buyer_unique_code(message: Message) -> None:
        text = (message.text or "").strip().replace(" ", "").replace("-", "")
        if text.startswith("/"):
            return
        if not UNIQUE_CODE_RE.fullmatch(text):
            if not is_admin(user_id(message)):
                await message.answer("🔑 Пришлите 16-символьный код покупки без пробелов.")
            return
        await message.answer("🔎 Проверяю код покупки...")
        try:
            purchase = await digiseller.purchase_by_unique_code(text)
            event = sale_event_from_unique_code(purchase, text)
            if not event.external_order_id or not event.external_product_id:
                await message.answer("⚠️ Код найден, но Digiseller не вернул номер заказа или ID товара.")
                return
            existing = db.get_sale_with_delivery(event.marketplace, event.external_order_id)
            if not existing and purchase.get("inv") not in (None, ""):
                existing = db.get_sale_with_delivery(event.marketplace, str(purchase["inv"]))
            if existing and existing.get("delivery_id"):
                await message.answer(existing["delivery_text"])
                return
            result = await delivery_service.handle_sale(event)
            try:
                if result.get("status") == "delivered":
                    await digiseller.mark_unique_code_delivered(text)
            except DigisellerApiError:
                pass
            await message.answer(result["delivery_text"])
        except DigisellerApiError as exc:
            await message.answer(f"⚠️ Не получилось проверить код: <code>{escape(str(exc))}</code>")
        except ValueError as exc:
            await message.answer(f"⚠️ Не получилось выдать доступ: <code>{escape(str(exc))}</code>")
        except Exception as exc:
            await message.answer(f"⚠️ Ошибка выдачи: <code>{escape(str(exc))}</code>")

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    return dispatcher


async def run_bot(
    *,
    token: str,
    db: Database,
    xyranet: XyraNetClient,
    digiseller: Any,
    runtime: RuntimeConfig,
    restart_bot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher(db=db, xyranet=xyranet, digiseller=digiseller, runtime=runtime, restart_bot=restart_bot)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
