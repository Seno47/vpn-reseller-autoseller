import unittest
from unittest.mock import AsyncMock, patch

import httpx

from reseller_autoseller.xyra_client import XyraNetApiError, XyraNetClient


class XyraNetClientTests(unittest.TestCase):
    def test_uses_documented_wholesale_base_url(self) -> None:
        client = XyraNetClient(base_url="https://xyranet.pro/api/wholesale", api_key="key")

        self.assertEqual(client.base_url, "https://xyranet.pro/api/wholesale")

    def test_accepts_legacy_origin_base_url(self) -> None:
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        self.assertEqual(client.base_url, "https://xyranet.pro/api/wholesale")


class XyraNetClientHttpTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fake_http(response: httpx.Response) -> AsyncMock:
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.request.return_value = response
        return http

    async def test_json_array_error_body_is_reported_as_api_error(self) -> None:
        http = self.fake_http(httpx.Response(400, json=["bad request"]))
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        with patch("reseller_autoseller.xyra_client.httpx.AsyncClient", return_value=http):
            with self.assertRaisesRegex(XyraNetApiError, "bad request"):
                await client.request("GET", "/summary")

    async def test_invalid_success_json_is_reported_as_api_error(self) -> None:
        http = self.fake_http(httpx.Response(200, text="not-json"))
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        with patch("reseller_autoseller.xyra_client.httpx.AsyncClient", return_value=http):
            with self.assertRaisesRegex(XyraNetApiError, "invalid JSON"):
                await client.request("GET", "/summary")

    async def test_explicit_order_id_cannot_be_overridden_by_payload(self) -> None:
        http = self.fake_http(httpx.Response(200, json={"order": {"order_id": "trusted-order"}}))
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        with patch("reseller_autoseller.xyra_client.httpx.AsyncClient", return_value=http):
            await client.traffic_purchase(
                "trusted-order",
                {"order_id": "other-order", "gigabytes": 10},
                idempotency_key="sale-1",
            )

        self.assertEqual(http.request.await_args.kwargs["json"]["order_id"], "trusted-order")

    async def test_order_id_is_encoded_as_one_path_segment(self) -> None:
        http = self.fake_http(httpx.Response(200, json={"order": {}}))
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        with patch("reseller_autoseller.xyra_client.httpx.AsyncClient", return_value=http):
            await client.get_order("order/../?target=other#fragment")

        self.assertEqual(
            http.request.await_args.args[1],
            "https://xyranet.pro/api/wholesale/orders/order%2F..%2F%3Ftarget%3Dother%23fragment",
        )


if __name__ == "__main__":
    unittest.main()
