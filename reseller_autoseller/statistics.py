from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any


STATS_PERIODS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "7d": "7 дней",
    "30d": "30 дней",
    "90d": "90 дней",
    "all": "Всё время",
}

RUB_ALIASES = {"rub", "rur", "₽", "руб", "руб."}
MONEY_KEYS = (
    "statistics_expense_rub",
    "api_price_rub",
    "wholesale_price_rub",
    "price_rub",
    "cost_rub",
    "charged_rub",
    "spent_rub",
    "total_rub",
    "amount_rub",
    "api_price",
    "wholesale_price",
    "price",
    "cost",
    "charged",
    "spent",
    "total",
    "amount",
)


def period_bounds(period: str) -> tuple[datetime | None, datetime | None, str]:
    key = period if period in STATS_PERIODS else "30d"
    now_local = datetime.now().astimezone()
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        return today.astimezone(timezone.utc), now_local.astimezone(timezone.utc), key
    if key == "yesterday":
        start = today - timedelta(days=1)
        return start.astimezone(timezone.utc), today.astimezone(timezone.utc), key
    if key == "7d":
        return (now_local - timedelta(days=7)).astimezone(timezone.utc), now_local.astimezone(timezone.utc), key
    if key == "30d":
        return (now_local - timedelta(days=30)).astimezone(timezone.utc), now_local.astimezone(timezone.utc), key
    if key == "90d":
        return (now_local - timedelta(days=90)).astimezone(timezone.utc), now_local.astimezone(timezone.utc), key
    return None, None, key


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        text = str(value).strip().replace(" ", "").replace(",", ".")
        text = "".join(char for char in text if char.isdigit() or char in ".-")
        if not text or text in {".", "-", "-."}:
            return Decimal("0")
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def normalize_currency(value: Any) -> str:
    currency = str(value or "RUB").strip()
    if not currency:
        return "RUB"
    return "RUB" if currency.lower() in RUB_ALIASES else currency.upper()


def expense_from_raw_response(raw_response: Any) -> Decimal:
    if isinstance(raw_response, str):
        if not raw_response.strip():
            return Decimal("0")
        try:
            raw_response = json.loads(raw_response)
        except ValueError:
            return Decimal("0")
    return _money_from_node(raw_response)


def _money_from_node(value: Any) -> Decimal:
    if isinstance(value, list):
        return sum((_money_from_node(item) for item in value), Decimal("0"))
    if not isinstance(value, dict):
        return Decimal("0")
    lowered = {str(key).lower(): item for key, item in value.items()}
    if "statistics_expense_rub" in lowered and not isinstance(lowered["statistics_expense_rub"], (dict, list)):
        return parse_decimal(lowered["statistics_expense_rub"])
    if "create" in value or "renewals" in value:
        total = _money_from_node(value.get("create"))
        total += _money_from_node(value.get("renewals"))
        for key in ("operations", "items", "purchases"):
            total += _money_from_node(value.get(key))
        return total
    for key in MONEY_KEYS:
        if key in lowered and not isinstance(lowered[key], (dict, list)):
            return parse_decimal(lowered[key])
    total = Decimal("0")
    for item in value.values():
        if isinstance(item, (dict, list)):
            total += _money_from_node(item)
    return total


def money_payload(value: Decimal) -> dict[str, Any]:
    rounded = value.quantize(Decimal("0.01"))
    return {"amount": float(rounded), "text": f"{rounded:f}".rstrip("0").rstrip(".") or "0"}


def revenue_list(revenue_by_currency: dict[str, Decimal]) -> list[dict[str, Any]]:
    return [
        {"currency": currency, **money_payload(amount)}
        for currency, amount in sorted(revenue_by_currency.items())
        if amount
    ]


def pct(numerator: Decimal, denominator: Decimal) -> float | None:
    if denominator == 0:
        return None
    return float((numerator / denominator * Decimal("100")).quantize(Decimal("0.01")))


def empty_group() -> dict[str, Any]:
    return {
        "sales_count": 0,
        "delivered_count": 0,
        "pending_count": 0,
        "revenue_by_currency": defaultdict(Decimal),
        "expense_rub": Decimal("0"),
    }


