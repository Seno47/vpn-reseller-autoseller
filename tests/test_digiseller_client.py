import unittest

from reseller_autoseller.digiseller_client import (
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


if __name__ == "__main__":
    unittest.main()
