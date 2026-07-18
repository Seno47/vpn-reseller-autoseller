from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_MARKETPLACES = {"plati", "digiseller", "ggsel"}


@dataclass(slots=True, frozen=True)
class SaleEvent:
    marketplace: str
    external_order_id: str
    external_product_id: str
    external_variant_id: str
    buyer_email: str | None
    buyer_name: str | None
    amount: str | None
    currency: str | None
    raw_payload: dict[str, Any]


def _pick(payload: dict[str, Any], *keys: str) -> str | None:
    key_set = {key.lower() for key in keys}
    for key in keys:
        value = payload.get(key)
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            return str(value).strip()
    for key, value in payload.items():
        if key.lower() in key_set and value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            return str(value).strip()
        if isinstance(value, dict):
            found = _pick(value, *keys)
            if found:
                return found
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _pick(item, *keys)
                    if found:
                        return found
    return None


def normalize_marketplace(value: str) -> str:
    marketplace = value.strip().lower()
    if marketplace in {"plati_market", "digiseller"}:
        marketplace = "plati"
    if marketplace not in SUPPORTED_MARKETPLACES:
        raise ValueError(f"Unsupported marketplace: {value}")
    return marketplace


def ggsel_variant_id(payload: dict[str, Any]) -> str:
    """Read a selected GGSEL variant without confusing it with the option ID."""

    if "external_variant_id" in payload:
        value = payload.get("external_variant_id")
        return "" if value in (None, "") else str(value).strip()
    for key in (
        "variant_id",
        "variantId",
        "button_id",
        "buttonId",
        "selection_id",
        "selectionId",
        "sku",
    ):
        value = payload.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()

    pending = [payload]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        options = current.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                for key in ("variant_id", "variantId", "user_data_id", "userDataId"):
                    value = option.get(key)
                    if value not in (None, "") and not isinstance(value, (dict, list)):
                        return str(value).strip()
        for key in ("data", "content", "raw_order"):
            nested = current.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return ""


def normalize_sale(marketplace: str, payload: dict[str, Any]) -> SaleEvent:
    marketplace = normalize_marketplace(marketplace)
    order_id = _pick(
        payload,
        "external_order_id",
        "invoice_id",
        "invoiceID",
        "invoice",
        "id_i",
        "order_id",
        "orderId",
        "sale_id",
        "saleId",
        "purchase_id",
        "purchaseId",
        "invoice_id",
        "invoiceId",
        "id",
        "uniquecode",
        "unique_code",
    )
    product_id = _pick(
        payload,
        "external_product_id",
        "id_goods",
        "goods_id",
        "product_id",
        "productId",
        "item_id",
        "itemId",
        "offer_id",
        "offerId",
        "id_good",
        "idGoods",
        "id_product",
        "good_id",
        "goodId",
        "variant_id",
        "variantId",
        "permalink",
        "product_permalink",
    )
    if marketplace == "ggsel":
        variant_id = ggsel_variant_id(payload)
    else:
        variant_id = _pick(
            payload,
            "external_variant_id",
            "variant_id",
            "variantId",
            "option_id",
            "optionId",
            "button_id",
            "buttonId",
            "selection_id",
            "selectionId",
            "sku",
            "variant",
            "option",
        )
    if not order_id:
        raise ValueError("Cannot find marketplace order id in sale payload")
    if not product_id:
        raise ValueError("Cannot find marketplace product id in sale payload")
    return SaleEvent(
        marketplace=marketplace,
        external_order_id=order_id,
        external_product_id=product_id,
        external_variant_id=variant_id or "",
        buyer_email=_pick(payload, "buyer_email", "email", "customer_email", "mail"),
        buyer_name=_pick(payload, "buyer_name", "customer_name", "name", "username"),
        amount=_pick(payload, "amount", "price", "sum", "total"),
        currency=_pick(payload, "currency", "curr", "currency_code", "currency_type"),
        raw_payload=payload,
    )
