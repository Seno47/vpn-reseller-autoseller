from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import re
from string import Template
from typing import Any, Callable
from weakref import WeakValueDictionary

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

DEFAULT_REVIEW_REQUEST = """Если всё работает как нужно, будем благодарны за положительный отзыв о покупке. Это помогает нам развивать сервис. Спасибо!"""

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
    "status": """📊 Статус подписки

🔑 ID заказа: {ORDER_ID}
👤 Профиль: {PANEL_USERNAME}
📦 Тариф: {TARIFF_CODE}
👥 Устройств: {DEVICE_LIMIT}
📶 LTE-квота: {LTE_QUOTA}
⏳ Действует до: {EXPIRE_AT}

🔗 Ссылка подключения:
{SUBSCRIPTION_URL}""",
}

DEFAULT_ACTION_TEMPLATES.update(
    {
        "command_help": """ℹ️ Полезная команда:
{STATUS_COMMAND_EXAMPLE} — проверить срок, тариф и ссылку подписки{FREE_REISSUE_COMMAND_LINE}""",
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
        "status_help": """ℹ️ Чтобы проверить статус подписки, отправьте:
{COMMAND_EXAMPLE}

ID заказа указан в первой выдаче доступа.""",
        "status_error": """⚠️ Не удалось получить статус подписки.

Проверьте ID заказа и попробуйте ещё раз.

Ошибка: {ERROR}""",
        "request_unique_code": """👋 Здравствуйте! Спасибо за покупку.

🔑 Для автоматической выдачи VPN-доступа пришлите, пожалуйста, уникальный 16-значный код Digiseller в этот чат.

Где найти код:
1. Откройте страницу оплаченного заказа.
2. Найдите строку «Уникальный код».
3. Скопируйте код и отправьте его сюда одним сообщением.

✅ После проверки кода бот сразу отправит VPN-доступ.
⚠️ Не отправляйте пароли, данные карты и лишние личные данные.""",
        "unique_code_invoice_mismatch": """⚠️ Этот уникальный код относится к другому заказу Digiseller.

📦 Текущий заказ: {MARKETPLACE_ORDER_ID}
🔑 Заказ, найденный по коду: {CODE_ORDER_ID}

Пожалуйста, пришлите уникальный код именно от этой покупки. Он указан на странице оплаченного заказа в строке «Уникальный код».""",
    }
)

for _action in ("renew", "reissue", "traffic", "ip_limit"):
    DEFAULT_ACTION_TEMPLATES.setdefault(f"{_action}_error", DEFAULT_ACTION_TEMPLATES["operation_error"])
    DEFAULT_ACTION_TEMPLATES.setdefault(f"{_action}_command_mismatch", DEFAULT_ACTION_TEMPLATES["command_mismatch"])

for _action in ("renew", "reissue", "traffic", "ip_limit"):
    DEFAULT_ACTION_TEMPLATES[_action] += "\n\n" + DEFAULT_REVIEW_REQUEST

DEFAULT_CREATE_TEMPLATE = DEFAULT_CREATE_TEMPLATE + "\n\n{COMMAND_HELP}\n\n" + DEFAULT_REVIEW_REQUEST
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
        "status": "Статус подписки",
        "status_help": "Подсказка статуса",
        "status_error": "Ошибка статуса",
        "request_unique_code": "Запрос уникального кода",
        "unique_code_invoice_mismatch": "Код от другого заказа",
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
    "status": "Команды",
    "status_help": "Команды",
    "status_error": "Ошибки",
    "request_unique_code": "Digiseller",
    "unique_code_invoice_mismatch": "Digiseller",
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
    "status": "!status",
}

