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
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_marketplace(value: str) -> str:
    marketplace = value.strip().lower()
    if marketplace == "plati_market":
        marketplace = "plati"
    if marketplace not in SUPPORTED_MARKETPLACES:
        raise ValueError(f"Unsupported marketplace: {value}")
    return marketplace


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
        "variant_id",
        "variantId",
        "permalink",
        "product_permalink",
    )
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
    )
    if not order_id:
        raise ValueError("Cannot find marketplace order id in webhook payload")
    if not product_id:
        raise ValueError("Cannot find marketplace product id in webhook payload")
    return SaleEvent(
        marketplace=marketplace,
        external_order_id=order_id,
        external_product_id=product_id,
        external_variant_id=variant_id or "",
        buyer_email=_pick(payload, "buyer_email", "email", "customer_email", "mail"),
        buyer_name=_pick(payload, "buyer_name", "customer_name", "name", "username"),
        amount=_pick(payload, "amount", "price", "sum", "total"),
        currency=_pick(payload, "currency", "curr", "currency_code"),
        raw_payload=payload,
    )
