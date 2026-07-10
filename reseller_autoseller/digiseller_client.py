from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any
from urllib.parse import quote

import httpx

from reseller_autoseller.marketplaces import SaleEvent


class DigisellerApiError(RuntimeError):
    pass


class DigisellerClient:
    _last_login_timestamp = 0

    def __init__(
        self,
        *,
        seller_id: str,
        api_key: str,
        timeout: float = 30.0,
        base_url: str = "https://api.digiseller.com/api",
    ) -> None:
        self.seller_id = str(seller_id).strip()
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def _next_login_timestamp(cls) -> int:
        timestamp = max(int(time.time()), cls._last_login_timestamp + 1)
        cls._last_login_timestamp = timestamp
        return timestamp

    async def token(self) -> str:
        if self._token and time.time() < self._token_valid_until:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_valid_until:
                return self._token
            if not self.seller_id or not self.api_key:
                raise DigisellerApiError("Digiseller seller ID/API key are not configured")
            try:
                seller_id = int(self.seller_id)
            except ValueError as exc:
                raise DigisellerApiError("Digiseller seller ID must be an integer") from exc
            last_error = "Digiseller login failed"
            for attempt in range(3):
                timestamp = self._next_login_timestamp()
                sign = hashlib.sha256(f"{self.api_key}{timestamp}".encode("utf-8")).hexdigest()
                payload = {"seller_id": seller_id, "timestamp": timestamp, "sign": sign}
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(
                            f"{self.base_url}/apilogin",
                            json=payload,
                            headers={"Accept": "application/json", "Content-Type": "application/json"},
                        )
                except httpx.HTTPError as exc:
                    raise DigisellerApiError(f"Cannot reach Digiseller API: {exc}") from exc
                data = self._json(response)
                if self._retval(data, default=-1) == 0 and data.get("token"):
                    self._token = str(data["token"])
                    self._token_valid_until = time.time() + 110 * 60
                    return self._token
                last_error = str(data.get("desc") or data.get("retdesc") or "Digiseller login failed")
                if "timestamp" not in last_error.lower() or attempt == 2:
                    break
                await asyncio.sleep(1.1)
            raise DigisellerApiError(last_error)

    def _invalidate_token(self, token: str) -> None:
        if self._token == token:
            self._token = None
            self._token_valid_until = 0.0

    async def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if method.upper() in {"POST", "PUT", "PATCH"}:
            headers["Content-Type"] = "application/json"
        for attempt in range(2):
            token = await self.token()
            request_params = {**(params or {}), "token": token}
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        params=request_params,
                        json=json,
                        headers=headers,
                    )
            except httpx.HTTPError as exc:
                raise DigisellerApiError(f"Cannot reach Digiseller API: {exc}") from exc
            if response.status_code != 401 or attempt == 1:
                return response
            self._invalidate_token(token)
        raise DigisellerApiError("Digiseller authentication failed")

    async def purchase_by_unique_code(self, unique_code: str) -> dict[str, Any]:
        response = await self._authenticated_request(
            "GET",
            f"/purchases/unique-code/{quote(unique_code, safe='')}",
        )
        data = self._json(response)
        if self._retval(data, default=-1) != 0:
            raise DigisellerApiError(str(data.get("retdesc") or data.get("desc") or "Invalid unique code"))
        return data

    async def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]:
        response = await self._authenticated_request(
            "PUT",
            f"/purchases/unique-code/{quote(unique_code, safe='')}/deliver",
        )
        data = self._json(response)
        if self._retval(data, default=-1) not in {0, 4}:
            raise DigisellerApiError(str(data.get("retdesc") or data.get("desc") or "Cannot mark code delivered"))
        return data

    async def last_sales(self, *, top: int = 100, group: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "seller_id": self.seller_id,
            "top": max(1, min(int(top), 1000)),
        }
        if group:
            params["group"] = group
        response = await self._authenticated_request("GET", "/seller-last-sales", params=params)
        data = self._json_any(response)
        self._raise_for_retval(data, "Cannot read last sales")
        return self._list_from_response(data)

    async def seller_sales(
        self,
        *,
        date_start: str,
        date_finish: str,
        product_ids: list[int] | None = None,
        returned: int = 1,
        page: int = 1,
        rows: int = 100,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "date_start": date_start,
            "date_finish": date_finish,
            "returned": returned,
            "page": max(1, int(page)),
            "rows": max(1, min(int(rows), 5000)),
        }
        if product_ids:
            payload["product_ids"] = product_ids
        response = await self._authenticated_request("POST", "/seller-sells/v2", json=payload)
        data = self._json_any(response)
        self._raise_for_retval(data, "Cannot read seller sales")
        return self._list_from_response(data)

    async def purchase_info(self, invoice_id: str) -> dict[str, Any]:
        response = await self._authenticated_request(
            "GET",
            f"/purchase/info/{quote(invoice_id, safe='')}",
        )
        data = self._json(response)
        if self._retval(data, default=-1) != 0:
            raise DigisellerApiError(str(data.get("retdesc") or data.get("desc") or "Cannot read purchase info"))
        return data

    async def order_chats(self, *, filter_new: bool = True, page: int = 1, rows: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": page,
            "pageSize": rows,
        }
        if filter_new:
            params["filter_new"] = 1
        response = await self._authenticated_request("GET", "/debates/v2/chats", params=params)
        data = self._json_any(response)
        self._raise_for_retval(data, "Cannot read order chats")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        for key in ("chats", "items", "list", "debates"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        return []

    async def order_messages(
        self,
        invoice_id: str,
        *,
        count: int = 100,
        newer: bool = False,
        old_id: str = "",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"id_i": invoice_id, "count": count}
        if newer:
            params["newer"] = 1
        if old_id:
            params["old_id"] = old_id
        response = await self._authenticated_request("GET", "/debates/v2", params=params)
        data = self._json_any(response)
        self._raise_for_retval(data, "Cannot read order messages")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        if isinstance(data.get("messages"), list):
            return [item for item in data["messages"] if isinstance(item, dict)]
        if isinstance(data.get("debates"), list):
            return [item for item in data["debates"] if isinstance(item, dict)]
        if isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]
        if isinstance(data.get("list"), list):
            return [item for item in data["list"] if isinstance(item, dict)]
        return []

    async def mark_order_messages_seen(self, invoice_id: str) -> dict[str, Any]:
        response = await self._authenticated_request(
            "POST",
            "/debates/v2/seen",
            params={"id_i": invoice_id},
        )
        if 200 <= response.status_code < 300 and not response.text.strip():
            return {"status": "ok"}
        data = self._json(response)
        self._raise_for_retval(data, "Cannot mark messages seen")
        return data

    async def send_order_message(self, invoice_id: str, message: str) -> dict[str, Any]:
        response = await self._authenticated_request(
            "POST",
            "/debates/v2/",
            params={"id_i": invoice_id},
            json={"message": message, "files": []},
        )
        if 200 <= response.status_code < 300 and not response.text.strip():
            return {"status": "ok"}
        data = self._json(response)
        self._raise_for_retval(data, "Cannot send message")
        return data

    @staticmethod
    def _retval(data: dict[str, Any], *, default: int) -> int:
        raw_value = data.get("retval", default)
        try:
            return int(raw_value)
        except (TypeError, ValueError) as exc:
            raise DigisellerApiError("Digiseller API returned an invalid result code") from exc

    @classmethod
    def _raise_for_retval(cls, data: Any, fallback: str) -> None:
        if not isinstance(data, dict) or "retval" not in data:
            return
        if cls._retval(data, default=0) != 0:
            raise DigisellerApiError(str(data.get("retdesc") or data.get("desc") or fallback))

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise DigisellerApiError(f"Digiseller API {response.status_code}: {response.text}")
        try:
            data = response.json()
        except ValueError as exc:
            raise DigisellerApiError("Digiseller API returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise DigisellerApiError("Digiseller API returned unexpected response")
        return data

    @staticmethod
    def _json_any(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise DigisellerApiError(f"Digiseller API {response.status_code}: {response.text}")
        try:
            return response.json()
        except ValueError as exc:
            raise DigisellerApiError("Digiseller API returned invalid JSON") from exc

    @staticmethod
    def _list_from_response(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("items", "sales", "rows", "list", "data", "content", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = DigisellerClient._list_from_response(value)
                if nested:
                    return nested
        return []


class RuntimeDigisellerClient:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._client: DigisellerClient | None = None
        self._fingerprint: tuple[str, str, float] | None = None

    def client(self) -> DigisellerClient:
        fingerprint = (
            self.runtime.get_text("digiseller_seller_id"),
            self.runtime.get_text("digiseller_api_key"),
            self.runtime.get_float("xyranet_timeout_seconds"),
        )
        if self._client is None or fingerprint != self._fingerprint:
            self._client = DigisellerClient(
                seller_id=fingerprint[0],
                api_key=fingerprint[1],
                timeout=fingerprint[2],
            )
            self._fingerprint = fingerprint
        return self._client

    async def purchase_by_unique_code(self, unique_code: str) -> dict[str, Any]:
        return await self.client().purchase_by_unique_code(unique_code)

    async def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]:
        return await self.client().mark_unique_code_delivered(unique_code)

    async def last_sales(self, *, top: int = 100, group: str = "") -> list[dict[str, Any]]:
        return await self.client().last_sales(top=top, group=group)

    async def seller_sales(
        self,
        *,
        date_start: str,
        date_finish: str,
        product_ids: list[int] | None = None,
        returned: int = 1,
        page: int = 1,
        rows: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.client().seller_sales(
            date_start=date_start,
            date_finish=date_finish,
            product_ids=product_ids,
            returned=returned,
            page=page,
            rows=rows,
        )

    async def purchase_info(self, invoice_id: str) -> dict[str, Any]:
        return await self.client().purchase_info(invoice_id)

    async def order_chats(self, *, filter_new: bool = True, page: int = 1, rows: int = 100) -> list[dict[str, Any]]:
        return await self.client().order_chats(filter_new=filter_new, page=page, rows=rows)

    async def order_messages(
        self,
        invoice_id: str,
        *,
        count: int = 100,
        newer: bool = False,
        old_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self.client().order_messages(invoice_id, count=count, newer=newer, old_id=old_id)

    async def mark_order_messages_seen(self, invoice_id: str) -> dict[str, Any]:
        return await self.client().mark_order_messages_seen(invoice_id)

    async def send_order_message(self, invoice_id: str, message: str) -> dict[str, Any]:
        return await self.client().send_order_message(invoice_id, message)


def sale_event_from_unique_code(purchase: dict[str, Any], unique_code: str = "") -> SaleEvent:
    options = purchase.get("options") if isinstance(purchase.get("options"), list) else []
    variant_id = ""
    for option in options:
        if isinstance(option, dict) and option.get("variant_id") not in (None, ""):
            variant_id = str(option["variant_id"])
            break
    invoice_id = str(purchase.get("inv") or "")
    sale_id = f"{invoice_id}:{unique_code}" if invoice_id and unique_code else invoice_id
    raw_payload = dict(purchase)
    if unique_code:
        raw_payload["unique_code"] = unique_code
    return SaleEvent(
        marketplace="plati",
        external_order_id=sale_id,
        external_product_id=str(purchase.get("id_goods") or ""),
        external_variant_id=variant_id,
        buyer_email=str(purchase.get("email") or "") or None,
        buyer_name=None,
        amount=str(purchase.get("amount") or "") or None,
        currency=str(purchase.get("type_curr") or "") or None,
        raw_payload=raw_payload,
    )


def unique_code_state(purchase: dict[str, Any]) -> int | None:
    content = purchase_content(purchase)
    state = (content.get("unique_code_state") or {}).get("state")
    try:
        return int(state)
    except (TypeError, ValueError):
        return None


def purchase_content(purchase: dict[str, Any]) -> dict[str, Any]:
    content = purchase.get("content")
    return content if isinstance(content, dict) else purchase


def purchase_invoice_id(purchase: dict[str, Any], fallback: str = "") -> str:
    content = purchase_content(purchase)
    for source in (purchase, content):
        for key in ("inv", "id_i", "invoice_id", "invoice", "order_id"):
            if source.get(key) not in (None, ""):
                return str(source[key]).strip()
    return str(fallback or "").strip()


def purchase_product_id(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    for key in ("id_goods", "item_id", "product_id", "goods_id"):
        if content.get(key) not in (None, ""):
            return str(content[key]).strip()
    return ""


def purchase_variant_id(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    options = content.get("options") if isinstance(content.get("options"), list) else []
    for option in options:
        if not isinstance(option, dict):
            continue
        for key in ("variant_id", "user_data_id", "id"):
            if option.get(key) not in (None, ""):
                return str(option[key]).strip()
    return ""


def purchase_buyer_email(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    buyer = content.get("buyer_info") if isinstance(content.get("buyer_info"), dict) else {}
    for source in (content, buyer):
        if source.get("email") not in (None, ""):
            return str(source["email"]).strip()
    return ""


def purchase_amount(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    for key in ("amount", "amount_usd"):
        if content.get(key) not in (None, ""):
            return str(content[key]).strip()
    return ""


def purchase_currency(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    for key in ("type_curr", "currency", "currency_type"):
        if content.get(key) not in (None, ""):
            return str(content[key]).strip()
    return ""


def purchase_paid_at(purchase: dict[str, Any]) -> str:
    content = purchase_content(purchase)
    for key in ("date_pay", "purchase_date", "date", "created_at"):
        if content.get(key) not in (None, ""):
            return str(content[key]).strip()
    return ""
