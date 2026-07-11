from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from reseller_autoseller import __version__
from reseller_autoseller.config import get_settings
from reseller_autoseller.chat_ui import chat_actions_reply_markup, format_chat_notification
from reseller_autoseller.db import Database
from reseller_autoseller.digiseller_client import (
    DigisellerApiError,
    RuntimeDigisellerClient,
    purchase_amount,
    purchase_buyer_email,
    purchase_currency,
    purchase_invoice_id,
    purchase_paid_at,
    purchase_product_id,
    purchase_variant_id,
    sale_event_from_unique_code,
    unique_code_state,
)
from reseller_autoseller.lot_parser import extract_product_id, is_allowed_lot_url, parse_lot_html
from reseller_autoseller.marketplaces import normalize_marketplace, normalize_sale
from reseller_autoseller.marketplace_chat import MarketplaceMessenger, RuntimeGgselClient
from reseller_autoseller.notifications import TelegramNotifier, compact_text, sale_title
from reseller_autoseller.runtime_config import (
    DEFAULT_ADMIN_SECRET_VALUES,
    MIN_ADMIN_PASSWORD_LENGTH,
    RuntimeConfig,
    RuntimeXyraNetClient,
)
from reseller_autoseller.services import (
    ACTION_LABELS,
    BASE_ACTION_LABELS,
    BUILTIN_COMPLEX_VARIABLES,
    DEFAULT_ACTION_TEMPLATES,
    DEFAULT_DELIVERY_TEMPLATE,
    DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS,
    DELIVERY_TEMPLATE_VARIABLES,
    DeliveryInProgressError,
    DeliveryService,
    MarketplaceMessageError,
    TEMPLATE_CATEGORIES,
    TEMPLATE_GROUPS,
    TEMPLATE_KEYS,
    delivery_template_key,
    extract_order_id_from_text,
    parse_chat_command,
)
from reseller_autoseller.statistics import build_sales_statistics
from reseller_autoseller.system_metrics import collect_system_metrics
from reseller_autoseller.telegram_bot import run_bot
from reseller_autoseller.updates import UpdateManager
from reseller_autoseller.xyra_client import XyraNetApiError


log = logging.getLogger(__name__)
UNIQUE_CODE_RE = re.compile(r"\b[A-Za-z0-9]{16}\b")
LOOSE_UNIQUE_CODE_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9\s-]{14,36}[A-Za-z0-9]\b")
ADMIN_SESSION_TTL_SECONDS = 24 * 60 * 60
LOGIN_RATE_LIMIT_MAX_FAILURES = 8
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
LOGIN_RATE_LIMIT_MAX_KEYS = 10000
DIGISELLER_NAIVE_DATETIME_ZONE = timezone(timedelta(hours=3), "MSK")


def digiseller_invoice_matches_chat(chat_invoice_id: str, code_invoice_id: str) -> bool:
    return bool(str(chat_invoice_id or "").strip() and str(code_invoice_id or "").strip()) and (
        str(chat_invoice_id).strip() == str(code_invoice_id).strip()
    )


class ProductMappingIn(BaseModel):
    marketplace: str = Field(min_length=1, max_length=32)
    external_product_id: str = Field(min_length=1, max_length=128)
    external_variant_id: str = Field(default="", max_length=128)
    action: str = Field(default="create", max_length=32)
    action_params: dict[str, Any] = Field(default_factory=dict)
    tariff_code: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    enabled: bool = True
    delivery_template: str = ""


class EnabledIn(BaseModel):
    enabled: bool


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class SettingsIn(BaseModel):
    settings: dict[str, Any]


class BotUserIn(BaseModel):
    telegram_id: int
    label: str = Field(default="", max_length=120)


class LotParseIn(BaseModel):
    source: str = Field(min_length=1, max_length=5000)


class CompletePendingIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)


class DeliveryTemplateIn(BaseModel):
    template: str = Field(default="", max_length=10000)


class ChatCommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=40)


class ComplexVariableIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    label: str = Field(default="", max_length=120)
    template: str = Field(default="", max_length=10000)


