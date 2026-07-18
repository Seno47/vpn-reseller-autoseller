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

from reseller_autoseller.chat_ui import chat_actions_reply_markup, format_chat_history, marketplace_label
from reseller_autoseller.db import Database
from reseller_autoseller.marketplace_chat import MarketplaceMessenger
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
QUICK_REPLY_TITLE_LIMIT = 80
CHAT_REPLY_BODY_LIMIT = 3000
CHAT_REPLY_RENDERED_BODY_LIMIT = 3300
QUICK_REPLY_PAGE_SIZE = 8


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


class ChatReplyState(StatesGroup):
    waiting_text = State()
    editing_text = State()


class QuickReplyState(StatesGroup):
    waiting_title = State()
    waiting_body = State()
    editing_title = State()
    editing_body = State()


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
        [
            InlineKeyboardButton(
                text=tr(language, "💬 Быстрые ответы", "💬 Quick replies"),
                callback_data="menu:quick_replies",
            )
        ],
    ]
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton(text=tr(language, "🔄 Обновления", "🔄 Updates"), callback_data="menu:update"),
                InlineKeyboardButton(text=tr(language, "⚙️ Настройки", "⚙️ Settings"), callback_data="menu:settings"),
            ]
        )
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
    ({"ru": "🛒 Маркетплейсы", "en": "🛒 Marketplaces"}, ["digiseller_seller_id", "digiseller_api_key", "digiseller_notification_password", "digiseller_unique_code_request_enabled", "digiseller_unique_code_request_delay_minutes", "ggsel_seller_id", "ggsel_api_key", "ggsel_notification_secret", "ggsel_sale_notifications_enabled", "ggsel_sales_polling_fallback_interval_seconds"]),
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
    "digiseller_notification_password": "🧾",
    "digiseller_unique_code_request_enabled": "📨",
    "digiseller_unique_code_request_delay_minutes": "⏱",
    "ggsel_seller_id": "🛍",
    "ggsel_api_key": "🔑",
    "ggsel_notification_secret": "🔐",
    "ggsel_sale_notifications_enabled": "📨",
    "ggsel_sales_polling_fallback_interval_seconds": "⏱",
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
    "status": "Subscription status",
    "command_help": "Chat commands",
    "ask_renew": "Ask for order_id: renewal",
    "ask_reissue": "Ask for order_id: reissue",
    "ask_traffic": "Ask for order_id: LTE traffic",
    "ask_ip_limit": "Ask for order_id: IP limit",
    "command_mismatch": "Command error",
    "operation_error": "Operation error",
    "free_reissue_help": "Free reissue hint",
    "free_reissue_disabled": "Free reissue disabled",
    "status_help": "Status command hint",
    "status_error": "Status error",
    "request_unique_code": "Ask for unique code",
    "unique_code_invoice_mismatch": "Code from another order",
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
    "status_help": "Command without order ID",
    "status": "Status received",
    "status_error": "Status error",
    "request_unique_code": "Buyer has not sent unique code",
    "unique_code_invoice_mismatch": "Code from another order",
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


def update_status_text(data: dict[str, Any], language: str = "ru") -> str:
    host_status = data.get("host_status") if isinstance(data.get("host_status"), dict) else {}
    available = bool(data.get("update_available"))
    supported = bool(data.get("update_supported"))
    lines = [
        f"🔄 <b>{tr(language, 'Обновления', 'Updates')}</b>",
        f"{tr(language, 'Текущая версия', 'Current version')}: <code>{escape(str(data.get('current_version') or 'unknown'))}</code>",
    ]
    if data.get("current_commit"):
        lines.append(f"{tr(language, 'Текущий commit', 'Current commit')}: <code>{escape(str(data['current_commit']))}</code>")
    if data.get("latest_version"):
        lines.append(f"{tr(language, 'Последняя версия', 'Latest version')}: <code>{escape(str(data['latest_version']))}</code>")
    if data.get("latest_commit"):
        lines.append(f"{tr(language, 'Последний commit', 'Latest commit')}: <code>{escape(str(data['latest_commit']))}</code>")
    if data.get("checked_at"):
        lines.append(f"{tr(language, 'Проверено', 'Checked')}: <code>{escape(str(data['checked_at']))}</code>")
    if data.get("error"):
        lines.append(f"⚠️ {tr(language, 'Проверка', 'Check')}: <code>{escape(str(data['error']))}</code>")
    if host_status:
        status = str(host_status.get("status") or "")
        message = str(host_status.get("message") or host_status.get("stage") or "")
        lines.append(f"{tr(language, 'Updater', 'Updater')}: <b>{escape(status or 'unknown')}</b>")
        if message:
            lines.append(f"<code>{escape(message[:500])}</code>")
    if not supported:
        lines.append(
            "⚠️ "
            + tr(
                language,
                "Обновление с кнопки не настроено на этом сервере.",
                "Button update is not configured on this server.",
            )
        )
    elif available:
        lines.append(f"✅ {tr(language, 'Доступно обновление.', 'An update is available.')}")
    else:
        lines.append(f"🟢 {tr(language, 'Установлена актуальная версия.', 'The installed version is current.')}")
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


