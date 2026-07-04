from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import re
from string import Template
from typing import Any

from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent
from reseller_autoseller.statistics import expense_from_raw_response, parse_decimal


DEFAULT_CREATE_TEMPLATE = """✅ VPN-доступ готов

🔑 ID заказа: {ORDER_ID}
👤 Профиль: {PANEL_USERNAME}
📦 Тариф: {TARIFF_CODE}
👥 Устройств: {DEVICE_LIMIT}
📶 LTE-квота: {LTE_QUOTA}
⏳ Действует до: {EXPIRE_AT}

🔗 Ссылка подключения:
{SUBSCRIPTION_URL}

Сохраните ID заказа. Он понадобится для продления, докупки LTE-трафика и расширения лимита IP.
Не публикуйте ссылку подписки в открытом доступе."""

DEFAULT_ACTION_TEMPLATES = {
    "create": DEFAULT_CREATE_TEMPLATE,
    "renew": """✅ Подписка продлена.

🔑 ID заказа: {ORDER_ID}
📦 Тариф: {TARIFF_CODE}
👥 Устройств: {DEVICE_LIMIT}
📶 LTE-квота: {LTE_QUOTA}
⏳ Действует до: {EXPIRE_AT}

🔗 Ссылка подключения:
{SUBSCRIPTION_URL}""",
    "reissue": """✅ Доступ перевыпущен.

🔑 ID заказа: {ORDER_ID}
🔗 Ссылка подключения:
{SUBSCRIPTION_URL}""",
    "traffic": """✅ LTE-трафик добавлен.

🔑 ID заказа: {ORDER_ID}
📶 LTE-квота: {LTE_QUOTA}""",
    "ip_limit": """✅ IP-лимит увеличен.

🔑 ID заказа: {ORDER_ID}
👥 Устройств: {DEVICE_LIMIT}""",
}

DEFAULT_ACTION_TEMPLATES.update(
    {
        "command_help": """ℹ️ Бесплатный перевыпуск подписки:
!reissue {ORDER_ID} — получить новую ссылку подписки""",
        "ask_renew": """✅ Оплата продления получена.

Чтобы применить продление, отправьте в этот чат:
{COMMAND_EXAMPLE}

ID заказа указан в первой выдаче доступа.""",
        "ask_reissue": """✅ Оплата перевыпуска получена.

Чтобы перевыпустить подписку, отправьте в этот чат:
{COMMAND_EXAMPLE}

ID заказа указан в первой выдаче доступа.""",
        "ask_traffic": """✅ Оплата LTE-трафика получена.

Чтобы добавить трафик, отправьте в этот чат:
{COMMAND_EXAMPLE}

ID заказа указан в первой выдаче доступа.""",
        "ask_ip_limit": """✅ Оплата расширения IP-лимита получена.

Чтобы увеличить лимит, отправьте в этот чат:
{COMMAND_EXAMPLE}

ID заказа указан в первой выдаче доступа.""",
        "command_mismatch": """⚠️ Для этой покупки ожидается команда {COMMAND_EXAMPLE}.

Полученная команда не совпадает с оплаченным действием.""",
        "operation_error": """⚠️ Не удалось применить услугу.

Действие: {ACTION_LABEL}
Ошибка: {ERROR}""",
        "free_reissue_help": """ℹ️ Чтобы бесплатно перевыпустить подписку, отправьте:
!reissue {ORDER_ID}

ID заказа указан в первой выдаче доступа.""",
        "free_reissue_disabled": """ℹ️ Бесплатный перевыпуск через чат сейчас отключён.

Чтобы перевыпустить подписку, купите лот перевыпуска и отправьте ID заказа в чат после оплаты.""",
    }
)

for _action in ("renew", "reissue", "traffic", "ip_limit"):
    DEFAULT_ACTION_TEMPLATES.setdefault(f"{_action}_error", DEFAULT_ACTION_TEMPLATES["operation_error"])
    DEFAULT_ACTION_TEMPLATES.setdefault(f"{_action}_command_mismatch", DEFAULT_ACTION_TEMPLATES["command_mismatch"])

DEFAULT_CREATE_TEMPLATE = DEFAULT_CREATE_TEMPLATE + "\n\n{COMMAND_HELP}"
DEFAULT_ACTION_TEMPLATES["create"] = DEFAULT_CREATE_TEMPLATE

DEFAULT_DELIVERY_TEMPLATE = DEFAULT_CREATE_TEMPLATE

ACTION_LABELS = {
    "create": "Покупка",
    "renew": "Продление",
    "reissue": "Перевыпуск",
    "traffic": "LTE-трафик",
    "ip_limit": "IP-лимит",
}

ACTION_LABELS.update(
    {
        "command_help": "Команды в чате",
        "ask_renew": "Запрос ID: продление",
        "ask_reissue": "Запрос ID: перевыпуск",
        "ask_traffic": "Запрос ID: LTE-трафик",
        "ask_ip_limit": "Запрос ID: IP-лимит",
        "command_mismatch": "Ошибка команды",
        "operation_error": "Ошибка выполнения",
        "free_reissue_help": "Подсказка бесплатного перевыпуска",
        "free_reissue_disabled": "Бесплатный перевыпуск отключён",
    }
)

