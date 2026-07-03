from __future__ import annotations

import logging
from typing import Any

import httpx


log = logging.getLogger(__name__)


class GgselChatClient:
    def __init__(self, *, api_key: str, timeout: float = 30.0, base_url: str = "https://api.ggsel.net") -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def send_order_message(self, order_id: str, message: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GGsel API key is not configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/seller/chats/messages",
                json={"order_id": order_id, "message": message},
                headers=self.headers(),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"GGsel chat API {response.status_code}: {response.text}")
        return response.json() if response.text.strip() else {"status": "ok"}

    async def order_messages(self, order_id: str) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("GGsel API key is not configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/api/seller/chats/messages",
                params={"order_id": order_id},
                headers=self.headers(),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"GGsel chat API {response.status_code}: {response.text}")
        data = response.json()
        if isinstance(data, list):
            return data
        for key in ("messages", "items", "data"):
            if isinstance(data.get(key), list):
                return list(data[key])
        return []


class MarketplaceMessenger:
    def __init__(self, *, digiseller: Any, ggsel: GgselChatClient | None = None) -> None:
        self.digiseller = digiseller
        self.ggsel = ggsel

    async def send_message(self, marketplace: str, external_order_id: str, text: str) -> bool:
        try:
            if marketplace in {"plati", "digiseller"}:
                await self.digiseller.send_order_message(external_order_id, text)
                return True
            if marketplace == "ggsel" and self.ggsel:
                await self.ggsel.send_order_message(external_order_id, text)
                return True
            log.warning("No messenger configured for marketplace %s", marketplace)
        except Exception:
            log.exception("Cannot send marketplace message for %s:%s", marketplace, external_order_id)
        return False

    async def order_messages(self, marketplace: str, external_order_id: str) -> list[dict[str, Any]]:
        try:
            if marketplace in {"plati", "digiseller"}:
                return await self.digiseller.order_messages(external_order_id)
            if marketplace == "ggsel" and self.ggsel:
                return await self.ggsel.order_messages(external_order_id)
        except Exception:
            log.exception("Cannot read marketplace messages for %s:%s", marketplace, external_order_id)
        return []
