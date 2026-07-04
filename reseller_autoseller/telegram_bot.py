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
from reseller_autoseller.system_metrics import collect_system_metrics
from reseller_autoseller.xyra_client import XyraNetClient


UNIQUE_CODE_RE = re.compile(r"^[A-Za-z0-9]{16}$")


def tr(language: str, ru: str, en: str) -> str:
    return en if language == "en" else ru


def runtime_language(runtime: RuntimeConfig) -> str:
    return runtime.language()


class MappingState(StatesGroup):
    waiting_payload = State()
    waiting_template = State()


class UserState(StatesGroup):
    waiting_payload = State()


class SettingState(StatesGroup):
    waiting_value = State()


def main_menu(is_owner: bool, language: str = "ru") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=tr(language, "📊 Статус", "📊 Status"), callback_data="menu:status"),
            InlineKeyboardButton(text=tr(language, "🖥 Метрики", "🖥 Metrics"), callback_data="menu:system"),
        ],
        [
            InlineKeyboardButton(text=tr(language, "💰 Баланс", "💰 Balance"), callback_data="menu:balance"),
            InlineKeyboardButton(text=tr(language, "📈 Статистика", "📈 Statistics"), callback_data="stats:period:30d"),
        ],
        [
            InlineKeyboardButton(text=tr(language, "🧾 Товары", "🧾 Products"), callback_data="menu:products"),
            InlineKeyboardButton(text=tr(language, "📦 Продажи", "📦 Sales"), callback_data="menu:sales"),
        ],
        [
            InlineKeyboardButton(text=tr(language, "🧭 Тарифы", "🧭 Tariffs"), callback_data="menu:tariffs"),
            InlineKeyboardButton(text=tr(language, "➕ Маппинг", "➕ Mapping"), callback_data="map:add"),
        ],
        [
            InlineKeyboardButton(text=tr(language, "📝 Шаблоны", "📝 Templates"), callback_data="menu:templates"),
            InlineKeyboardButton(text=tr(language, "👥 Доступы", "👥 Access"), callback_data="menu:users"),
        ],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text=tr(language, "⚙️ Настройки", "⚙️ Settings"), callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu(language: str = "ru", callback_data: str = "menu:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data=callback_data)]]
    )


def user_id(message: Message | CallbackQuery) -> int | None:
    source = message.from_user
    return source.id if source else None


def format_product_key(row: dict) -> str:
    key = f"{row['marketplace']}:{row['external_product_id']}"
    if row.get("external_variant_id"):
        key += f":{row['external_variant_id']}"
    return key


def format_tariff_label(row: dict[str, Any], language: str = "ru") -> str:
    code = str(row.get("code") or "")
    family = str(row.get("family_code") or code.split("_")[0] or "tariff").upper()
    period = f"{row.get('duration_days')} {tr(language, 'дн', 'days')}" if row.get("duration_days") else str(row.get("period_key") or "")
    ip = f"{row.get('ip_limit')} IP" if row.get("ip_limit") else ""
    traffic = tr(language, "безлимит", "unlimited") if row.get("is_unlimited_traffic") else ""
    price = f"{row.get('api_price_rub')} ₽" if row.get("api_price_rub") else ""
    parts = [part for part in (family, period, ip, traffic, price) if part]
    return " • ".join(parts) + f" ({code})"


SETTING_GROUPS = [
    ({"ru": "🌐 Интерфейс", "en": "🌐 Interface"}, ["panel_language"]),
    ({"ru": "🔌 XyraNet", "en": "🔌 XyraNet"}, ["xyranet_api_base_url", "xyranet_api_key", "xyranet_timeout_seconds"]),
    ({"ru": "🛒 Маркетплейсы", "en": "🛒 Marketplaces"}, ["digiseller_seller_id", "digiseller_api_key", "ggsel_seller_id", "ggsel_api_key"]),
    ({"ru": "🤖 Telegram", "en": "🤖 Telegram"}, ["enable_telegram", "telegram_bot_token", "free_reissue_enabled"]),
    ({"ru": "🔔 Уведомления", "en": "🔔 Notifications"}, ["notify_new_purchases", "notify_chat_messages", "notify_errors", "notify_pending", "notify_daily_statistics"]),
    ({"ru": "🛡 Веб-панель", "en": "🛡 Web panel"}, ["app_base_url", "admin_username", "admin_password"]),
]