def create_app() -> FastAPI:
    settings = get_settings()
    db = Database(settings.database_file)
    db.init()
    runtime = RuntimeConfig(settings=settings, db=db)
    notifier = TelegramNotifier(runtime)
    xyranet = RuntimeXyraNetClient(runtime)
    digiseller = RuntimeDigisellerClient(runtime)
    ggsel = RuntimeGgselClient(runtime)
    update_manager = UpdateManager(settings=settings, db=db)
    messenger = MarketplaceMessenger(
        digiseller=digiseller,
        ggsel=ggsel,
        db=db,
    )
    delivery_service = DeliveryService(
        db=db,
        xyranet=xyranet,
        messenger=messenger,
        free_reissue_enabled=lambda: runtime.get_bool("free_reissue_enabled"),
    )
    bot_task: asyncio.Task[Any] | None = None
    chat_task: asyncio.Task[Any] | None = None
    daily_task: asyncio.Task[Any] | None = None
    update_check_task: asyncio.Task[Any] | None = None
    bot_lock = asyncio.Lock()
    bot_last_error = ""
    recent_notifications: dict[str, float] = {}
    recent_digiseller_blind_syncs: dict[str, float] = {}
    digiseller_chat_sync_locks: dict[str, asyncio.Lock] = {}
    poll_error_notifications: dict[str, float] = {}
    login_failures: dict[str, list[float]] = {}
    admin_sessions: dict[str, float] = {}
    digiseller_api_backoff_until = 0.0

    if not runtime.get_text("digiseller_notification_secret"):
        db.set_setting("digiseller_notification_secret", secrets.token_urlsafe(32))

    def notify_admins(
        text: str,
        kind: str = "errors",
        reply_markup: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> None:
        setting_key = f"notify_{kind}"
        if setting_key in {
            "notify_new_purchases",
            "notify_chat_messages",
            "notify_errors",
            "notify_pending",
            "notify_daily_statistics",
        } and not runtime.get_bool(setting_key):
            return
        now = time.monotonic()
        dedupe_window = 60 if kind == "chat_messages" else 20
        notification_key = f"{kind}:{dedupe_key or text}"
        last_sent = recent_notifications.get(notification_key, 0)
        if now - last_sent < dedupe_window:
            return
        recent_notifications[notification_key] = now
        if len(recent_notifications) > 300:
            cutoff = now - 3600
            for key, timestamp in list(recent_notifications.items()):
                if timestamp < cutoff:
                    recent_notifications.pop(key, None)

        async def runner() -> None:
            await notifier.send_admins(text, reply_markup=reply_markup)

        try:
            asyncio.create_task(runner())
        except RuntimeError:
            log.exception("Cannot schedule Telegram admin notification")

    def notify_chat_record(row: dict[str, Any]) -> None:
        notify_admins(
            format_chat_notification(row, runtime.language()),
            kind="chat_messages",
            reply_markup=chat_actions_reply_markup(int(row["id"]), row.get("external_order_id")),
            dedupe_key=f"chat-message:{row['id']}",
        )

    messenger.on_message = notify_chat_record

    def tr(ru: str, en: str) -> str:
        return en if runtime.language() == "en" else ru

    def action_label(action: str) -> str:
        labels_ru = {
            "create": "покупка",
            "renew": "продление",
            "reissue": "перевыпуск",
            "traffic": "LTE-трафик",
            "ip_limit": "IP-лимит",
        }
        labels_en = {
            "create": "purchase",
            "renew": "renewal",
            "reissue": "reissue",
            "traffic": "LTE traffic",
            "ip_limit": "IP limit",
        }
        labels = labels_en if runtime.language() == "en" else labels_ru
        return labels.get(action, action or tr("покупка", "purchase"))

    def sale_notification_text(result: dict[str, Any], *, source: str) -> str:
        sale = result.get("sale") or {}
        status = str(result.get("status") or "")
        action = "create"
        pending = result.get("pending") or {}
        if pending:
            action = str(pending.get("action") or action)
        product = db.get_product_by_external(
            str(sale.get("marketplace") or ""),
            str(sale.get("external_product_id") or ""),
            str(sale.get("external_variant_id") or ""),
        ) if sale else None
        if product:
            action = str(product.get("action") or action)
        lines = [
            f"🛒 <b>{tr('Новая покупка', 'New purchase')}</b>",
            f"{tr('Источник', 'Source')}: <b>{escape(source)}</b>",
            f"{tr('Заказ', 'Order')}: <code>{sale_title(sale)}</code>",
            f"{tr('Товар', 'Product')}: <code>{escape(str(sale.get('external_product_id') or ''))}</code>",
            f"{tr('Действие', 'Action')}: <b>{escape(action_label(action))}</b>",
        ]
        if sale.get("amount"):
            lines.append(f"{tr('Сумма', 'Amount')}: <b>{escape(str(sale.get('amount')))} {escape(str(sale.get('currency') or ''))}</b>")
        if status == "waiting_order_id":
            lines.append(f"{tr('Статус', 'Status')}: ⏳ {tr('ждём ID заказа', 'waiting for order ID')}")
        elif status == "delivered":
            delivery = result.get("delivery") or {}
            lines.append(f"{tr('Статус', 'Status')}: ✅ {tr('выдано', 'delivered')}")
            if delivery.get("xyranet_order_id"):
                lines.append(f"{tr('ID заказа', 'Order ID')}: <code>{escape(str(delivery['xyranet_order_id']))}</code>")
        elif status == "duplicate":
            lines.append(f"{tr('Статус', 'Status')}: ♻️ {tr('повтор, отправлена сохранённая выдача', 'duplicate, saved delivery was resent')}")
        else:
            lines.append(f"{tr('Статус', 'Status')}: <b>{escape(status)}</b>")
        return "\n".join(lines)

    def chat_message_notification(marketplace: str, external_order_id: str, text: str) -> str:
        return (
            f"💬 <b>{tr('Новое сообщение в чате', 'New chat message')}</b>\n"
            f"{tr('Площадка', 'Marketplace')}: <b>{escape(marketplace)}</b>\n"
            f"{tr('Заказ/чат', 'Order/chat')}: <code>{escape(external_order_id)}</code>\n"
            f"<pre>{escape(compact_text(text, 700))}</pre>"
        )

    def daily_statistics_text() -> str:
        data = build_sales_statistics(db.list_sales_for_statistics(), period="yesterday")
        totals = data["totals"]
        revenue = ", ".join(f"{item['text']} {item['currency']}" for item in totals.get("revenue", [])) or "0 ₽"
        period_label = "Yesterday" if runtime.language() == "en" else str(data["period"]["label"])
        return (
            f"📈 <b>{tr('Ежедневная статистика', 'Daily statistics')}</b>\n"
            f"{tr('Период', 'Period')}: <b>{escape(period_label)}</b>\n"
            f"{tr('Продаж', 'Sales')}: <b>{totals['sales_count']}</b> ({totals['delivered_count']} {tr('выдано', 'delivered')}, {totals['pending_count']} {tr('ждёт', 'pending')})\n"
            f"{tr('Сумма', 'Revenue')}: <b>{escape(revenue)}</b>\n"
            f"{tr('Расход', 'Expense')}: <b>{escape(str(totals['expense_rub']['text']))} ₽</b>\n"
            f"{tr('Прибыль', 'Profit')}: <b>{escape(str(totals['profit_rub']['text']))} ₽</b>"
        )

    async def daily_statistics_loop() -> None:
        last_sent = db.get_setting("_daily_statistics_last_sent") or ""
        while True:
            try:
                today = date.today().isoformat()
                if runtime.get_bool("notify_daily_statistics") and last_sent != today:
                    notify_admins(daily_statistics_text(), kind="daily_statistics")
                    last_sent = today
                    db.set_setting("_daily_statistics_last_sent", today)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Daily statistics notification failed")
            await asyncio.sleep(3600)

    async def update_check_loop() -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await update_manager.check(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Update check failed")
            await asyncio.sleep(3600)

    def message_id(message: dict[str, Any]) -> str:
        for key in ("id", "message_id", "id_d", "id_msg", "id_debate"):
            if message.get(key) not in (None, ""):
                return str(message[key])
        return ""

    def message_text(message: dict[str, Any]) -> str:
        for key in ("message", "text", "body", "content", "info"):
            if message.get(key):
                return str(message[key])
        return ""

    def message_is_from_buyer(message: dict[str, Any]) -> bool:
        seller_markers = ("seller", "is_seller", "from_seller", "seller_message")
        buyer_markers = ("buyer", "is_buyer", "from_buyer", "customer", "is_customer")
        lowered = {str(key).lower(): value for key, value in message.items()}
        for key in seller_markers:
            if key in lowered and str(lowered.get(key)).strip().lower() in {"1", "true", "yes"}:
                return False
        for key in buyer_markers:
            if key in lowered and str(lowered.get(key)).strip().lower() in {"1", "true", "yes"}:
                return True
        if any(key in lowered for key in (*seller_markers, *buyer_markers)):
            return False
        return True

    def message_action_signature(text: str) -> str:
        command = parse_chat_command(text)
        if command:
            return f"command:{command['command']}:{command['order_id']}"
        codes = unique_codes_from_text(text)
        if codes:
            return f"unique_code:{codes[0]}"
        return ""

    def pick_recursive(payload: Any, *keys: str) -> str:
        key_set = {key.lower() for key in keys}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key.lower() in key_set and value not in (None, ""):
                    return str(value).strip()
                found = pick_recursive(value, *keys)
                if found:
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = pick_recursive(item, *keys)
                if found:
                    return found
        return ""

    async def notification_payload(request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = {key: value for key, value in request.query_params.items()}
        if request.method.upper() != "POST":
            return payload
        content_type = request.headers.get("content-type", "").lower()
        try:
            if "application/json" in content_type:
                body = await request.json()
                if isinstance(body, dict):
                    payload.update(body)
            elif "application/x-www-form-urlencoded" in content_type:
                raw = (await request.body()).decode("utf-8", errors="replace")
                payload.update({key: value for key, value in parse_qsl(raw, keep_blank_values=True)})
            else:
                raw = (await request.body()).decode("utf-8", errors="replace").strip()
                if raw:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            payload.update(parsed)
                    except ValueError:
                        pass
        except Exception:
            log.exception("Cannot parse Digiseller notification body")
        return payload

    def payload_get(payload: dict[str, Any], *keys: str) -> str:
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
            value = lowered.get(key.lower())
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def notification_secret_ok(secret: str) -> bool:
        expected = runtime.get_text("digiseller_notification_secret")
        return bool(expected) and secrets.compare_digest(str(secret or ""), expected)

    def verify_notification_secret(secret: str) -> None:
        if not notification_secret_ok(secret):
            raise HTTPException(status_code=404, detail="Not found")

    def digiseller_base_notification_urls() -> tuple[str, str]:
        base_url = runtime.get_text("app_base_url").rstrip("/") or "http://127.0.0.1:8095"
        secret = runtime.get_text("digiseller_notification_secret")
        return (
            f"{base_url}/api/digiseller/notify/sale/{secret}",
            f"{base_url}/api/digiseller/notify/message/{secret}",
        )

    def mapped_plati_products_by_id(product_id: str) -> list[dict[str, Any]]:
        selected = str(product_id or "").strip()
        if not selected:
            return []
        products = [
            product
            for product in db.list_products()
            if str(product.get("marketplace") or "") in {"plati", "digiseller"}
            and str(product.get("external_product_id") or "") == selected
            and int(product.get("enabled", 0))
        ]
        return sorted(products, key=lambda product: str(product.get("marketplace") or "") != "plati")

    def sale_notification_signature_ok(payload: dict[str, Any], invoice_id: str, product_id: str) -> bool:
        if not runtime.get_bool("digiseller_validate_sale_sha256"):
            return True
        received = payload_get(payload, "sha256", "SHA256")
        if not received:
            return False
        secrets_to_try = [
            runtime.get_text("digiseller_notification_password"),
            runtime.get_text("digiseller_api_key"),
        ]
        for secret_value in dict.fromkeys(value.strip().lower() for value in secrets_to_try if value.strip()):
            expected = hashlib.sha256(f"{secret_value};{invoice_id};{product_id}".encode("utf-8")).hexdigest()
            if secrets.compare_digest(received.lower(), expected.lower()):
                return True
        return False

    def event_payload(event: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(str(event.get("payload") or "{}"))
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def chat_invoice_id(chat: dict[str, Any]) -> str:
        for key in ("id_i", "invoice_id", "inv", "order_id"):
            if chat.get(key) not in (None, ""):
                return str(chat[key])
        return ""

    def ggsel_order_id(payload: dict[str, Any]) -> str:
        return pick_recursive(
            payload,
            "invoice_id",
            "invoiceId",
            "invoice",
            "order_id",
            "orderId",
            "purchase_id",
            "purchaseId",
            "id",
        )

    def ggsel_order_error_is_permanent(exc: Exception) -> bool:
        text = str(exc).lower()
        return bool(re.search(r"\b(?:400|404|410)\b", text)) or any(
            marker in text for marker in ("not found", "не найден", "invalid order", "unknown order")
        )

    def merge_sale_payload(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        detail_data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
        result = {**summary, **detail, **detail_data}
        result["raw_sale"] = summary
        result["raw_order"] = detail
        return result

    def digiseller_polling_configured() -> bool:
        return bool(runtime.get_text("digiseller_seller_id") and runtime.get_text("digiseller_api_key"))

    def digiseller_unique_code_requests_enabled() -> bool:
        return bool(digiseller_polling_configured() and runtime.get_bool("digiseller_unique_code_request_enabled"))

    def digiseller_unique_code_request_delay() -> timedelta:
        try:
            minutes = runtime.get_float("digiseller_unique_code_request_delay_minutes")
        except Exception:
            minutes = 5.0
        return timedelta(minutes=max(0.0, min(minutes, 24 * 60)))

    def marketplace_messages_configured(marketplace: str) -> bool:
        if marketplace in {"plati", "digiseller"}:
            return digiseller_polling_configured()
        if marketplace == "ggsel":
            return ggsel.configured_for_polling()
        return False

    def digiseller_api_paused() -> bool:
        return time.monotonic() < digiseller_api_backoff_until

    def note_digiseller_api_error(exc: Exception) -> None:
        nonlocal digiseller_api_backoff_until
        text = str(exc).lower()
        if "quota exceeded" in text or "слишком много запросов" in text:
            digiseller_api_backoff_until = max(digiseller_api_backoff_until, time.monotonic() + 60 * 60)

    def log_poll_error(key: str, message: str, exc: Exception | None = None) -> None:
        if exc is not None and key.startswith("digiseller"):
            note_digiseller_api_error(exc)
        now = time.monotonic()
        last = poll_error_notifications.get(key, 0)
        if now - last < 300:
            return
        poll_error_notifications[key] = now
        log.exception(message)

    def sale_chat_id(sale: dict[str, Any]) -> str:
        if str(sale.get("marketplace") or "") in {"plati", "digiseller"}:
            try:
                payload = json.loads(str(sale.get("raw_payload") or "{}"))
            except ValueError:
                payload = {}
            for key in ("inv", "id_i", "invoice_id", "order_id"):
                if payload.get(key) not in (None, ""):
                    return str(payload[key])
        return str(sale.get("external_order_id") or "")

    def unique_codes_from_text(text: str) -> list[str]:
        found: list[str] = []
        for match in UNIQUE_CODE_RE.finditer(text):
            found.append(match.group(0))
        for match in LOOSE_UNIQUE_CODE_RE.finditer(text):
            normalized = re.sub(r"[^A-Za-z0-9]", "", match.group(0))
            if len(normalized) == 16:
                found.append(normalized)
        return list(dict.fromkeys(found))

    def parse_datetime(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        for candidate in (normalized, normalized.replace(" ", "T", 1)):
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=DIGISELLER_NAIVE_DATETIME_ZONE).astimezone(timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=DIGISELLER_NAIVE_DATETIME_ZONE).astimezone(timezone.utc)
            except ValueError:
                pass
        return None

    def event_created_at(event: dict[str, Any]) -> datetime | None:
        return parse_datetime(event.get("created_at"))

    def sale_paid_at(sale: dict[str, Any], purchase: dict[str, Any]) -> datetime | None:
        return parse_datetime(purchase_paid_at(purchase)) or parse_datetime(
            pick_recursive(sale, "date_pay", "purchase_date", "date", "created_at")
        )

    def event_recent(events: list[dict[str, Any]], event_types: set[str], delay: timedelta) -> bool:
        now = datetime.now(timezone.utc)
        for event in events:
            if str(event.get("event_type") or "") not in event_types:
                continue
            created_at = event_created_at(event)
            if not created_at or now - created_at < delay:
                return True
        return False

    def unique_code_request_context(invoice_id: str, purchase: dict[str, Any], product: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "marketplace_order_id": invoice_id,
            "product_id": purchase_product_id(purchase),
            "product_title": str((product or {}).get("title") or pick_recursive(purchase, "name", "title") or ""),
            "buyer_email": purchase_buyer_email(purchase),
            "purchase_amount": purchase_amount(purchase),
            "purchase_currency": purchase_currency(purchase),
            "unique_code_state": str(unique_code_state(purchase) or ""),
        }

    def mapped_plati_product(purchase: dict[str, Any]) -> dict[str, Any] | None:
        product_id = purchase_product_id(purchase)
        if not product_id:
            return None
        return db.get_product_by_external("plati", product_id, purchase_variant_id(purchase))

    def mapped_plati_product_ids() -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for product in db.list_products():
            if str(product.get("marketplace") or "") not in {"plati", "digiseller"} or not int(product.get("enabled", 0)):
                continue
            try:
                product_id = int(str(product.get("external_product_id") or "").strip())
            except ValueError:
                continue
            if product_id not in seen:
                result.append(product_id)
                seen.add(product_id)
        return result

    def is_paid_digiseller_purchase(purchase: dict[str, Any]) -> bool:
        invoice_state = pick_recursive(purchase, "invoice_state")
        if not invoice_state:
            return True
        try:
            return int(invoice_state) == 3
        except ValueError:
            return False

    def should_request_unique_code(invoice_id: str, sale: dict[str, Any], purchase: dict[str, Any]) -> tuple[bool, str]:
        if not is_paid_digiseller_purchase(purchase):
            return False, "invoice is not paid"
        if unique_code_state(purchase) != 1:
            return False, "unique code state is not waiting for verification"
        if db.digiseller_invoice_has_delivery(invoice_id):
            return False, "invoice already has saved delivery"
        product = mapped_plati_product(purchase)
        if not product:
            return False, "mapped product was not found"
        if not int(product.get("enabled", 0)):
            return False, "mapped product is disabled"
        paid_at = sale_paid_at(sale, purchase)
        now = datetime.now(timezone.utc)
        if paid_at:
            age = now - paid_at
            if age < digiseller_unique_code_request_delay():
                return False, "payment is too recent"
            if age > timedelta(hours=72):
                return False, "payment is older than reminder window"
        events = db.list_order_events(marketplace="plati", external_order_id=invoice_id, limit=50)
        if any(str(event.get("event_type") or "") == "unique_code_request_sent" and str(event.get("status") or "") == "success" for event in events):
            return False, "request was already sent"
        retry_delay = max(digiseller_unique_code_request_delay(), timedelta(minutes=30))
        if event_recent(events, {"unique_code_request_failed"}, retry_delay):
            return False, "request failed recently"
        if not paid_at:
            if event_recent(events, {"unique_code_request_candidate_seen"}, digiseller_unique_code_request_delay()):
                return False, "candidate was seen recently"
            if not any(str(event.get("event_type") or "") == "unique_code_request_candidate_seen" for event in events):
                db.add_order_event(
                    marketplace="plati",
                    external_order_id=invoice_id,
                    event_type="unique_code_request_candidate_seen",
                    payload={"product_id": purchase_product_id(purchase), "variant_id": purchase_variant_id(purchase)},
                )
                return False, "candidate was recorded for delayed request"
        return True, "ok"

    def sale_notification_purchase(invoice_id: str, payload: dict[str, Any], product_id: str) -> dict[str, Any]:
        return {
            "content": {
                "id_i": invoice_id,
                "item_id": product_id,
                "amount": payload.get("amount"),
                "currency_type": payload.get("currency"),
                "date_pay": payload.get("date"),
                "buyer_info": {"email": payload.get("email")},
                "unique_code_state": {"state": 1},
            }
        }

    async def request_unique_code_from_sale_notification(invoice_id: str, payload: dict[str, Any], *, event_id: int) -> bool:
        if not runtime.get_bool("digiseller_unique_code_request_enabled"):
            return False
        if db.digiseller_invoice_has_delivery(invoice_id):
            return False
        events = db.list_order_events(marketplace="plati", external_order_id=invoice_id, limit=100)
        if any(str(event.get("event_type") or "") == "unique_code_seen" for event in events):
            return False
        if any(str(event.get("event_type") or "") == "unique_code_request_sent" and str(event.get("status") or "") == "success" for event in events):
            return False
        retry_delay = max(digiseller_unique_code_request_delay(), timedelta(minutes=30))
        if event_recent(events, {"unique_code_request_failed"}, retry_delay):
            return False
        product_id = str(payload.get("product_id") or "").strip()
        mapped_products = mapped_plati_products_by_id(product_id)
        if not product_id or not mapped_products:
            return False
        product = mapped_products[0]
        purchase = sale_notification_purchase(invoice_id, payload, product_id)
        text = delivery_service.render_system_text(
            "request_unique_code",
            unique_code_request_context(invoice_id, purchase, product),
        )

        if await messenger.send_message("plati", invoice_id, text):
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="unique_code_request_sent",
                status="success",
                payload={
                    "product_id": product_id,
                    "source": "digiseller_sale_notification",
                    "sale_notification_event_id": event_id,
                },
            )
            notify_admins(
                f"📨 <b>{tr('Запрошен уникальный код Digiseller', 'Digiseller unique code requested')}</b>\n"
                f"{tr('Источник', 'Source')}: <b>URL notification</b>\n"
                f"{tr('Заказ', 'Order')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('Товар', 'Product')}: <code>{escape(product_id)}</code>",
                kind="pending",
            )
            return True
        db.add_order_event(
            marketplace="plati",
            external_order_id=invoice_id,
            event_type="unique_code_request_failed",
            status="error",
            message="Cannot send unique code request to Digiseller chat",
            payload={
                "product_id": product_id,
                "source": "digiseller_sale_notification",
                "sale_notification_event_id": event_id,
            },
        )
        return False

    async def process_digiseller_sale_notification_reminders() -> None:
        if not runtime.get_bool("digiseller_sale_notifications_enabled"):
            return
        if not runtime.get_bool("digiseller_unique_code_request_enabled"):
            return
        delay = digiseller_unique_code_request_delay()
        now = datetime.now(timezone.utc)
        processed: set[str] = set()
        events = sorted(
            [
                event
                for event in db.list_order_events(marketplace="plati", limit=1000)
                if str(event.get("event_type") or "") == "digiseller_sale_notification_received"
            ],
            key=lambda event: int(event.get("id") or 0),
        )
        for event in events:
            invoice_id = str(event.get("external_order_id") or "").strip()
            if not invoice_id or invoice_id in processed:
                continue
            processed.add(invoice_id)
            created_at = event_created_at(event)
            if created_at and now - created_at < delay:
                continue
            await request_unique_code_from_sale_notification(
                invoice_id,
                event_payload(event),
                event_id=int(event.get("id") or 0),
            )

    async def mark_unique_code_delivery(
        *,
        invoice_id: str,
        code: str,
        purchase: dict[str, Any],
        event_external_order_id: str,
        sale_id: int | None,
        pending_operation_id: int | None = None,
    ) -> bool:
        events = db.list_order_events(
            marketplace="plati",
            external_order_id=event_external_order_id,
            limit=100,
        )
        if any(
            str(item.get("event_type") or "") == "unique_code_marked_delivered"
            and event_payload(item).get("unique_code") == code
            for item in events
        ):
            return True
        try:
            # States 2/3/4 already mean Digiseller has a delivery record. This
            # also closes the crash window after a successful API call but
            # before our local success event was committed.
            if unique_code_state(purchase) not in {2, 3, 4}:
                await digiseller.mark_unique_code_delivered(code)
        except Exception as exc:
            db.add_order_event(
                marketplace="plati",
                external_order_id=event_external_order_id,
                sale_id=sale_id,
                pending_operation_id=pending_operation_id,
                event_type="unique_code_mark_delivery_failed",
                status="error",
                message=str(exc),
                payload={"unique_code": code, "invoice_id": invoice_id},
            )
            notify_admins(
                f"⚠️ <b>{tr('Выдача отправлена, но статус кода Digiseller не обновлён', 'Delivery sent, but Digiseller code status was not updated')}</b>\n"
                f"{tr('Заказ', 'Order')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>",
                kind="errors",
            )
            return False
        db.add_order_event(
            marketplace="plati",
            external_order_id=event_external_order_id,
            sale_id=sale_id,
            pending_operation_id=pending_operation_id,
            event_type="unique_code_marked_delivered",
            status="success",
            payload={"unique_code": code},
        )
        return True

    def message_role(message: dict[str, Any]) -> str:
        seller_markers = ("seller", "is_seller", "from_seller", "seller_message")
        buyer_markers = ("buyer", "is_buyer", "from_buyer", "customer", "is_customer")
        lowered = {str(key).lower(): value for key, value in message.items()}
        for key in seller_markers:
            if key in lowered and str(lowered.get(key)).strip().lower() in {"1", "true", "yes"}:
                return "seller"
        for key in buyer_markers:
            if key in lowered and str(lowered.get(key)).strip().lower() in {"1", "true", "yes"}:
                return "buyer"
        if any(key in lowered for key in (*seller_markers, *buyer_markers)):
            return "system"
        return "buyer"

    def save_remote_chat_message(
        *,
        marketplace: str,
        external_order_id: str,
        payload: dict[str, Any],
        text: str = "",
        remote_id: str = "",
        remote_date: str = "",
        source: str,
    ) -> tuple[dict[str, Any], bool]:
        selected_text = str(text or message_text(payload)).strip()
        selected_id = str(remote_id or message_id(payload)).strip()
        selected_date = str(
            remote_date
            or payload_get(payload, "date_written", "MessageDate", "message_date", "date")
            or ""
        ).strip()
        selected_role = message_role(payload)
        recent_outgoing = db.find_recent_chat_message_by_text(
            marketplace,
            external_order_id,
            selected_text,
        )
        if recent_outgoing and selected_role != "buyer":
            return recent_outgoing, False
        if selected_id:
            selected_key = f"remote:{selected_id}"
        else:
            fingerprint = "\x1f".join(
                (source, marketplace, external_order_id, selected_role, selected_date, selected_text)
            )
            selected_key = "event:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        row, created = db.add_chat_message(
            marketplace=marketplace,
            external_order_id=external_order_id,
            role=selected_role,
            text=selected_text,
            external_message_id=selected_id,
            message_key=selected_key,
            source=source,
            message_date=selected_date,
            is_file=payload_get(payload, "is_file", "IsFile").lower() in {"1", "true", "yes"},
            file_name=payload_get(payload, "filename", "FileName"),
            file_url=payload_get(payload, "url", "FileUrl", "file_url"),
            raw_payload={
                key: payload.get(key)
                for key in ("buyer", "seller", "deleted", "is_file", "filename", "url")
                if key in payload
            },
        )
        if created:
            notify_chat_record(row)
        return row, created

    async def process_unique_code_message(invoice_id: str, code: str) -> bool:
        db.add_order_event(
            marketplace="plati",
            external_order_id=invoice_id,
            event_type="unique_code_seen",
            payload={"unique_code": code},
        )
        try:
            purchase = await digiseller.purchase_by_unique_code(code)
            code_invoice_id = purchase_invoice_id(purchase)
            if not digiseller_invoice_matches_chat(invoice_id, code_invoice_id):
                db.add_order_event(
                    marketplace="plati",
                    external_order_id=invoice_id,
                    event_type="unique_code_invoice_mismatch",
                    status="warning",
                    message="Unique code belongs to another Digiseller invoice",
                    payload={
                        "unique_code": code,
                        "chat_invoice_id": invoice_id,
                        "code_invoice_id": code_invoice_id,
                        "id_goods": purchase.get("id_goods"),
                    },
                )
                await messenger.send_message(
                    "plati",
                    invoice_id,
                    delivery_service.render_system_text(
                        "unique_code_invoice_mismatch",
                        {
                            "marketplace_order_id": invoice_id,
                            "code_order_id": code_invoice_id,
                            "product_id": str(purchase.get("id_goods") or ""),
                        },
                    ),
                )
                notify_admins(
                    f"⚠️ <b>{tr('Уникальный код не от этого заказа', 'Unique code belongs to another order')}</b>\n"
                    f"{tr('Чат', 'Chat')}: <code>{escape(invoice_id)}</code>\n"
                    f"{tr('Заказ кода', 'Code order')}: <code>{escape(code_invoice_id or tr('не определён', 'unknown'))}</code>",
                    kind="pending",
                )
                return True
            event = sale_event_from_unique_code(purchase, code)
            db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                event_type="unique_code_verified",
                payload={"invoice_id": invoice_id, "unique_code": code, "id_goods": purchase.get("id_goods")},
            )
            existing = db.get_sale_with_delivery(event.marketplace, event.external_order_id)
            if not existing and purchase.get("inv") not in (None, ""):
                existing = db.get_sale_with_delivery(event.marketplace, str(purchase["inv"]))
            if existing and existing.get("delivery_id"):
                await delivery_service.ensure_delivery_message_sent(
                    existing,
                    marketplace="plati",
                    external_order_id=invoice_id,
                    sale_id=int(existing["id"]),
                )
                await mark_unique_code_delivery(
                    invoice_id=invoice_id,
                    code=code,
                    purchase=purchase,
                    event_external_order_id=event.external_order_id,
                    sale_id=int(existing["id"]),
                )
                return True
            result = await delivery_service.handle_sale(event, notify_marketplace=True)
            notify_admins(sale_notification_text(result, source="Digiseller chat"), kind="new_purchases")
            if result.get("status") in {"delivered", "waiting_order_id"}:
                await mark_unique_code_delivery(
                    invoice_id=invoice_id,
                    code=code,
                    purchase=purchase,
                    event_external_order_id=event.external_order_id,
                    sale_id=int(result["sale"]["id"]) if result.get("sale") else None,
                    pending_operation_id=int(result["pending"]["id"]) if result.get("pending") else None,
                )
            return True
        except DigisellerApiError as exc:
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="unique_code_verify_failed",
                status="error",
                message=str(exc),
                payload={"unique_code": code},
            )
            await messenger.send_message(
                "plati",
                invoice_id,
                f"⚠️ Не удалось проверить уникальный код Digiseller: {exc}",
            )
            notify_admins(
                f"⚠️ <b>{tr('Ошибка проверки уникального кода', 'Unique code verification error')}</b>\n"
                f"{tr('Чат', 'Chat')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('Код', 'Code')}: <code>{escape(code)}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>"
            , kind="errors")
            return True
        except Exception as exc:
            log.exception("Cannot process Digiseller unique code from chat %s", invoice_id)
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="delivery_failed",
                status="error",
                message=str(exc),
                payload={"unique_code": code},
            )
            await messenger.send_message(
                "plati",
                invoice_id,
                f"⚠️ Не удалось выдать доступ по коду: {exc}",
            )
            notify_admins(
                f"🚨 <b>{tr('Ошибка выдачи по коду', 'Code delivery error')}</b>\n"
                f"{tr('Чат', 'Chat')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('Код', 'Code')}: <code>{escape(code)}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>"
            , kind="errors")
            return True

    def pending_operation_for_chat(marketplace: str, external_order_id: str) -> dict[str, Any] | None:
        for operation in db.list_pending_operations():
            if str(operation["marketplace"]) == marketplace and str(operation["external_order_id"]) == external_order_id:
                return operation
        return None

    def order_ownership_error_text() -> str:
        return tr(
            "⚠️ Не удалось подтвердить этот ID заказа для текущего чата. Проверьте ID и попробуйте ещё раз.",
            "⚠️ This order ID could not be verified for the current chat. Check the ID and try again.",
        )

    async def process_free_reissue_command(invoice_id: str, text: str, message_key: str = "") -> bool:
        command = parse_chat_command(text)
        if not command:
            return False
        if command["command"] != delivery_service.expected_command("reissue"):
            return False
        pending = pending_operation_for_chat("plati", invoice_id)
        if pending and str(pending["action"]) == "reissue":
            return False
        if not delivery_service.is_free_reissue_enabled():
            await messenger.send_message("plati", invoice_id, delivery_service.render_system_text("free_reissue_disabled"))
            return True
        if not command["order_id"]:
            await messenger.send_message("plati", invoice_id, delivery_service.render_system_text("free_reissue_help"))
            return True
        if not db.marketplace_chat_owns_order("plati", invoice_id, command["order_id"]):
            await messenger.send_message("plati", invoice_id, order_ownership_error_text())
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="free_reissue_ownership_rejected",
                status="warning",
                payload={"target_order_id": command["order_id"]},
            )
            return True
        try:
            result = await delivery_service.free_reissue(
                command["order_id"],
                idempotency_key=f"plati:{invoice_id}:free-reissue:{message_key or command['order_id']}",
            )
            if not await messenger.send_message("plati", invoice_id, str(result["delivery_text"])):
                raise MarketplaceMessageError("Cannot send free reissue result to marketplace chat")
            notify_admins(
                f"🔄 <b>{tr('Бесплатный перевыпуск', 'Free reissue')}</b>\n"
                f"{tr('Чат', 'Chat')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('ID заказа', 'Order ID')}: <code>{escape(command['order_id'])}</code>\n"
                f"{tr('Статус', 'Status')}: ✅ {tr('выполнено', 'completed')}"
            , kind="pending")
            return True
        except MarketplaceMessageError:
            raise
        except Exception as exc:
            log.exception("Cannot process free reissue command from chat %s", invoice_id)
            await messenger.send_message("plati", invoice_id, delivery_service.operation_error_text("reissue", exc))
            notify_admins(
                f"🚨 <b>{tr('Ошибка бесплатного перевыпуска', 'Free reissue error')}</b>\n"
                f"{tr('Чат', 'Chat')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('ID заказа', 'Order ID')}: <code>{escape(command['order_id'])}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>"
            , kind="errors")
            return True

    async def process_subscription_status_command(marketplace: str, chat_id: str, text: str) -> bool:
        command = parse_chat_command(text)
        if not command:
            return False
        if command["command"] != delivery_service.expected_command("status"):
            return False
        if not command["order_id"]:
            await messenger.send_message(
                marketplace,
                chat_id,
                delivery_service.render_system_text("status_help", delivery_service.command_context("status")),
            )
            return True
        if not db.marketplace_chat_owns_order(marketplace, chat_id, command["order_id"]):
            await messenger.send_message(marketplace, chat_id, order_ownership_error_text())
            db.add_order_event(
                marketplace=marketplace,
                external_order_id=chat_id,
                event_type="subscription_status_ownership_rejected",
                status="warning",
                payload={"target_order_id": command["order_id"]},
            )
            return True
        try:
            result = await delivery_service.subscription_status(command["order_id"])
            if not await messenger.send_message(marketplace, chat_id, str(result["delivery_text"])):
                raise MarketplaceMessageError("Cannot send subscription status to marketplace chat")
            db.add_order_event(
                marketplace=marketplace,
                external_order_id=chat_id,
                event_type="subscription_status_sent",
                status="success",
                payload={"target_order_id": command["order_id"]},
            )
            return True
        except MarketplaceMessageError:
            raise
        except Exception as exc:
            log.exception("Cannot process subscription status command from chat %s:%s", marketplace, chat_id)
            await messenger.send_message(
                marketplace,
                chat_id,
                delivery_service.render_system_text(
                    "status_error",
                    {**delivery_service.command_context("status"), "order_id": command["order_id"], "error": str(exc)},
                ),
            )
            db.add_order_event(
                marketplace=marketplace,
                external_order_id=chat_id,
                event_type="subscription_status_failed",
                status="error",
                message=str(exc),
                payload={"target_order_id": command["order_id"]},
            )
            notify_admins(
                f"🚨 <b>{tr('Ошибка статуса подписки', 'Subscription status error')}</b>\n"
                f"{tr('Чат', 'Chat')}: <code>{escape(marketplace)}:{escape(chat_id)}</code>\n"
                f"{tr('ID заказа', 'Order ID')}: <code>{escape(command['order_id'])}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>",
                kind="errors",
            )
            return True

    async def process_pending_operation_message(marketplace: str, chat_id: str, text: str) -> bool:
        operation = pending_operation_for_chat(marketplace, chat_id)
        if not operation:
            return False
        command = parse_chat_command(text)
        found_order_id = ""
        if command:
            if command["command"] == delivery_service.expected_command("status"):
                return False
            expected_action = str(operation["action"])
            if command["command"] != delivery_service.expected_command(expected_action):
                command_action = delivery_service.action_for_command(command["command"]) or command["action"]
                wrong_command_action = command_action or command["command"]
                db.add_order_event(
                    marketplace=str(operation["marketplace"]),
                    external_order_id=str(operation["external_order_id"]),
                    sale_id=int(operation["sale_id"]),
                    pending_operation_id=int(operation["id"]),
                    event_type="pending_wrong_command",
                    status="warning",
                    message=wrong_command_action,
                    payload={"expected_action": operation["action"], "source": "digiseller_message_notification"},
                )
                await messenger.send_message(
                    str(operation["marketplace"]),
                    str(operation["external_order_id"]),
                    delivery_service.command_mismatch_text(str(operation["action"]), wrong_command_action),
                )
                return True
            found_order_id = command["order_id"]
            if not found_order_id:
                db.add_order_event(
                    marketplace=str(operation["marketplace"]),
                    external_order_id=str(operation["external_order_id"]),
                    sale_id=int(operation["sale_id"]),
                    pending_operation_id=int(operation["id"]),
                    event_type="pending_missing_order_id",
                    status="warning",
                    payload={"action": operation["action"], "source": "digiseller_message_notification"},
                )
                await messenger.send_message(
                    str(operation["marketplace"]),
                    str(operation["external_order_id"]),
                    delivery_service.ask_order_id_text(str(operation["action"])),
                )
                return True
        else:
            found_order_id = extract_order_id_from_text(text)
        if not found_order_id:
            return False
        try:
            db.add_order_event(
                marketplace=str(operation["marketplace"]),
                external_order_id=str(operation["external_order_id"]),
                sale_id=int(operation["sale_id"]),
                pending_operation_id=int(operation["id"]),
                event_type="pending_order_id_received",
                payload={"target_order_id": found_order_id, "action": operation["action"], "source": "digiseller_message_notification"},
            )
            result = await delivery_service.complete_pending_operation(operation, found_order_id)
            notify_admins(
                f"✅ <b>{tr('Услуга применена', 'Service applied')}</b>\n"
                f"{tr('Заказ', 'Order')}: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                f"{tr('Действие', 'Action')}: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                f"{tr('ID заказа', 'Order ID')}: <code>{escape(found_order_id)}</code>\n"
                f"{tr('Статус', 'Status')}: <b>{escape(str(result.get('status') or 'delivered'))}</b>",
                kind="pending",
            )
            return True
        except DeliveryInProgressError:
            return True
        except Exception as exc:
            db.add_order_event(
                marketplace=str(operation["marketplace"]),
                external_order_id=str(operation["external_order_id"]),
                sale_id=int(operation["sale_id"]),
                pending_operation_id=int(operation["id"]),
                event_type="pending_failed",
                status="error",
                message=str(exc),
                payload={"target_order_id": found_order_id, "action": operation["action"], "source": "digiseller_message_notification"},
            )
            await messenger.send_message(
                str(operation["marketplace"]),
                str(operation["external_order_id"]),
                delivery_service.operation_error_text(str(operation["action"]), exc),
            )
            notify_admins(
                f"🚨 <b>{tr('Ошибка pending-операции', 'Pending operation error')}</b>\n"
                f"{tr('Заказ', 'Order')}: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                f"{tr('Действие', 'Action')}: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                f"{tr('ID заказа', 'Order ID')}: <code>{escape(found_order_id)}</code>\n"
                f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>",
                kind="errors",
            )
            return True

    async def process_digiseller_chat_messages(invoice_id: str, *, raise_on_read_error: bool = False) -> bool:
        if digiseller_api_paused():
            return False
        last_seen = db.get_chat_cursor("plati", invoice_id)
        try:
            messages = await digiseller.order_messages(
                invoice_id,
                count=50,
                newer=bool(last_seen),
                old_id=last_seen,
            )
        except Exception as exc:
            log_poll_error("digiseller_messages", f"Cannot read Digiseller messages for {invoice_id}", exc)
            if raise_on_read_error:
                raise
            return False
        if not messages and last_seen:
            try:
                await digiseller.mark_order_messages_seen(invoice_id)
            except Exception as exc:
                log_poll_error("digiseller_seen", f"Cannot mark Digiseller chat as seen for {invoice_id}", exc)
            return False
        cursor_to_save = last_seen
        handled = False
        handled_signature = ""
        # Persist and notify every returned message first. Business processing
        # may deliberately stop at one actionable buyer command, but that must
        # never truncate the locally saved conversation.
        for message in messages:
            text = message_text(message)
            if text or message.get("is_file"):
                save_remote_chat_message(
                    marketplace="plati",
                    external_order_id=invoice_id,
                    payload=message,
                    text=text,
                    remote_id=message_id(message),
                    source="digiseller_api",
                )
        for message in messages:
            current_id = message_id(message)
            role = message_role(message)
            text = message_text(message)
            signature = message_action_signature(text) if text else ""
            if handled:
                if signature and signature == handled_signature:
                    if current_id:
                        cursor_to_save = current_id
                    continue
                break
            if current_id:
                cursor_to_save = current_id
            if role != "buyer":
                continue
            for code in unique_codes_from_text(text):
                handled = await process_unique_code_message(invoice_id, code)
                if handled:
                    handled_signature = signature or f"unique_code:{code}"
                    break
            if handled:
                continue
            handled = await process_subscription_status_command("plati", invoice_id, text)
            if handled:
                handled_signature = signature
                continue
            handled = await process_free_reissue_command(invoice_id, text, current_id)
            if handled:
                handled_signature = signature
                continue
            handled = await process_pending_operation_message("plati", invoice_id, text)
            if handled:
                handled_signature = signature
                continue
        if cursor_to_save and cursor_to_save != last_seen:
            db.set_chat_cursor("plati", invoice_id, cursor_to_save)
        if handled:
            try:
                await digiseller.mark_order_messages_seen(invoice_id)
            except Exception as exc:
                log_poll_error("digiseller_seen", f"Cannot mark Digiseller chat as seen for {invoice_id}", exc)
        return handled

    async def poll_digiseller_unique_code_chats() -> None:
        if (
            not digiseller_polling_configured()
            or digiseller_api_paused()
            or not runtime.get_bool("digiseller_polling_fallback_enabled")
        ):
            return
        try:
            chats = await digiseller.order_chats(filter_new=True, rows=100)
        except Exception as exc:
            log_poll_error("digiseller_chats", "Cannot read Digiseller unread chats", exc)
            chats = []
        for chat in chats:
            invoice_id = chat_invoice_id(chat)
            if not invoice_id:
                continue
            await process_digiseller_chat_messages(invoice_id)

    async def poll_digiseller_unclaimed_unique_code_sales() -> None:
        if (
            not digiseller_unique_code_requests_enabled()
            or digiseller_api_paused()
            or not runtime.get_bool("digiseller_polling_fallback_enabled")
        ):
            return
        now = datetime.now(DIGISELLER_NAIVE_DATETIME_ZONE)
        start = now - timedelta(hours=72)
        try:
            sales = await digiseller.seller_sales(
                date_start=start.strftime("%Y-%m-%d %H:%M:%S"),
                date_finish=now.strftime("%Y-%m-%d %H:%M:%S"),
                product_ids=mapped_plati_product_ids(),
                rows=100,
            )
        except Exception as exc:
            log_poll_error("digiseller_seller_sales", "Cannot read Digiseller seller sales", exc)
            return
        if not sales:
            return
        for sale in sales:
            invoice_id = pick_recursive(sale, "inv", "id_i", "invoice_id", "invoice", "order_id")
            if not invoice_id:
                continue
            if db.digiseller_invoice_has_delivery(invoice_id):
                continue
            try:
                purchase = await digiseller.purchase_info(invoice_id)
            except Exception as exc:
                log_poll_error("digiseller_purchase_info", f"Cannot read Digiseller purchase info for {invoice_id}", exc)
                continue
            if not purchase_product_id(purchase):
                content = dict(purchase.get("content") if isinstance(purchase.get("content"), dict) else {})
                product_id = pick_recursive(sale, "id_goods", "item_id", "product_id", "goods_id")
                if product_id:
                    content["item_id"] = product_id
                purchase = {**purchase, "content": content}
            product = mapped_plati_product(purchase)
            if (
                is_paid_digiseller_purchase(purchase)
                and unique_code_state(purchase) == 1
                and not db.digiseller_invoice_has_delivery(invoice_id)
                and product
                and int(product.get("enabled", 0))
                and await process_digiseller_chat_messages(invoice_id)
            ):
                continue
            should_send, reason = should_request_unique_code(invoice_id, sale, purchase)
            if not should_send:
                if reason not in {
                    "unique code state is not waiting for verification",
                    "mapped product was not found",
                    "payment is too recent",
                    "request was already sent",
                }:
                    log.debug("Skip Digiseller unique-code request for %s: %s", invoice_id, reason)
                continue
            text = delivery_service.render_system_text(
                "request_unique_code",
                unique_code_request_context(invoice_id, purchase, product),
            )
            if await messenger.send_message("plati", invoice_id, text):
                db.add_order_event(
                    marketplace="plati",
                    external_order_id=invoice_id,
                    event_type="unique_code_request_sent",
                    status="success",
                    payload={
                        "product_id": purchase_product_id(purchase),
                        "variant_id": purchase_variant_id(purchase),
                        "unique_code_state": unique_code_state(purchase),
                    },
                )
                notify_admins(
                    f"📨 <b>{tr('Запрошен уникальный код Digiseller', 'Digiseller unique code requested')}</b>\n"
                    f"{tr('Заказ', 'Order')}: <code>{escape(invoice_id)}</code>\n"
                    f"{tr('Товар', 'Product')}: <code>{escape(purchase_product_id(purchase))}</code>",
                    kind="pending",
                )
            else:
                db.add_order_event(
                    marketplace="plati",
                    external_order_id=invoice_id,
                    event_type="unique_code_request_failed",
                    status="error",
                    message="Cannot send unique code request to Digiseller chat",
                    payload={
                        "product_id": purchase_product_id(purchase),
                        "variant_id": purchase_variant_id(purchase),
                        "unique_code_state": unique_code_state(purchase),
                    },
                )

    async def poll_ggsel_sales() -> None:
        if not ggsel.configured_for_polling():
            return
        cursor_key = "_last_sales"
        try:
            sales = await ggsel.last_sales()
        except Exception:
            log_poll_error("ggsel_sales", "Cannot read GGsel last sales")
            return
        sales = [sale for sale in sales if ggsel_order_id(sale)]
        if not sales:
            return
        newest_order_id = ggsel_order_id(sales[0])
        last_seen = db.get_chat_cursor("ggsel", cursor_key)
        if not last_seen:
            db.set_chat_cursor("ggsel", cursor_key, newest_order_id)
            db.add_order_event(
                marketplace="ggsel",
                external_order_id=newest_order_id,
                event_type="polling_cursor_initialized",
                payload={"visible_sales": len(sales)},
            )
            return

        pending_sales = []
        for sale in sales:
            order_id = ggsel_order_id(sale)
            if order_id == last_seen:
                break
            pending_sales.append(sale)
        if not pending_sales:
            return

        cursor_candidate = last_seen
        can_advance_cursor = True
        for sale in reversed(pending_sales):
            order_id = ggsel_order_id(sale)
            existing = db.get_sale_with_delivery("ggsel", order_id)
            if (
                existing
                and existing.get("delivery_id")
                and str(existing.get("marketplace_message_status") or "") == "sent"
            ):
                if can_advance_cursor:
                    cursor_candidate = order_id
                continue
            try:
                detail = await ggsel.order_info(order_id)
            except Exception as exc:
                log_poll_error("ggsel_order_info", f"Cannot read GGsel order info for {order_id}")
                db.add_order_event(
                    marketplace="ggsel",
                    external_order_id=order_id,
                    event_type="polling_sale_failed",
                    status="error",
                    message=str(exc),
                    payload=sale,
                )
                if ggsel_order_error_is_permanent(exc):
                    if can_advance_cursor:
                        cursor_candidate = order_id
                    continue
                can_advance_cursor = False
                continue
            payload = merge_sale_payload(sale, detail)
            try:
                event = normalize_sale("ggsel", payload)
            except ValueError as exc:
                db.add_order_event(
                    marketplace="ggsel",
                    external_order_id=order_id,
                    event_type="polling_sale_skipped",
                    status="error",
                    message=str(exc),
                    payload=payload,
                )
                notify_admins(
                    f"⚠️ <b>{tr('Продажа GGsel пропущена: некорректные данные', 'GGsel sale skipped: invalid data')}</b>\n"
                    f"{tr('Заказ', 'Order')}: <code>{escape(order_id)}</code>\n"
                    f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>",
                    kind="errors",
                )
                if can_advance_cursor:
                    cursor_candidate = order_id
                continue
            product = db.get_product_by_external(
                event.marketplace,
                event.external_product_id,
                event.external_variant_id,
            )
            if not product or not int(product.get("enabled", 0)):
                reason = "Product mapping is missing or disabled; sale will be retried"
                db.add_order_event(
                    marketplace="ggsel",
                    external_order_id=order_id,
                    event_type="polling_sale_deferred",
                    status="error",
                    message=reason,
                    payload=payload,
                )
                notify_admins(
                    f"⚠️ <b>{tr('Продажа GGsel без активного маппинга', 'GGsel sale has no active mapping')}</b>\n"
                    f"{tr('Заказ', 'Order')}: <code>{escape(order_id)}</code>\n"
                    f"{tr('Товар', 'Product')}: <code>{escape(event.external_product_id)}</code>",
                    kind="errors",
                )
                can_advance_cursor = False
                continue
            try:
                result = await delivery_service.handle_sale(event)
                notify_admins(sale_notification_text(result, source="GGsel polling"), kind="new_purchases")
            except DeliveryInProgressError:
                can_advance_cursor = False
                continue
            except Exception as exc:
                db.add_order_event(
                    marketplace="ggsel",
                    external_order_id=order_id,
                    event_type="polling_sale_failed",
                    status="error",
                    message=str(exc),
                    payload=payload,
                )
                notify_admins(
                    f"🚨 <b>{tr('Ошибка обработки продажи GGsel', 'GGsel sale processing error')}</b>\n"
                    f"{tr('Заказ', 'Order')}: <code>{escape(order_id)}</code>\n"
                    f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>",
                    kind="errors",
                )
                can_advance_cursor = False
                continue
            if can_advance_cursor:
                cursor_candidate = order_id
        if cursor_candidate != last_seen:
            db.set_chat_cursor("ggsel", cursor_key, cursor_candidate)

    async def poll_marketplace_chats() -> None:
        last_digiseller_fallback_poll = 0.0
        last_unclaimed_unique_code_poll = 0.0
        while True:
            try:
                for operation in db.recover_stale_pending_operations():
                    target_order_id = str(operation.get("target_order_id") or "").strip()
                    db.add_order_event(
                        marketplace=str(operation["marketplace"]),
                        external_order_id=str(operation["external_order_id"]),
                        sale_id=int(operation["sale_id"]),
                        pending_operation_id=int(operation["id"]),
                        event_type="pending_processing_recovered",
                        status="warning",
                        payload={"target_order_id": target_order_id},
                    )
                    if not target_order_id or not marketplace_messages_configured(str(operation["marketplace"])):
                        continue
                    try:
                        await delivery_service.complete_pending_operation(operation, target_order_id)
                    except DeliveryInProgressError:
                        continue
                    except Exception as exc:
                        log_poll_error(
                            f"pending_recovery:{operation['id']}",
                            f"Cannot recover pending operation {operation['id']}",
                            exc,
                        )
                await process_digiseller_sale_notification_reminders()
                now_monotonic = time.monotonic()
                if now_monotonic - last_digiseller_fallback_poll >= 300:
                    last_digiseller_fallback_poll = now_monotonic
                    await poll_digiseller_unique_code_chats()
                if now_monotonic - last_unclaimed_unique_code_poll >= 600:
                    last_unclaimed_unique_code_poll = now_monotonic
                    await poll_digiseller_unclaimed_unique_code_sales()
                await poll_ggsel_sales()
                for operation in db.list_pending_operations():
                    if not marketplace_messages_configured(str(operation["marketplace"])):
                        continue
                    # DigiSeller pending replies are handled by the message webhook.
                    # The optional fallback reads only the unread-chat list above,
                    # never every pending chat on every 20-second loop.
                    if str(operation["marketplace"]) in {"plati", "digiseller"}:
                        continue
                    messages = await messenger.order_messages(
                        str(operation["marketplace"]),
                        str(operation["external_order_id"]),
                    )
                    last_seen = str(operation.get("last_message_id") or "")
                    last_message = last_seen
                    found_order_id = ""
                    wrong_command_action = ""
                    missing_command_order_id = False
                    status_command_handled = False
                    should_process = not last_seen
                    for message in messages:
                        current_id = message_id(message)
                        if current_id:
                            last_message = current_id
                        if not should_process:
                            if current_id == last_seen:
                                should_process = True
                            continue
                        if not message_is_from_buyer(message):
                            continue
                        text = message_text(message)
                        if text and current_id != last_seen:
                            notify_admins(
                                chat_message_notification(
                                    str(operation["marketplace"]),
                                    str(operation["external_order_id"]),
                                    text,
                                ),
                                kind="chat_messages",
                            )
                        command = parse_chat_command(text)
                        if command:
                            if command["command"] == delivery_service.expected_command("status"):
                                status_command_handled = await process_subscription_status_command(
                                    str(operation["marketplace"]),
                                    str(operation["external_order_id"]),
                                    text,
                                )
                                break
                            expected_action = str(operation["action"])
                            if command["command"] != delivery_service.expected_command(expected_action):
                                command_action = delivery_service.action_for_command(command["command"]) or command["action"]
                                wrong_command_action = command_action or command["command"]
                                break
                            found_order_id = command["order_id"]
                            if found_order_id:
                                break
                            missing_command_order_id = True
                            break
                        found_order_id = extract_order_id_from_text(text)
                        if found_order_id:
                            break
                    if last_message and last_message != last_seen:
                        db.update_pending_last_message(int(operation["id"]), last_message)
                    if status_command_handled:
                        continue
                    if wrong_command_action:
                        db.add_order_event(
                            marketplace=str(operation["marketplace"]),
                            external_order_id=str(operation["external_order_id"]),
                            sale_id=int(operation["sale_id"]),
                            pending_operation_id=int(operation["id"]),
                            event_type="pending_wrong_command",
                            status="warning",
                            message=wrong_command_action,
                            payload={"expected_action": operation["action"]},
                        )
                        await messenger.send_message(
                            str(operation["marketplace"]),
                            str(operation["external_order_id"]),
                            delivery_service.command_mismatch_text(str(operation["action"]), wrong_command_action),
                        )
                        notify_admins(
                            f"⚠️ <b>{tr('Неверная команда в pending-заказе', 'Wrong command in pending order')}</b>\n"
                            f"{tr('Заказ', 'Order')}: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"{tr('Ожидали', 'Expected')}: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"{tr('Получили', 'Received')}: <code>{escape(wrong_command_action)}</code>"
                        , kind="pending")
                        continue
                    if missing_command_order_id:
                        db.add_order_event(
                            marketplace=str(operation["marketplace"]),
                            external_order_id=str(operation["external_order_id"]),
                            sale_id=int(operation["sale_id"]),
                            pending_operation_id=int(operation["id"]),
                            event_type="pending_missing_order_id",
                            status="warning",
                            payload={"action": operation["action"]},
                        )
                        await messenger.send_message(
                            str(operation["marketplace"]),
                            str(operation["external_order_id"]),
                            delivery_service.ask_order_id_text(str(operation["action"])),
                        )
                        continue
                    if not found_order_id:
                        continue
                    try:
                        db.add_order_event(
                            marketplace=str(operation["marketplace"]),
                            external_order_id=str(operation["external_order_id"]),
                            sale_id=int(operation["sale_id"]),
                            pending_operation_id=int(operation["id"]),
                            event_type="pending_order_id_received",
                            payload={"target_order_id": found_order_id, "action": operation["action"]},
                        )
                        result = await delivery_service.complete_pending_operation(operation, found_order_id)
                        notify_admins(
                            f"✅ <b>{tr('Услуга применена', 'Service applied')}</b>\n"
                            f"{tr('Заказ', 'Order')}: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"{tr('Действие', 'Action')}: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"{tr('ID заказа', 'Order ID')}: <code>{escape(found_order_id)}</code>\n"
                            f"{tr('Статус', 'Status')}: <b>{escape(str(result.get('status') or 'delivered'))}</b>"
                        , kind="pending")
                    except DeliveryInProgressError:
                        continue
                    except Exception as exc:
                        db.add_order_event(
                            marketplace=str(operation["marketplace"]),
                            external_order_id=str(operation["external_order_id"]),
                            sale_id=int(operation["sale_id"]),
                            pending_operation_id=int(operation["id"]),
                            event_type="pending_failed",
                            status="error",
                            message=str(exc),
                            payload={"target_order_id": found_order_id, "action": operation["action"]},
                        )
                        await messenger.send_message(
                            str(operation["marketplace"]),
                            str(operation["external_order_id"]),
                            delivery_service.operation_error_text(str(operation["action"]), exc),
                        )
                        notify_admins(
                            f"🚨 <b>{tr('Ошибка pending-операции', 'Pending operation error')}</b>\n"
                            f"{tr('Заказ', 'Order')}: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"{tr('Действие', 'Action')}: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"{tr('ID заказа', 'Order ID')}: <code>{escape(found_order_id)}</code>\n"
                            f"{tr('Ошибка', 'Error')}: <code>{escape(str(exc))}</code>"
                        , kind="errors")
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Marketplace chat polling failed")
            await asyncio.sleep(20)

    def telegram_status() -> dict[str, Any]:
        return {
            "enabled": runtime.get_bool("enable_telegram"),
            "token_configured": bool(runtime.get_text("telegram_bot_token")),
            "admins": len(runtime.bot_admin_ids()),
            "running": bool(bot_task and not bot_task.done()),
            "last_error": bot_last_error,
        }

    async def stop_telegram_bot() -> None:
        nonlocal bot_task
        if not bot_task:
            return
        task = bot_task
        bot_task = None
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def start_telegram_bot() -> dict[str, Any]:
        nonlocal bot_task, bot_last_error
        status = telegram_status()
        if not status["enabled"]:
            return {**status, "running": False, "reason": "telegram disabled"}
        if not status["token_configured"]:
            return {**status, "running": False, "reason": "telegram token is not configured"}
        if not status["admins"]:
            return {**status, "running": False, "reason": "no telegram admins configured"}
        bot_last_error = ""

        async def telegram_supervisor() -> None:
            nonlocal bot_last_error
            delay = 5
            while True:
                try:
                    await run_bot(
                        token=runtime.get_text("telegram_bot_token"),
                        db=db,
                        xyranet=xyranet,
                        digiseller=digiseller,
                        runtime=runtime,
                        messenger=messenger,
                        restart_bot=restart_telegram_bot,
                        check_updates=lambda force=True: update_manager.check(force=force),
                        start_update=update_manager.start_update,
                    )
                    bot_last_error = ""
                    delay = 5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    bot_last_error = str(exc)
                    log.exception("Telegram bot polling failed; web panel stays online")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300)

        bot_task = asyncio.create_task(telegram_supervisor())
        return telegram_status()

    async def restart_telegram_bot() -> dict[str, Any]:
        async with bot_lock:
            await stop_telegram_bot()
            result = await start_telegram_bot()
        if result.get("running"):
            async def restart_notice() -> None:
                await asyncio.sleep(1)
                await notifier.send_admins(
                    f"✅ <b>{tr('Telegram-бот перезапущен', 'Telegram bot restarted')}</b>\n"
                    f"{tr('Бот снова работает.', 'The bot is running again.')}"
                )

            asyncio.create_task(restart_notice())
        return result

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal chat_task, daily_task, update_check_task
        db.init()
        async with bot_lock:
            await start_telegram_bot()
        chat_task = asyncio.create_task(poll_marketplace_chats())
        daily_task = asyncio.create_task(daily_statistics_loop())
        update_check_task = asyncio.create_task(update_check_loop())
        yield
        if chat_task:
            chat_task.cancel()
            try:
                await chat_task
            except asyncio.CancelledError:
                pass
        if daily_task:
            daily_task.cancel()
            try:
                await daily_task
            except asyncio.CancelledError:
                pass
        if update_check_task:
            update_check_task.cancel()
            try:
                await update_check_task
            except asyncio.CancelledError:
                pass
        async with bot_lock:
            await stop_telegram_bot()

    app = FastAPI(title="XyraNet Reseller Autoseller", version=__version__, lifespan=lifespan)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path == "/" or request.url.path.startswith("/admin/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def unsafe_admin_secret(value: str, *, minimum_length: int) -> bool:
        normalized = value.strip()
        return normalized.lower() in DEFAULT_ADMIN_SECRET_VALUES or len(normalized) < minimum_length

    def ensure_admin_secrets_are_safe() -> None:
        if unsafe_admin_secret(runtime.get_text("admin_password"), minimum_length=MIN_ADMIN_PASSWORD_LENGTH):
            raise HTTPException(status_code=503, detail="Set a strong ADMIN_PASSWORD before using the web panel")

    def create_admin_session() -> str:
        cleanup_admin_sessions()
        token = secrets.token_urlsafe(48)
        admin_sessions[token] = time.monotonic()
        return token

    def cleanup_admin_sessions() -> None:
        now = time.monotonic()
        for token, issued_at in list(admin_sessions.items()):
            if now - issued_at > ADMIN_SESSION_TTL_SECONDS:
                admin_sessions.pop(token, None)

    def valid_admin_session(token: str) -> bool:
        cleanup_admin_sessions()
        issued_at = admin_sessions.get(token)
        if issued_at is None:
            return False
        admin_sessions[token] = time.monotonic()
        return True

    def invalidate_admin_sessions() -> None:
        admin_sessions.clear()

    def login_rate_key(request: Request, username: str) -> str:
        del username
        client_host = request.client.host if request.client else "unknown"
        if client_host in {"127.0.0.1", "::1"}:
            forwarded_host = request.headers.get("x-real-ip", "").strip()
            if not forwarded_host:
                forwarded_host = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
            try:
                client_host = str(ipaddress.ip_address(forwarded_host))
            except ValueError:
                pass
        return client_host

    def cleanup_login_failures(now: float) -> None:
        for key, timestamps in list(login_failures.items()):
            recent = [timestamp for timestamp in timestamps if now - timestamp < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
            if recent:
                login_failures[key] = recent
            else:
                login_failures.pop(key, None)
        if len(login_failures) > LOGIN_RATE_LIMIT_MAX_KEYS:
            oldest = sorted(login_failures, key=lambda key: max(login_failures[key]))
            for key in oldest[: len(login_failures) - LOGIN_RATE_LIMIT_MAX_KEYS]:
                login_failures.pop(key, None)

    def check_login_rate_limit(key: str) -> None:
        now = time.monotonic()
        cleanup_login_failures(now)
        recent = [timestamp for timestamp in login_failures.get(key, []) if now - timestamp < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
        login_failures[key] = recent
        if len(recent) >= LOGIN_RATE_LIMIT_MAX_FAILURES:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    def record_login_failure(key: str) -> None:
        now = time.monotonic()
        cleanup_login_failures(now)
        login_failures[key] = [
            timestamp
            for timestamp in login_failures.get(key, [])
            if now - timestamp < LOGIN_RATE_LIMIT_WINDOW_SECONDS
        ] + [now]

    def require_admin(authorization: str | None = Header(default=None)) -> str:
        ensure_admin_secrets_are_safe()
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token or not valid_admin_session(token):
            raise HTTPException(status_code=401, detail="Invalid admin session")
        return token

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (static_dir / "index.html").read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return {
            "version": __version__,
            "commit": os.environ.get("APP_UPDATE_CURRENT_COMMIT", ""),
        }

    @app.api_route("/api/digiseller/notify/sale/{secret}", methods=["GET", "POST"])
    async def digiseller_sale_notification(secret: str, request: Request) -> dict[str, Any]:
        verify_notification_secret(secret)
        if not runtime.get_bool("digiseller_sale_notifications_enabled"):
            return {"status": "ignored", "reason": "sale notifications disabled"}
        payload = await notification_payload(request)
        invoice_id = payload_get(payload, "id_i", "ID_I", "invoice_id", "InvoiceId", "inv")
        product_id = payload_get(payload, "id_d", "ID_D", "product_id", "ProductId", "id_goods")
        amount = payload_get(payload, "amount", "Amount")
        currency = payload_get(payload, "curr", "Currency", "currency")
        email = payload_get(payload, "email", "Email")
        sale_date = payload_get(payload, "date", "Date")
        is_my_product = payload_get(payload, "isMyProduct", "IsMyProduct")
        if not invoice_id or not product_id:
            raise HTTPException(status_code=400, detail="Digiseller notification has no invoice or product ID")
        if is_my_product and is_my_product.strip().lower() in {"0", "false", "no"}:
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="digiseller_sale_notification_ignored",
                status="info",
                message="Not seller product",
                payload={"product_id": product_id},
            )
            return {"status": "ignored", "reason": "not seller product"}
        if not sale_notification_signature_ok(payload, invoice_id, product_id):
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="digiseller_sale_notification_signature_failed",
                status="error",
                payload={"product_id": product_id},
            )
            raise HTTPException(status_code=403, detail="Invalid Digiseller sale notification signature")
        mapped_products = mapped_plati_products_by_id(product_id)
        sale_event = db.add_order_event(
            marketplace="plati",
            external_order_id=invoice_id,
            event_type="digiseller_sale_notification_received",
            status="info",
            payload={
                "product_id": product_id,
                "amount": amount,
                "currency": currency,
                "email": email,
                "date": sale_date,
                "mapped_products": len(mapped_products),
            },
        )
        if db.digiseller_invoice_has_delivery(invoice_id):
            return {"status": "ignored", "reason": "invoice already has delivery"}
        if not mapped_products:
            notify_admins(
                f"⚠️ <b>{tr('Продажа Digiseller без маппинга', 'Digiseller sale without mapping')}</b>\n"
                f"{tr('Заказ', 'Order')}: <code>{escape(invoice_id)}</code>\n"
                f"{tr('Товар', 'Product')}: <code>{escape(product_id)}</code>",
                kind="errors",
            )
            return {"status": "ignored", "reason": "mapped product was not found"}
        if not runtime.get_bool("digiseller_unique_code_request_enabled"):
            return {"status": "ignored", "reason": "unique code requests disabled"}
        events = db.list_order_events(marketplace="plati", external_order_id=invoice_id, limit=100)
        if any(str(event.get("event_type") or "") == "unique_code_request_sent" and str(event.get("status") or "") == "success" for event in events):
            return {"status": "ignored", "reason": "request was already sent"}
        return {
            "status": "ok",
            "action": "unique_code_request_scheduled",
            "delay_minutes": runtime.get_float("digiseller_unique_code_request_delay_minutes"),
            "event_id": sale_event.get("id"),
        }

    @app.api_route("/api/digiseller/notify/message/{secret}", methods=["GET", "POST"])
    async def digiseller_message_notification(secret: str, request: Request) -> dict[str, Any]:
        verify_notification_secret(secret)
        if not runtime.get_bool("digiseller_message_notifications_enabled"):
            return {"status": "ignored", "reason": "message notifications disabled"}
        payload = await notification_payload(request)
        invoice_id = payload_get(payload, "InvoiceId", "invoice_id", "id_i", "ID_I", "inv")
        text = payload_get(payload, "Message", "MessageText", "message", "text")
        message_id = payload_get(payload, "DebateId", "MessageId", "ID_D", "ID_M", "message_id", "id")
        message_date = payload_get(payload, "MessageDate", "message_date", "date")
        # DigiSeller sends administration/service notifications to the same URL.
        # They do not belong to a buyer invoice, so acknowledge them instead of
        # returning 400 and triggering repeated delivery warnings in the cabinet.
        if not invoice_id:
            return {"status": "ignored", "reason": "notification has no buyer invoice"}

        # Some notification variants contain only the invoice. Resolve that one
        # chat once; normal UI actions always read the local history and never
        # call DigiSeller.
        if not text:
            sync_lock = digiseller_chat_sync_locks.setdefault(invoice_id, asyncio.Lock())
            async with sync_lock:
                now_monotonic = time.monotonic()
                if now_monotonic - recent_digiseller_blind_syncs.get(invoice_id, 0.0) < 10:
                    return {"status": "ignored", "reason": "chat was synchronized recently"}
                try:
                    handled = await process_digiseller_chat_messages(invoice_id, raise_on_read_error=True)
                except Exception as exc:
                    raise HTTPException(status_code=503, detail="Cannot synchronize Digiseller buyer chat") from exc
                recent_digiseller_blind_syncs[invoice_id] = time.monotonic()
                if len(recent_digiseller_blind_syncs) > 500:
                    cutoff = time.monotonic() - 300
                    for key, synced_at in list(recent_digiseller_blind_syncs.items()):
                        if synced_at < cutoff:
                            recent_digiseller_blind_syncs.pop(key, None)
                            old_lock = digiseller_chat_sync_locks.get(key)
                            if old_lock is not None and not old_lock.locked():
                                digiseller_chat_sync_locks.pop(key, None)
            return {"status": "ok", "action": "chat_synced", "handled": handled}

        row, created = save_remote_chat_message(
            marketplace="plati",
            external_order_id=invoice_id,
            payload=payload,
            text=text,
            remote_id=message_id,
            remote_date=message_date,
            source="digiseller_webhook",
        )
        last_seen = db.get_chat_cursor("plati", invoice_id)
        if not created and message_id and last_seen == message_id:
            return {
                "status": "ignored",
                "reason": "duplicate message notification",
                "message_id": row.get("id"),
            }
        if created:
            db.add_order_event(
                marketplace="plati",
                external_order_id=invoice_id,
                event_type="digiseller_message_notification_received",
                status="info",
                payload={"message_id": message_id, "message_date": message_date, "text_preview": compact_text(text, 300)},
            )
        role = str(row.get("role") or "system")
        if role != "buyer":
            if message_id:
                db.set_chat_cursor("plati", invoice_id, message_id)
            return {
                "status": "ok" if created else "ignored",
                "handled": False,
                "action": "stored" if created else "duplicate",
                "role": role,
            }

        handled = False
        handled_action = ""
        for code in unique_codes_from_text(text):
            handled = await process_unique_code_message(invoice_id, code)
            if handled:
                handled_action = "unique_code"
                break
        if not handled:
            handled = await process_subscription_status_command("plati", invoice_id, text)
            handled_action = "status" if handled else ""
        if not handled:
            handled = await process_free_reissue_command(invoice_id, text, message_id)
            handled_action = "free_reissue" if handled else ""
        if not handled:
            handled = await process_pending_operation_message("plati", invoice_id, text)
            handled_action = "pending_operation" if handled else ""
        if message_id:
            db.set_chat_cursor("plati", invoice_id, message_id)
        return {"status": "ok", "handled": handled, "action": handled_action}

    @app.post("/admin/api/login")
    async def admin_login(request: Request, payload: LoginIn) -> dict[str, str]:
        ensure_admin_secrets_are_safe()
        rate_key = login_rate_key(request, payload.username)
        check_login_rate_limit(rate_key)
        username_ok = secrets.compare_digest(payload.username, runtime.get_text("admin_username"))
        password_ok = secrets.compare_digest(payload.password, runtime.get_text("admin_password"))
        if not username_ok or not password_ok:
            record_login_failure(rate_key)
            raise HTTPException(status_code=401, detail="Invalid admin credentials")
        login_failures.pop(rate_key, None)
        return {"token": create_admin_session(), "username": runtime.get_text("admin_username")}

    @app.post("/admin/api/logout", status_code=204)
    async def admin_logout(token: str = Depends(require_admin)) -> Response:
        admin_sessions.pop(token, None)
        return Response(status_code=204)

    @app.get("/admin/api/status", dependencies=[Depends(require_admin)])
    async def admin_status() -> dict[str, Any]:
        return {
            "status": "ok",
            "products": len(db.list_products()),
            "sales": len(db.list_sales(limit=1000)),
            "telegram_enabled": bool(runtime.get_bool("enable_telegram") and runtime.get_text("telegram_bot_token")),
            "telegram_running": telegram_status()["running"],
            "bot_admins": len(runtime.bot_admin_ids()),
        }

    @app.get("/admin/api/digiseller/notification-urls", dependencies=[Depends(require_admin)])
    async def admin_digiseller_notification_urls() -> dict[str, Any]:
        sale_url, message_url = digiseller_base_notification_urls()
        return {
            "sale_url": sale_url,
            "sale_method": "POST",
            "message_url": message_url,
            "message_method": "POST",
            "message_send_body": True,
            "app_base_url": runtime.get_text("app_base_url"),
            "sale_notifications_enabled": runtime.get_bool("digiseller_sale_notifications_enabled"),
            "message_notifications_enabled": runtime.get_bool("digiseller_message_notifications_enabled"),
            "sha256_validation_enabled": runtime.get_bool("digiseller_validate_sale_sha256"),
            "polling_fallback_enabled": runtime.get_bool("digiseller_polling_fallback_enabled"),
        }

    @app.get("/admin/api/system", dependencies=[Depends(require_admin)])
    async def admin_system_metrics() -> dict[str, Any]:
        return collect_system_metrics(settings.database_file.parent)

    @app.get("/admin/api/update", dependencies=[Depends(require_admin)])
    async def admin_update_status() -> dict[str, Any]:
        return update_manager.status()

    @app.post("/admin/api/update/check", dependencies=[Depends(require_admin)])
    async def admin_update_check() -> dict[str, Any]:
        return await update_manager.check(force=True)

    @app.post("/admin/api/update/start", dependencies=[Depends(require_admin)])
    async def admin_update_start() -> dict[str, Any]:
        try:
            return update_manager.start_update()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/admin/api/summary", dependencies=[Depends(require_admin)])
    async def admin_summary() -> Any:
        try:
            return await xyranet.summary()
        except XyraNetApiError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/admin/api/tariffs", dependencies=[Depends(require_admin)])
    async def admin_tariffs() -> Any:
        try:
            return await xyranet.tariffs()
        except XyraNetApiError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/admin/api/delivery-template", dependencies=[Depends(require_admin)])
    async def admin_delivery_template() -> dict[str, Any]:
        actions = []
        templates: dict[str, str] = {}
        action_groups = []
        for group in TEMPLATE_GROUPS:
            stages = []
            for stage in group["stages"]:
                key = str(stage["key"])
                current = db.get_setting(delivery_template_key(key)) or ""
                templates[key] = current
                stages.append(
                    {
                        **stage,
                        "default_template": DEFAULT_ACTION_TEMPLATES[key],
                        "template": current,
                    }
                )
            command_action = str(group.get("command_action") or "")
            action_groups.append(
                {
                    "key": group["key"],
                    "label": group["label"],
                    "command_action": command_action,
                    "command": delivery_service.expected_command(command_action) if command_action else "",
                    "stages": stages,
                }
            )
        for action, label in ACTION_LABELS.items():
            current = db.get_setting(delivery_template_key(action)) or ""
            templates[action] = current
            actions.append(
                {
                    "key": action,
                    "label": label,
                    "category": TEMPLATE_CATEGORIES.get(action, "Прочее"),
                    "default_template": DEFAULT_ACTION_TEMPLATES[action],
                    "template": current,
                }
            )
        variables = [
            {
                "key": key,
                "token": "{" + key + "}",
                "legacy_token": "${" + key.lower() + "}",
                "label": label,
                "description": DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS.get(key, ""),
            }
            for key, label in DELIVERY_TEMPLATE_VARIABLES.items()
        ]
        known_variable_keys = {str(item["key"]) for item in variables}
        for variable in delivery_service.custom_complex_variables():
            key = str(variable["key"])
            if key not in known_variable_keys:
                variables.append(
                    {
                        "key": key,
                        "token": "{" + key + "}",
                        "legacy_token": "${" + key.lower() + "}",
                        "label": str(variable["label"]),
                        "description": str(variable.get("description") or ""),
                    }
                )
        return {
            "default_template": DEFAULT_DELIVERY_TEMPLATE,
            "default_templates": DEFAULT_ACTION_TEMPLATES,
            "templates": templates,
            "action_groups": action_groups,
            "actions": actions,
            "variables": variables,
        }

    @app.put("/admin/api/delivery-template/{action}", dependencies=[Depends(require_admin)])
    async def admin_delivery_template_update(action: str, payload: DeliveryTemplateIn) -> dict[str, Any]:
        action = action.strip().lower()
        if action not in TEMPLATE_KEYS and action not in BASE_ACTION_LABELS:
            raise HTTPException(status_code=404, detail="Unknown delivery template action")
        db.set_setting(delivery_template_key(action), payload.template.strip())
        return {
            "key": action,
            "label": ACTION_LABELS.get(action, action),
            "category": TEMPLATE_CATEGORIES.get(action, "Прочее"),
            "default_template": DEFAULT_ACTION_TEMPLATES[action],
            "template": db.get_setting(delivery_template_key(action)) or "",
        }

    @app.put("/admin/api/chat-command/{action}", dependencies=[Depends(require_admin)])
    async def admin_chat_command_update(action: str, payload: ChatCommandIn) -> dict[str, Any]:
        action = action.strip().lower()
        if action not in {"renew", "reissue", "traffic", "ip_limit", "status"}:
            raise HTTPException(status_code=404, detail="Unknown command action")
        try:
            command = delivery_service.set_expected_command(action, payload.command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"action": action, "command": command}

    @app.get("/admin/api/complex-variables", dependencies=[Depends(require_admin)])
    async def admin_complex_variables() -> dict[str, Any]:
        ordinary = [
            {
                "key": key,
                "token": "{" + key + "}",
                "label": label,
                "description": DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS.get(key, ""),
            }
            for key, label in DELIVERY_TEMPLATE_VARIABLES.items()
            if key not in BUILTIN_COMPLEX_VARIABLES
        ]
        variables = []
        for variable in delivery_service.list_complex_variables():
            key = str(variable["key"])
            variables.append(
                {
                    **variable,
                    "token": "{" + key + "}",
                    "template": str(variable.get("template") or ""),
                    "default_template": str(variable.get("default_template") or ""),
                }
            )
        return {"variables": variables, "ordinary_variables": ordinary}

    @app.put("/admin/api/complex-variables/{key}", dependencies=[Depends(require_admin)])
    async def admin_complex_variable_update(key: str, payload: ComplexVariableIn) -> dict[str, Any]:
        try:
            variable = delivery_service.save_complex_variable(
                key=payload.key or key,
                label=payload.label,
                template=payload.template,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved_key = str(variable["key"])
        return {**variable, "token": "{" + saved_key + "}"}

    @app.delete("/admin/api/complex-variables/{key}", dependencies=[Depends(require_admin)])
    async def admin_complex_variable_delete(key: str) -> Response:
        if not delivery_service.delete_complex_variable(key):
            raise HTTPException(status_code=404, detail="Unknown custom variable")
        return Response(status_code=204)

    @app.post("/admin/api/parse-lot", dependencies=[Depends(require_admin)])
    async def admin_parse_lot(payload: LotParseIn) -> dict[str, Any]:
        source = payload.source.strip()
        if not is_allowed_lot_url(source):
            return {"marketplace": "", "productId": extract_product_id(source), "variantId": "", "variants": []}
        try:
            async with httpx.AsyncClient(
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                },
            ) as client:
                current_url = source
                response: httpx.Response | None = None
                for _ in range(5):
                    if not is_allowed_lot_url(current_url):
                        raise HTTPException(status_code=400, detail="Lot URL redirected to an unsupported host")
                    response = await client.get(current_url, follow_redirects=False)
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("location")
                    if not location:
                        break
                    current_url = urljoin(str(response.url), location)
                if response is None:
                    raise HTTPException(status_code=502, detail="Cannot fetch lot page")
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise HTTPException(status_code=400, detail="Lot URL has too many redirects")
                if is_allowed_lot_url(str(response.url)) is False:
                    raise HTTPException(status_code=400, detail="Lot URL redirected to an unsupported host")
                response.raise_for_status()
                html = response.content.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot fetch lot page: {exc}") from exc
        return parse_lot_html(str(response.url), html)

    @app.get("/admin/api/products", dependencies=[Depends(require_admin)])
    async def admin_products() -> list[dict[str, Any]]:
        return db.list_products()

    @app.post("/admin/api/products", dependencies=[Depends(require_admin)])
    async def admin_product_upsert(payload: ProductMappingIn) -> dict[str, Any]:
        data = payload.model_dump()
        try:
            data["marketplace"] = normalize_marketplace(data["marketplace"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Supported marketplaces: plati (Digiseller), ggsel") from exc
        data["external_product_id"] = data["external_product_id"].strip()
        data["external_variant_id"] = data["external_variant_id"].strip()
        data["action"] = data["action"].strip().lower() or "create"
        if data["action"] not in {"create", "renew", "reissue", "traffic", "ip_limit"}:
            raise HTTPException(status_code=400, detail="Supported actions: create, renew, reissue, traffic, ip_limit")
        data["tariff_code"] = data["tariff_code"].strip().lower()
        return db.upsert_product(data)

    @app.put("/admin/api/products/{product_id}", dependencies=[Depends(require_admin)])
    async def admin_product_update(product_id: int, payload: ProductMappingIn) -> dict[str, Any]:
        data = payload.model_dump()
        try:
            data["marketplace"] = normalize_marketplace(data["marketplace"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Supported marketplaces: plati (Digiseller), ggsel") from exc
        data["external_product_id"] = data["external_product_id"].strip()
        data["external_variant_id"] = data["external_variant_id"].strip()
        data["action"] = data["action"].strip().lower() or "create"
        if data["action"] not in {"create", "renew", "reissue", "traffic", "ip_limit"}:
            raise HTTPException(status_code=400, detail="Supported actions: create, renew, reissue, traffic, ip_limit")
        data["tariff_code"] = data["tariff_code"].strip().lower()
        try:
            product = db.update_product(product_id, data)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Mapping with this lot and variant already exists") from exc
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        return product

    @app.patch("/admin/api/products/{product_id}/enabled", dependencies=[Depends(require_admin)])
    async def admin_product_enabled(product_id: int, payload: EnabledIn) -> dict[str, Any]:
        product = db.set_product_enabled(product_id, payload.enabled)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        return product

    @app.delete("/admin/api/products/{product_id}", dependencies=[Depends(require_admin)])
    async def admin_product_delete(product_id: int) -> dict[str, str]:
        try:
            deleted = db.delete_product(product_id)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="Product mapping is referenced by existing operations and cannot be deleted",
            ) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Product not found")
        return {"status": "deleted"}

    @app.get("/admin/api/sales", dependencies=[Depends(require_admin)])
    async def admin_sales(limit: int = 50) -> list[dict[str, Any]]:
        return db.list_sales(limit=max(1, min(limit, 500)))

    @app.post("/admin/api/sales/{sale_id}/resend", dependencies=[Depends(require_admin)])
    async def admin_sale_resend(sale_id: int) -> dict[str, Any]:
        sale = db.get_sale_with_delivery_by_id(sale_id)
        if not sale:
            raise HTTPException(status_code=404, detail="Sale not found")
        if not sale.get("delivery_id") or not sale.get("delivery_text"):
            raise HTTPException(status_code=400, detail="Sale has no saved delivery")
        chat_id = sale_chat_id(sale)
        if str(sale.get("marketplace_message_status") or "") == "sent":
            ok = await messenger.send_message(str(sale["marketplace"]), chat_id, str(sale["delivery_text"]))
        else:
            try:
                ok = await delivery_service.ensure_delivery_message_sent(
                    sale,
                    marketplace=str(sale["marketplace"]),
                    external_order_id=chat_id,
                    sale_id=int(sale["id"]),
                )
            except (MarketplaceMessageError, DeliveryInProgressError):
                ok = False
        db.add_order_event(
            marketplace=str(sale["marketplace"]),
            external_order_id=str(sale["external_order_id"]),
            sale_id=int(sale["id"]),
            event_type="manual_resend",
            status="success" if ok else "error",
            message="Manual resend from web panel",
            payload={"chat_id": chat_id},
        )
        if not ok:
            raise HTTPException(status_code=502, detail="Cannot send saved delivery to marketplace chat")
        return {"status": "ok", "chat_id": chat_id}

    @app.get("/admin/api/statistics", dependencies=[Depends(require_admin)])
    async def admin_statistics(period: str = "30d") -> dict[str, Any]:
        return build_sales_statistics(db.list_sales_for_statistics(), period=period)

    @app.get("/admin/api/pending-operations", dependencies=[Depends(require_admin)])
    async def admin_pending_operations(status: str = "waiting_order_id") -> list[dict[str, Any]]:
        selected = status.strip() or "waiting_order_id"
        return db.list_pending_operations(None if selected == "all" else selected)

    @app.get("/admin/api/order-events", dependencies=[Depends(require_admin)])
    async def admin_order_events(
        marketplace: str = "",
        external_order_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return db.list_order_events(
            marketplace=marketplace.strip() or None,
            external_order_id=external_order_id.strip() or None,
            limit=limit,
        )

    @app.post("/admin/api/pending-operations/{operation_id}/complete", dependencies=[Depends(require_admin)])
    async def admin_pending_complete(operation_id: int, payload: CompletePendingIn) -> dict[str, Any]:
        pending = next((row for row in db.list_pending_operations(None) if int(row["id"]) == operation_id), None)
        if not pending:
            raise HTTPException(status_code=404, detail="Pending operation not found")
        try:
            return await delivery_service.complete_pending_operation(pending, payload.order_id.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/admin/api/pending-operations/{operation_id}/retry", dependencies=[Depends(require_admin)])
    async def admin_pending_retry(operation_id: int) -> dict[str, Any]:
        pending = db.retry_pending_operation(operation_id)
        if not pending:
            raise HTTPException(status_code=404, detail="Pending operation not found")
        db.add_order_event(
            marketplace=str(pending["marketplace"]),
            external_order_id=str(pending["external_order_id"]),
            sale_id=int(pending["sale_id"]),
            pending_operation_id=int(pending["id"]),
            event_type="manual_pending_retry",
            status="info",
            message="Pending operation was returned to waiting_order_id",
        )
        return pending

    @app.get("/admin/api/backup/database", dependencies=[Depends(require_admin)])
    async def admin_backup_database() -> FileResponse:
        database_file = settings.database_file
        if not database_file.exists():
            raise HTTPException(status_code=404, detail="Database file not found")
        return FileResponse(
            database_file,
            media_type="application/octet-stream",
            filename=f"xyranet-reseller-backup-{database_file.name}",
        )

    @app.post("/admin/api/smoke-tests/xyranet", dependencies=[Depends(require_admin)])
    async def admin_smoke_xyranet() -> dict[str, Any]:
        try:
            data = await xyranet.summary()
            return {"status": "ok", "detail": "XyraNet API is reachable", "summary": data}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.post("/admin/api/smoke-tests/digiseller", dependencies=[Depends(require_admin)])
    async def admin_smoke_digiseller() -> dict[str, Any]:
        try:
            await digiseller.client().token()
            return {"status": "ok", "detail": "Digiseller token received"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.post("/admin/api/smoke-tests/ggsel", dependencies=[Depends(require_admin)])
    async def admin_smoke_ggsel() -> dict[str, Any]:
        if not ggsel.configured_for_polling():
            return {"status": "error", "detail": "GGsel seller ID/API key are not configured"}
        try:
            await ggsel.token()
            return {"status": "ok", "detail": "GGsel token received"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.post("/admin/api/smoke-tests/telegram", dependencies=[Depends(require_admin)])
    async def admin_smoke_telegram() -> dict[str, Any]:
        try:
            await notifier.send_admins(
                f"✅ <b>{tr('Тестовое уведомление', 'Test notification')}</b>\n"
                f"{tr('Telegram-уведомления работают.', 'Telegram notifications are working.')}"
            )
            return {"status": "ok", "detail": "Test notification sent to admins"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.get("/admin/api/settings", dependencies=[Depends(require_admin)])
    async def admin_settings() -> list[dict[str, Any]]:
        return runtime.setting_payload()

    @app.patch("/admin/api/settings", dependencies=[Depends(require_admin)])
    async def admin_settings_update(payload: SettingsIn) -> dict[str, Any]:
        try:
            runtime.set_many(payload.settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "admin_username" in payload.settings or (
            "admin_password" in payload.settings and str(payload.settings.get("admin_password") or "").strip()
        ):
            invalidate_admin_sessions()
        return {"status": "ok", "settings": runtime.setting_payload()}

    @app.get("/admin/api/telegram/status", dependencies=[Depends(require_admin)])
    async def admin_telegram_status() -> dict[str, Any]:
        return telegram_status()

    @app.post("/admin/api/telegram/restart", dependencies=[Depends(require_admin)])
    async def admin_telegram_restart() -> dict[str, Any]:
        return {"status": "ok", "telegram": await restart_telegram_bot()}

    @app.get("/admin/api/bot-users", dependencies=[Depends(require_admin)])
    async def admin_bot_users() -> list[dict[str, Any]]:
        return runtime.list_bot_users()

    @app.post("/admin/api/bot-users", dependencies=[Depends(require_admin)])
    async def admin_bot_user_add(payload: BotUserIn) -> dict[str, Any]:
        if payload.telegram_id in settings.admin_ids:
            return next(row for row in runtime.list_bot_users() if int(row["telegram_id"]) == payload.telegram_id)
        return db.upsert_bot_user(payload.telegram_id, payload.label)

    @app.patch("/admin/api/bot-users/{telegram_id}/enabled", dependencies=[Depends(require_admin)])
    async def admin_bot_user_enabled(telegram_id: int, payload: EnabledIn) -> dict[str, Any]:
        if telegram_id in settings.admin_ids:
            raise HTTPException(status_code=400, detail="ENV admin cannot be disabled")
        user = db.set_bot_user_enabled(telegram_id, payload.enabled)
        if not user:
            raise HTTPException(status_code=404, detail="Bot user not found")
        return user

    @app.delete("/admin/api/bot-users/{telegram_id}", dependencies=[Depends(require_admin)])
    async def admin_bot_user_delete(telegram_id: int) -> dict[str, str]:
        if telegram_id in settings.admin_ids:
            raise HTTPException(status_code=400, detail="ENV admin cannot be deleted")
        if not db.delete_bot_user(telegram_id):
            raise HTTPException(status_code=404, detail="Bot user not found")
        return {"status": "deleted"}

    return app