def aiogram_markup(payload: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(**dict(button)) for button in row]
            for row in payload.get("inline_keyboard", [])
        ]
    )


def telegram_author(event: Message | CallbackQuery) -> str:
    source = event.from_user
    if source is None:
        return "Telegram admin"
    full_name = str(source.full_name or "").strip()
    username = str(source.username or "").strip()
    if full_name and username:
        return f"{full_name} (@{username})"
    return full_name or (f"@{username}" if username else str(source.id))


def escaped_excerpt(value: Any, limit: int = CHAT_REPLY_RENDERED_BODY_LIMIT) -> str:
    raw = str(value or "")
    rendered = escape(raw)
    if len(rendered) <= limit:
        return rendered
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if len(escape(raw[:middle])) <= limit - 1:
            low = middle
        else:
            high = middle - 1
    return escape(raw[:low]).rstrip() + "…"


def valid_chat_reply_body(body: str) -> bool:
    return bool(body) and len(body) <= CHAT_REPLY_BODY_LIMIT and len(escape(body)) <= CHAT_REPLY_RENDERED_BODY_LIMIT


def chat_draft_preview(draft: dict[str, Any], language: str = "ru") -> str:
    body = escaped_excerpt(draft.get("body"))
    return (
        f"✍️ <b>{tr(language, 'Проверьте ответ перед отправкой', 'Review the reply before sending')}</b>\n"
        f"🛒 <b>{escape(marketplace_label(draft.get('marketplace')))}</b> · "
        f"{tr(language, 'заказ', 'order')} <code>{escape(str(draft.get('external_order_id') or ''))}</code>\n\n"
        f"<blockquote>{body}</blockquote>\n\n"
        f"⚠️ {tr(language, 'Сообщение уйдёт покупателю только после подтверждения.', 'The message is sent to the buyer only after confirmation.')}"
    )


def chat_draft_keyboard(draft_id: int, language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "✅ Отправить", "✅ Send"),
                    callback_data=f"chat:draft_send:{int(draft_id)}",
                ),
                InlineKeyboardButton(
                    text=tr(language, "✏️ Изменить", "✏️ Edit"),
                    callback_data=f"chat:draft_edit:{int(draft_id)}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "❌ Отмена", "❌ Cancel"),
                    callback_data=f"chat:draft_cancel:{int(draft_id)}",
                )
            ],
        ]
    )


def quick_reply_summary(template: dict[str, Any], language: str = "ru") -> str:
    enabled = bool(int(template.get("enabled") or 0))
    state = tr(language, "✅ Активна", "✅ Enabled") if enabled else tr(language, "⏸ Отключена", "⏸ Disabled")
    body = escaped_excerpt(template.get("body"))
    return (
        f"⚡ <b>{escape(str(template.get('title') or ''))}</b>\n"
        f"{state}\n\n"
        f"<blockquote>{body}</blockquote>"
    )


