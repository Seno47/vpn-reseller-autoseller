from __future__ import annotations

from typing import Any

import httpx


class XyraNetApiError(RuntimeError):
    pass


class XyraNetClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = self.normalize_base_url(base_url)
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def normalize_base_url(base_url: str) -> str:
        normalized = (base_url or "https://xyranet.pro/api/wholesale").rstrip("/")
        if not normalized.endswith("/api/wholesale"):
            normalized = f"{normalized}/api/wholesale"
        return normalized

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if not self.api_key:
            raise XyraNetApiError("XYRANET_API_KEY is not configured")
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", json=json, headers=headers)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise XyraNetApiError(f"XyraNet API {response.status_code}: {detail}")
        return response.json()

    async def summary(self) -> dict[str, Any]:
        result = await self.request("GET", "/summary")
        return dict(result)

    async def tariffs(self) -> list[dict[str, Any]]:
        result = await self.request("GET", "/tariffs")
        return list(result)

    async def create_order(self, tariff_code: str, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/orders",
            json={"tariff_code": tariff_code},
            idempotency_key=idempotency_key,
        )
        return dict(result)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        result = await self.request("GET", f"/orders/{order_id}")
        return dict(result)

    async def renew_order(self, order_id: str, tariff_code: str | None = None, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/orders/{order_id}/renew",
            json={"tariff_code": tariff_code} if tariff_code else {},
            idempotency_key=idempotency_key,
        )
        return dict(result)

    async def reissue_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/orders/{order_id}/reissue",
            json={},
            idempotency_key=idempotency_key,
        )
        return dict(result)

    async def traffic_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/traffic/quote",
            json={"order_id": order_id, **payload},
        )
        return dict(result)

    async def traffic_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/traffic/purchase",
            json={"order_id": order_id, **payload},
            idempotency_key=idempotency_key,
        )
        return dict(result)

    async def ip_limit_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/ip-limit/quote",
            json={"order_id": order_id, **payload},
        )
        return dict(result)

    async def ip_limit_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/ip-limit/purchase",
            json={"order_id": order_id, **payload},
            idempotency_key=idempotency_key,
        )
        return dict(result)


def extract_order_delivery(response: dict[str, Any]) -> dict[str, str]:
    order = response.get("order") or {}
    subscription = order.get("subscription") or {}
    return {
        "order_id": str(order.get("order_id") or ""),
        "panel_username": str(order.get("panel_username") or ""),
        "subscription_url": str(subscription.get("subscription_url") or ""),
        "tariff_code": str(subscription.get("tariff_code") or ""),
        "expire_at": str(subscription.get("expire_at") or ""),
    }
