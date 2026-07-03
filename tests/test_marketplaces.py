import unittest

from reseller_autoseller.marketplaces import normalize_sale


class MarketplaceNormalizeTests(unittest.TestCase):
    def test_normalize_digiseller_payload(self) -> None:
        event = normalize_sale(
            "plati",
            {
                "invoice_id": "123",
                "id_goods": "456",
                "email": "buyer@example.com",
                "amount": "299",
                "currency": "RUB",
            },
        )

        self.assertEqual(event.marketplace, "plati")
        self.assertEqual(event.external_order_id, "123")
        self.assertEqual(event.external_product_id, "456")
        self.assertEqual(event.external_variant_id, "")
        self.assertEqual(event.buyer_email, "buyer@example.com")

    def test_normalize_ggsel_payload(self) -> None:
        event = normalize_sale(
            "ggsel",
            {
                "order_id": "ord-1",
                "offer_id": "offer-9",
                "button_id": "lite-button",
                "customer_email": "buyer@example.com",
            },
        )

        self.assertEqual(event.external_order_id, "ord-1")
        self.assertEqual(event.external_product_id, "offer-9")
        self.assertEqual(event.external_variant_id, "lite-button")

    def test_rejects_unsupported_marketplace(self) -> None:
        with self.assertRaises(ValueError):
            normalize_sale("shopify", {"order_id": "1", "product_id": "2"})


if __name__ == "__main__":
    unittest.main()