TEMPLATE_CATEGORIES = {
    "create": "Выдача",
    "renew": "Выдача",
    "reissue": "Выдача",
    "traffic": "Выдача",
    "ip_limit": "Выдача",
    "command_help": "Команды",
    "ask_renew": "Запрос order_id",
    "ask_reissue": "Запрос order_id",
    "ask_traffic": "Запрос order_id",
    "ask_ip_limit": "Запрос order_id",
    "command_mismatch": "Ошибки",
    "operation_error": "Ошибки",
    "free_reissue_help": "Команды",
    "free_reissue_disabled": "Команды",
}

OPERATION_ACTIONS = {"create", "renew", "reissue", "traffic", "ip_limit"}

BASE_ACTION_LABELS = {
    "create": ACTION_LABELS["create"],
    "renew": ACTION_LABELS["renew"],
    "reissue": ACTION_LABELS["reissue"],
    "traffic": ACTION_LABELS["traffic"],
    "ip_limit": ACTION_LABELS["ip_limit"],
}

DEFAULT_CHAT_COMMANDS = {
    "renew": "!renew",
    "reissue": "!reissue",
    "traffic": "!traffic",
    "ip_limit": "!ip",
}

TEMPLATE_GROUPS = [
    {
        "key": "create",
        "label": BASE_ACTION_LABELS["create"],
        "command_action": "reissue",
        "stages": [
            {"key": "create", "stage": "delivered", "label": "Заказ выдан"},
            {"key": "free_reissue_help", "stage": "free_reissue_help", "label": "Команда без ID заказа"},
            {"key": "free_reissue_disabled", "stage": "free_reissue_disabled", "label": "Бесплатный перевыпуск отключён"},
        ],
    },
    {
        "key": "renew",
        "label": BASE_ACTION_LABELS["renew"],
        "command_action": "renew",
        "stages": [
            {"key": "ask_renew", "stage": "waiting_param", "label": "Заказ получен, ждём order_id"},
            {"key": "renew", "stage": "delivered", "label": "order_id получен, продление выполнено"},
            {"key": "renew_command_mismatch", "stage": "wrong_command", "label": "Команда не совпала с лотом"},
            {"key": "renew_error", "stage": "error", "label": "Ошибка выполнения"},
        ],
    },
    {
        "key": "reissue",
        "label": BASE_ACTION_LABELS["reissue"],
        "command_action": "reissue",
        "stages": [
            {"key": "ask_reissue", "stage": "waiting_param", "label": "Платный заказ получен, ждём order_id"},
            {"key": "reissue", "stage": "delivered", "label": "order_id получен, перевыпуск выполнен"},
            {"key": "reissue_command_mismatch", "stage": "wrong_command", "label": "Команда не совпала с лотом"},
            {"key": "reissue_error", "stage": "error", "label": "Ошибка выполнения"},
            {"key": "free_reissue_help", "stage": "free_reissue_help", "label": "Бесплатная команда без ID"},
            {"key": "free_reissue_disabled", "stage": "free_reissue_disabled", "label": "Бесплатный режим отключён"},
        ],
    },
    {
        "key": "traffic",
        "label": BASE_ACTION_LABELS["traffic"],
        "command_action": "traffic",
        "stages": [
            {"key": "ask_traffic", "stage": "waiting_param", "label": "Заказ получен, ждём order_id"},
            {"key": "traffic", "stage": "delivered", "label": "order_id получен, LTE добавлен"},
            {"key": "traffic_command_mismatch", "stage": "wrong_command", "label": "Команда не совпала с лотом"},
            {"key": "traffic_error", "stage": "error", "label": "Ошибка выполнения"},
        ],
    },
    {
        "key": "ip_limit",
        "label": BASE_ACTION_LABELS["ip_limit"],
        "command_action": "ip_limit",
        "stages": [
            {"key": "ask_ip_limit", "stage": "waiting_param", "label": "Заказ получен, ждём order_id"},
            {"key": "ip_limit", "stage": "delivered", "label": "order_id получен, IP-лимит увеличен"},
            {"key": "ip_limit_command_mismatch", "stage": "wrong_command", "label": "Команда не совпала с лотом"},
            {"key": "ip_limit_error", "stage": "error", "label": "Ошибка выполнения"},
        ],
    },
]

TEMPLATE_KEYS = {stage["key"] for group in TEMPLATE_GROUPS for stage in group["stages"]}
for _group in TEMPLATE_GROUPS:
    for _stage in _group["stages"]:
        ACTION_LABELS.setdefault(str(_stage["key"]), f"{_group['label']} / {_stage['label']}")

CHAT_COMMAND_BY_ACTION = {
    "renew": "!renew",
    "reissue": "!reissue",
    "traffic": "!traffic",
    "ip_limit": "!ip",
}
CHAT_COMMAND_ALIASES = {
    "renew": "renew",
    "extend": "renew",
    "reissue": "reissue",
    "reset": "reissue",
    "traffic": "traffic",
    "lte": "traffic",
    "gb": "traffic",
    "ip": "ip_limit",
    "ip_limit": "ip_limit",
    "limit": "ip_limit",
}
CHAT_COMMAND_RE = re.compile(r"(?:^|\s)!([a-z0-9_:-]+)\b([^\n\r]*)", re.IGNORECASE)