SETTING_ICONS = {
    "panel_language": "🌐",
    "app_base_url": "🌐",
    "xyranet_api_base_url": "🔗",
    "xyranet_api_key": "🔑",
    "xyranet_timeout_seconds": "⏱",
    "digiseller_seller_id": "🏪",
    "digiseller_api_key": "🔑",
    "ggsel_seller_id": "🛍",
    "ggsel_api_key": "🔑",
    "enable_telegram": "🤖",
    "telegram_bot_token": "🔐",
    "free_reissue_enabled": "♻️",
    "notify_new_purchases": "🛒",
    "notify_chat_messages": "💬",
    "notify_errors": "🚨",
    "notify_pending": "⏳",
    "notify_daily_statistics": "📈",
    "admin_username": "👤",
    "admin_password": "🔒",
}


def setting_button_label(runtime: RuntimeConfig, item: dict[str, Any]) -> str:
    icon = SETTING_ICONS.get(str(item["key"]), "⚙️")
    restart = " 🔄" if item["restart_required"] else ""
    if item["kind"] == "boolean":
        state_icon = "✅" if runtime.get_bool(str(item["key"])) else "❌"
        return f"{state_icon} {icon} {item['label']}{restart}"
    return f"{icon} {item['label']}{restart}"


def setting_value_label(runtime: RuntimeConfig, item: dict[str, Any]) -> str:
    language = runtime.language()
    if item["sensitive"]:
        if language == "en":
            return "set" if item["configured"] else "empty"
        return "задано" if item["configured"] else "пусто"
    if item["kind"] == "boolean":
        if language == "en":
            return "on" if runtime.get_bool(item["key"]) else "off"
        return "вкл" if runtime.get_bool(item["key"]) else "выкл"
    if item["kind"] == "select":
        for option in item.get("options") or []:
            if str(option.get("value")) == str(item.get("value")):
                return str(option.get("label"))
    return str(item["value"])


def setting_group_title(labels: dict[str, str], language: str) -> str:
    return labels.get(language) or labels.get("en") or next(iter(labels.values()))


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


def template_help_text(language: str = "ru") -> str:
    lines = [tr(language, "Доступные переменные:", "Available variables:")]
    for key, label in DELIVERY_TEMPLATE_VARIABLES.items():
        lines.append(f"<code>{{{key}}}</code> — {escape(label)}")
    lines.append(
        "\n"
        + tr(
            language,
            "Сообщение <code>-</code>, <code>default</code> или <code>стандарт</code> сбросит шаблон.",
            "Send <code>-</code>, <code>default</code> or <code>standard</code> to reset the template.",
        )
    )
    return "\n".join(lines)


TEMPLATE_LABELS_EN = {
    "create": "Purchase",
    "renew": "Renewal",
    "reissue": "Reissue",
    "traffic": "LTE traffic",
    "ip_limit": "IP limit",
    "command_help": "Chat commands",
    "ask_renew": "Ask for order_id: renewal",
    "ask_reissue": "Ask for order_id: reissue",
    "ask_traffic": "Ask for order_id: LTE traffic",
    "ask_ip_limit": "Ask for order_id: IP limit",
    "command_mismatch": "Command error",
    "operation_error": "Operation error",
    "free_reissue_help": "Free reissue hint",
    "free_reissue_disabled": "Free reissue disabled",
    "renew_command_mismatch": "Wrong command: renewal",
    "renew_error": "Renewal error",
    "reissue_command_mismatch": "Wrong command: reissue",
    "reissue_error": "Reissue error",
    "traffic_command_mismatch": "Wrong command: LTE traffic",
    "traffic_error": "LTE traffic error",
    "ip_limit_command_mismatch": "Wrong command: IP limit",
    "ip_limit_error": "IP limit error",
}

TEMPLATE_STAGE_LABELS_EN = {
    "create": "Order delivered",
    "free_reissue_help": "Command without order ID",
    "free_reissue_disabled": "Free reissue disabled",
    "ask_renew": "Order received, waiting for order_id",
    "renew": "order_id received, renewal completed",
    "renew_command_mismatch": "Command does not match the lot",
    "renew_error": "Operation error",
    "ask_reissue": "Paid order received, waiting for order_id",
    "reissue": "order_id received, reissue completed",
    "reissue_command_mismatch": "Command does not match the lot",
    "reissue_error": "Operation error",
    "ask_traffic": "Order received, waiting for order_id",
    "traffic": "order_id received, LTE traffic added",
    "traffic_command_mismatch": "Command does not match the lot",
    "traffic_error": "Operation error",
    "ask_ip_limit": "Order received, waiting for order_id",
    "ip_limit": "order_id received, IP limit increased",
    "ip_limit_command_mismatch": "Command does not match the lot",
    "ip_limit_error": "Operation error",
}


