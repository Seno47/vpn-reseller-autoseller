import unittest

from reseller_autoseller.digiseller_client import sale_event_from_unique_code, unique_code_state


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
        self.assertIsNone(unique_code_state({"unique_code_state": {"state": "bad"}}))


if __name__ == "__main__":
    unittest.main()
