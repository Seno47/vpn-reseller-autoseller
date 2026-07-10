import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent


class ProductMappingDbTests(unittest.TestCase):
    @staticmethod
    def _create_product_sale_and_delivery(db: Database) -> tuple[dict, dict, dict]:
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
        delivery = db.create_delivery(
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
        return product, sale, delivery

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

    def test_connections_enforce_foreign_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            _, sale, _ = self._create_product_sale_and_delivery(db)

            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO deliveries
                            (sale_id, product_mapping_id, xyranet_order_id, subscription_url,
                             panel_username, tariff_code, delivery_text, raw_response, created_at)
                        VALUES (?, ?, 'xyra-invalid', 'https://x.example/invalid', 'invalid',
                                'lite_monthly', 'invalid', '{}', '2026-01-01T00:00:00+00:00')
                        """,
                        (sale["id"], 999999),
                    )

    def test_referenced_product_mapping_cannot_be_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product, _, _ = self._create_product_sale_and_delivery(db)

            with self.assertRaises(sqlite3.IntegrityError):
                db.delete_product(product["id"])

            self.assertEqual(db.get_product(product["id"])["id"], product["id"])

    def test_old_product_schema_migration_keeps_child_foreign_keys_valid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE product_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    marketplace TEXT NOT NULL,
                    external_product_id TEXT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    delivery_template TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (marketplace, external_product_id)
                );
                CREATE TABLE sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    marketplace TEXT NOT NULL,
                    external_order_id TEXT NOT NULL,
                    external_product_id TEXT NOT NULL,
                    buyer_email TEXT,
                    buyer_name TEXT,
                    amount TEXT,
                    currency TEXT,
                    raw_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (marketplace, external_order_id)
                );
                CREATE TABLE deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL,
                    product_mapping_id INTEGER NOT NULL,
                    xyranet_order_id TEXT NOT NULL,
                    subscription_url TEXT NOT NULL,
                    panel_username TEXT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    delivery_text TEXT NOT NULL,
                    raw_response TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (sale_id) REFERENCES sales(id),
                    FOREIGN KEY (product_mapping_id) REFERENCES product_mappings(id)
                );
                INSERT INTO product_mappings
                    (id, marketplace, external_product_id, tariff_code, title, enabled,
                     delivery_template, created_at, updated_at)
                VALUES (7, 'plati', '5968452', 'lite_monthly', 'Lite', 1, '',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO sales
                    (id, marketplace, external_order_id, external_product_id, raw_payload, created_at)
                VALUES (8, 'plati', '12345', '5968452', '{}', '2026-01-01T00:00:00+00:00');
                INSERT INTO deliveries
                    (id, sale_id, product_mapping_id, xyranet_order_id, subscription_url,
                     panel_username, tariff_code, delivery_text, raw_response, created_at)
                VALUES (9, 8, 7, 'xyra-1', 'https://x.example/1', 'user1', 'lite_monthly',
                        'first', '{}', '2026-01-01T00:00:00+00:00');
                """
            )
            conn.close()

            db = Database(path)
            db.init()

            with db.connect() as migrated:
                targets = {
                    row["table"]
                    for row in migrated.execute("PRAGMA foreign_key_list(deliveries)").fetchall()
                }
                self.assertIn("product_mappings", targets)
                self.assertNotIn("product_mappings_legacy", targets)
                self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                delivery = migrated.execute("SELECT * FROM deliveries WHERE id=9").fetchone()
                self.assertEqual(delivery["product_mapping_id"], 7)
                self.assertEqual(delivery["xyranet_order_id"], "xyra-1")

    def test_init_repairs_existing_legacy_parent_reference_without_row_loss(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db = Database(path)
            db.init()
            _, _, delivery = self._create_product_sale_and_delivery(db)

            conn = sqlite3.connect(path)
            original_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'"
            ).fetchone()[0]
            broken_sql = original_sql.replace(
                "CREATE TABLE deliveries",
                "CREATE TABLE deliveries_broken",
                1,
            ).replace(
                "REFERENCES product_mappings(id)",
                "REFERENCES product_mappings_legacy(id)",
                1,
            )
            self.assertIn("product_mappings_legacy", broken_sql)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(deliveries)")]
            column_list = ", ".join(f'"{name}"' for name in columns)
            conn.execute(broken_sql)
            conn.execute(
                f"INSERT INTO deliveries_broken ({column_list}) SELECT {column_list} FROM deliveries"
            )
            conn.execute("DROP TABLE deliveries")
            conn.execute("ALTER TABLE deliveries_broken RENAME TO deliveries")
            conn.execute("CREATE UNIQUE INDEX deliveries_sale_id_unique ON deliveries (sale_id)")
            conn.commit()
            conn.close()

            db.init()

            with db.connect() as repaired:
                targets = {
                    row["table"]
                    for row in repaired.execute("PRAGMA foreign_key_list(deliveries)").fetchall()
                }
                self.assertIn("product_mappings", targets)
                self.assertNotIn("product_mappings_legacy", targets)
                self.assertEqual(repaired.execute("PRAGMA foreign_key_check").fetchall(), [])
                restored = repaired.execute(
                    "SELECT * FROM deliveries WHERE id=?", (delivery["id"],)
                ).fetchone()
                self.assertEqual(dict(restored), delivery)

    def test_plati_lookup_supports_legacy_digiseller_mapping(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            legacy = db.upsert_product(
                {
                    "marketplace": "digiseller",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "legacy_monthly",
                    "title": "Legacy",
                }
            )

            found = db.get_product_by_external("plati", "5968452", "23468281")

            self.assertEqual(found["id"], legacy["id"])

    def test_plati_lookup_prefers_canonical_mapping_over_legacy_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product(
                {
                    "marketplace": "digiseller",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "legacy_monthly",
                    "title": "Legacy",
                }
            )
            canonical = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "tariff_code": "canonical_monthly",
                    "title": "Canonical",
                }
            )

            found = db.get_product_by_external("digiseller", "5968452", "23468281")

            self.assertEqual(found["id"], canonical["id"])

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

    def test_sale_claim_heartbeat_preserves_ownership(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            sale = db.create_sale(
                SaleEvent(
                    marketplace="ggsel",
                    external_order_id="o1",
                    external_product_id="p1",
                    external_variant_id="",
                    buyer_email=None,
                    buyer_name=None,
                    amount=None,
                    currency=None,
                    raw_payload={},
                )
            )
            token = db.claim_sale_processing(int(sale["id"]))
            self.assertTrue(token)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE sales SET processing_started_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (sale["id"],),
                )

            self.assertTrue(db.refresh_sale_processing(int(sale["id"]), str(token)))
            self.assertIsNone(db.claim_sale_processing(int(sale["id"]), stale_after_seconds=60))
            self.assertFalse(db.refresh_sale_processing(int(sale["id"]), "wrong-token"))

            db.release_sale_processing(int(sale["id"]), "wrong-token")
            with db.connect() as conn:
                owner = conn.execute("SELECT processing_token FROM sales WHERE id=?", (sale["id"],)).fetchone()[0]
            self.assertEqual(owner, token)
            db.release_sale_processing(int(sale["id"]), str(token))

    def test_paid_non_create_delivery_does_not_grant_free_chat_ownership(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "p-renew",
                    "action": "renew",
                    "tariff_code": "lite_monthly",
                }
            )
            sale = db.create_sale(
                SaleEvent(
                    "plati",
                    "296100002:ABCDEFGHIJKLMNOP",
                    "p-renew",
                    "",
                    None,
                    None,
                    None,
                    None,
                    {"inv": "296100002"},
                )
            )
            db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "ord_victim12345",
                    "subscription_url": "https://sub/victim",
                    "panel_username": "victim",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "renewed",
                    "raw_response": {},
                },
            )

            self.assertFalse(db.marketplace_chat_owns_order("plati", "296100002", "ord_victim12345"))


if __name__ == "__main__":
    unittest.main()
