from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


log = logging.getLogger(__name__)


class GgselOrderValidationError(ValueError):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class GgselApiError(RuntimeError):
    """An actionable GGsel API failure.

    ``status_code`` and ``retry_after`` let pollers distinguish throttling and
    temporary server failures from permanent request errors without parsing an
    exception message.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or bool(self.status_code and self.status_code >= 500)


def _ggsel_response_layers(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    layers: list[dict[str, Any]] = []
    pending = [payload]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        layers.append(current)
        for key in ("data", "content"):
            nested = current.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return layers


def _ggsel_scalar(payload: dict[str, Any], key: str) -> str:
    for layer in _ggsel_response_layers(payload):
        value = layer.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def _ggsel_summary_product_id(summary: dict[str, Any]) -> str:
    for key in ("item_id", "id_goods", "product_id", "offer_id"):
        value = summary.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    product = summary.get("product")
    if isinstance(product, dict):
        for key in ("id", "item_id", "product_id", "offer_id"):
            value = product.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value).strip()
    return ""


def verified_ggsel_order_content(
    summary: dict[str, Any],
    detail: dict[str, Any],
    *,
    seller_id: str,
) -> dict[str, Any]:
    """Return the verified order content or reject it before fulfillment."""

    layers = _ggsel_response_layers(detail)
    content = next(
        (
            layer
            for layer in layers
            if any(key in layer for key in ("invoice_state", "owner", "item_id"))
        ),
        {},
    )
    state_text = _ggsel_scalar(content, "invoice_state")
    if not state_text:
        raise GgselOrderValidationError("GGsel order has no invoice_state")
    try:
        state = int(state_text)
    except ValueError as exc:
        raise GgselOrderValidationError("GGsel order has invalid invoice_state") from exc
    if state not in {3, 4}:
        raise GgselOrderValidationError(
            f"GGsel order is not payable for delivery (invoice_state={state})",
            permanent=state in {2, 5},
        )

    owner = _ggsel_scalar(content, "owner")
    expected_owner = str(seller_id or "").strip()
    if not owner:
        raise GgselOrderValidationError("GGsel order has no owner")
    if not expected_owner or owner != expected_owner:
        raise GgselOrderValidationError(
            f"GGsel order owner mismatch (expected {expected_owner or '<configured seller>'}, got {owner})",
            permanent=True,
        )

    item_id = _ggsel_scalar(content, "item_id")
    if not item_id:
        raise GgselOrderValidationError("GGsel order has no item_id")
    summary_item_id = _ggsel_summary_product_id(summary)
    if summary_item_id and item_id != summary_item_id:
        raise GgselOrderValidationError(
            f"GGsel order item mismatch (summary {summary_item_id}, detail {item_id})",
            permanent=True,
        )
    return content


class GgselChatClient:
    LAST_SALES_TOP = 100

    def __init__(
        self,
        *,
        api_key: str,
        seller_id: str = "",
        timeout: float = 30.0,
        base_url: str = "https://seller.ggsel.com",
    ) -> None:
        self.api_key = api_key.strip()
        self.seller_id = seller_id.strip()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._token_until = 0.0

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _v1_headers(*, locale: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if locale:
            headers["locale"] = "ru"
        return headers

    def configured_for_polling(self) -> bool:
        return bool(self.seller_id and self.api_key)

    async def token(self) -> str:
        if not self.configured_for_polling():
            raise RuntimeError("GGsel seller ID/API key are not configured")
        if self._token and time.monotonic() < self._token_until:
            return self._token
        try:
            seller_id = int(self.seller_id)
        except ValueError as exc:
            raise RuntimeError("GGsel seller ID must be an integer") from exc
        timestamp = str(int(time.time()))
        sign = hashlib.sha256(f"{self.api_key}{timestamp}".encode("utf-8")).hexdigest()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api_sellers/api/apilogin",
                json={"seller_id": seller_id, "timestamp": timestamp, "sign": sign},
                headers=self._v1_headers(),
            )
        data = self._v1_response(response, "GGsel login API")
        data_dict = data if isinstance(data, dict) else {}
        token = ""
        for layer in _ggsel_response_layers(data_dict):
            token = self._pick_text(layer, "token", "access_token", "jwt", "api_token")
            if token:
                break
        if not token:
            message = self._response_message(data_dict)
            suffix = f": {message}" if message else ""
            raise RuntimeError(f"GGsel login API did not return token{suffix}")
        self._token = token
        self._token_until = time.monotonic() + 50 * 60
        return token

    async def auth_headers(self) -> dict[str, str]:
        await self.token()
        return self._v1_headers()

    @staticmethod
    def _pick_text(payload: dict[str, Any], *keys: str) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After", "").strip()
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                return max(0.0, retry_at.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _json_response(response: httpx.Response, label: str) -> Any:
        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
            except ValueError:
                payload = None
            message = GgselChatClient._response_message(payload)
            if message:
                detail = message
            raise GgselApiError(
                f"{label} {response.status_code}: {detail}",
                status_code=response.status_code,
                retry_after=GgselChatClient._retry_after(response),
            )
        if not response.text.strip():
            return {"status": "ok"}
        try:
            return response.json()
        except ValueError as exc:
            raise GgselApiError(
                f"{label} returned invalid JSON",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _response_message(payload: Any) -> str:
        for layer in _ggsel_response_layers(payload):
            for key in ("retdesc", "desc", "message", "error"):
                value = layer.get(key)
                if value not in (None, "") and not isinstance(value, (dict, list)):
                    return str(value).strip()
        return ""

    @classmethod
    def _v1_response(cls, response: httpx.Response, label: str) -> Any:
        data = cls._json_response(response, label)
        for layer in _ggsel_response_layers(data):
            retval = layer.get("retval")
            if retval not in (None, "", 0, "0"):
                message = cls._response_message(data) or f"retval={retval}"
                raise GgselApiError(
                    f"{label}: {message}",
                    status_code=response.status_code,
                    retry_after=cls._retry_after(response),
                )
        return data

    @staticmethod
    def _list_from_response(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in (
            "items",
            "sales",
            "orders",
            "purchases",
            "messages",
            "chats",
            "rows",
            "data",
            "content",
            "result",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = GgselChatClient._list_from_response(value)
                if nested:
                    return nested
        return []

    async def last_sales(self) -> list[dict[str, Any]]:
        if not self.configured_for_polling():
            return []
        token = await self.token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api_sellers/api/seller-last-sales",
                params={"token": token, "top": self.LAST_SALES_TOP},
                headers=self._v1_headers(locale=True),
            )
        return self._list_from_response(self._v1_response(response, "GGsel sales API"))

    async def order_info(self, order_id: str) -> dict[str, Any]:
        if not self.configured_for_polling():
            return {}
        token = await self.token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api_sellers/api/purchase/info/{order_id}",
                params={"token": token},
                headers=self._v1_headers(locale=True),
            )
        data = self._v1_response(response, "GGsel order API")
        return data if isinstance(data, dict) else {"items": data}

    async def unread_chats(self) -> list[dict[str, Any]]:
        if not self.configured_for_polling():
            return []
        token = await self.token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api_sellers/api/debates/v2/chats",
                params={"token": token},
                headers=self._v1_headers(locale=True),
            )
        return self._list_from_response(self._v1_response(response, "GGsel unread chats API"))

    async def send_order_message(self, order_id: str, message: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GGsel API key is not configured")
        if self.configured_for_polling():
            token = await self.token()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api_sellers/api/debates/v2",
                    params={"token": token, "id_i": order_id},
                    json={"message": message},
                    headers=self._v1_headers(),
                )
            return self._v1_response(response, "GGsel chat API")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/seller/chats/messages",
                json={"order_id": order_id, "message": message},
                headers=self.headers(),
            )
        return self._json_response(response, "GGsel chat API")

    async def order_messages(self, order_id: str) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("GGsel API key is not configured")
        if self.configured_for_polling():
            token = await self.token()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api_sellers/api/debates/v2",
                    params={"token": token, "id_i": order_id, "count": 100},
                    headers=self._v1_headers(),
                )
            return self._list_from_response(self._v1_response(response, "GGsel chat API"))
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api/seller/chats/messages",
                params={"order_id": order_id},
                headers=self.headers(),
            )
        return self._list_from_response(self._json_response(response, "GGsel chat API"))


class RuntimeGgselClient:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._client: GgselChatClient | None = None
        self._config: tuple[str, str, float] | None = None

    def client(self) -> GgselChatClient:
        config = (
            self.runtime.get_text("ggsel_seller_id"),
            self.runtime.get_text("ggsel_api_key"),
            self.runtime.get_float("xyranet_timeout_seconds"),
        )
        if self._client is None or self._config != config:
            self._config = config
            self._client = GgselChatClient(
                seller_id=config[0],
                api_key=config[1],
                timeout=config[2],
            )
        return self._client

    def configured_for_polling(self) -> bool:
        return bool(self.runtime.get_text("ggsel_seller_id") and self.runtime.get_text("ggsel_api_key"))

    async def token(self) -> str:
        return await self.client().token()

    async def last_sales(self) -> list[dict[str, Any]]:
        return await self.client().last_sales()

    async def order_info(self, order_id: str) -> dict[str, Any]:
        return await self.client().order_info(order_id)

    async def unread_chats(self) -> list[dict[str, Any]]:
        return await self.client().unread_chats()

    async def send_order_message(self, order_id: str, message: str) -> dict[str, Any]:
        return await self.client().send_order_message(order_id, message)

    async def order_messages(self, order_id: str) -> list[dict[str, Any]]:
        return await self.client().order_messages(order_id)


class MarketplaceMessenger:
    def __init__(
        self,
        *,
        digiseller: Any,
        ggsel: GgselChatClient | None = None,
        db: Any | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self.digiseller = digiseller
        self.ggsel = ggsel
        self.db = db
        self.on_message = on_message
        self.on_error = on_error

    async def send_message(
        self,
        marketplace: str,
        external_order_id: str,
        text: str,
        *,
        role: str = "bot",
        author_name: str = "XyraNet",
        source: str = "automation",
    ) -> bool:
        try:
            if marketplace in {"plati", "digiseller"}:
                response = await self.digiseller.send_order_message(external_order_id, text)
            elif marketplace == "ggsel" and self.ggsel:
                response = await self.ggsel.send_order_message(external_order_id, text)
            else:
                log.warning("No messenger configured for marketplace %s", marketplace)
                return False
        except Exception as exc:
            log.exception("Cannot send marketplace message for %s:%s", marketplace, external_order_id)
            if self.on_error:
                self.on_error(marketplace, exc)
            return False
        try:
            self._record_sent_message(
                marketplace=marketplace,
                external_order_id=external_order_id,
                text=text,
                role=role,
                author_name=author_name,
                source=source,
                response=response,
            )
        except Exception:
            # The marketplace already accepted the message. A local history or
            # Telegram-notification failure must not make callers retry it and
            # accidentally send the buyer a duplicate.
            log.exception("Cannot record sent marketplace message for %s:%s", marketplace, external_order_id)
        return True

    def _record_sent_message(
        self,
        *,
        marketplace: str,
        external_order_id: str,
        text: str,
        role: str,
        author_name: str,
        source: str,
        response: Any,
    ) -> None:
        if self.db is None:
            return
        response_data = response if isinstance(response, dict) else {}
        external_message_id = ""
        for key in ("id", "message_id", "MessageId", "DebateId"):
            if response_data.get(key) not in (None, ""):
                external_message_id = str(response_data[key]).strip()
                break
        row, created = self.db.add_chat_message(
            marketplace=marketplace,
            external_order_id=external_order_id,
            role=role,
            author_name=author_name,
            text=text,
            external_message_id=external_message_id,
            source=source,
            raw_payload=response_data,
        )
        if created and self.on_message:
            self.on_message(row)

    async def order_messages(self, marketplace: str, external_order_id: str) -> list[dict[str, Any]]:
        try:
            if marketplace in {"plati", "digiseller"}:
                return await self.digiseller.order_messages(external_order_id)
            if marketplace == "ggsel" and self.ggsel:
                return await self.ggsel.order_messages(external_order_id)
        except Exception:
            log.exception("Cannot read marketplace messages for %s:%s", marketplace, external_order_id)
        return []