TEMPLATE_GROUPS = [
    {
        "key": "create",
        "label": BASE_ACTION_LABELS["create"],
        "stages": [
            {"key": "create", "stage": "delivered", "label": "Заказ выдан"},
        ],
    },
    {
        "key": "digiseller",
        "label": "Digiseller",
        "stages": [
            {"key": "request_unique_code", "stage": "waiting_unique_code", "label": "Покупатель не прислал уникальный код"},
            {"key": "unique_code_invoice_mismatch", "stage": "wrong_unique_code", "label": "Код от другого заказа"},
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
    {
        "key": "status",
        "label": ACTION_LABELS["status"],
        "command_action": "status",
        "stages": [
            {"key": "status_help", "stage": "missing_order_id", "label": "Команда без ID заказа"},
            {"key": "status", "stage": "delivered", "label": "Статус получен"},
            {"key": "status_error", "stage": "error", "label": "Ошибка получения статуса"},
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
    "status": "status",
    "info": "status",
    "check": "status",
}
CHAT_COMMAND_RE = re.compile(r"(?:^|\s)!([a-z0-9_:-]+)\b([^\n\r]*)", re.IGNORECASE)

DELIVERY_TEMPLATE_VARIABLES = {
    "ACTION_LABEL": "Название действия",
    "COMMAND": "Команда в чате",
    "COMMAND_EXAMPLE": "Пример команды",
    "STATUS_COMMAND_EXAMPLE": "Пример команды статуса",
    "REISSUE_COMMAND_EXAMPLE": "Пример команды перевыпуска",
    "FREE_REISSUE_COMMAND_LINE": "Строка бесплатного перевыпуска",
    "ERROR": "Текст ошибки",
    "COMMAND_HELP": "Подсказка по командам",
    "PURCHASE_QUANTITY": "Количество купленных единиц",
    "ORDER_ID": "ID заказа",
    "PANEL_USERNAME": "Профиль в панели",
    "TARIFF_CODE": "Код тарифа",
    "TARIFF_NAME": "Понятное название тарифа",
    "EXPIRE_AT": "Дата окончания",
    "SUBSCRIPTION_URL": "Ссылка подписки",
    "DEVICE_LIMIT": "Лимит устройств / IP",
    "LTE_QUOTA": "LTE-квота",
    "MARKETPLACE_ORDER_ID": "ID заказа/чата на площадке",
    "PRODUCT_ID": "ID товара на площадке",
    "PRODUCT_TITLE": "Название товара на площадке",
    "BUYER_EMAIL": "Email покупателя",
    "PURCHASE_AMOUNT": "Сумма покупки",
    "PURCHASE_CURRENCY": "Валюта покупки",
    "UNIQUE_CODE_STATE": "Статус уникального кода",
    "CODE_ORDER_ID": "ID заказа, которому принадлежит присланный код",
}

DELIVERY_TEMPLATE_VARIABLE_DESCRIPTIONS = {
    "ACTION_LABEL": "Название текущего действия: покупка, продление, перевыпуск, LTE-трафик или IP-лимит.",
    "COMMAND": "Команда для выбранного сценария без параметров, например !renew.",
    "COMMAND_EXAMPLE": "Готовый пример команды с ID заказа, например !renew {ORDER_ID}.",
    "STATUS_COMMAND_EXAMPLE": "Готовый пример команды проверки статуса с ID заказа.",
    "REISSUE_COMMAND_EXAMPLE": "Готовый пример команды перевыпуска с ID заказа.",
    "FREE_REISSUE_COMMAND_LINE": "Строка с командой бесплатного перевыпуска. Пустая, если бесплатный перевыпуск выключен.",
    "ERROR": "Текст ошибки, если выполнение услуги не удалось.",
    "COMMAND_HELP": "Составная переменная с большим редактируемым блоком подсказки по командам.",
    "PURCHASE_QUANTITY": "Количество купленных единиц товара в заказе.",
    "ORDER_ID": "ID заказа XyraNet, который получает покупатель.",
    "PANEL_USERNAME": "Имя профиля в панели XyraNet.",
    "TARIFF_CODE": "Код тарифа, по которому создана или продлена подписка.",
    "TARIFF_NAME": "Понятное покупателю название тарифа, например Lite · 1 месяц.",
    "EXPIRE_AT": "Дата окончания подписки из ответа API.",
    "SUBSCRIPTION_URL": "Ссылка подключения к подписке.",
    "DEVICE_LIMIT": "Текущий лимит устройств или IP из ответа API.",
    "LTE_QUOTA": "LTE-квота из ответа API или параметров покупки.",
    "MARKETPLACE_ORDER_ID": "Номер заказа или чата на Plati.Market/Digiseller/GGsel.",
    "PRODUCT_ID": "ID купленного товара на площадке.",
    "PRODUCT_TITLE": "Название купленного товара на площадке, если площадка передала его в API.",
    "BUYER_EMAIL": "Email покупателя из данных площадки, если он доступен.",
    "PURCHASE_AMOUNT": "Сумма покупки из данных площадки.",
    "PURCHASE_CURRENCY": "Валюта покупки из данных площадки.",
    "UNIQUE_CODE_STATE": "Числовой статус уникального кода Digiseller из purchase/info.",
    "CODE_ORDER_ID": "Номер заказа Digiseller, который вернулся при проверке уникального кода.",
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


class DeliveryInProgressError(RuntimeError):
    """Another worker owns the durable claim for this sale or operation."""


class MarketplaceMessageError(RuntimeError):
    """A saved delivery could not be sent to the marketplace chat."""


class PendingOrderOwnershipError(ValueError):
    """A remote marketplace operation targeted an order owned by another chat."""


class DeliveryService:
    def __init__(self, *, db: Database, xyranet: Any, messenger: Any | None = None, free_reissue_enabled: Any | None = None) -> None:
        self.db = db
        self.xyranet = xyranet
        self.messenger = messenger
        self.free_reissue_enabled = free_reissue_enabled
        self._tariff_prices_rub: dict[str, Decimal] | None = None
        self._sale_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._pending_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def handle_sale(self, event: SaleEvent, *, notify_marketplace: bool = True) -> dict[str, Any]:
        lock_key = f"{event.marketplace}:{event.external_order_id}"
        lock = self._sale_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._sale_locks[lock_key] = lock
        async with lock:
            return await self._handle_sale_locked(event, notify_marketplace=notify_marketplace)

    async def _handle_sale_locked(self, event: SaleEvent, *, notify_marketplace: bool) -> dict[str, Any]:
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
            if notify_marketplace:
                await self.ensure_delivery_message_sent(
                    existing,
                    marketplace=event.marketplace,
                    external_order_id=marketplace_chat_order_id(event),
                    sale_id=int(existing["id"]),
                )
                existing = self.db.get_sale_with_delivery(event.marketplace, event.external_order_id) or existing
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
        if pending:
            pending_status = str(pending.get("status") or "")
            if pending_status == "waiting_order_id":
                ask_text = self.ask_order_id_text(str(pending["action"]))
                if notify_marketplace:
                    await self.ensure_pending_request_message_sent(pending, ask_text)
                self.db.add_order_event(
                    marketplace=event.marketplace,
                    external_order_id=event.external_order_id,
                    sale_id=int(sale["id"]),
                    pending_operation_id=int(pending["id"]),
                    event_type="pending_replayed",
                    message="Existing pending operation was reused",
                )
                return {"status": "waiting_order_id", "delivery_text": ask_text, "sale": sale, "pending": pending}
            if pending_status == "processing":
                raise DeliveryInProgressError(f"Pending operation is already being processed: {pending['id']}")
            if pending_status == "completed":
                target_order_id = str(pending.get("target_order_id") or "").strip()
                if event.marketplace == "ggsel" and not self.db.marketplace_chat_owns_order(
                    "ggsel",
                    event.external_order_id,
                    target_order_id,
                ):
                    raise PendingOrderOwnershipError("Target order does not belong to this GGSEL purchase chat")
                completed_sale = self.db.get_sale_with_delivery_by_id(int(sale["id"]))
                if not completed_sale or not completed_sale.get("delivery_id"):
                    raise ValueError("Completed pending operation has no saved delivery")
                if notify_marketplace:
                    await self.ensure_delivery_message_sent(
                        completed_sale,
                        marketplace=event.marketplace,
                        external_order_id=marketplace_chat_order_id(event),
                        sale_id=int(sale["id"]),
                    )
                return {
                    "status": "duplicate",
                    "delivery_text": str(completed_sale.get("delivery_text") or ""),
                    "sale": completed_sale,
                    "delivery": completed_sale,
                    "pending": pending,
                }
            if pending_status == "error":
                raise RuntimeError(f"Pending operation requires an explicit retry: {pending.get('error_text') or pending['id']}")

        claim_token = self.db.claim_sale_processing(int(sale["id"]))
        if not claim_token:
            refreshed = self.db.get_sale_with_delivery_by_id(int(sale["id"]))
            if refreshed and refreshed.get("delivery_id"):
                if notify_marketplace:
                    await self.ensure_delivery_message_sent(
                        refreshed,
                        marketplace=event.marketplace,
                        external_order_id=marketplace_chat_order_id(event),
                        sale_id=int(refreshed["id"]),
                    )
                return {"status": "duplicate", "delivery_text": refreshed["delivery_text"], "sale": refreshed}
            refreshed_pending = self.db.get_pending_operation_by_sale(int(sale["id"]))
            if refreshed_pending and refreshed_pending.get("status") == "waiting_order_id":
                return {
                    "status": "waiting_order_id",
                    "delivery_text": self.ask_order_id_text(str(refreshed_pending["action"])),
                    "sale": sale,
                    "pending": refreshed_pending,
                }
            raise DeliveryInProgressError(f"Sale is already being processed: {event.marketplace}:{event.external_order_id}")
        try:
            return await self._process_claimed_sale(
                event=event,
                sale=sale,
                product=product,
                notify_marketplace=notify_marketplace,
                claim_token=claim_token,
            )
        finally:
            self.db.release_sale_processing(int(sale["id"]), claim_token)

    async def _process_claimed_sale(
        self,
        *,
        event: SaleEvent,
        sale: dict[str, Any],
        product: dict[str, Any],
        notify_marketplace: bool,
        claim_token: str,
    ) -> dict[str, Any]:
        action = str(product.get("action") or "create").strip().lower()
        action_params = self.product_action_params(product)
        idempotency_key = f"{event.marketplace}:{event.external_order_id}"
        heartbeat = lambda: self.db.refresh_sale_processing(int(sale["id"]), claim_token)

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
                    await self.ensure_pending_request_message_sent(pending, ask_text)
                return {"status": "waiting_order_id", "delivery_text": ask_text, "sale": sale, "pending": pending}
            return await self.complete_operation(
                sale=sale,
                product=product,
                action=action,
                action_params=action_params,
                target_order_id=target_order_id,
                idempotency_key=idempotency_key,
                message_order_id=marketplace_chat_order_id(event),
                heartbeat=heartbeat,
                notify_marketplace=notify_marketplace,
            )

        quantity = sale_quantity(event.raw_payload)
        self.db.add_order_event(
            marketplace=event.marketplace,
            external_order_id=event.external_order_id,
            sale_id=int(sale["id"]),
            event_type="xyranet_create_started",
            payload={"tariff_code": product["tariff_code"], "quantity": quantity},
        )
        if not heartbeat():
            raise DeliveryInProgressError("Sale processing claim was lost")
        api_response = await self.xyranet.create_order(product["tariff_code"], idempotency_key=idempotency_key)
        if not heartbeat():
            raise DeliveryInProgressError("Sale processing claim was lost")
        delivery = extract_order_delivery(api_response)
        if not delivery["order_id"] or not delivery["subscription_url"]:
            raise ValueError("XyraNet response does not contain order_id or subscription_url")
        renew_responses: list[dict[str, Any]] = []
        for item_number in range(2, quantity + 1):
            if not heartbeat():
                raise DeliveryInProgressError("Sale processing claim was lost")
            renew_response = await self.xyranet.renew_order(
                delivery["order_id"],
                product.get("tariff_code") or None,
                idempotency_key=f"{idempotency_key}:quantity-renew:{item_number}",
            )
            if not heartbeat():
                raise DeliveryInProgressError("Sale processing claim was lost")
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
        delivery["command_help"] = self.render_system_text("command_help", delivery)

        product_template = str(product.get("delivery_template") or "").strip()
        if product_template:
            delivery_text = self.render_template_with_complex_variables(
                product_template,
                {**self.command_context("create"), **delivery},
            )
        else:
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
                "action": "create",
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
            await self.ensure_delivery_message_sent(
                saved,
                marketplace=event.marketplace,
                external_order_id=marketplace_chat_order_id(event),
                sale_id=int(sale["id"]),
            )
        return {"status": "delivered", "delivery_text": delivery_text, "sale": sale, "delivery": saved}

    async def complete_pending_operation(self, pending: dict[str, Any], target_order_id: str) -> dict[str, Any]:
        operation_id = int(pending["id"])
        lock = self._pending_locks.get(operation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._pending_locks[operation_id] = lock
        async with lock:
            return await self._complete_pending_operation_locked(pending, target_order_id)

    async def _complete_pending_operation_locked(self, pending: dict[str, Any], target_order_id: str) -> dict[str, Any]:
        current = self.db.get_pending_operation(int(pending["id"])) or pending
        selected_target_order_id = str(target_order_id or "").strip()
        if not selected_target_order_id:
            raise ValueError("Target order ID is required")
        if str(current.get("marketplace") or "").strip().lower() == "ggsel" and not self.db.marketplace_chat_owns_order(
            "ggsel",
            str(current.get("external_order_id") or ""),
            selected_target_order_id,
        ):
            # GGSEL does not currently expose a stable, verified buyer identity
            # that can link two different purchase chats. Until it does, never
            # let a paid operation mutate or reveal an order from another chat.
            # Keep this guard before the completed branch so corrupted/replayed
            # pending rows cannot resend a victim's saved subscription URL.
            raise PendingOrderOwnershipError("Target order does not belong to this GGSEL purchase chat")
        if str(current.get("status") or "") == "completed":
            sale = self.db.get_sale_with_delivery_by_id(int(current["sale_id"]))
            if not sale or not sale.get("delivery_id"):
                raise ValueError("Completed pending operation has no saved delivery")
            await self.ensure_delivery_message_sent(
                sale,
                marketplace=str(current["marketplace"]),
                external_order_id=str(current["external_order_id"]),
                sale_id=int(current["sale_id"]),
            )
            raw_response = parse_json_object(sale.get("delivery_raw_response"))
            return {
                "status": "duplicate",
                "delivery_text": str(sale.get("delivery_text") or ""),
                "sale": sale,
                "delivery": sale,
                "raw_response": raw_response,
                "pending": current,
            }

        claimed = self.db.claim_pending_operation(int(current["id"]), target_order_id=selected_target_order_id)
        if not claimed:
            raise DeliveryInProgressError(f"Pending operation is already being processed: {current['id']}")
        claim_token = str(claimed["processing_token"])
        heartbeat = lambda: self.db.refresh_pending_processing(int(claimed["id"]), claim_token)
        try:
            sale = self.db.get_sale_with_delivery_by_id(int(claimed["sale_id"]))
            if not sale:
                raise ValueError("Pending sale was not found")
            if sale.get("delivery_id"):
                await self.ensure_delivery_message_sent(
                    sale,
                    marketplace=str(claimed["marketplace"]),
                    external_order_id=str(claimed["external_order_id"]),
                    sale_id=int(claimed["sale_id"]),
                )
                raw_response = parse_json_object(sale.get("delivery_raw_response"))
                result = {
                    "status": "delivered",
                    "delivery_text": str(sale.get("delivery_text") or ""),
                    "sale": sale,
                    "delivery": sale,
                    "raw_response": raw_response,
                }
            else:
                product = self.db.get_product(int(claimed["product_mapping_id"]))
                if not product:
                    raise ValueError("Pending product mapping was not found")
                result = await self.complete_operation(
                    sale=sale,
                    product=product,
                    action=str(claimed["action"]),
                    action_params=parse_json_object(claimed.get("action_params")),
                    target_order_id=selected_target_order_id,
                    idempotency_key=f"{claimed['marketplace']}:{claimed['external_order_id']}:pending:{claimed['id']}",
                    message_order_id=str(claimed["external_order_id"]),
                    heartbeat=heartbeat,
                )
            completed = self.db.complete_pending_operation(
                int(claimed["id"]),
                target_order_id=selected_target_order_id,
                result_text=result["delivery_text"],
                raw_response=result.get("raw_response") or {},
                claim_token=claim_token,
            )
            if not completed:
                raise DeliveryInProgressError("Pending operation claim was lost before completion")
        except Exception as exc:
            self.db.fail_pending_operation(int(claimed["id"]), str(exc), claim_token=claim_token)
            raise
        self.db.add_order_event(
            marketplace=str(claimed["marketplace"]),
            external_order_id=str(claimed["external_order_id"]),
            sale_id=int(claimed["sale_id"]),
            pending_operation_id=int(claimed["id"]),
            event_type="pending_completed",
            status="success",
            payload={"target_order_id": selected_target_order_id, "action": claimed["action"]},
        )
        return {**result, "pending": completed}

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
        heartbeat: Callable[[], bool] | None = None,
        notify_marketplace: bool = True,
    ) -> dict[str, Any]:
        def refresh_claim() -> None:
            if heartbeat is not None and not heartbeat():
                raise DeliveryInProgressError("Operation processing claim was lost")

        if action == "renew":
            self.db.add_order_event(
                marketplace=str(sale["marketplace"]),
                external_order_id=str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
                event_type="xyranet_operation_started",
                payload={"action": action, "target_order_id": target_order_id},
            )
            refresh_claim()
            response = await self.xyranet.renew_order(
                target_order_id,
                product.get("tariff_code") or None,
                idempotency_key=idempotency_key,
            )
            delivery = extract_order_delivery(response)
            text = self.render_action_text("renew", {**delivery, "order_id": target_order_id})
            refresh_claim()
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
            refresh_claim()
            response = await self.xyranet.reissue_order(target_order_id, idempotency_key=idempotency_key)
            refresh_claim()
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
            refresh_claim()
            quote = await self.safe_operation_quote("traffic", target_order_id, action_params)
            refresh_claim()
            response = await self.xyranet.traffic_purchase(target_order_id, action_params, idempotency_key=idempotency_key)
            refresh_claim()
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
            refresh_claim()
            quote = await self.safe_operation_quote("ip_limit", target_order_id, action_params)
            refresh_claim()
            response = await self.xyranet.ip_limit_purchase(target_order_id, action_params, idempotency_key=idempotency_key)
            refresh_claim()
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
                "action": action,
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
        if notify_marketplace:
            await self.ensure_delivery_message_sent(
                saved,
                marketplace=str(sale["marketplace"]),
                external_order_id=message_order_id or str(sale["external_order_id"]),
                sale_id=int(sale["id"]),
            )
        return {"status": "delivered", "delivery_text": text, "sale": sale, "delivery": saved, "raw_response": raw_response}

    async def ensure_pending_request_message_sent(self, pending: dict[str, Any], text: str) -> bool:
        operation_id = int(pending["id"])
        latest = self.db.get_pending_operation(operation_id)
        if not latest:
            raise ValueError("Pending operation was not found")
        if str(latest.get("request_message_status") or "") == "sent":
            return True
        claim_token = self.db.claim_pending_request_message(operation_id)
        if not claim_token:
            latest = self.db.get_pending_operation(operation_id)
            if latest and str(latest.get("request_message_status") or "") == "sent":
                return True
            raise DeliveryInProgressError(f"Pending request message is already being sent: {operation_id}")
        marketplace = str(latest["marketplace"])
        external_order_id = str(latest["external_order_id"])
        sent = await self.send_marketplace_message(marketplace, external_order_id, text)
        if sent:
            if not self.db.mark_pending_request_message_sent(operation_id, claim_token):
                raise DeliveryInProgressError(f"Pending request message claim was lost: {operation_id}")
            pending["request_message_status"] = "sent"
            self.db.add_order_event(
                marketplace=marketplace,
                external_order_id=external_order_id,
                sale_id=int(latest["sale_id"]),
                pending_operation_id=operation_id,
                event_type="pending_request_message_sent",
                status="success",
            )
            return True
        error_text = "Marketplace messenger returned false"
        self.db.mark_pending_request_message_failed(operation_id, claim_token, error_text)
        self.db.add_order_event(
            marketplace=marketplace,
            external_order_id=external_order_id,
            sale_id=int(latest["sale_id"]),
            pending_operation_id=operation_id,
            event_type="pending_request_message_failed",
            status="error",
            message=error_text,
        )
        raise MarketplaceMessageError(error_text)

    async def ensure_delivery_message_sent(
        self,
        delivery: dict[str, Any],
        *,
        marketplace: str,
        external_order_id: str,
        sale_id: int | None = None,
    ) -> bool:
        delivery_id = int(delivery.get("delivery_id") or delivery.get("id") or 0)
        if not delivery_id:
            raise ValueError("Saved delivery has no delivery ID")
        latest = self.db.get_delivery(delivery_id)
        if not latest:
            raise ValueError("Saved delivery was not found")
        if str(latest.get("marketplace_message_status") or "") == "sent":
            return True
        claim_token = self.db.claim_delivery_message(delivery_id)
        if not claim_token:
            latest = self.db.get_delivery(delivery_id)
            if latest and str(latest.get("marketplace_message_status") or "") == "sent":
                return True
            raise DeliveryInProgressError(f"Marketplace delivery message is already being sent: {delivery_id}")
        text = str(latest.get("delivery_text") or delivery.get("delivery_text") or "")
        sent = await self.send_marketplace_message(marketplace, external_order_id, text)
        if sent:
            if not self.db.mark_delivery_message_sent(delivery_id, claim_token):
                raise DeliveryInProgressError(f"Marketplace delivery message claim was lost: {delivery_id}")
            delivery["marketplace_message_status"] = "sent"
            self.db.add_order_event(
                marketplace=marketplace,
                external_order_id=external_order_id,
                sale_id=sale_id,
                event_type="marketplace_message_sent",
                status="success",
                payload={"delivery_id": delivery_id},
            )
            return True
        error_text = "Marketplace messenger returned false"
        self.db.mark_delivery_message_failed(delivery_id, claim_token, error_text)
        self.db.add_order_event(
            marketplace=marketplace,
            external_order_id=external_order_id,
            sale_id=sale_id,
            event_type="marketplace_message_failed",
            status="error",
            message=error_text,
            payload={"delivery_id": delivery_id},
        )
        raise MarketplaceMessageError(error_text)

    async def send_marketplace_message(self, marketplace: str, external_order_id: str, text: str) -> bool:
        if not self.messenger:
            return True
        try:
            result = await asyncio.wait_for(self.messenger.send_message(marketplace, external_order_id, text), timeout=5)
            return result is not False
        except Exception:
            return False

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

    async def subscription_status(self, target_order_id: str) -> dict[str, Any]:
        response = await self.xyranet.get_order(target_order_id)
        delivery = extract_order_delivery(response)
        text = self.render_action_text("status", {**delivery, "order_id": target_order_id})
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
        return self.render_template_with_complex_variables(source, {**self.command_context(template_key), **(values or {})})

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
        for action in DEFAULT_CHAT_COMMANDS:
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
        context = {
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "command": command,
            "command_example": f"{command} {{ORDER_ID}}",
            "free_reissue_command_line": (
                f"\n{self.expected_command('reissue')} {{ORDER_ID}} — получить новую ссылку подписки"
                if self.is_free_reissue_enabled()
                else ""
            ),
            "order_id": "{ORDER_ID}",
        }
        for command_action in DEFAULT_CHAT_COMMANDS:
            action_command = self.expected_command(command_action)
            context[f"{command_action}_command"] = action_command
            context[f"{command_action}_command_example"] = f"{action_command} {{ORDER_ID}}"
        return context


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_order_delivery(response: dict[str, Any]) -> dict[str, str]:
    raw_order = response.get("order") if isinstance(response, dict) else None
    order = raw_order if isinstance(raw_order, dict) else {}
    raw_subscription = order.get("subscription")
    subscription = raw_subscription if isinstance(raw_subscription, dict) else {}
    tariff_code = str(subscription.get("tariff_code") or "")
    return {
        "order_id": str(order.get("order_id") or ""),
        "panel_username": str(order.get("panel_username") or ""),
        "subscription_url": str(subscription.get("subscription_url") or ""),
        "tariff_code": tariff_code,
        "tariff_name": tariff_display_name(tariff_code),
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


def tariff_display_name(value: Any) -> str:
    code = str(value or "").strip()
    normalized = code.lower()
    tier_code, separator, term_code = normalized.partition("_")
    if not separator:
        return code
    tier = {"lite": "Lite", "pro": "Premium", "premium": "Premium"}.get(tier_code)
    term = {
        "weekly": "7 дней",
        "monthly": "1 месяц",
        "3m": "3 месяца",
        "6m": "6 месяцев",
        "1y": "12 месяцев",
        "2y": "24 месяца",
    }.get(term_code)
    if not tier or not term:
        return code
    return f"{tier} · {term}"


def extract_target_order_id(payload: dict[str, Any]) -> str:
    keys = {
        "xyranet_order_id",
        "xyra_order_id",
        "target_order_id",
        "subscription_order_id",
        "vpn_order_id",
    }
    found = _extract_key_recursive(payload, keys)
    return found


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