def build_dispatcher(
    *,
    db: Database,
    xyranet: XyraNetClient,
    digiseller: Any,
    runtime: RuntimeConfig,
    messenger: MarketplaceMessenger | None = None,
    restart_bot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    check_updates: Callable[[bool], Awaitable[dict[str, Any]]] | None = None,
    start_update: Callable[[], dict[str, Any]] | None = None,
) -> Dispatcher:
    router = Router()
    delivery_service = DeliveryService(db=db, xyranet=xyranet, free_reissue_enabled=lambda: runtime.get_bool("free_reissue_enabled"))
    chat_messenger = messenger or MarketplaceMessenger(digiseller=digiseller, db=db)

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
                    "🔑 Для получения покупки отправьте 16-символьный код в чат вашего заказа на Plati.Market/Digiseller. В Telegram код не принимается, потому что выдача привязана к конкретному чату покупки.",
                    "🔑 To receive your purchase, send the 16-character code in your Plati.Market/Digiseller order chat. Telegram does not accept codes because delivery is tied to the exact purchase chat.",
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

    def chat_anchor(message_id: int) -> dict[str, Any] | None:
        return db.get_chat_message(int(message_id))

    def chat_action_keyboard(message_id: int, marketplace: str, external_order_id: str) -> InlineKeyboardMarkup:
        payload = chat_actions_reply_markup(message_id, external_order_id)
        if marketplace not in {"plati", "digiseller"}:
            payload = {
                "inline_keyboard": [
                    row
                    for row in payload.get("inline_keyboard", [])
                    if not any(button.get("url") for button in row)
                ]
            }
        return aiogram_markup(payload)

    def latest_chat_action_keyboard(draft: dict[str, Any], language: str) -> InlineKeyboardMarkup:
        marketplace = str(draft.get("marketplace") or "digiseller")
        external_order_id = str(draft.get("external_order_id") or "")
        rows = db.list_chat_messages(marketplace, external_order_id, limit=1)
        if not rows:
            return back_menu(language)
        return chat_action_keyboard(int(rows[-1]["id"]), marketplace, external_order_id)

    async def show_chat_templates(callback: CallbackQuery, anchor_id: int, page: int, *, replace: bool) -> None:
        language = runtime_language(runtime)
        anchor = chat_anchor(anchor_id)
        if not anchor:
            await callback.answer(tr(language, "⚠️ Сообщение не найдено", "⚠️ Message not found"), show_alert=True)
            return
        templates = db.list_quick_reply_templates(enabled_only=True)
        total_pages = max(1, (len(templates) + QUICK_REPLY_PAGE_SIZE - 1) // QUICK_REPLY_PAGE_SIZE)
        selected_page = max(0, min(int(page), total_pages - 1))
        start = selected_page * QUICK_REPLY_PAGE_SIZE
        selected = templates[start : start + QUICK_REPLY_PAGE_SIZE]
        buttons: list[list[InlineKeyboardButton]] = []
        for template in selected:
            title = str(template.get("title") or "").strip()
            label = title if len(title) <= 42 else title[:41].rstrip() + "…"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"⚡ {label}",
                        callback_data=f"chat:template:{int(anchor_id)}:{int(template['id'])}",
                    )
                ]
            )
        navigation: list[InlineKeyboardButton] = []
        if selected_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=f"chat:templates:{int(anchor_id)}:{selected_page - 1}",
                )
            )
        if selected_page + 1 < total_pages:
            navigation.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=f"chat:templates:{int(anchor_id)}:{selected_page + 1}",
                )
            )
        if navigation:
            buttons.append(navigation)
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        text=tr(language, "✍️ Написать вручную", "✍️ Write manually"),
                        callback_data=f"chat:reply:{int(anchor_id)}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "⚙️ Управление заготовками", "⚙️ Manage quick replies"),
                        callback_data="menu:quick_replies",
                    )
                ],
            ]
        )
        if templates:
            text = (
                f"⚡ <b>{tr(language, 'Заготовки ответов', 'Quick replies')}</b>\n"
                f"🧾 {tr(language, 'Заказ', 'Order')}: "
                f"<code>{escape(str(anchor.get('external_order_id') or ''))}</code>\n\n"
                f"{tr(language, 'Выберите текст — перед отправкой его можно будет проверить и изменить.', 'Choose a reply. You can review and edit it before sending.')}"
            )
        else:
            text = (
                f"⚡ <b>{tr(language, 'Заготовок пока нет', 'No quick replies yet')}</b>\n"
                f"{tr(language, 'Создайте первую заготовку в разделе управления.', 'Create the first quick reply in the management section.')}"
            )
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        if replace and callback.message:
            await callback.message.edit_text(text, reply_markup=markup)
        elif callback.message:
            await callback.message.answer(text, reply_markup=markup)
        await callback.answer()

    async def show_quick_replies(callback: CallbackQuery, page: int = 0) -> None:
        language = runtime_language(runtime)
        rows = db.list_quick_reply_templates()
        total_pages = max(1, (len(rows) + QUICK_REPLY_PAGE_SIZE - 1) // QUICK_REPLY_PAGE_SIZE)
        selected_page = max(0, min(int(page), total_pages - 1))
        start = selected_page * QUICK_REPLY_PAGE_SIZE
        selected = rows[start : start + QUICK_REPLY_PAGE_SIZE]
        buttons: list[list[InlineKeyboardButton]] = []
        lines = [
            f"💬 <b>{tr(language, 'Быстрые ответы', 'Quick replies')}</b>",
            tr(
                language,
                "Заготовки помогают отвечать покупателям в пару нажатий. Перед отправкой всегда показывается предпросмотр.",
                "Quick replies make buyer support faster. A preview is always shown before sending.",
            ),
        ]
        for template in selected:
            enabled = bool(int(template.get("enabled") or 0))
            icon = "✅" if enabled else "⏸"
            title = str(template.get("title") or "").strip()
            lines.append(f"{icon} <b>{escape(title)}</b>")
            label = title if len(title) <= 38 else title[:37].rstrip() + "…"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {label}",
                        callback_data=f"quick:view:{int(template['id'])}",
                    )
                ]
            )
        if not selected:
            lines.append(f"\n{tr(language, 'Пока нет ни одной заготовки.', 'There are no quick replies yet.')}")
        navigation: list[InlineKeyboardButton] = []
        if selected_page > 0:
            navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"quick:list:{selected_page - 1}"))
        if selected_page + 1 < total_pages:
            navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"quick:list:{selected_page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        text=tr(language, "➕ Новая заготовка", "➕ New quick reply"),
                        callback_data="quick:add",
                    )
                ],
                [InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")],
            ]
        )
        await answer_or_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    async def show_quick_reply(callback: CallbackQuery, template_id: int) -> None:
        language = runtime_language(runtime)
        template = db.get_quick_reply_template(template_id)
        if not template:
            await callback.answer(tr(language, "⚠️ Заготовка не найдена", "⚠️ Quick reply not found"), show_alert=True)
            return
        enabled = bool(int(template.get("enabled") or 0))
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=tr(language, "✏️ Название", "✏️ Title"),
                        callback_data=f"quick:edit_title:{int(template_id)}",
                    ),
                    InlineKeyboardButton(
                        text=tr(language, "📝 Текст", "📝 Text"),
                        callback_data=f"quick:edit_body:{int(template_id)}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "⏸ Отключить", "⏸ Disable") if enabled else tr(language, "▶️ Включить", "▶️ Enable"),
                        callback_data=f"quick:toggle:{int(template_id)}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "🗑 Удалить", "🗑 Delete"),
                        callback_data=f"quick:delete:{int(template_id)}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "⬅️ К списку", "⬅️ Back to list"),
                        callback_data="menu:quick_replies",
                    )
                ],
            ]
        )
        await answer_or_edit(callback, quick_reply_summary(template, language), buttons)

    @router.callback_query(F.data.startswith("chat:more:"))
    async def chat_more(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        anchor_id = int((callback.data or "").rsplit(":", 1)[-1])
        anchor = chat_anchor(anchor_id)
        if not anchor:
            await callback.answer(tr(language, "⚠️ Сообщение не найдено", "⚠️ Message not found"), show_alert=True)
            return
        marketplace = str(anchor.get("marketplace") or "digiseller")
        external_order_id = str(anchor.get("external_order_id") or "")
        messages = db.list_chat_messages(marketplace, external_order_id, limit=2000)
        text = format_chat_history(
            messages,
            marketplace=marketplace,
            external_order_id=external_order_id,
            language=language,
        )
        if callback.message:
            await callback.message.answer(
                text,
                reply_markup=chat_action_keyboard(anchor_id, marketplace, external_order_id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("chat:reply:"))
    async def chat_reply(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        anchor_id = int((callback.data or "").rsplit(":", 1)[-1])
        anchor = chat_anchor(anchor_id)
        if not anchor:
            await callback.answer(tr(language, "⚠️ Сообщение не найдено", "⚠️ Message not found"), show_alert=True)
            return
        await state.clear()
        await state.set_state(ChatReplyState.waiting_text)
        await state.update_data(
            chat_anchor_id=anchor_id,
            marketplace=str(anchor.get("marketplace") or "digiseller"),
            external_order_id=str(anchor.get("external_order_id") or ""),
        )
        if callback.message:
            await callback.message.answer(
                f"✍️ <b>{tr(language, 'Ответ покупателю', 'Reply to buyer')}</b>\n"
                f"🧾 {tr(language, 'Заказ', 'Order')}: "
                f"<code>{escape(str(anchor.get('external_order_id') or ''))}</code>\n\n"
                f"{tr(language, 'Отправьте текст ответа следующим сообщением.', 'Send the reply text in your next message.')}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=tr(language, "❌ Отмена", "❌ Cancel"),
                                callback_data="chat:input_cancel",
                            )
                        ]
                    ]
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("chat:templates:"))
    async def chat_templates(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        parts = (callback.data or "").split(":")
        anchor_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        await show_chat_templates(callback, anchor_id, page, replace=len(parts) > 3)

    @router.callback_query(F.data.startswith("chat:template:"))
    async def chat_template_select(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        parts = (callback.data or "").split(":")
        anchor = chat_anchor(int(parts[2]))
        template = db.get_quick_reply_template(int(parts[3]))
        if not anchor or not template or not bool(int(template.get("enabled") or 0)):
            await callback.answer(tr(language, "⚠️ Заготовка недоступна", "⚠️ Quick reply is unavailable"), show_alert=True)
            return
        body = str(template.get("body") or "").strip()
        if not valid_chat_reply_body(body):
            await callback.answer(
                tr(language, "⚠️ Текст заготовки пустой или слишком длинный", "⚠️ The quick reply is empty or too long"),
                show_alert=True,
            )
            return
        draft = db.create_chat_reply_draft(
            marketplace=str(anchor.get("marketplace") or "digiseller"),
            external_order_id=str(anchor.get("external_order_id") or ""),
            telegram_user_id=int(uid),
            author_name=telegram_author(callback),
            body=body,
            template_id=int(template["id"]),
        )
        await state.clear()
        await answer_or_edit(callback, chat_draft_preview(draft, language), chat_draft_keyboard(int(draft["id"]), language))

    @router.callback_query(F.data == "chat:input_cancel")
    async def chat_input_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await state.clear()
        await answer_or_edit(callback, tr(language, "❌ Ответ отменён.", "❌ Reply cancelled."), back_menu(language))

    @router.message(ChatReplyState.waiting_text)
    async def chat_reply_text(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        body = str(message.text or "").strip()
        if not body:
            await message.answer(tr(language, "⚠️ Ответ не может быть пустым.", "⚠️ The reply cannot be empty."))
            return
        if not valid_chat_reply_body(body):
            await message.answer(
                tr(
                    language,
                    f"⚠️ Слишком длинный ответ: максимум {CHAT_REPLY_BODY_LIMIT} символов.",
                    f"⚠️ The reply is too long: {CHAT_REPLY_BODY_LIMIT} characters maximum.",
                )
            )
            return
        data = await state.get_data()
        try:
            draft = db.create_chat_reply_draft(
                marketplace=str(data.get("marketplace") or "digiseller"),
                external_order_id=str(data.get("external_order_id") or ""),
                telegram_user_id=int(uid),
                author_name=telegram_author(message),
                body=body,
            )
        except ValueError as exc:
            await message.answer(f"⚠️ {escape(str(exc))}")
            return
        await state.clear()
        await message.answer(
            chat_draft_preview(draft, language),
            reply_markup=chat_draft_keyboard(int(draft["id"]), language),
        )

    @router.callback_query(F.data.startswith("chat:draft_edit:"))
    async def chat_draft_edit(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        draft_id = int((callback.data or "").rsplit(":", 1)[-1])
        draft = db.get_chat_reply_draft(draft_id)
        if not draft or int(draft.get("telegram_user_id") or 0) != int(uid) or str(draft.get("status")) != "draft":
            await callback.answer(tr(language, "⚠️ Черновик уже недоступен", "⚠️ Draft is no longer available"), show_alert=True)
            return
        await state.clear()
        await state.set_state(ChatReplyState.editing_text)
        await state.update_data(chat_draft_id=draft_id)
        if callback.message:
            await callback.message.answer(
                f"✏️ <b>{tr(language, 'Изменение ответа', 'Edit reply')}</b>\n"
                f"{tr(language, 'Отправьте новый текст.', 'Send the new text.')}\n\n"
                f"{tr(language, 'Сейчас', 'Current')}:\n<blockquote>{escaped_excerpt(draft.get('body'))}</blockquote>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=tr(language, "❌ Отмена", "❌ Cancel"),
                                callback_data="chat:input_cancel",
                            )
                        ]
                    ]
                ),
            )
        await callback.answer()

    @router.message(ChatReplyState.editing_text)
    async def chat_draft_edit_text(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        body = str(message.text or "").strip()
        if not body:
            await message.answer(tr(language, "⚠️ Ответ не может быть пустым.", "⚠️ The reply cannot be empty."))
            return
        if not valid_chat_reply_body(body):
            await message.answer(
                tr(
                    language,
                    f"⚠️ Слишком длинный ответ: максимум {CHAT_REPLY_BODY_LIMIT} символов.",
                    f"⚠️ The reply is too long: {CHAT_REPLY_BODY_LIMIT} characters maximum.",
                )
            )
            return
        data = await state.get_data()
        draft = db.update_chat_reply_draft_body(int(data.get("chat_draft_id") or 0), int(uid), body)
        if not draft:
            await state.clear()
            await message.answer(tr(language, "⚠️ Черновик уже недоступен.", "⚠️ Draft is no longer available."))
            return
        await state.clear()
        await message.answer(
            chat_draft_preview(draft, language),
            reply_markup=chat_draft_keyboard(int(draft["id"]), language),
        )

    @router.callback_query(F.data.startswith("chat:draft_cancel:"))
    async def chat_draft_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        draft_id = int((callback.data or "").rsplit(":", 1)[-1])
        if not db.cancel_chat_reply_draft(draft_id, int(uid)):
            await callback.answer(tr(language, "⚠️ Черновик уже обработан", "⚠️ Draft was already processed"), show_alert=True)
            return
        await state.clear()
        await answer_or_edit(callback, tr(language, "❌ Отправка отменена.", "❌ Sending cancelled."), back_menu(language))

    @router.callback_query(F.data.startswith("chat:draft_send:"))
    async def chat_draft_send(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        draft_id = int((callback.data or "").rsplit(":", 1)[-1])
        claimed = db.claim_chat_reply_draft(draft_id, int(uid))
        if not claimed:
            await callback.answer(
                tr(language, "⚠️ Этот ответ уже отправляется или обработан", "⚠️ This reply is already sending or processed"),
                show_alert=True,
            )
            return
        draft, token = claimed
        await state.clear()
        await callback.answer(tr(language, "📨 Отправляю…", "📨 Sending…"))
        error_text = ""
        try:
            sent = await chat_messenger.send_message(
                str(draft.get("marketplace") or "digiseller"),
                str(draft.get("external_order_id") or ""),
                str(draft.get("body") or ""),
                role="admin",
                author_name=str(draft.get("author_name") or telegram_author(callback)),
                source="telegram",
            )
        except Exception as exc:
            sent = False
            error_text = str(exc)
        if sent:
            db.finish_chat_reply_draft(draft_id, token, status="sent")
            text = (
                f"✅ <b>{tr(language, 'Ответ отправлен покупателю', 'Reply sent to buyer')}</b>\n"
                f"🧾 {tr(language, 'Заказ', 'Order')}: "
                f"<code>{escape(str(draft.get('external_order_id') or ''))}</code>\n\n"
                f"<blockquote>{escaped_excerpt(draft.get('body'))}</blockquote>"
            )
        else:
            db.finish_chat_reply_draft(
                draft_id,
                token,
                status="uncertain",
                error_text=error_text or "Marketplace API did not confirm delivery",
            )
            text = (
                f"⚠️ <b>{tr(language, 'Статус отправки не подтверждён', 'Sending was not confirmed')}</b>\n"
                f"{tr(language, 'Автоматический повтор отключён, чтобы покупатель не получил дубликат. Проверьте чат DigiSeller вручную.', 'Automatic retry is disabled to avoid a duplicate. Check the DigiSeller chat manually.')}"
            )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=latest_chat_action_keyboard(draft, language))

    @router.callback_query(F.data == "menu:quick_replies")
    async def quick_replies(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await show_quick_replies(callback)

    @router.callback_query(F.data.startswith("quick:list:"))
    async def quick_replies_page(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        page = int((callback.data or "").rsplit(":", 1)[-1])
        await show_quick_replies(callback, page)

    @router.callback_query(F.data.startswith("quick:view:"))
    async def quick_reply_view(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await show_quick_reply(callback, int((callback.data or "").rsplit(":", 1)[-1]))

    @router.callback_query(F.data == "quick:add")
    async def quick_reply_add(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await state.clear()
        await state.set_state(QuickReplyState.waiting_title)
        await answer_or_edit(
            callback,
            f"➕ <b>{tr(language, 'Новая заготовка', 'New quick reply')}</b>\n"
            f"{tr(language, 'Отправьте короткое понятное название.', 'Send a short, clear title.')}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "❌ Отмена", "❌ Cancel"),
                            callback_data="quick:input_cancel",
                        )
                    ]
                ]
            ),
        )

    @router.message(QuickReplyState.waiting_title)
    async def quick_reply_title(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        title = str(message.text or "").strip()
        if not title or len(title) > QUICK_REPLY_TITLE_LIMIT:
            await message.answer(
                tr(
                    language,
                    f"⚠️ Название должно содержать от 1 до {QUICK_REPLY_TITLE_LIMIT} символов.",
                    f"⚠️ The title must contain 1 to {QUICK_REPLY_TITLE_LIMIT} characters.",
                )
            )
            return
        await state.update_data(quick_reply_title=title)
        await state.set_state(QuickReplyState.waiting_body)
        await message.answer(
            f"📝 <b>{tr(language, 'Текст заготовки', 'Quick reply text')}</b>\n"
            f"{tr(language, 'Теперь отправьте сообщение, которое будет предложено для ответа покупателю.', 'Now send the message that will be suggested as the buyer reply.')}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "❌ Отмена", "❌ Cancel"),
                            callback_data="quick:input_cancel",
                        )
                    ]
                ]
            ),
        )

    @router.message(QuickReplyState.waiting_body)
    async def quick_reply_body(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        body = str(message.text or "").strip()
        if not valid_chat_reply_body(body):
            await message.answer(
                tr(
                    language,
                    f"⚠️ Текст должен содержать от 1 до {CHAT_REPLY_BODY_LIMIT} символов.",
                    f"⚠️ The text must contain 1 to {CHAT_REPLY_BODY_LIMIT} characters.",
                )
            )
            return
        data = await state.get_data()
        template = db.create_quick_reply_template(
            str(data.get("quick_reply_title") or ""),
            body,
            created_by=int(uid),
        )
        await state.clear()
        await message.answer(
            f"✅ <b>{tr(language, 'Заготовка сохранена', 'Quick reply saved')}</b>\n\n"
            f"{quick_reply_summary(template, language)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "⚙️ Открыть настройки", "⚙️ Open settings"),
                            callback_data=f"quick:view:{int(template['id'])}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr(language, "⬅️ К списку", "⬅️ Back to list"),
                            callback_data="menu:quick_replies",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("quick:edit_title:"))
    async def quick_reply_edit_title(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        template_id = int((callback.data or "").rsplit(":", 1)[-1])
        template = db.get_quick_reply_template(template_id)
        if not template:
            await callback.answer(tr(language, "⚠️ Заготовка не найдена", "⚠️ Quick reply not found"), show_alert=True)
            return
        await state.clear()
        await state.set_state(QuickReplyState.editing_title)
        await state.update_data(quick_reply_template_id=template_id)
        await answer_or_edit(
            callback,
            f"✏️ <b>{tr(language, 'Новое название', 'New title')}</b>\n"
            f"{tr(language, 'Сейчас', 'Current')}: <code>{escape(str(template.get('title') or ''))}</code>\n\n"
            f"{tr(language, 'Отправьте новое название.', 'Send the new title.')}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "❌ Отмена", "❌ Cancel"),
                            callback_data="quick:input_cancel",
                        )
                    ]
                ]
            ),
        )

    @router.message(QuickReplyState.editing_title)
    async def quick_reply_edit_title_text(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        title = str(message.text or "").strip()
        if not title or len(title) > QUICK_REPLY_TITLE_LIMIT:
            await message.answer(
                tr(
                    language,
                    f"⚠️ Название должно содержать от 1 до {QUICK_REPLY_TITLE_LIMIT} символов.",
                    f"⚠️ The title must contain 1 to {QUICK_REPLY_TITLE_LIMIT} characters.",
                )
            )
            return
        data = await state.get_data()
        template = db.update_quick_reply_template(int(data.get("quick_reply_template_id") or 0), title=title)
        await state.clear()
        if not template:
            await message.answer(tr(language, "⚠️ Заготовка не найдена.", "⚠️ Quick reply not found."))
            return
        await message.answer(
            f"✅ {tr(language, 'Название обновлено.', 'Title updated.')}\n\n{quick_reply_summary(template, language)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=tr(language, "⚙️ Настройки", "⚙️ Settings"), callback_data=f"quick:view:{int(template['id'])}")]
                ]
            ),
        )

    @router.callback_query(F.data.startswith("quick:edit_body:"))
    async def quick_reply_edit_body(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        template_id = int((callback.data or "").rsplit(":", 1)[-1])
        template = db.get_quick_reply_template(template_id)
        if not template:
            await callback.answer(tr(language, "⚠️ Заготовка не найдена", "⚠️ Quick reply not found"), show_alert=True)
            return
        await state.clear()
        await state.set_state(QuickReplyState.editing_body)
        await state.update_data(quick_reply_template_id=template_id)
        await answer_or_edit(
            callback,
            f"📝 <b>{tr(language, 'Новый текст', 'New text')}</b>\n"
            f"{tr(language, 'Сейчас', 'Current')}:\n<blockquote>{escaped_excerpt(template.get('body'))}</blockquote>\n\n"
            f"{tr(language, 'Отправьте новый текст заготовки.', 'Send the new quick reply text.')}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "❌ Отмена", "❌ Cancel"),
                            callback_data="quick:input_cancel",
                        )
                    ]
                ]
            ),
        )

    @router.message(QuickReplyState.editing_body)
    async def quick_reply_edit_body_text(message: Message, state: FSMContext) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await state.clear()
            await deny_message(message)
            return
        body = str(message.text or "").strip()
        if not valid_chat_reply_body(body):
            await message.answer(
                tr(
                    language,
                    f"⚠️ Текст должен содержать от 1 до {CHAT_REPLY_BODY_LIMIT} символов.",
                    f"⚠️ The text must contain 1 to {CHAT_REPLY_BODY_LIMIT} characters.",
                )
            )
            return
        data = await state.get_data()
        template = db.update_quick_reply_template(int(data.get("quick_reply_template_id") or 0), body=body)
        await state.clear()
        if not template:
            await message.answer(tr(language, "⚠️ Заготовка не найдена.", "⚠️ Quick reply not found."))
            return
        await message.answer(
            f"✅ {tr(language, 'Текст обновлён.', 'Text updated.')}\n\n{quick_reply_summary(template, language)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=tr(language, "⚙️ Настройки", "⚙️ Settings"), callback_data=f"quick:view:{int(template['id'])}")]
                ]
            ),
        )

    @router.callback_query(F.data.startswith("quick:toggle:"))
    async def quick_reply_toggle(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        template_id = int((callback.data or "").rsplit(":", 1)[-1])
        current = db.get_quick_reply_template(template_id)
        if not current:
            await callback.answer(tr(language, "⚠️ Заготовка не найдена", "⚠️ Quick reply not found"), show_alert=True)
            return
        db.update_quick_reply_template(template_id, enabled=not bool(int(current.get("enabled") or 0)))
        await callback.answer(tr(language, "✅ Статус изменён", "✅ Status changed"))
        if callback.message:
            updated = db.get_quick_reply_template(template_id)
            enabled = bool(int((updated or {}).get("enabled") or 0))
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "✏️ Название", "✏️ Title"),
                            callback_data=f"quick:edit_title:{template_id}",
                        ),
                        InlineKeyboardButton(
                            text=tr(language, "📝 Текст", "📝 Text"),
                            callback_data=f"quick:edit_body:{template_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr(language, "⏸ Отключить", "⏸ Disable") if enabled else tr(language, "▶️ Включить", "▶️ Enable"),
                            callback_data=f"quick:toggle:{template_id}",
                        )
                    ],
                    [InlineKeyboardButton(text=tr(language, "🗑 Удалить", "🗑 Delete"), callback_data=f"quick:delete:{template_id}")],
                    [InlineKeyboardButton(text=tr(language, "⬅️ К списку", "⬅️ Back to list"), callback_data="menu:quick_replies")],
                ]
            )
            await callback.message.edit_text(quick_reply_summary(updated or current, language), reply_markup=markup)

    @router.callback_query(F.data.startswith("quick:delete:"))
    async def quick_reply_delete(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        template_id = int((callback.data or "").rsplit(":", 1)[-1])
        template = db.get_quick_reply_template(template_id)
        if not template:
            await callback.answer(tr(language, "⚠️ Заготовка не найдена", "⚠️ Quick reply not found"), show_alert=True)
            return
        await answer_or_edit(
            callback,
            f"🗑 <b>{tr(language, 'Удалить заготовку?', 'Delete quick reply?')}</b>\n\n"
            f"<code>{escape(str(template.get('title') or ''))}</code>\n\n"
            f"{tr(language, 'Это действие нельзя отменить.', 'This action cannot be undone.')}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "🗑 Да, удалить", "🗑 Yes, delete"),
                            callback_data=f"quick:delete_confirm:{template_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr(language, "⬅️ Отмена", "⬅️ Cancel"),
                            callback_data=f"quick:view:{template_id}",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("quick:delete_confirm:"))
    async def quick_reply_delete_confirm(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        template_id = int((callback.data or "").rsplit(":", 1)[-1])
        try:
            deleted = db.delete_quick_reply_template(template_id)
        except Exception as exc:
            await callback.answer(f"⚠️ {escape(str(exc))}", show_alert=True)
            return
        if not deleted:
            await callback.answer(tr(language, "⚠️ Заготовка уже удалена", "⚠️ Quick reply was already deleted"), show_alert=True)
            return
        await answer_or_edit(
            callback,
            tr(language, "🗑 Заготовка удалена.", "🗑 Quick reply deleted."),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=tr(language, "⬅️ К списку", "⬅️ Back to list"), callback_data="menu:quick_replies")]
                ]
            ),
        )

    @router.callback_query(F.data == "quick:input_cancel")
    async def quick_reply_input_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_admin(uid):
            await deny_callback(callback)
            return
        await state.clear()
        await answer_or_edit(
            callback,
            tr(language, "❌ Изменения отменены.", "❌ Changes cancelled."),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=tr(language, "⬅️ К списку", "⬅️ Back to list"), callback_data="menu:quick_replies")]
                ]
            ),
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

    def update_keyboard(data: dict[str, Any], language: str) -> InlineKeyboardMarkup:
        buttons = [[InlineKeyboardButton(text=tr(language, "🔍 Проверить", "🔍 Check"), callback_data="update:check")]]
        if data.get("update_supported") and data.get("update_available"):
            buttons.append([InlineKeyboardButton(text=tr(language, "⬇️ Обновить", "⬇️ Update"), callback_data="update:confirm")])
        buttons.append([InlineKeyboardButton(text=tr(language, "⬅️ Назад", "⬅️ Back"), callback_data="menu:home")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    async def show_update_status(callback: CallbackQuery | None, message: Message | None = None, *, force: bool = False) -> None:
        language = runtime_language(runtime)
        if not check_updates:
            text = "⚠️ " + tr(language, "Проверка обновлений недоступна.", "Update checking is unavailable.")
            markup = back_menu(language)
        else:
            data = await check_updates(force)
            text = update_status_text(data, language)
            markup = update_keyboard(data, language)
        if callback:
            await answer_or_edit(callback, text, markup)
        elif message:
            await message.answer(text, reply_markup=markup)

    @router.message(Command("update"))
    async def update_command(message: Message) -> None:
        uid = user_id(message)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_message(message)
            return
        await message.answer(tr(language, "🔍 Проверяю обновления...", "🔍 Checking for updates..."))
        await show_update_status(None, message, force=True)

    @router.callback_query(F.data == "menu:update")
    async def update_menu(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        await show_update_status(callback, force=False)

    @router.callback_query(F.data == "update:check")
    async def update_check(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        await show_update_status(callback, force=True)

    @router.callback_query(F.data == "update:confirm")
    async def update_confirm(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=tr(language, "✅ Да, обновить", "✅ Yes, update"), callback_data="update:start")],
                [InlineKeyboardButton(text=tr(language, "⬅️ Отмена", "⬅️ Cancel"), callback_data="menu:update")],
            ]
        )
        await answer_or_edit(
            callback,
            "⚠️ "
            + tr(
                language,
                "После запуска приложение обновится и перезапустится. Запустить обновление?",
                "After starting, the app will update and restart. Start update?",
            ),
            buttons,
        )

    @router.callback_query(F.data == "update:start")
    async def update_start(callback: CallbackQuery) -> None:
        uid = user_id(callback)
        language = runtime_language(runtime)
        if not is_owner(uid):
            await deny_callback(callback)
            return
        if not start_update:
            await callback.answer(tr(language, "⚠️ Обновление не настроено", "⚠️ Updates are not configured"), show_alert=True)
            return
        try:
            result = start_update()
        except Exception as exc:
            await callback.answer(f"⚠️ {escape(str(exc))}", show_alert=True)
            return
        await answer_or_edit(
            callback,
            f"⬇️ <b>{tr(language, 'Обновление запущено', 'Update started')}</b>\n"
            f"{tr(language, 'ID запроса', 'Request ID')}: <code>{escape(str(result.get('request_id') or ''))}</code>\n"
            f"{tr(language, 'Приложение перезапустится после завершения.', 'The app will restart after completion.')}",
            back_menu(language),
        )

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
        if UNIQUE_CODE_RE.fullmatch(text):
            await message.answer(
                tr(
                    language,
                    "🔒 Для безопасности отправьте уникальный код именно в чат заказа Plati.Market/Digiseller. Так я смогу проверить, что код относится к этой покупке.",
                    "🔒 For safety, send the unique code in the Plati.Market/Digiseller order chat. That lets me verify that the code belongs to that purchase.",
                )
            )
            return
        if not is_admin(user_id(message)):
            await message.answer(
                tr(
                    language,
                    "🔑 Уникальный код нужно отправить в чат заказа Plati.Market/Digiseller, не сюда.",
                    "🔑 Send the unique code in the Plati.Market/Digiseller order chat, not here.",
                )
            )

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
    messenger: MarketplaceMessenger | None = None,
    restart_bot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    check_updates: Callable[[bool], Awaitable[dict[str, Any]]] | None = None,
    start_update: Callable[[], dict[str, Any]] | None = None,
) -> None:
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher(
        db=db,
        xyranet=xyranet,
        digiseller=digiseller,
        runtime=runtime,
        messenger=messenger,
        restart_bot=restart_bot,
        check_updates=check_updates,
        start_update=start_update,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
