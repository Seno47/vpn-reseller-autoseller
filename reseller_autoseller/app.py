from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from reseller_autoseller.config import get_settings
from reseller_autoseller.db import Database
from reseller_autoseller.digiseller_client import (
    DigisellerApiError,
    RuntimeDigisellerClient,
    sale_event_from_unique_code,
)
from reseller_autoseller.lot_parser import extract_product_id, is_allowed_lot_url, parse_lot_html
from reseller_autoseller.marketplaces import SUPPORTED_MARKETPLACES, normalize_sale
from reseller_autoseller.marketplace_chat import MarketplaceMessenger, RuntimeGgselClient
from reseller_autoseller.notifications import TelegramNotifier, compact_text, sale_title
from reseller_autoseller.runtime_config import RuntimeConfig, RuntimeXyraNetClient
from reseller_autoseller.services import (
    ACTION_LABELS,
    BASE_ACTION_LABELS,
    BUILTIN_COMPLEX_VARIABLES,
    DEFAULT_ACTION_TEMPLATES,
    DEFAULT_DELIVERY_TEMPLATE,
    DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS,
    DELIVERY_TEMPLATE_VARIABLES,
    DeliveryService,
    TEMPLATE_CATEGORIES,
    TEMPLATE_GROUPS,
    TEMPLATE_KEYS,
    delivery_template_key,
    extract_order_id_from_text,
    parse_chat_command,
)
from reseller_autoseller.statistics import build_sales_statistics
from reseller_autoseller.telegram_bot import run_bot
from reseller_autoseller.xyra_client import XyraNetApiError


