from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlparse


SUPPORTED_LOT_HOSTS = (
    "plati.io",
    "plati.market",
    "ggsel.net",
    "ggsel.com",
    "digiseller.com",
)

MARKETPLACE_LOT_HOSTS = {
    "plati": ("plati.io", "plati.market", "digiseller.com"),
    "ggsel": ("ggsel.net", "ggsel.com"),
}

PRODUCT_ID_KEYS = {
    "id_goods",
    "goods_id",
    "product_id",
    "id_d",
    "idd",
    "lot_id",
    "item_id",
}

STRUCTURED_PRODUCT_ID_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_-])(?:data-)?["']?
    (?:id[-_]goods|goods[-_]id|product[-_]id|id[-_]d|idd|lot[-_]id|item[-_]id)
    ["']?\s*[:=]\s*["']?
    ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})
    """,
    re.IGNORECASE | re.VERBOSE,
)

VARIANT_SELECT_HINT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:options?|variants?|variations?|modifications?)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class LotOptionParser(HTMLParser):
    def __init__(self, product_id: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.product_id = product_id
        self.options: dict[str, dict[str, Any]] = {}
        self._label_for = ""
        self._label_depth = 0
        self._label_parts: list[str] = []
        self._variant_select_stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key.lower(): value or "" for key, value in attrs}
        tag_name = tag.lower()
        if tag_name == "select":
            self._variant_select_stack.append(self._is_variant_select(data))
        if tag_name == "input":
            self._collect_input(data)
        if tag_name == "option" and self._variant_select_stack and self._variant_select_stack[-1]:
            self._collect_select_option(data)
        if tag_name == "label" and data.get("for") in self.options:
            self._label_for = data["for"]
            self._label_depth = 1
            self._label_parts = []
            return
        if self._label_for:
            self._label_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "select" and self._variant_select_stack:
            self._variant_select_stack.pop()
        if not self._label_for:
            return
        self._label_depth -= 1
        if self._label_depth <= 0:
            label = normalize_space(" ".join(self._label_parts))
            if label:
                self.options[self._label_for]["label"] = label
            self._label_for = ""
            self._label_parts = []

    def handle_data(self, data: str) -> None:
        if self._label_for:
            self._label_parts.append(data)

    def _collect_input(self, data: dict[str, str]) -> None:
        input_type = data.get("type", "").lower()
        classes = data.get("class", "")
        option_id = data.get("id", "")
        value = data.get("value", "").strip()
        if input_type not in {"radio", "checkbox"}:
            return
        if not value:
            return
        looks_like_variant = (
            "cl_checked_option" in classes
            or "id_delta_rb" in classes
            or option_id.startswith("CheckedOption_")
            or data.get("data-id")
            or data.get("data-item-id")
        )
        if not looks_like_variant:
            return
        if self.product_id and data.get("data-item-id") and data.get("data-item-id") != self.product_id:
            return
        option_key = option_id or f"option-{value}"
        self.options[option_key] = {
            "id": value,
            "label": value,
            "price_delta": data.get("data-delta-price", ""),
            "price_delta_label": data.get("data-delta-unit", ""),
            "selected": "checked" in data,
        }

    def _collect_select_option(self, data: dict[str, str]) -> None:
        value = data.get("value", "").strip()
        if not value or value in {"0", "-1"}:
            return
        option_key = f"select-{len(self.options)}-{value}"
        self.options[option_key] = {"id": value, "label": value, "selected": "selected" in data}

    @staticmethod
    def _is_variant_select(data: dict[str, str]) -> bool:
        marker_values = [
            data.get("id", ""),
            data.get("name", ""),
            data.get("class", ""),
            data.get("data-role", ""),
            data.get("data-type", ""),
            data.get("data-name", ""),
        ]
        if any(value.strip().lower() in {"id_o", "id-o", "id_v", "id-v"} for value in marker_values):
            return True
        markers = " ".join(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value) for value in marker_values)
        return VARIANT_SELECT_HINT_RE.search(markers) is not None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def detect_marketplace(source: str) -> str:
    text = source.strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host:
        for marketplace, allowed_hosts in MARKETPLACE_LOT_HOSTS.items():
            if any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
                return marketplace
        return ""
    text = text.lower()
    if "ggsel" in text:
        return "ggsel"
    if "digiseller" in text:
        return "plati"
    if "plati" in text:
        return "plati"
    return ""


def extract_product_id(source: str) -> str:
    text = source.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        value = pick_nested(data, PRODUCT_ID_KEYS)
        if value:
            return value
    parsed = urlparse(text)
    is_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    query_text = parsed.query if is_url else text.lstrip("?") if "=" in text and "<" not in text else ""
    if query_text:
        query = {key.lower(): value for key, value in parse_qsl(query_text, keep_blank_values=False)}
        for key in PRODUCT_ID_KEYS:
            if query.get(key):
                return query[key]
    marker = STRUCTURED_PRODUCT_ID_RE.search(text)
    if marker:
        return marker.group(1)
    if is_url:
        numbers = re.findall(r"\d{4,}", parsed.path)
        return numbers[-1] if numbers else ""
    return ""


def pick_nested(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and not isinstance(item, (dict, list)) and str(item).strip():
                return str(item).strip()
            found = pick_nested(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = pick_nested(item, keys)
            if found:
                return found
    return ""


def is_allowed_lot_url(source: str) -> bool:
    parsed = urlparse(source.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in SUPPORTED_LOT_HOSTS)


def parse_lot_html(source: str, html: str) -> dict[str, Any]:
    product_id = extract_product_id(source) or extract_product_id(html)
    parser = LotOptionParser(product_id=product_id)
    parser.feed(html)
    variants = unique_variants(parser.options.values())
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = normalize_space(re.sub(r"^Купить\s+", "", title_match.group(1), flags=re.IGNORECASE))
    return {
        "marketplace": detect_marketplace(source or html),
        "productId": product_id,
        "variantId": "",
        "variants": variants,
        "title": title,
    }


def unique_variants(rows: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        option_id = normalize_space(str(row.get("id", "")))
        if not option_id or option_id in seen:
            continue
        label = normalize_space(str(row.get("label") or option_id))
        label = normalize_space(re.sub(r"(Выбран|[+-]\s*[\d\s\u00a0]+ ₽ за .*)$", "", label))
        delta = normalize_space(str(row.get("price_delta", "")))
        if delta and delta != "0":
            label = f"{label} ({delta} ₽)"
        result.append({"id": option_id, "label": label})
        seen.add(option_id)
    return result
