from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx


log = logging.getLogger(__name__)


class GgselChatClient:
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
        token = self._token or self.api_key
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def configured_for_polling(self) -> bool:
        return bool(self.seller_id and self.api_key)

    async def token(self) -> str:
        if not self.configured_for_polling():
            raise RuntimeError("GGsel seller ID/API key are not configured")
        if self._token and time.monotonic() < self._token_until:
            return self._token
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api_sellers/api/apilogin",
                json={"seller_id": self.seller_id, "api_key": self.api_key},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        data = self._json_response(response, "GGsel login API")
        data_dict = data if isinstance(data, dict) else {}
        token = self._pick_text(data_dict, "token", "access_token", "jwt", "api_token")
        if not token:
            nested = data_dict.get("data")
            token = self._pick_text(nested if isinstance(nested, dict) else {}, "token", "access_token", "jwt")
        if not token:
            raise RuntimeError("GGsel login API did not return token")
        self._token = token
        self._token_until = time.monotonic() + 50 * 60
        return token

    async def auth_headers(self) -> dict[str, str]:
        await self.token()
        return self.headers()

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
    def _json_response(response: httpx.Response, label: str) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(f"{label} {response.status_code}: {response.text}")
        if not response.text.strip():
            return {"status": "ok"}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{label} returned invalid JSON") from exc

    @staticmethod
    def _list_from_response(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("items", "sales", "orders", "purchases", "messages", "chats", "data", "result"):
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
        headers = await self.auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api_sellers/api/seller-last-sales",
                headers=headers,
            )
        return self._list_from_response(self._json_response(response, "GGsel sales API"))

    async def order_info(self, order_id: str) -> dict[str, Any]:
        if not self.configured_for_polling():
            return {}
        headers = await self.auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api_sellers/api/purchase/info/{order_id}",
                headers=headers,
            )
        data = self._json_response(response, "GGsel order API")
        return data if isinstance(data, dict) else {"items": data}

    async def send_order_message(self, order_id: str, message: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GGsel API key is not configured")
        if self.configured_for_polling():
            try:
                headers = await self.auth_headers()
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/api_sellers/api/debates/v2",
                        json={"invoice_id": order_id, "message": message},
                        headers=headers,
                    )
                return self._json_response(response, "GGsel chat API")
            except Exception:
                log.exception("Cannot send GGsel message through seller API, falling back to legacy chat endpoint")
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
            try:
                headers = await self.auth_headers()
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        f"{self.base_url}/api_sellers/api/debates/v2",
                        params={"invoice_id": order_id},
                        headers=headers,
                    )
                return self._list_from_response(self._json_response(response, "GGsel chat API"))
            except Exception:
                log.exception("Cannot read GGsel messages through seller API, falling back to legacy chat endpoint")
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
    ) -> None:
        self.digiseller = digiseller
        self.ggsel = ggsel
        self.db = db
        self.on_message = on_message

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
        except Exception:
            log.exception("Cannot send marketplace message for %s:%s", marketplace, external_order_id)
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