DELIVERY_TEMPLATE_VARIABLES = {
    "ACTION_LABEL": "Название действия",
    "COMMAND": "Команда в чате",
    "COMMAND_EXAMPLE": "Пример команды",
    "ERROR": "Текст ошибки",
    "COMMAND_HELP": "Подсказка по командам",
    "PURCHASE_QUANTITY": "Количество купленных единиц",
    "ORDER_ID": "ID заказа",
    "PANEL_USERNAME": "Профиль в панели",
    "TARIFF_CODE": "Код тарифа",
    "EXPIRE_AT": "Дата окончания",
    "SUBSCRIPTION_URL": "Ссылка подписки",
    "DEVICE_LIMIT": "Лимит устройств / IP",
    "LTE_QUOTA": "LTE-квота",
}

DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS = {
    "ACTION_LABEL": "Название текущего действия: покупка, продление, перевыпуск, LTE-трафик или IP-лимит.",
    "COMMAND": "Команда для выбранного сценария без параметров, например !renew.",
    "COMMAND_EXAMPLE": "Готовый пример команды с ID заказа, например !renew {ORDER_ID}.",
    "ERROR": "Текст ошибки, если выполнение услуги не удалось.",
    "COMMAND_HELP": "Составная переменная с большим редактируемым блоком подсказки по командам.",
    "PURCHASE_QUANTITY": "Количество купленных единиц товара в заказе.",
    "ORDER_ID": "ID заказа XyraNet, который получает покупатель.",
    "PANEL_USERNAME": "Имя профиля в панели XyraNet.",
    "TARIFF_CODE": "Код тарифа, по которому создана или продлена подписка.",
    "EXPIRE_AT": "Дата окончания подписки из ответа API.",
    "SUBSCRIPTION_URL": "Ссылка подключения к подписке.",
    "DEVICE_LIMIT": "Текущий лимит устройств или IP из ответа API.",
    "LTE_QUOTA": "LTE-квота из ответа API или параметров покупки.",
}

COMPLEX_VARIABLES_SETTING = "custom_complex_variables"
MAX_COMPLEX_VARIABLE_DEPTH = 6
COMPLEX_VARIABLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")

BUILTIN_COMPLEX_VARIABLES = {
    "COMMAND_HELP": {
        "key": "COMMAND_HELP",
        "label": "Подсказка по командам",
        "description": "Составной блок для сообщения выдачи. По умолчанию объясняет бесплатный перевыпуск подписки.",
        "template_key": "command_help",
        "default_template": DEFAULT_ACTION_TEMPLATES["command_help"],
        "builtin": True,
    }
}


ASK_ORDER_ID_TEXT = """✅ Оплата получена.

Для выполнения услуги нужен ID заказа, к которому применить покупку.
Пожалуйста, отправьте ID заказа в этот чат одним сообщением.

ID заказа указан в сообщении первой выдачи доступа как "ID заказа"."""


ORDER_ID_RE = re.compile(
    r"\b(?:(?:ord|order)[_-]?[a-z0-9][a-z0-9_-]{4,}|[a-z0-9]{8,}[-_][a-z0-9_-]{4,}|\d{5,})\b",
    re.IGNORECASE,
)

MAX_SALE_QUANTITY = 120
QUANTITY_KEYS = {
    "cnt_goods",
    "quantity",
    "qty",
    "count",
    "count_goods",
    "goods_count",
    "unit_count",
    "units",
    "items_count",
}


