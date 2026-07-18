import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx

from reseller_autoseller.db import Database
from reseller_autoseller.marketplace_chat import (
    GgselApiError,
    GgselChatClient,
    GgselOrderValidationError,
    MarketplaceMessenger,
    verified_ggsel_order_content,
)


class GgselChatClientTests(unittest.TestCase):
    def test_uses_live_seller_office_api_host(self) -> None:
        client = GgselChatClient(seller_id="1", api_key="key")

        self.assertEqual(client.base_url, "https://seller.ggsel.com")

    def test_invalid_json_has_contextual_api_error(self) -> None:
        response = httpx.Response(200, text="not-json")

        with self.assertRaisesRegex(RuntimeError, "GGsel sales API returned invalid JSON"):
            GgselChatClient._json_response(response, "GGsel sales API")

    def test_validates_paid_order_owner_and_item(self) -> None:
        content = verified_ggsel_order_content(
            {"invoice_id": 10, "product": {"id": 5968452}},
            {
                "retval": 0,
                "retdesc": "OK",
                "data": {
                    "content": {
                        "invoice_state": 3,
                        "owner": 123,
                        "item_id": 5968452,
                    }
                },
            },
            seller_id="123",
        )

        self.assertEqual(content["item_id"], 5968452)

    def test_rejects_unpaid_foreign_or_mismatched_order(self) -> None:
        cases = (
            (
                {"content": {"invoice_state": 1, "owner": 123, "item_id": 5968452}},
                "not payable",
            ),
            (
                {"content": {"invoice_state": 3, "owner": 999, "item_id": 5968452}},
                "owner mismatch",
            ),
            (
                {"content": {"invoice_state": 3, "owner": 123, "item_id": 999}},
                "item mismatch",
            ),
        )
        for detail, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(GgselOrderValidationError, message):
                    verified_ggsel_order_content(
                        {"product": {"id": 5968452}},
                        detail,
                        seller_id="123",
                    )


class GgselChatClientHttpTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _http() -> AsyncMock:
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        return http

    async def test_login_uses_timestamp_and_sha256_signature(self) -> None:
        http = self._http()
        http.post.return_value = httpx.Response(
            200,
            json={"retval": 0, "desc": "OK", "data": {"token": "signed-token"}},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch("reseller_autoseller.marketplace_chat.time.time", return_value=1721304000),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            token = await client.token()

        expected_sign = hashlib.sha256(b"secret1721304000").hexdigest()
        request = http.post.await_args
        self.assertEqual(token, "signed-token")
        self.assertEqual(
            request.kwargs["json"],
            {"seller_id": 123, "timestamp": "1721304000", "sign": expected_sign},
        )
        self.assertNotIn("Authorization", request.kwargs["headers"])

    async def test_last_sales_uses_query_token_locale_and_larger_window(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(
            200,
            json={"retval": 0, "content": {"sales": [{"invoice_id": 10}]}},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            sales = await client.last_sales()

        request = http.get.await_args
        self.assertEqual(sales, [{"invoice_id": 10}])
        self.assertEqual(request.kwargs["params"], {"token": "v1-token", "top": 100})
        self.assertEqual(request.kwargs["headers"]["locale"], "ru")
        self.assertNotIn("Authorization", request.kwargs["headers"])

    async def test_order_info_uses_query_token_and_locale(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(
            200,
            json={"retval": 0, "retdesc": "OK", "content": {"invoice_state": 3}},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            result = await client.order_info("10")

        request = http.get.await_args
        self.assertEqual(result["content"]["invoice_state"], 3)
        self.assertEqual(request.kwargs["params"], {"token": "v1-token"})
        self.assertEqual(request.kwargs["headers"]["locale"], "ru")
        self.assertNotIn("Authorization", request.kwargs["headers"])

    async def test_debate_send_uses_token_and_id_i_query_parameters(self) -> None:
        http = self._http()
        http.post.return_value = httpx.Response(200, json={"retval": 0, "retdesc": "OK"})
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            result = await client.send_order_message("10", "Delivery")

        request = http.post.await_args
        self.assertEqual(result["retval"], 0)
        self.assertEqual(request.kwargs["params"], {"token": "v1-token", "id_i": "10"})
        self.assertEqual(request.kwargs["json"], {"message": "Delivery"})
        self.assertNotIn("Authorization", request.kwargs["headers"])

    async def test_debate_list_uses_token_and_id_i_query_parameters(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(
            200,
            json={"retval": 0, "content": {"messages": [{"id": 1, "message": "Hello"}]}},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            messages = await client.order_messages("10")

        request = http.get.await_args
        self.assertEqual(messages, [{"id": 1, "message": "Hello"}])
        self.assertEqual(
            request.kwargs["params"],
            {"token": "v1-token", "id_i": "10", "count": 100},
        )
        self.assertNotIn("Authorization", request.kwargs["headers"])

    async def test_unread_chats_uses_official_v1_endpoint(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(
            200,
            json={"retval": 0, "content": {"chats": [{"id_i": 10, "cnt_new": 2}]}},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            chats = await client.unread_chats()

        request = http.get.await_args
        self.assertEqual(chats, [{"id_i": 10, "cnt_new": 2}])
        self.assertEqual(
            request.args[0],
            "https://seller.ggsel.com/api_sellers/api/debates/v2/chats",
        )
        self.assertEqual(request.kwargs["params"], {"token": "v1-token"})
        self.assertEqual(request.kwargs["headers"]["locale"], "ru")

    async def test_http_error_exposes_status_and_retry_after(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"message": "Too many requests"},
        )
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaises(GgselApiError) as caught:
                await client.unread_chats()

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertTrue(caught.exception.retryable)

    async def test_v1_send_error_never_falls_back_to_legacy_endpoint(self) -> None:
        http = self._http()
        http.post.return_value = httpx.Response(503, text="unavailable")
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaises(GgselApiError):
                await client.send_order_message("10", "Delivery")

        self.assertEqual(http.post.await_count, 1)
        self.assertEqual(
            http.post.await_args.args[0],
            "https://seller.ggsel.com/api_sellers/api/debates/v2",
        )

    async def test_v1_read_error_never_falls_back_to_legacy_endpoint(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(503, text="unavailable")
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaises(GgselApiError):
                await client.order_messages("10")

        self.assertEqual(http.get.await_count, 1)
        self.assertEqual(
            http.get.await_args.args[0],
            "https://seller.ggsel.com/api_sellers/api/debates/v2",
        )

    async def test_legacy_chat_endpoint_is_used_only_without_seller_id(self) -> None:
        http = self._http()
        http.post.return_value = httpx.Response(200, json={"status": "ok"})
        client = GgselChatClient(api_key="secret")

        with patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http):
            await client.send_order_message("10", "Delivery")

        self.assertEqual(
            http.post.await_args.args[0],
            "https://seller.ggsel.com/api/seller/chats/messages",
        )
        self.assertEqual(http.post.await_args.kwargs["headers"]["Authorization"], "Bearer secret")

    async def test_v1_error_uses_retdesc_from_response(self) -> None:
        http = self._http()
        http.get.return_value = httpx.Response(200, json={"retval": 1, "retdesc": "Permission denied"})
        client = GgselChatClient(seller_id="123", api_key="secret")

        with (
            patch.object(client, "token", AsyncMock(return_value="v1-token")),
            patch("reseller_autoseller.marketplace_chat.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                await client.order_info("10")


class FakeDigiseller:
    async def send_order_message(self, order_id: str, text: str) -> dict[str, int]:
        return {"retval": 0}


class BrokenHistoryDatabase:
    def add_chat_message(self, **kwargs):
        raise RuntimeError("local database is temporarily unavailable")


class MarketplaceMessengerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_message_is_saved_with_its_role(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            notified: list[dict] = []
            messenger = MarketplaceMessenger(
                digiseller=FakeDigiseller(),
                db=db,
                on_message=notified.append,
            )

            sent = await messenger.send_message(
                "plati",
                "1001",
                "Здравствуйте!",
                role="admin",
                author_name="Оператор",
                source="telegram",
            )

            self.assertTrue(sent)
            rows = db.list_chat_messages("plati", "1001")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["role"], "admin")
            self.assertEqual(rows[0]["author_name"], "Оператор")
            self.assertEqual([row["id"] for row in notified], [rows[0]["id"]])

    async def test_local_history_failure_does_not_retry_remote_send(self) -> None:
        messenger = MarketplaceMessenger(
            digiseller=FakeDigiseller(),
            db=BrokenHistoryDatabase(),
        )

        self.assertTrue(await messenger.send_message("plati", "1001", "Уже отправлено"))


if __name__ == "__main__":
    unittest.main()