def add_to_group(group: dict[str, Any], *, delivered: bool, amount: Decimal, currency: str, expense: Decimal) -> None:
    group["sales_count"] += 1
    if delivered:
        group["delivered_count"] += 1
    else:
        group["pending_count"] += 1
    if amount:
        group["revenue_by_currency"][currency] += amount
    group["expense_rub"] += expense


def finalize_group(key: str, label: str, group: dict[str, Any]) -> dict[str, Any]:
    revenue_rub = group["revenue_by_currency"].get("RUB", Decimal("0"))
    expense = group["expense_rub"]
    profit = revenue_rub - expense
    return {
        "key": key,
        "label": label,
        "sales_count": group["sales_count"],
        "delivered_count": group["delivered_count"],
        "pending_count": group["pending_count"],
        "revenue": revenue_list(group["revenue_by_currency"]),
        "revenue_rub": money_payload(revenue_rub),
        "expense_rub": money_payload(expense),
        "profit_rub": money_payload(profit),
        "margin_percent": pct(profit, revenue_rub),
    }


def build_sales_statistics(rows: list[dict[str, Any]], *, period: str = "30d") -> dict[str, Any]:
    start, end, key = period_bounds(period)
    total = empty_group()
    marketplaces: dict[str, dict[str, Any]] = defaultdict(empty_group)
    actions: dict[str, dict[str, Any]] = defaultdict(empty_group)
    tariffs: dict[str, dict[str, Any]] = defaultdict(empty_group)
    days: dict[str, dict[str, Any]] = defaultdict(empty_group)

    for row in rows:
        created_at = parse_datetime(row.get("created_at"))
        if created_at is None:
            continue
        if start and created_at < start:
            continue
        if end and created_at >= end:
            continue
        amount = parse_decimal(row.get("amount"))
        currency = normalize_currency(row.get("currency"))
        delivered = bool(row.get("delivery_id") or row.get("xyranet_order_id"))
        expense = expense_from_raw_response(row.get("delivery_raw_response") or row.get("raw_response") or "")
        action = str(row.get("product_action") or "create")
        marketplace = str(row.get("marketplace") or "unknown")
        tariff = str(row.get("delivered_tariff_code") or row.get("tariff_code") or "unknown")
        day = created_at.astimezone().date().isoformat()

        for group in (total, marketplaces[marketplace], actions[action], tariffs[tariff], days[day]):
            add_to_group(group, delivered=delivered, amount=amount, currency=currency, expense=expense)

    revenue_rub = total["revenue_by_currency"].get("RUB", Decimal("0"))
    expense = total["expense_rub"]
    profit = revenue_rub - expense
    avg_order = revenue_rub / total["sales_count"] if total["sales_count"] else Decimal("0")

    return {
        "period": {
            "key": key,
            "label": STATS_PERIODS[key],
            "from": start.isoformat() if start else "",
            "to": end.isoformat() if end else "",
            "available": [{"key": item_key, "label": label} for item_key, label in STATS_PERIODS.items()],
        },
        "totals": {
            "sales_count": total["sales_count"],
            "delivered_count": total["delivered_count"],
            "pending_count": total["pending_count"],
            "revenue": revenue_list(total["revenue_by_currency"]),
            "revenue_rub": money_payload(revenue_rub),
            "expense_rub": money_payload(expense),
            "profit_rub": money_payload(profit),
            "margin_percent": pct(profit, revenue_rub),
            "avg_order_rub": money_payload(avg_order),
        },
        "marketplaces": sorted(
            (finalize_group(item_key, item_key, group) for item_key, group in marketplaces.items()),
            key=lambda item: (-item["sales_count"], item["label"]),
        ),
        "actions": sorted(
            (finalize_group(item_key, item_key, group) for item_key, group in actions.items()),
            key=lambda item: (-item["sales_count"], item["label"]),
        ),
        "tariffs": sorted(
            (finalize_group(item_key, item_key, group) for item_key, group in tariffs.items()),
            key=lambda item: (-item["sales_count"], item["label"]),
        )[:20],
        "days": [
            finalize_group(item_key, item_key, group)
            for item_key, group in sorted(days.items())
        ],
    }