class DeliveryService:
    def __init__(self, *, db: Database, xyranet: Any, messenger: Any | None = None, free_reissue_enabled: Any | None = None) -> None:
        self.db = db
        self.xyranet = xyranet
        self.messenger = messenger
        self.free_reissue_enabled = free_reissue_enabled
        self._tariff_prices_rub: dict[str, Decimal] | None = None

    async def handle_sale(self, event: SaleEvent, *, notify_marketplace: bool = True) -> dict[str, Any]:
        self.db.add_order_event(
            marketplace=event.marketplace,
            external_order_id=event.external_order_id,
            event_type="sale_received",
            payload={
                "external_product_id": event.external_product_id,
                "external_variant_id": event.external_variant_id,
                "amount": event.amount,
                "currency": event.currency,
            },
        )
        existing = self.db.get_sale_with_delivery(event.marketplace, event.external_order_id)
        if existing and existing.get("delivery_id"):
            self.db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                sale_id=int(existing["id"]),
                event_type="delivery_replayed",
                message="Existing saved delivery was reused",
            )
            return {"status": "duplicate", "delivery_text": existing["delivery_text"], "sale": existing}

        product = self.db.get_product_by_external(
            event.marketplace,
            event.external_product_id,
            event.external_variant_id,
        )
        if product is None:
            product_key = f"{event.marketplace}:{event.external_product_id}"
            if event.external_variant_id:
                product_key += f":{event.external_variant_id}"
            self.db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                event_type="mapping_missing",
                status="error",
                message=product_key,
            )
            raise ValueError(f"No product mapping for {product_key}")
        if not int(product["enabled"]):
            self.db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                event_type="mapping_disabled",
                status="error",
                message=f"{event.marketplace}:{event.external_product_id}",
            )
            raise ValueError(f"Product mapping is disabled: {event.marketplace}:{event.external_product_id}")

        sale = self.db.create_sale(event)
        self.db.add_order_event(
            marketplace=event.marketplace,
            external_order_id=event.external_order_id,
            sale_id=int(sale["id"]),
            event_type="sale_saved",
            payload={"product_mapping_id": product["id"], "action": product.get("action") or "create"},
        )
        pending = self.db.get_pending_operation_by_sale(int(sale["id"]))
        if pending and pending["status"] == "waiting_order_id":
            self.db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                sale_id=int(sale["id"]),
                pending_operation_id=int(pending["id"]),
                event_type="pending_replayed",
                message="Existing pending operation was reused",
            )
            return {"status": "waiting_order_id", "delivery_text": self.ask_order_id_text(str(pending["action"])), "sale": sale, "pending": pending}

        action = str(product.get("action") or "create").strip().lower()
        action_params = self.product_action_params(product)
        idempotency_key = f"{event.marketplace}:{event.external_order_id}"

        if action != "create":
            target_order_id = extract_target_order_id(event.raw_payload)
            if not target_order_id:
                pending = self.db.create_pending_operation(
                    sale_id=int(sale["id"]),
                    product_id=int(product["id"]),
                    marketplace=event.marketplace,
                    external_order_id=marketplace_chat_order_id(event),
                    action=action,
                    action_params=action_params,
                )
                self.db.add_order_event(
                    marketplace=event.marketplace,
                    external_order_id=marketplace_chat_order_id(event),
                    sale_id=int(sale["id"]),
                    pending_operation_id=int(pending["id"]),
                    event_type="pending_created",
                    message="Waiting for target order_id",
                    payload={"action": action, "action_params": action_params},
                )
                ask_text = self.ask_order_id_text(action)
                if notify_marketplace:
                    await self.send_marketplace_message(event.marketplace, marketplace_chat_order_id(event), ask_text)
                return {"status": "waiting_order_id", "delivery_text": ask_text, "sale": sale, "pending": pending}
            return await self.complete_operation(
                sale=sale,
                product=product,
                action=action,
                action_params=action_params,
                target_order_id=target_order_id,
                idempotency_key=idempotency_key,
                message_order_id=marketplace_chat_order_id(event),
            )

        quantity = sale_quantity(event.raw_payload)
        self.db.add_order_event(
            marketplace=event.marketplace,
            external_order_id=event.external_order_id,
            sale_id=int(sale["id"]),
            event_type="xyranet_create_started",
            payload={"tariff_code": product["tariff_code"], "quantity": quantity},
        )
        api_response = await self.xyranet.create_order(product["tariff_code"], idempotency_key=idempotency_key)
        delivery = extract_order_delivery(api_response)
        if not delivery["order_id"] or not delivery["subscription_url"]:
            raise ValueError("XyraNet response does not contain order_id or subscription_url")
        renew_responses: list[dict[str, Any]] = []
        for item_number in range(2, quantity + 1):
            renew_response = await self.xyranet.renew_order(
                delivery["order_id"],
                product.get("tariff_code") or None,
                idempotency_key=f"{idempotency_key}:quantity-renew:{item_number}",
            )
            renew_responses.append(renew_response)
            renewed_delivery = extract_order_delivery(renew_response)
            delivery = {**delivery, **{key: value for key, value in renewed_delivery.items() if value}}
            self.db.add_order_event(
                marketplace=event.marketplace,
                external_order_id=event.external_order_id,
                sale_id=int(sale["id"]),
                event_type="xyranet_quantity_renewed",
                payload={"item_number": item_number, "order_id": delivery["order_id"]},
            )
        delivery["purchase_quantity"] = str(quantity)
        delivery["command_help"] = self.render_system_text("command_help", delivery) if self.is_free_reissue_enabled() else ""

        delivery_text = self.render_action_text("create", delivery)
        raw_response: dict[str, Any] = api_response
        if renew_responses:
            raw_response = {
                "quantity": quantity,
                "create": api_response,
                "renewals": renew_responses,
            }
        raw_response = with_statistics_expense(
            raw_response,
            await self.estimate_subscription_expense(product.get("tariff_code") or "", quantity),
        )
        saved = self.db.create_delivery(
            int(sale["id"]),
            int(product["id"]),
            {
                "xyranet_order_id": delivery["order_id"],
                "subscription_url": delivery["subscription_url"],
                "panel_username": delivery["panel_username"],
                "tariff_code": delivery["tariff_code"],
                "delivery_text": delivery_text,
                "raw_response": raw_response,
            },
        )
        self.db.add_order_event(
            marketplace=event.marketplace,
            external_order_id=event.external_order_id,
            sale_id=int(sale["id"]),
            event_type="delivery_saved",
            status="success",
            payload={"delivery_id": saved["id"], "xyranet_order_id": delivery["order_id"]},
        )
        if notify_marketplace:
            await self.send_marketplace_message(event.marketplace, marketplace_chat_order_id(event), delivery_text)
        return {"status": "delivered", "delivery_text": delivery_text, "sale": sale, "delivery": saved}

    async def complete_pending_operation(self, pending: dict[str, Any], target_order_id: str) -> dict[str, Any]:
        sale = self.db.get_sale_with_delivery(str(pending["marketplace"]), str(pending["external_order_id"]))
        if not sale:
            raise ValueError("Pending sale was not found")
        product = self.db.get_product_by_external(
            str(sale["marketplace"]),
            str(sale["external_product_id"]),
            str(sale.get("external_variant_id") or ""),
        )
        if not product:
            raise ValueError("Pending product mapping was not found")
        result = await self.complete_operation(
            sale=sale,
            product=product,
            action=str(pending["action"]),
            action_params=json.loads(str(pending.get("action_params") or "{}")),
            target_order_id=target_order_id,
            idempotency_key=f"{pending['marketplace']}:{pending['external_order_id']}:pending:{pending['id']}",
            message_order_id=str(pending["external_order_id"]),
        )
        self.db.complete_pending_operation(
            int(pending["id"]),
            target_order_id=target_order_id,
            result_text=result["delivery_text"],
            raw_response=result.get("raw_response") or {},
        )
        self.db.add_order_event(
            marketplace=str(pending["marketplace"]),
            external_order_id=str(pending["external_order_id"]),
            sale_id=int(pending["sale_id"]),
            pending_operation_id=int(pending["id"]),
            event_type="pending_completed",
            status="success",
            payload={"target_order_id": target_order_id, "action": pending["action"]},
        )
        return {**result, "pending": pending}

    async def complete_operation(
        self,
        *,
        sale: dict[str, Any],
        product: dict[str, Any],
        action: str,
        action_params: dict[str, Any],
        target_order_id: str,
        idempotency_key: str,
        message_order_id: str | None = None,
    ) -> dict[str, Any]:
        if action == "renew":
            self.db.add_order_event(
                marketplace=str(sale["marketplace"]),
                external_order_id=str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
                event_type="xyranet_operation_started",
                payload={"action": action, "target_order_id": target_order_id},
            )
            response = await self.xyranet.renew_order(
                target_order_id,
                product.get("tariff_code") or None,
                idempotency_key=idempotency_key,
            )
            delivery = extract_order_delivery(response)
            text = self.render_action_text("renew", {**delivery, "order_id": target_order_id})
            raw_response = with_statistics_expense(
                response,
                await self.estimate_subscription_expense(product.get("tariff_code") or "", 1),
            )
        elif action == "reissue":
            self.db.add_order_event(
                marketplace=str(sale["marketplace"]),
                external_order_id=str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
                event_type="xyranet_operation_started",
                payload={"action": action, "target_order_id": target_order_id},
            )
            response = await self.xyranet.reissue_order(target_order_id, idempotency_key=idempotency_key)
            delivery = extract_order_delivery(response)
            text = self.render_action_text("reissue", {**delivery, "order_id": target_order_id})
            raw_response = with_statistics_expense(response, Decimal("0"))
        elif action == "traffic":
            self.db.add_order_event(
                marketplace=str(sale["marketplace"]),
                external_order_id=str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
                event_type="xyranet_operation_started",
                payload={"action": action, "target_order_id": target_order_id, "action_params": action_params},
            )
            quote = await self.safe_operation_quote("traffic", target_order_id, action_params)
            response = await self.xyranet.traffic_purchase(target_order_id, action_params, idempotency_key=idempotency_key)
            delivery = extract_order_delivery(response)
            text = self.render_action_text("traffic", {**delivery, "order_id": target_order_id, "lte_quota": lte_quota_from_action(action_params)})
            raw_response = with_statistics_expense({"quote": quote, "purchase": response}, expense_from_raw_response(response) or expense_from_raw_response(quote))
        elif action == "ip_limit":
            self.db.add_order_event(
                marketplace=str(sale["marketplace"]),
                external_order_id=str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
                event_type="xyranet_operation_started",
                payload={"action": action, "target_order_id": target_order_id, "action_params": action_params},
            )
            quote = await self.safe_operation_quote("ip_limit", target_order_id, action_params)
            response = await self.xyranet.ip_limit_purchase(target_order_id, action_params, idempotency_key=idempotency_key)
            delivery = extract_order_delivery(response)
            text = self.render_action_text("ip_limit", {**delivery, "order_id": target_order_id})
            raw_response = with_statistics_expense({"quote": quote, "purchase": response}, expense_from_raw_response(response) or expense_from_raw_response(quote))
        else:
            raise ValueError(f"Unsupported action: {action}")

        saved = self.db.create_delivery(
            int(sale["id"]),
            int(product["id"]),
            {
                "xyranet_order_id": target_order_id,
                "subscription_url": delivery.get("subscription_url") or "",
                "panel_username": delivery.get("panel_username") or "",
                "tariff_code": delivery.get("tariff_code") or str(product.get("tariff_code") or ""),
                "delivery_text": text,
                "raw_response": raw_response,
            },
        )
        self.db.add_order_event(
            marketplace=str(sale["marketplace"]),
            external_order_id=str(sale["external_order_id"]),
            sale_id=int(sale["id"]),
            event_type="delivery_saved",
            status="success",
            payload={"delivery_id": saved["id"], "xyranet_order_id": target_order_id, "action": action},
        )
        await self.send_marketplace_message(str(sale["marketplace"]), message_order_id or str(sale["external_order_id"]), text)
        return {"status": "delivered", "delivery_text": text, "sale": sale, "delivery": saved, "raw_response": raw_response}

    async def send_marketplace_message(self, marketplace: str, external_order_id: str, text: str) -> None:
        if not self.messenger:
            return
        try:
            await asyncio.wait_for(self.messenger.send_message(marketplace, external_order_id, text), timeout=5)
        except Exception:
            return

    def is_free_reissue_enabled(self) -> bool:
        if self.free_reissue_enabled is None:
            return True
        if callable(self.free_reissue_enabled):
            return bool(self.free_reissue_enabled())
        return bool(self.free_reissue_enabled)

    async def free_reissue(self, target_order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        response = await self.xyranet.reissue_order(target_order_id, idempotency_key=idempotency_key)
        delivery = extract_order_delivery(response)
        text = self.render_action_text("reissue", {**delivery, "order_id": target_order_id})
        return {"status": "delivered", "delivery_text": text, "raw_response": response}

    @staticmethod
    def product_action_params(product: dict[str, Any]) -> dict[str, Any]:
        try:
            params = json.loads(str(product.get("action_params") or "{}"))
            return params if isinstance(params, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def render_delivery_text(template: str, values: dict[str, str]) -> str:
        source = template.strip() or DEFAULT_DELIVERY_TEMPLATE
        return render_template(source, values)

    def render_action_text(self, action: str, values: dict[str, Any]) -> str:
        source = self.db.get_setting(delivery_template_key(action)) or DEFAULT_ACTION_TEMPLATES.get(action, DEFAULT_CREATE_TEMPLATE)
        return self.render_template_with_complex_variables(source, {**self.command_context(action), **values})

    def render_system_text(self, template_key: str, values: dict[str, Any] | None = None) -> str:
        source = self.db.get_setting(delivery_template_key(template_key)) or DEFAULT_ACTION_TEMPLATES.get(template_key, "")
        return self.render_template_with_complex_variables(source, values or self.command_context(""))

    def render_template_with_complex_variables(self, source: str, values: dict[str, Any]) -> str:
        context = {str(key): "" if value is None else str(value) for key, value in values.items()}
        for _ in range(MAX_COMPLEX_VARIABLE_DEPTH):
            changed = False
            for variable in self.list_complex_variables():
                key = str(variable["key"])
                lower_key = key.lower()
                if lower_key in context or key in context:
                    rendered = str(context.get(lower_key, context.get(key, "")))
                else:
                    rendered = render_template(str(variable.get("template") or variable.get("default_template") or ""), context)
                if context.get(lower_key) != rendered:
                    context[lower_key] = rendered
                    changed = True
            if not changed:
                break
        return render_template(source, context)

    def list_complex_variables(self) -> list[dict[str, Any]]:
        variables: list[dict[str, Any]] = []
        for variable in BUILTIN_COMPLEX_VARIABLES.values():
            template_key = str(variable["template_key"])
            template = self.db.get_setting(delivery_template_key(template_key)) or ""
            variables.append({**variable, "template": template})
        variables.extend(self.custom_complex_variables())
        return variables

    def custom_complex_variables(self) -> list[dict[str, Any]]:
        raw = self.db.get_setting(COMPLEX_VARIABLES_SETTING) or "[]"
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        variables: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            key = normalize_complex_variable_key(str(item.get("key") or ""))
            if not key or key in DELIVERY_TEMPLATE_VARIABLES:
                continue
            variables.append(
                {
                    "key": key,
                    "label": str(item.get("label") or key),
                    "description": str(item.get("description") or "Пользовательская составная переменная."),
                    "template": str(item.get("template") or ""),
                    "default_template": "",
                    "builtin": False,
                }
            )
        return variables

    def save_complex_variable(self, *, key: str, label: str, template: str) -> dict[str, Any]:
        normalized = normalize_complex_variable_key(key)
        if not normalized:
            raise ValueError("Имя переменной должно быть в формате CUSTOM_TEXT: латиница, цифры и подчёркивание.")
        if normalized in DELIVERY_TEMPLATE_VARIABLES and normalized not in BUILTIN_COMPLEX_VARIABLES:
            raise ValueError("Это имя уже занято обычной переменной.")
        if normalized in BUILTIN_COMPLEX_VARIABLES:
            template_key = str(BUILTIN_COMPLEX_VARIABLES[normalized]["template_key"])
            db_template = template.strip()
            self.db.set_setting(delivery_template_key(template_key), db_template)
            return {**BUILTIN_COMPLEX_VARIABLES[normalized], "template": db_template}
        variables = [item for item in self.custom_complex_variables() if item["key"] != normalized]
        saved = {
            "key": normalized,
            "label": label.strip() or normalized,
            "description": "Пользовательская составная переменная.",
            "template": template.strip(),
            "default_template": "",
            "builtin": False,
        }
        variables.append(saved)
        variables.sort(key=lambda item: str(item["key"]))
        self.db.set_setting(COMPLEX_VARIABLES_SETTING, json.dumps(variables, ensure_ascii=False, sort_keys=True))
        return saved

    def delete_complex_variable(self, key: str) -> bool:
        normalized = normalize_complex_variable_key(key)
        if not normalized or normalized in BUILTIN_COMPLEX_VARIABLES:
            return False
        variables = self.custom_complex_variables()
        kept = [item for item in variables if item["key"] != normalized]
        if len(kept) == len(variables):
            return False
        self.db.set_setting(COMPLEX_VARIABLES_SETTING, json.dumps(kept, ensure_ascii=False, sort_keys=True))
        return True

    def ask_order_id_text(self, action: str) -> str:
        key = f"ask_{action}"
        if key not in DEFAULT_ACTION_TEMPLATES:
            key = "ask_renew"
        return self.render_system_text(key, self.command_context(action))

    def command_mismatch_text(self, expected_action: str, actual_action: str) -> str:
        values = {
            **self.command_context(expected_action),
            "actual_action": actual_action,
            "actual_action_label": ACTION_LABELS.get(actual_action, actual_action),
        }
        return self.render_system_text(f"{expected_action}_command_mismatch", values)

    def operation_error_text(self, action: str, error: Any) -> str:
        return self.render_system_text(
            f"{action}_error",
            {**self.command_context(action), "error": str(error)},
        )

    def expected_command(self, action: str) -> str:
        command = self.db.get_setting(f"chat_command_{action}") or DEFAULT_CHAT_COMMANDS.get(action, "")
        command = normalize_chat_command(command)
        return command or DEFAULT_CHAT_COMMANDS.get(action, "")

    def action_for_command(self, command: str) -> str:
        normalized = normalize_chat_command(command)
        for action in ("renew", "reissue", "traffic", "ip_limit"):
            if normalized == self.expected_command(action):
                return action
        return CHAT_COMMAND_ALIASES.get(normalized.lstrip("!"), "")

    def set_expected_command(self, action: str, command: str) -> str:
        if action not in DEFAULT_CHAT_COMMANDS:
            raise ValueError(f"Unsupported command action: {action}")
        normalized = normalize_chat_command(command)
        if not normalized:
            raise ValueError("Command cannot be empty")
        self.db.set_setting(f"chat_command_{action}", normalized)
        return normalized

    async def estimate_subscription_expense(self, tariff_code: str, quantity: int) -> Decimal:
        price = await self.tariff_price_rub(tariff_code)
        return price * max(1, quantity)

    async def tariff_price_rub(self, tariff_code: str) -> Decimal:
        code = str(tariff_code or "").strip().lower()
        if not code or not hasattr(self.xyranet, "tariffs"):
            return Decimal("0")
        if self._tariff_prices_rub is None:
            prices: dict[str, Decimal] = {}
            try:
                rows = await self.xyranet.tariffs()
            except Exception:
                rows = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                row_code = str(row.get("code") or row.get("tariff_code") or "").strip().lower()
                if not row_code:
                    continue
                price = (
                    parse_decimal(row.get("api_price_rub"))
                    or parse_decimal(row.get("wholesale_price_rub"))
                    or parse_decimal(row.get("price_rub"))
                    or parse_decimal(row.get("cost_rub"))
                )
                if price:
                    prices[row_code] = price
            self._tariff_prices_rub = prices
        return self._tariff_prices_rub.get(code, Decimal("0"))

    async def safe_operation_quote(self, action: str, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if action == "traffic" and hasattr(self.xyranet, "traffic_quote"):
                return await self.xyranet.traffic_quote(order_id, payload)
            if action == "ip_limit" and hasattr(self.xyranet, "ip_limit_quote"):
                return await self.xyranet.ip_limit_quote(order_id, payload)
        except Exception:
            return {}
        return {}

    def command_context(self, action: str) -> dict[str, str]:
        command = self.expected_command(action)
        return {
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "command": command,
            "command_example": f"{command} {{ORDER_ID}}",
            "order_id": "{ORDER_ID}",
        }


def extract_order_delivery(response: dict[str, Any]) -> dict[str, str]:
    order = response.get("order") or {}
    subscription = order.get("subscription") or {}
    return {
        "order_id": str(order.get("order_id") or ""),
        "panel_username": str(order.get("panel_username") or ""),
        "subscription_url": str(subscription.get("subscription_url") or ""),
        "tariff_code": str(subscription.get("tariff_code") or ""),
        "expire_at": str(subscription.get("expire_at") or ""),
        "device_limit": str(subscription.get("ip_limit") or subscription.get("device_limit") or order.get("ip_limit") or order.get("device_limit") or ""),
        "lte_quota": format_lte_quota(
            subscription.get("lte_quota")
            or subscription.get("traffic_quota")
            or subscription.get("traffic_limit")
            or subscription.get("included_traffic_bytes")
            or subscription.get("traffic_limit_bytes")
        ),
    }


def extract_target_order_id(payload: dict[str, Any]) -> str:
    keys = {
        "xyranet_order_id",
        "xyra_order_id",
        "target_order_id",
        "subscription_order_id",
        "vpn_order_id",
    }
    found = _extract_key_recursive(payload, keys)
    if found:
        return found
    text = json.dumps(payload, ensure_ascii=False)
    return extract_order_id_from_text(text)


def clean_order_id(value: str) -> str:
    return value.strip().strip("{}[]()<>,.:;\"'")


def normalize_chat_command(value: str) -> str:
    command = str(value or "").strip().lower()
    if not command:
        return ""
    if not command.startswith("!"):
        command = f"!{command}"
    command = re.sub(r"\s+", "", command)
    if not re.fullmatch(r"![a-z0-9_:-]{2,32}", command):
        return ""
    return command


def is_real_order_id(value: str) -> bool:
    normalized = clean_order_id(value)
    return bool(normalized) and normalized.lower() not in {"order_id", "orderid", "id", "заказ", "id_заказа"}


def extract_order_id_from_text(text: str) -> str:
    for match in ORDER_ID_RE.finditer(text):
        candidate = clean_order_id(match.group(0))
        if is_real_order_id(candidate):
            return candidate
    return ""


def parse_chat_command(text: str) -> dict[str, str] | None:
    match = CHAT_COMMAND_RE.search(text)
    if not match:
        return None
    raw_command = match.group(1).strip().lower()
    action = CHAT_COMMAND_ALIASES.get(raw_command)
    command = normalize_chat_command(raw_command)
    if not command:
        return None
    tail = match.group(2) or ""
    order_id = extract_order_id_from_text(tail)
    if not order_id:
        order_id = extract_order_id_from_text(text)
    return {"command": command, "action": action or "", "order_id": order_id}


def marketplace_chat_order_id(event: SaleEvent) -> str:
    if event.marketplace in {"plati", "digiseller"}:
        invoice_id = event.raw_payload.get("inv") or event.raw_payload.get("id_i") or event.raw_payload.get("invoice_id")
        if invoice_id not in (None, ""):
            return str(invoice_id)
    return event.external_order_id


def _extract_key_recursive(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and str(item).strip():
                return str(item).strip()
            found = _extract_key_recursive(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _extract_key_recursive(item, keys)
            if found:
                return found
    return ""


def render_operation_text(prefix: str, order_id: str, delivery: dict[str, str], response: dict[str, Any]) -> str:
    lines = [prefix, "", f"🔑 ID заказа: {order_id}"]
    if delivery.get("expire_at"):
        lines.append(f"⏳ Действует до: {delivery['expire_at']}")
    if delivery.get("subscription_url"):
        lines.extend(["", "🔗 Ссылка подписки:", delivery["subscription_url"]])
    if not delivery.get("subscription_url"):
        detail = response.get("message") or response.get("status") or ""
        if detail:
            lines.extend(["", str(detail)])
    return "\n".join(lines).strip()


def delivery_template_key(action: str) -> str:
    return f"delivery_template_{action}"


def with_statistics_expense(response: dict[str, Any], expense_rub: Decimal) -> dict[str, Any]:
    return {**response, "statistics_expense_rub": str(expense_rub)}


def normalize_complex_variable_key(value: str) -> str:
    key = str(value or "").strip().upper()
    key = key.removeprefix("{").removesuffix("}")
    key = re.sub(r"[^A-Z0-9_]", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")
    return key if COMPLEX_VARIABLE_NAME_RE.fullmatch(key) else ""


def render_template(source: str, values: dict[str, Any]) -> str:
    normalized = {str(key): "" if value is None else str(value) for key, value in values.items()}
    normalized.update({key.upper(): value for key, value in normalized.items()})
    text = Template(source).safe_substitute(normalized)
    for key, value in normalized.items():
        text = text.replace("{" + key.upper() + "}", value)
    return text


def format_lte_quota(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            value = float(stripped)
        except ValueError:
            return stripped
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number <= 0:
        return ""
    if number >= 1024 * 1024 * 1024:
        gb = number / 1024 / 1024 / 1024
        return f"{gb:g} ГБ"
    return f"{number:g} ГБ"


def lte_quota_from_action(action_params: dict[str, Any]) -> str:
    value = action_params.get("gigabytes") or action_params.get("gb")
    return format_lte_quota(value)


def sale_quantity(payload: dict[str, Any]) -> int:
    value = _extract_key_recursive(payload, QUANTITY_KEYS)
    if not value:
        return 1
    try:
        quantity = int(Decimal(str(value).replace(",", ".")).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError):
        return 1
    if quantity < 1:
        return 1
    if quantity > MAX_SALE_QUANTITY:
        raise ValueError(f"Sale quantity is too large: {quantity}")
    return quantity