def template_label(key: str, fallback: str, language: str) -> str:
    if language == "en":
        return TEMPLATE_LABELS_EN.get(key) or TEMPLATE_STAGE_LABELS_EN.get(key) or fallback
    return fallback


def template_state_label(is_custom: bool, language: str) -> str:
    return tr(language, "свой", "custom") if is_custom else tr(language, "стандарт", "default")


def money_text(value: dict[str, Any] | None, currency: str = "₽") -> str:
    text = str((value or {}).get("text") or "0")
    return f"{text} {currency}" if currency else text


def revenue_text(row: dict[str, Any]) -> str:
    items = row.get("revenue") or []
    if not items:
        return "0 ₽"
    return ", ".join(f"{item.get('text', '0')} {item.get('currency', 'RUB')}" for item in items)


def action_label(action: str, language: str = "ru") -> str:
    labels = {
        "create": "Покупка",
        "renew": "Продление",
        "reissue": "Перевыпуск",
        "traffic": "LTE-трафик",
        "ip_limit": "IP-лимит",
    }
    labels_en = {
        "create": "Purchase",
        "renew": "Renewal",
        "reissue": "Reissue",
        "traffic": "LTE traffic",
        "ip_limit": "IP limit",
    }
    return (labels_en if language == "en" else labels).get(action, action)


STATS_PERIOD_LABELS_EN = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
    "all": "All time",
}