log = logging.getLogger(__name__)
UNIQUE_CODE_RE = re.compile(r"\b[A-Za-z0-9]{16}\b")
LOOSE_UNIQUE_CODE_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9\s-]{14,36}[A-Za-z0-9]\b")


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
    messenger = MarketplaceMessenger(
        digiseller=digiseller,
        ggsel=ggsel,
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
    bot_lock = asyncio.Lock()
    bot_last_error = ""
    recent_notifications: dict[str, float] = {}
    poll_error_notifications: dict[str, float] = {}

    def notify_admins(text: str, kind: str = "errors") -> None:
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
        dedupe_key = f"{kind}:{text}"
        last_sent = recent_notifications.get(dedupe_key, 0)
        if now - last_sent < dedupe_window:
            return
        recent_notifications[dedupe_key] = now
        if len(recent_notifications) > 300:
            cutoff = now - 3600
            for key, timestamp in list(recent_notifications.items()):
                if timestamp < cutoff:
                    recent_notifications.pop(key, None)

        async def runner() -> None:
            await notifier.send_admins(text)

        try:
            asyncio.create_task(runner())
        except RuntimeError:
            log.exception("Cannot schedule Telegram admin notification")

    def action_label(action: str) -> str:
        return {
            "create": "покупка",
            "renew": "продление",
            "reissue": "перевыпуск",
            "traffic": "LTE-трафик",
            "ip_limit": "IP-лимит",
        }.get(action, action or "покупка")

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
            "🛒 <b>Новая покупка</b>",
            f"Источник: <b>{escape(source)}</b>",
            f"Заказ: <code>{sale_title(sale)}</code>",
            f"Товар: <code>{escape(str(sale.get('external_product_id') or ''))}</code>",
            f"Действие: <b>{escape(action_label(action))}</b>",
        ]
        if sale.get("amount"):
            lines.append(f"Сумма: <b>{escape(str(sale.get('amount')))} {escape(str(sale.get('currency') or ''))}</b>")
        if status == "waiting_order_id":
            lines.append("Статус: ⏳ ждём ID заказа")
        elif status == "delivered":
            delivery = result.get("delivery") or {}
            lines.append(f"Статус: ✅ выдано")
            if delivery.get("xyranet_order_id"):
                lines.append(f"ID заказа: <code>{escape(str(delivery['xyranet_order_id']))}</code>")
        elif status == "duplicate":
            lines.append("Статус: ♻️ повтор, отправлена сохранённая выдача")
        else:
            lines.append(f"Статус: <b>{escape(status)}</b>")
        return "\n".join(lines)

    def chat_message_notification(marketplace: str, external_order_id: str, text: str) -> str:
        return (
            "💬 <b>Новое сообщение в чате</b>\n"
            f"Площадка: <b>{escape(marketplace)}</b>\n"
            f"Заказ/чат: <code>{escape(external_order_id)}</code>\n"
            f"<pre>{escape(compact_text(text, 700))}</pre>"
        )

    def daily_statistics_text() -> str:
        data = build_sales_statistics(db.list_sales_for_statistics(), period="yesterday")
        totals = data["totals"]
        revenue = ", ".join(f"{item['text']} {item['currency']}" for item in totals.get("revenue", [])) or "0 ₽"
        return (
            "📈 <b>Ежедневная статистика</b>\n"
            f"Период: <b>{escape(str(data['period']['label']))}</b>\n"
            f"Продаж: <b>{totals['sales_count']}</b> ({totals['delivered_count']} выдано, {totals['pending_count']} ждёт)\n"
            f"Сумма: <b>{escape(revenue)}</b>\n"
            f"Расход: <b>{escape(str(totals['expense_rub']['text']))} ₽</b>\n"
            f"Прибыль: <b>{escape(str(totals['profit_rub']['text']))} ₽</b>"
        )

    async def daily_statistics_loop() -> None:
        last_sent = ""
        while True:
            try:
                today = date.today().isoformat()
                if runtime.get_bool("notify_daily_statistics") and last_sent != today:
                    notify_admins(daily_statistics_text(), kind="daily_statistics")
                    last_sent = today
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Daily statistics notification failed")
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

    def merge_sale_payload(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        detail_data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
        result = {**summary, **detail, **detail_data}
        result["raw_sale"] = summary
        result["raw_order"] = detail
        return result

    def digiseller_polling_configured() -> bool:
        return bool(runtime.get_text("digiseller_seller_id") and runtime.get_text("digiseller_api_key"))

    def marketplace_messages_configured(marketplace: str) -> bool:
        if marketplace in {"plati", "digiseller"}:
            return digiseller_polling_configured()
        if marketplace == "ggsel":
            return ggsel.configured_for_polling()
        return False

    def log_poll_error(key: str, message: str) -> None:
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

    async def process_unique_code_message(invoice_id: str, code: str) -> bool:
        db.add_order_event(
            marketplace="plati",
            external_order_id=invoice_id,
            event_type="unique_code_seen",
            payload={"unique_code": code},
        )
        try:
            purchase = await digiseller.purchase_by_unique_code(code)
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
                if not await messenger.send_message("plati", invoice_id, str(existing["delivery_text"])):
                    db.add_order_event(
                        marketplace=event.marketplace,
                        external_order_id=event.external_order_id,
                        sale_id=int(existing["id"]),
                        event_type="marketplace_message_failed",
                        status="error",
                        message="Cannot resend saved delivery",
                        payload={"invoice_id": invoice_id},
                    )
                    raise RuntimeError("не удалось отправить сохранённую выдачу в чат Digiseller")
                db.add_order_event(
                    marketplace=event.marketplace,
                    external_order_id=event.external_order_id,
                    sale_id=int(existing["id"]),
                    event_type="marketplace_message_sent",
                    status="success",
                    message="Saved delivery was resent",
                    payload={"invoice_id": invoice_id},
                )
                notify_admins(
                    "♻️ <b>Повторная отправка выдачи</b>\n"
                    f"Заказ: <code>{escape(str(existing.get('marketplace')))}:{escape(str(existing.get('external_order_id')))}</code>\n"
                    f"Чат: <code>{escape(invoice_id)}</code>"
                , kind="pending")
                return True
            result = await delivery_service.handle_sale(event, notify_marketplace=False)
            if not await messenger.send_message("plati", invoice_id, str(result["delivery_text"])):
                db.add_order_event(
                    marketplace=event.marketplace,
                    external_order_id=event.external_order_id,
                    sale_id=int(result["sale"]["id"]) if result.get("sale") else None,
                    pending_operation_id=int(result["pending"]["id"]) if result.get("pending") else None,
                    event_type="marketplace_message_failed",
                    status="error",
                    message="Cannot send delivery text to Digiseller chat",
                    payload={"invoice_id": invoice_id, "status": result.get("status")},
                )
                raise RuntimeError("не удалось отправить выдачу в чат Digiseller")
            db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                sale_id=int(result["sale"]["id"]) if result.get("sale") else None,
                pending_operation_id=int(result["pending"]["id"]) if result.get("pending") else None,
                event_type="marketplace_message_sent",
                status="success",
                payload={"invoice_id": invoice_id, "status": result.get("status")},
            )
            notify_admins(sale_notification_text(result, source="Digiseller chat"), kind="new_purchases")
            if result.get("status") in {"delivered", "waiting_order_id"}:
                await digiseller.mark_unique_code_delivered(code)
                db.add_order_event(
                    marketplace=event.marketplace,
                    external_order_id=event.external_order_id,
                    sale_id=int(result["sale"]["id"]) if result.get("sale") else None,
                    pending_operation_id=int(result["pending"]["id"]) if result.get("pending") else None,
                    event_type="unique_code_marked_delivered",
                    status="success",
                    payload={"unique_code": code},
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
                "⚠️ <b>Ошибка проверки уникального кода</b>\n"
                f"Чат: <code>{escape(invoice_id)}</code>\n"
                f"Код: <code>{escape(code)}</code>\n"
                f"Ошибка: <code>{escape(str(exc))}</code>"
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
                "🚨 <b>Ошибка выдачи по коду</b>\n"
                f"Чат: <code>{escape(invoice_id)}</code>\n"
                f"Код: <code>{escape(code)}</code>\n"
                f"Ошибка: <code>{escape(str(exc))}</code>"
            , kind="errors")
            return True

    def pending_operation_for_chat(marketplace: str, external_order_id: str) -> dict[str, Any] | None:
        for operation in db.list_pending_operations():
            if str(operation["marketplace"]) == marketplace and str(operation["external_order_id"]) == external_order_id:
                return operation
        return None

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
        try:
            result = await delivery_service.free_reissue(
                command["order_id"],
                idempotency_key=f"plati:{invoice_id}:free-reissue:{message_key or command['order_id']}",
            )
            await messenger.send_message("plati", invoice_id, str(result["delivery_text"]))
            notify_admins(
                "🔄 <b>Бесплатный перевыпуск</b>\n"
                f"Чат: <code>{escape(invoice_id)}</code>\n"
                f"ID заказа: <code>{escape(command['order_id'])}</code>\n"
                "Статус: ✅ выполнено"
            , kind="pending")
            return True
        except Exception as exc:
            log.exception("Cannot process free reissue command from chat %s", invoice_id)
            await messenger.send_message("plati", invoice_id, delivery_service.operation_error_text("reissue", exc))
            notify_admins(
                "🚨 <b>Ошибка бесплатного перевыпуска</b>\n"
                f"Чат: <code>{escape(invoice_id)}</code>\n"
                f"ID заказа: <code>{escape(command['order_id'])}</code>\n"
                f"Ошибка: <code>{escape(str(exc))}</code>"
            , kind="errors")
            return True

    async def poll_digiseller_unique_code_chats() -> None:
        if not digiseller_polling_configured():
            return
        try:
            chats = await digiseller.order_chats(filter_new=True, rows=100)
        except Exception:
            log_poll_error("digiseller_chats", "Cannot read Digiseller unread chats")
            return
        for chat in chats:
            invoice_id = chat_invoice_id(chat)
            if not invoice_id:
                continue
            last_seen = db.get_chat_cursor("plati", invoice_id)
            try:
                messages = await digiseller.order_messages(
                    invoice_id,
                    count=50,
                    newer=bool(last_seen),
                    old_id=last_seen,
                )
            except Exception:
                log_poll_error("digiseller_messages", f"Cannot read Digiseller messages for {invoice_id}")
                continue
            if not messages and last_seen:
                try:
                    await digiseller.mark_order_messages_seen(invoice_id)
                except Exception:
                    log_poll_error("digiseller_seen", f"Cannot mark Digiseller chat as seen for {invoice_id}")
                continue
            last_message = last_seen
            handled = False
            for message in messages:
                current_id = message_id(message)
                if current_id:
                    last_message = current_id
                text = message_text(message)
                if text and current_id != last_seen:
                    notify_admins(chat_message_notification("plati", invoice_id, text), kind="chat_messages")
                for code in unique_codes_from_text(text):
                    handled = await process_unique_code_message(invoice_id, code)
                    if handled:
                        break
                if handled:
                    break
                handled = await process_free_reissue_command(invoice_id, text, current_id)
                if handled:
                    break
            if last_message and last_message != last_seen:
                db.set_chat_cursor("plati", invoice_id, last_message)
            if handled:
                try:
                    await digiseller.mark_order_messages_seen(invoice_id)
                except Exception:
                    log_poll_error("digiseller_seen", f"Cannot mark Digiseller chat as seen for {invoice_id}")

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

        for sale in reversed(pending_sales):
            order_id = ggsel_order_id(sale)
            try:
                detail = await ggsel.order_info(order_id)
            except Exception:
                log_poll_error("ggsel_order_info", f"Cannot read GGsel order info for {order_id}")
                detail = {}
            payload = merge_sale_payload(sale, detail)
            try:
                event = normalize_sale("ggsel", payload)
                result = await delivery_service.handle_sale(event)
                notify_admins(sale_notification_text(result, source="GGsel polling"), kind="new_purchases")
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
                    "🚨 <b>Ошибка обработки продажи GGsel</b>\n"
                    f"Заказ: <code>{escape(order_id)}</code>\n"
                    f"Ошибка: <code>{escape(str(exc))}</code>",
                    kind="errors",
                )
        db.set_chat_cursor("ggsel", cursor_key, newest_order_id)

    async def poll_marketplace_chats() -> None:
        while True:
            try:
                await poll_digiseller_unique_code_chats()
                await poll_ggsel_sales()
                for operation in db.list_pending_operations():
                    if not marketplace_messages_configured(str(operation["marketplace"])):
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
                    should_process = not last_seen
                    for message in messages:
                        current_id = message_id(message)
                        if current_id:
                            last_message = current_id
                        if not should_process:
                            if current_id == last_seen:
                                should_process = True
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
                            "⚠️ <b>Неверная команда в pending-заказе</b>\n"
                            f"Заказ: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"Ожидали: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"Получили: <code>{escape(wrong_command_action)}</code>"
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
                            "✅ <b>Услуга применена</b>\n"
                            f"Заказ: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"Действие: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"ID заказа: <code>{escape(found_order_id)}</code>\n"
                            f"Статус: <b>{escape(str(result.get('status') or 'delivered'))}</b>"
                        , kind="pending")
                    except Exception as exc:
                        db.fail_pending_operation(int(operation["id"]), str(exc))
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
                            "🚨 <b>Ошибка pending-операции</b>\n"
                            f"Заказ: <code>{escape(str(operation['marketplace']))}:{escape(str(operation['external_order_id']))}</code>\n"
                            f"Действие: <b>{escape(action_label(str(operation['action'])))}</b>\n"
                            f"ID заказа: <code>{escape(found_order_id)}</code>\n"
                            f"Ошибка: <code>{escape(str(exc))}</code>"
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
                        restart_bot=restart_telegram_bot,
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
            return await start_telegram_bot()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal chat_task, daily_task
        db.init()
        async with bot_lock:
            await start_telegram_bot()
        chat_task = asyncio.create_task(poll_marketplace_chats())
        daily_task = asyncio.create_task(daily_statistics_loop())
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
        async with bot_lock:
            await stop_telegram_bot()

    app = FastAPI(title="XyraNet Reseller Autoseller", version="0.1.0", lifespan=lifespan)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token or token != settings.admin_token:
            raise HTTPException(status_code=401, detail="Invalid admin token")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (static_dir / "index.html").read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.post("/admin/api/login")
    async def admin_login(payload: LoginIn) -> dict[str, str]:
        username_ok = secrets.compare_digest(payload.username, runtime.get_text("admin_username"))
        password_ok = secrets.compare_digest(payload.password, runtime.get_text("admin_password"))
        if not username_ok or not password_ok:
            raise HTTPException(status_code=401, detail="Invalid admin credentials")
        return {"token": settings.admin_token, "username": runtime.get_text("admin_username")}

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
        if action not in {"renew", "reissue", "traffic", "ip_limit"}:
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
                follow_redirects=True,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                },
            ) as client:
                response = await client.get(source)
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
        data["marketplace"] = data["marketplace"].strip().lower()
        if data["marketplace"] not in SUPPORTED_MARKETPLACES:
            raise HTTPException(status_code=400, detail="Supported marketplaces: plati, digiseller, ggsel")
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
        data["marketplace"] = data["marketplace"].strip().lower()
        if data["marketplace"] not in SUPPORTED_MARKETPLACES:
            raise HTTPException(status_code=400, detail="Supported marketplaces: plati, digiseller, ggsel")
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
        if not db.delete_product(product_id):
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
        ok = await messenger.send_message(str(sale["marketplace"]), chat_id, str(sale["delivery_text"]))
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
            await notifier.send_admins("✅ <b>Тестовое уведомление</b>\nTelegram-уведомления работают.")
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
