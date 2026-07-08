import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent


class ProductMappingDbTests(unittest.TestCase):
    def test_deletes_product_mapping(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            row = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                }
            )

            self.assertTrue(db.delete_product(row["id"]))
            self.assertFalse(db.delete_product(row["id"]))
            self.assertEqual(db.list_products(), [])

    def test_updates_product_mapping_by_id(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            row = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                }
            )

            updated = db.update_product(
                row["id"],
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "premium_monthly",
                    "title": "Premium",
                    "enabled": True,
                },
            )

            self.assertIsNotNone(updated)
            self.assertEqual(updated["id"], row["id"])
            self.assertEqual(updated["tariff_code"], "premium_monthly")
            self.assertEqual(updated["title"], "Premium")

    def test_update_rejects_duplicate_mapping_key(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            first = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                }
            )
            second = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23469997",
                    "tariff_code": "premium_monthly",
                    "title": "Premium",
                }
            )

            with self.assertRaises(sqlite3.IntegrityError):
                db.update_product(
                    second["id"],
                    {
                        "marketplace": "plati",
                        "external_product_id": "5968452",
                        "external_variant_id": first["external_variant_id"],
                        "tariff_code": "premium_monthly",
                        "title": "Duplicate",
                        "enabled": True,
                    },
                )

    def test_delivery_is_unique_per_sale(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                }
            )
            sale = db.create_sale(
                SaleEvent(
                    marketplace="plati",
                    external_order_id="12345",
                    external_product_id="5968452",
                    external_variant_id="23468281",
                    buyer_email=None,
                    buyer_name=None,
                    amount=None,
                    currency=None,
                    raw_payload={"inv": "12345"},
                )
            )

            first = db.create_delivery(
                sale["id"],
                product["id"],
                {
                    "xyranet_order_id": "xyra-1",
                    "subscription_url": "https://x.example/1",
                    "panel_username": "user1",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "first",
                    "raw_response": {"ok": 1},
                },
            )
            second = db.create_delivery(
                sale["id"],
                product["id"],
                {
                    "xyranet_order_id": "xyra-2",
                    "subscription_url": "https://x.example/2",
                    "panel_username": "user2",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "second",
                    "raw_response": {"ok": 2},
                },
            )

            self.assertEqual(second["id"], first["id"])
            self.assertEqual(second["xyranet_order_id"], "xyra-1")
            with db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM deliveries WHERE sale_id=?", (sale["id"],)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_order_events_are_persistent(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()

            db.add_order_event(
                marketplace="plati",
                external_order_id="o1",
                event_type="sale_received",
                status="info",
                payload={"code": "abc"},
            )

            rows = db.list_order_events(marketplace="plati", external_order_id="o1")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_type"], "sale_received")
            self.assertIn('"code": "abc"', rows[0]["payload"])

    def test_chat_cursors_can_be_listed_by_marketplace(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()

            db.set_chat_cursor("plati", "296135343", "240824924")
            db.set_chat_cursor("ggsel", "gg-1", "10")

            rows = db.list_chat_cursors("plati")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["marketplace"], "plati")
            self.assertEqual(rows[0]["external_order_id"], "296135343")
            self.assertEqual(rows[0]["last_message_id"], "240824924")


if __name__ == "__main__":
    unittest.main()