def statistics_keyboard(active_period: str, language: str = "ru") -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in STATS_PERIODS.items():
        prefix = "✅ " if key == active_period else ""
        label = STATS_PERIOD_LABELS_EN.get(key, label) if language == "en" else label
        row.append(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"stats:period:{key}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def statistics_text(data: dict[str, Any], language: str = "ru") -> str:
    totals = data["totals"]
    period_label = STATS_PERIOD_LABELS_EN.get(str(data["period"]["key"]), str(data["period"]["label"])) if language == "en" else str(data["period"]["label"])
    lines = [
        f"📈 <b>{tr(language, 'Статистика', 'Statistics')}: {escape(period_label)}</b>",
        f"🧾 {tr(language, 'Продаж', 'Sales')}: <b>{totals['sales_count']}</b> ({totals['delivered_count']} {tr(language, 'выдано', 'delivered')}, {totals['pending_count']} {tr(language, 'ждёт', 'pending')})",
        f"💵 {tr(language, 'Сумма продаж', 'Revenue')}: <b>{escape(revenue_text(totals))}</b>",
        f"💸 {tr(language, 'Расход XyraNet', 'XyraNet cost')}: <b>{escape(money_text(totals['expense_rub']))}</b>",
        f"📊 {tr(language, 'Прибыль', 'Profit')}: <b>{escape(money_text(totals['profit_rub']))}</b>",
    ]
    if totals.get("margin_percent") is not None:
        lines[-1] += f" · {tr(language, 'маржа', 'margin')} {totals['margin_percent']}%"
    lines.append(f"🧮 {tr(language, 'Средний чек', 'Average order')}: <b>{escape(money_text(totals['avg_order_rub']))}</b>")

    if data.get("marketplaces"):
        lines.append(f"\n<b>{tr(language, 'Площадки', 'Marketplaces')}</b>")
        for item in data["marketplaces"][:4]:
            lines.append(f"• {escape(str(item['label']))}: {item['sales_count']} · {escape(revenue_text(item))}")

    if data.get("actions"):
        lines.append(f"\n<b>{tr(language, 'Действия', 'Actions')}</b>")
        for item in data["actions"][:5]:
            lines.append(f"• {escape(action_label(str(item['key']), language))}: {item['sales_count']} · {escape(revenue_text(item))}")

    if data.get("tariffs"):
        lines.append(f"\n<b>{tr(language, 'Топ тарифов', 'Top tariffs')}</b>")
        for item in data["tariffs"][:5]:
            lines.append(f"• <code>{escape(str(item['label']))}</code>: {item['sales_count']} · {escape(revenue_text(item))}")
    return "\n".join(lines)


def percent_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def mb_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if number >= 1024:
        return f"{number / 1024:.1f} GB"
    return f"{number:.0f} MB"


def system_metrics_text(data: dict[str, Any], language: str = "ru") -> str:
    load_average = data.get("cpu", {}).get("load_average")
    load_text = " / ".join(f"{float(item):.2f}" for item in load_average) if load_average else "n/a"
    memory = data.get("memory", {})
    disk = data.get("disk", {})
    process = data.get("process", {})
    return "\n".join(
        [
            f"🖥 <b>{tr(language, 'Сервер', 'Server')}</b>",
            f"Host: <code>{escape(str(data.get('hostname') or 'n/a'))}</code>",
            f"OS: <code>{escape(str(data.get('platform') or 'n/a'))}</code>",
            f"Python: <code>{escape(str(data.get('python') or 'n/a'))}</code>",
            "",
            f"CPU: <b>{percent_text(data.get('cpu', {}).get('percent'))}</b> · {tr(language, 'ядер', 'cores')} {escape(str(data.get('cpu', {}).get('cores') or 'n/a'))}",
            f"Load avg: <code>{escape(load_text)}</code>",
            f"RAM: <b>{percent_text(memory.get('percent'))}</b> · {mb_text(memory.get('used_mb'))} / {mb_text(memory.get('total_mb'))}",
            f"Swap: {mb_text(memory.get('swap_used_mb'))} / {mb_text(memory.get('swap_total_mb'))}",
            f"{tr(language, 'Диск', 'Disk')}: <b>{percent_text(disk.get('percent'))}</b> · {tr(language, 'свободно', 'free')} {mb_text(disk.get('free_mb'))}",
            "",
            f"{tr(language, 'Процесс', 'Process')}: PID <code>{escape(str(process.get('pid') or 'n/a'))}</code> · RAM {mb_text(process.get('rss_mb'))} · CPU {percent_text(process.get('cpu_percent'))}",
            f"{tr(language, 'Аптайм', 'Uptime')}: {tr(language, 'сервер', 'server')} <b>{escape(str(data.get('uptime') or 'n/a'))}</b> · {tr(language, 'приложение', 'app')} <b>{escape(str(process.get('uptime') or 'n/a'))}</b>",
        ]
    )


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
        language = runtime_language(runtime)
        await message.answer(tr(language, "🔒 Доступ только для администраторов.", "🔒 Administrators only."))

    async def deny_callback(callback: CallbackQuery) -> None:
        language = runtime_language(runtime)
        await callback.answer(tr(language, "🔒 Нет доступа", "🔒 Access denied"), show_alert=True)

    async def send_home(message: Message) -> None:
        language = runtime_language(runtime)
        uid = user_id(message)
        if not is_admin(uid):
            await message.answer(
                tr(
                    language,
                    "🔑 Пришлите сюда 16-символьный код покупки Plati.Market/Digiseller. Я проверю оплату и выдам VPN-доступ.",
                    "🔑 Send the 16-character Plati.Market/Digiseller purchase code here. I will verify the payment and deliver VPN access.",
                )
            )
            return
        await message.answer(
            f"✨ <b>XyraNet Reseller Autoseller</b>\n{tr(language, 'Выберите действие:', 'Choose an action:')}",
            reply_markup=main_menu(is_owner(uid), language),
        )

    @router.message(Command("start", "menu"))
    async def start(message: Message) -> None:
        await send_home(message)

    @router.callback_query(F.data == "menu:home")
    async def home(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await answer_or_edit(
            callback,
            f"✨ <b>XyraNet Reseller Autoseller</b>\n{tr(language, 'Выберите действие:', 'Choose an action:')}",
            main_menu(is_owner(uid), language),
        )

    @router.callback_query(F.data == "menu:status")
    async def status(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        products = db.list_products()
        sales = db.list_sales(limit=5)
        await answer_or_edit(
            callback,
            f"📊 <b>{tr(language, 'Статус', 'Status')}</b>\n"
            f"🧾 {tr(language, 'Маппингов', 'Mappings')}: <b>{len(products)}</b>\n"
            f"📦 {tr(language, 'Последних продаж в базе', 'Recent sales in database')}: <b>{len(sales)}</b>\n"
            f"👥 {tr(language, 'Telegram-админов', 'Telegram admins')}: <b>{len(runtime.bot_admin_ids())}</b>",
            back_menu(language),
        )

    @router.callback_query(F.data == "menu:system")
    async def system_status(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await answer_or_edit(callback, system_metrics_text(collect_system_metrics(), language), back_menu(language))

    @router.callback_query(F.data == "menu:balance")
    async def balance(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        try:
            data = await xyranet.summary()
            text = (
                "💰 <b>Wholesale</b>\n"
                f"{tr(language, 'Баланс', 'Balance')}: <b>{escape(str(data.get('balance')))} {escape(str(data.get('currency', 'RUB')))}</b>\n"
                f"{tr(language, 'Оборот API', 'API turnover')}: <b>{escape(str(data.get('api_spent_total')))}</b>\n"
                f"{tr(language, 'Покупок', 'Purchases')}: <b>{escape(str(data.get('api_purchase_count')))}</b>"
            )
        except Exception as exc:
            text = f"⚠️ {tr(language, 'Не смог получить баланс', 'Could not fetch balance')}: <code>{escape(str(exc))}</code>"
        await answer_or_edit(callback, text, back_menu(language))

    @router.callback_query(F.data == "menu:tariffs")
    async def tariffs(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        try:
            rows = await xyranet.tariffs()
            lines = [f"🧭 <b>{tr(language, 'Доступные тарифы', 'Available tariffs')}</b>"]
            for item in rows[:30]:
                lines.append(f"• {escape(format_tariff_label(item, language))}")
            text = "\n".join(lines)
        except Exception as exc:
            text = f"⚠️ {tr(language, 'Не смог получить тарифы', 'Could not fetch tariffs')}: <code>{escape(str(exc))}</code>"
        await answer_or_edit(callback, text, back_menu(language))

    @router.callback_query(F.data == "menu:products")
    async def products(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = db.list_products()
        if not rows:
            await answer_or_edit(callback, tr(language, "🧾 Маппингов пока нет.", "🧾 No mappings yet."), back_menu(language))
            return
        lines = [f"🧾 <b>{tr(language, 'Товары', 'Products')}</b>"]
        buttons = []
        for item in rows[:20]:
            state = tr(language, "✅ вкл", "✅ on") if int(item["enabled"]) else tr(language, "⏸ выкл", "⏸ off")
            lines.append(
                f"#{item['id']} {escape(format_product_key(item))} → "
                f"<code>{escape(item['tariff_code'])}</code> ({state})"
            )
            next_state = "0" if int(item["enabled"]) else "1"
            buttons.append([InlineKeyboardButton(text=f"#{item['id']} {state}", callback_data=f"product:toggle:{item['id']}:{next_state}")])
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("product:toggle:"))
    async def toggle_product(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        _, _, product_id, enabled = (callback.data or "").split(":")
        db.set_product_enabled(int(product_id), enabled == "1")
        await callback.answer(tr(runtime_language(runtime), "✅ Готово", "✅ Done"))
        await products(callback)

    @router.callback_query(F.data == "menu:templates")
    async def templates(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        buttons = [
            [
                InlineKeyboardButton(
                    text=template_label(str(group["key"]), str(group["label"]), language),
                    callback_data=f"template:group:{group['key']}",
                )
            ]
            for group in TEMPLATE_GROUPS
        ]
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
        await answer_or_edit(
            callback,
            f"📝 <b>{tr(language, 'Шаблоны', 'Templates')}</b>\n{tr(language, 'Выберите вид действия.', 'Choose an action type.')}",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data.startswith("template:group:"))
    async def template_group(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        group_key = (callback.data or "").split(":")[-1]
        group = next((item for item in TEMPLATE_GROUPS if item["key"] == group_key), None)
        if not group:
            await callback.answer(tr(language, "Вид действия не найден", "Action type not found"), show_alert=True)
            return
        lines = [f"📝 <b>{escape(template_label(group_key, str(group['label']), language))}</b>"]
        buttons = []
        command_action = str(group.get("command_action") or "")
        if command_action:
            service = DeliveryService(db=db, xyranet=xyranet)
            lines.append(f"{tr(language, 'Команда', 'Command')}: <code>{escape(service.expected_command(command_action))}</code>")
        for stage in group["stages"]:
            key = str(stage["key"])
            is_custom = bool((db.get_setting(delivery_template_key(key)) or "").strip())
            stage_label = template_label(key, str(stage["label"]), language)
            lines.append(f"• {escape(stage_label)} — {template_state_label(is_custom, language)}")
            buttons.append([InlineKeyboardButton(text=f"✏️ {stage_label}", callback_data=f"template:edit:{key}")])
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:templates")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("template:edit:"))
    async def edit_template(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        action = (callback.data or "").split(":")[-1]
        if action not in DEFAULT_ACTION_TEMPLATES:
            await callback.answer(tr(language, "Вид действия не найден", "Action type not found"), show_alert=True)
            return
        await state.set_state(MappingState.waiting_template)
        await state.update_data(action=action)
        current = (db.get_setting(delivery_template_key(action)) or "").strip() or DEFAULT_ACTION_TEMPLATES[action]
        label = template_label(action, ACTION_LABELS[action], language)
        await answer_or_edit(
            callback,
            f"✏️ <b>{tr(language, 'Шаблон', 'Template')}: {escape(label)}</b>\n\n"
            f"{template_help_text(language)}\n\n"
            f"{tr(language, 'Текущий шаблон', 'Current template')}:\n"
            f"<pre>{escape(current)}</pre>",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "♻️ Сбросить на стандартный", "♻️ Reset to default"),
                            callback_data=f"template:reset:{action}",
                        )
                    ],
                    [InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:templates")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("template:reset:"))
    async def reset_template(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        action = (callback.data or "").split(":")[-1]
        if action not in DEFAULT_ACTION_TEMPLATES:
            await callback.answer(tr(language, "Вид действия не найден", "Action type not found"), show_alert=True)
            return
        db.set_setting(delivery_template_key(action), "")
        await callback.answer(tr(language, "✅ Шаблон сброшен", "✅ Template reset"))
        await templates(callback)

    @router.callback_query(F.data == "menu:sales")
    async def sales(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = db.list_sales(limit=10)
        if not rows:
            await answer_or_edit(callback, tr(language, "📦 Продаж пока нет.", "📦 No sales yet."), back_menu(language))
            return
        lines = [f"📦 <b>{tr(language, 'Последние продажи', 'Latest sales')}</b>"]
        for item in rows:
            mark = "✅" if item.get("xyranet_order_id") else "⏳"
            key = f"{item['marketplace']}:{item['external_product_id']}"
            if item.get("external_variant_id"):
                key += f":{item['external_variant_id']}"
            lines.append(f"{mark} {escape(item['external_order_id'])} — {escape(key)}")
        await answer_or_edit(callback, "\n".join(lines), back_menu(language))

    @router.callback_query(F.data.startswith("stats:period:"))
    async def statistics(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        period = (callback.data or "").split(":")[-1]
        language = runtime_language(runtime)
        data = build_sales_statistics(db.list_sales_for_statistics(), period=period)
        await answer_or_edit(callback, statistics_text(data, language), statistics_keyboard(str(data["period"]["key"]), language))

    @router.callback_query(F.data == "map:add")
    async def map_add(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await state.set_state(MappingState.waiting_payload)
        await answer_or_edit(
            callback,
            tr(language, "➕ <b>Новый маппинг</b>", "➕ <b>New mapping</b>")
            + "\n"
            + tr(language, "Отправьте строку:", "Send a line:")
            + "\n<code>plati | 123456 | lite_monthly | Lite 1 month</code>\n\n"
            + tr(language, "Для варианта/кнопки GGsel:", "For a GGsel variant/button:")
            + "\n<code>ggsel | offer-9 | button-lite | lite_monthly | Lite 1 month</code>",
            back_menu(language),
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
        language = runtime_language(runtime)
        if action not in ACTION_LABELS:
            await state.clear()
            await message.answer(
                tr(language, "⚠️ Вид действия не найден.", "⚠️ Action type not found."),
                reply_markup=main_menu(is_owner(uid), language),
            )
            return
        text = message.text or ""
        template = "" if text.strip().lower() in {"-", "default", "стандарт"} else text.strip()
        db.set_setting(delivery_template_key(action), template)
        await state.clear()
        label = template_label(action, ACTION_LABELS[action], language)
        await message.answer(
            tr(language, "✅ Шаблон сохранён", "✅ Template saved")
            + f"\n{escape(label)}\n"
            + (
                tr(language, "Используется свой шаблон.", "Custom template is used.")
                if template
                else tr(language, "Используется стандартный шаблон.", "Default template is used.")
            ),
            reply_markup=main_menu(is_owner(uid), language),
        )

    async def save_mapping_from_text(message: Message, text: str) -> None:
        language = runtime_language(runtime)
        try:
            product = db.upsert_product(parse_mapping_payload(text))
        except ValueError as exc:
            detail = escape(str(exc)) if language == "ru" else tr(language, "", "Invalid mapping format")
            await message.answer(f"⚠️ {tr(language, 'Не разобрал маппинг', 'Could not parse mapping')}: {detail}")
            return
        await message.answer(
            tr(language, "✅ Маппинг сохранён", "✅ Mapping saved")
            + "\n"
            f"{escape(format_product_key(product))}\n"
            f"🧭 {tr(language, 'Тариф', 'Tariff')}: <code>{escape(product['tariff_code'])}</code>",
            reply_markup=main_menu(is_owner(user_id(message)), language),
        )

    @router.callback_query(F.data == "menu:users")
    async def users(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        rows = runtime.list_bot_users()
        lines = [f"👥 <b>{tr(language, 'Доступ к боту', 'Bot access')}</b>"]
        buttons = []
        owner = is_owner(uid)
        for row in rows:
            locked = " 🔒 env" if row["locked"] else ""
            state = tr(language, "✅ вкл", "✅ on") if row["enabled"] else tr(language, "⏸ выкл", "⏸ off")
            label = f" — {escape(row['label'])}" if row.get("label") else ""
            lines.append(f"{row['telegram_id']}{label} ({state}{locked})")
            if owner and not row["locked"]:
                buttons.append(
                    [InlineKeyboardButton(text=f"{tr(language, '🗑 Убрать', '🗑 Remove')} {row['telegram_id']}", callback_data=f"user:delete:{row['telegram_id']}")]
                )
        if owner:
            buttons.append([InlineKeyboardButton(text=tr(language, "➕ Добавить пользователя", "➕ Add user"), callback_data="user:add")])
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "user:add")
    async def user_add(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        await state.set_state(UserState.waiting_payload)
        await answer_or_edit(
            callback,
            tr(language, "➕ Отправьте Telegram ID и подпись, например:", "➕ Send Telegram ID and a label, for example:")
            + "\n<code>123456789 Admin</code>",
            back_menu(language),
        )

    @router.message(UserState.waiting_payload)
    async def user_payload(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await state.clear()
            await deny_message(message)
            return
        try:
            telegram_id, label = parse_user_payload(message.text or "")
        except ValueError as exc:
            detail = escape(str(exc)) if language == "ru" else tr(language, "", "Telegram ID must be first")
            await message.answer(f"⚠️ {tr(language, 'Не разобрал пользователя', 'Could not parse user')}: {detail}")
            return
        if runtime.is_env_admin(telegram_id):
            await message.answer(tr(language, "🔒 Этот пользователь уже указан в env и не удаляется.", "🔒 This user is defined in env and cannot be removed."))
        else:
            db.upsert_bot_user(telegram_id, label, added_by=uid)
            await message.answer(tr(language, "✅ Пользователь добавлен.", "✅ User added."), reply_markup=main_menu(True, language))
        await state.clear()

    @router.callback_query(F.data.startswith("user:delete:"))
    async def user_delete(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        telegram_id = int((callback.data or "").split(":")[-1])
        if runtime.is_env_admin(telegram_id):
            await callback.answer(tr(language, "🔒 ENV-админа нельзя удалить", "🔒 ENV admin cannot be removed"), show_alert=True)
            return
        db.delete_bot_user(telegram_id)
        await callback.answer(tr(language, "🗑 Удалён", "🗑 Removed"))
        await users(callback)

    @router.callback_query(F.data == "menu:settings")
    async def settings_menu(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        lines = [f"⚙️ <b>{tr(language, 'Настройки', 'Settings')}</b>"]
        buttons = []
        settings_by_key = {item["key"]: item for item in runtime.setting_payload()}
        for group_labels, keys in SETTING_GROUPS:
            lines.append(f"\n<b>{escape(setting_group_title(group_labels, language))}</b>")
            group_buttons: list[InlineKeyboardButton] = []
            for key in keys:
                item = settings_by_key.get(key)
                if not item:
                    continue
                value = setting_value_label(runtime, item)
                label = setting_button_label(runtime, item)
                lines.append(f"{escape(label)}: <code>{escape(value)}</code>")
                group_buttons.append(InlineKeyboardButton(text=label, callback_data=f"setting:edit:{item['key']}"))
                if len(group_buttons) == 2:
                    buttons.append(group_buttons)
                    group_buttons = []
            if group_buttons:
                buttons.append(group_buttons)
        if restart_bot:
            buttons.append([InlineKeyboardButton(text=tr(language, "🔄 Перезапустить Telegram-бота", "🔄 Restart Telegram bot"), callback_data="bot:restart")])
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "bot:restart")
    async def bot_restart(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        if not restart_bot:
            await callback.answer(tr(language, "⚠️ Рестарт недоступен", "⚠️ Restart is unavailable"), show_alert=True)
            return
        await callback.answer(tr(language, "🔄 Перезапускаю Telegram-бота", "🔄 Restarting Telegram bot"))
        if callback.message:
            await callback.message.edit_text(tr(language, "🔄 Telegram-бот перезапускается.", "🔄 Telegram bot is restarting."))
        asyncio.create_task(restart_bot())

    @router.callback_query(F.data.startswith("setting:edit:"))
    async def setting_edit(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        key = (callback.data or "").removeprefix("setting:edit:")
        definition = SETTING_BY_KEY[key]
        item = next((row for row in runtime.setting_payload() if row["key"] == key), None)
        if definition.kind == "boolean":
            runtime.set_value(key, not runtime.get_bool(key))
            await callback.answer(tr(language, "✅ Переключено", "✅ Switched"))
            await settings_menu(callback)
            return
        if definition.kind == "select":
            options = [
                [InlineKeyboardButton(text=str(option["label"]), callback_data=f"setting:select:{key}:{option['value']}")]
                for option in (item or {}).get("options", [])
            ]
            options.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:settings")])
            label = str((item or {}).get("label") or definition.label)
            await answer_or_edit(
                callback,
                f"🌐 <b>{escape(label)}</b>\n{tr(language, 'Выберите значение:', 'Choose a value:')}",
                InlineKeyboardMarkup(inline_keyboard=options),
            )
            return
        await state.set_state(SettingState.waiting_value)
        await state.update_data(setting_key=key)
        label = str((item or {}).get("label") or definition.label)
        await answer_or_edit(
            callback,
            f"✏️ {tr(language, 'Отправьте новое значение для', 'Send a new value for')} <b>{escape(label)}</b>.",
            back_menu(language),
        )

    @router.callback_query(F.data.startswith("setting:select:"))
    async def setting_select(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        _, _, key, value = (callback.data or "").split(":", 3)
        try:
            runtime.set_value(key, value)
        except ValueError as exc:
            detail = str(exc) if language == "ru" else "Unsupported setting value"
            await callback.answer(f"⚠️ {detail}", show_alert=True)
            return
        await callback.answer(tr(language, "✅ Сохранено", "✅ Saved"))
        await settings_menu(callback)

    @router.message(SettingState.waiting_value)
    async def setting_value(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await state.clear()
            await deny_message(message)
            return
        data = await state.get_data()
        key = str(data.get("setting_key") or "")
        try:
            runtime.set_value(key, message.text or "")
        except ValueError as exc:
            detail = escape(str(exc)) if language == "ru" else tr(language, "", "Could not save this value")
            await message.answer(f"⚠️ {tr(language, 'Не сохранил настройку', 'Could not save setting')}: {detail}")
            return
        definition = SETTING_BY_KEY[key]
        note = (
            "\n"
            + tr(
                language,
                "🔄 Нажмите «Перезапустить Telegram-бота», чтобы применить настройку.",
                "🔄 Press “Restart Telegram bot” to apply this setting.",
            )
            if definition.restart_required
            else ""
        )
        await message.answer(
            f"{tr(language, '✅ Настройка сохранена.', '✅ Setting saved.')}{note}",
            reply_markup=main_menu(True, language),
        )
        await state.clear()

    @router.message(F.text)
    async def buyer_unique_code(message: Message) -> None:
        language = runtime_language(runtime)
        text = (message.text or "").strip().replace(" ", "").replace("-", "")
        if text.startswith("/"):
            return
        if not UNIQUE_CODE_RE.fullmatch(text):
            if not is_admin(user_id(message)):
                await message.answer(
                    tr(
                        language,
                        "🔑 Пришлите 16-символьный код покупки без пробелов.",
                        "🔑 Send the 16-character purchase code without spaces.",
                    )
                )
            return
        await message.answer(tr(language, "🔎 Проверяю код покупки...", "🔎 Checking purchase code..."))
        try:
            purchase = await digiseller.purchase_by_unique_code(text)
            event = sale_event_from_unique_code(purchase, text)
            if not event.external_order_id or not event.external_product_id:
                await message.answer(
                    tr(
                        language,
                        "⚠️ Код найден, но Digiseller не вернул номер заказа или ID товара.",
                        "⚠️ Code found, but Digiseller did not return the order number or product ID.",
                    )
                )
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
            await message.answer(
                f"⚠️ {tr(language, 'Не получилось проверить код', 'Could not verify code')}: <code>{escape(str(exc))}</code>"
            )
        except ValueError as exc:
            await message.answer(
                f"⚠️ {tr(language, 'Не получилось выдать доступ', 'Could not deliver access')}: <code>{escape(str(exc))}</code>"
            )
        except Exception as exc:
            await message.answer(f"⚠️ {tr(language, 'Ошибка выдачи', 'Delivery error')}: <code>{escape(str(exc))}</code>")

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
