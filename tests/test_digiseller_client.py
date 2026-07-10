import unittest
from unittest.mock import AsyncMock, patch

import httpx

from reseller_autoseller.digiseller_client import (
    DigisellerApiError,
    DigisellerClient,
    purchase_amount,
    purchase_buyer_email,
    purchase_currency,
    purchase_invoice_id,
    purchase_paid_at,
    purchase_product_id,
    purchase_variant_id,
    sale_event_from_unique_code,
    unique_code_state,
)


class DigisellerClientTests(unittest.TestCase):
    def test_builds_sale_event_from_unique_code_purchase(self) -> None:
        event = sale_event_from_unique_code(
            {
                "inv": 12345,
                "id_goods": 678,
                "email": "buyer@example.com",
                "amount": 299,
                "type_curr": "RUB",
                "options": [{"name": "Tariff", "variant_id": 42}],
            }
        )

        self.assertEqual(event.marketplace, "plati")
        self.assertEqual(event.external_order_id, "12345")
        self.assertEqual(event.external_product_id, "678")
        self.assertEqual(event.external_variant_id, "42")
        self.assertEqual(event.buyer_email, "buyer@example.com")

    def test_unique_code_becomes_part_of_sale_key(self) -> None:
        event = sale_event_from_unique_code(
            {
                "inv": 12345,
                "id_goods": 678,
                "options": [{"variant_id": 42}],
            },
            "ABCDEF1234567890",
        )

        self.assertEqual(event.external_order_id, "12345:ABCDEF1234567890")
        self.assertEqual(event.raw_payload["inv"], 12345)
        self.assertEqual(event.raw_payload["unique_code"], "ABCDEF1234567890")

    def test_reads_unique_code_state(self) -> None:
        self.assertEqual(unique_code_state({"unique_code_state": {"state": 5}}), 5)
        self.assertEqual(unique_code_state({"content": {"unique_code_state": {"state": 1}}}), 1)
        self.assertIsNone(unique_code_state({"unique_code_state": {"state": "bad"}}))

    def test_reads_purchase_info_content_fields(self) -> None:
        purchase = {
            "content": {
                "item_id": 5968452,
                "amount": "299.00",
                "currency_type": "RUB",
                "date_pay": "2026-07-08 12:30:00",
                "buyer_info": {"email": "buyer@example.com"},
                "options": [{"id": 10, "name": "Tariff", "user_data_id": 42}],
            }
        }

        self.assertEqual(purchase_invoice_id({"invoice_id": 123}), "123")
        self.assertEqual(purchase_invoice_id({}, "456"), "456")
        self.assertEqual(purchase_product_id(purchase), "5968452")
        self.assertEqual(purchase_variant_id(purchase), "42")
        self.assertEqual(purchase_buyer_email(purchase), "buyer@example.com")
        self.assertEqual(purchase_amount(purchase), "299.00")
        self.assertEqual(purchase_currency(purchase), "RUB")
        self.assertEqual(purchase_paid_at(purchase), "2026-07-08 12:30:00")

    def test_reads_rows_list_from_seller_sales_response(self) -> None:
        rows = DigisellerClient._list_from_response({"retval": 0, "rows": [{"invoice_id": 123}]})

        self.assertEqual(rows, [{"invoice_id": 123}])


class DigisellerClientHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_delivered_accepts_already_delivered_retval(self) -> None:
        response = httpx.Response(
            200,
            json={"retval": 4, "retdesc": "The unique code was already delivered"},
        )
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.request.return_value = response
        client = DigisellerClient(seller_id="1", api_key="key")

        with (
            patch.object(client, "token", AsyncMock(return_value="token")),
            patch("reseller_autoseller.digiseller_client.httpx.AsyncClient", return_value=http),
        ):
            result = await client.mark_unique_code_delivered("ABCDEF1234567890")

        self.assertEqual(result["retval"], 4)

    async def test_mark_delivered_still_rejects_other_error_retvals(self) -> None:
        response = httpx.Response(200, json={"retval": 5, "retdesc": "Invalid state"})
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.request.return_value = response
        client = DigisellerClient(seller_id="1", api_key="key")

        with (
            patch.object(client, "token", AsyncMock(return_value="token")),
            patch("reseller_autoseller.digiseller_client.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaises(DigisellerApiError):
                await client.mark_unique_code_delivered("ABCDEF1234567890")

    async def test_authenticated_request_refreshes_an_expired_token_once(self) -> None:
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.request.side_effect = [
            httpx.Response(401, text="expired"),
            httpx.Response(200, json=[]),
        ]
        client = DigisellerClient(seller_id="1", api_key="key")

        with (
            patch.object(client, "token", AsyncMock(side_effect=["stale", "fresh"])) as token,
            patch("reseller_autoseller.digiseller_client.httpx.AsyncClient", return_value=http),
        ):
            messages = await client.order_messages("123")

        self.assertEqual(messages, [])
        self.assertEqual(token.await_count, 2)
        self.assertEqual(http.request.await_args_list[0].kwargs["params"]["token"], "stale")
        self.assertEqual(http.request.await_args_list[1].kwargs["params"]["token"], "fresh")

    async def test_chat_error_payload_is_not_silently_treated_as_empty(self) -> None:
        response = httpx.Response(200, json={"retval": 1, "retdesc": "Permission denied"})
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.request.return_value = response
        client = DigisellerClient(seller_id="1", api_key="key")

        with (
            patch.object(client, "token", AsyncMock(return_value="token")),
            patch("reseller_autoseller.digiseller_client.httpx.AsyncClient", return_value=http),
        ):
            with self.assertRaisesRegex(DigisellerApiError, "Permission denied"):
                await client.order_messages("123")


if __name__ == "__main__":
    unittest.main()
