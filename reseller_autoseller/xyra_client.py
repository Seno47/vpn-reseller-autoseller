from __future__ import annotations

from typing import Any
from urllib.parse import quote

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
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, f"{self.base_url}{path}", json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise XyraNetApiError(f"Cannot reach XyraNet API: {exc}") from exc
        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                detail = response.text
            else:
                if isinstance(error_payload, dict):
                    detail = error_payload.get("detail") or error_payload.get("message") or error_payload
                else:
                    detail = error_payload
            raise XyraNetApiError(f"XyraNet API {response.status_code}: {detail}")
        try:
            result = response.json()
        except ValueError as exc:
            raise XyraNetApiError("XyraNet API returned invalid JSON") from exc
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and all(isinstance(item, dict) for item in result):
            return result
        raise XyraNetApiError("XyraNet API returned an unexpected response")

    @staticmethod
    def _object_result(result: dict[str, Any] | list[dict[str, Any]], label: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise XyraNetApiError(f"XyraNet {label} API returned an unexpected response")
        return result

    @staticmethod
    def _list_result(result: dict[str, Any] | list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
        if not isinstance(result, list):
            raise XyraNetApiError(f"XyraNet {label} API returned an unexpected response")
        return result

    async def summary(self) -> dict[str, Any]:
        result = await self.request("GET", "/summary")
        return self._object_result(result, "summary")

    async def tariffs(self) -> list[dict[str, Any]]:
        result = await self.request("GET", "/tariffs")
        return self._list_result(result, "tariffs")

    async def create_order(self, tariff_code: str, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/orders",
            json={"tariff_code": tariff_code},
            idempotency_key=idempotency_key,
        )
        return self._object_result(result, "create order")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        result = await self.request("GET", f"/orders/{quote(order_id, safe='')}")
        return self._object_result(result, "get order")

    async def renew_order(self, order_id: str, tariff_code: str | None = None, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/orders/{quote(order_id, safe='')}/renew",
            json={"tariff_code": tariff_code} if tariff_code else {},
            idempotency_key=idempotency_key,
        )
        return self._object_result(result, "renew order")

    async def reissue_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/orders/{quote(order_id, safe='')}/reissue",
            json={},
            idempotency_key=idempotency_key,
        )
        return self._object_result(result, "reissue order")

    async def traffic_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/traffic/quote",
            json={**payload, "order_id": order_id},
        )
        return self._object_result(result, "traffic quote")

    async def traffic_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/traffic/purchase",
            json={**payload, "order_id": order_id},
            idempotency_key=idempotency_key,
        )
        return self._object_result(result, "traffic purchase")

    async def ip_limit_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/ip-limit/quote",
            json={**payload, "order_id": order_id},
        )
        return self._object_result(result, "IP limit quote")

    async def ip_limit_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/ip-limit/purchase",
            json={**payload, "order_id": order_id},
            idempotency_key=idempotency_key,
        )
        return self._object_result(result, "IP limit purchase")


def extract_order_delivery(response: dict[str, Any]) -> dict[str, str]:
    raw_order = response.get("order")
    order = raw_order if isinstance(raw_order, dict) else {}
    raw_subscription = order.get("subscription")
    subscription = raw_subscription if isinstance(raw_subscription, dict) else {}
    return {
        "order_id": str(order.get("order_id") or ""),
        "panel_username": str(order.get("panel_username") or ""),
        "subscription_url": str(subscription.get("subscription_url") or ""),
        "tariff_code": str(subscription.get("tariff_code") or ""),
        "expire_at": str(subscription.get("expire_at") or ""),
    }
