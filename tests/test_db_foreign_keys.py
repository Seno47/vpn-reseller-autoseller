import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from reseller_autoseller.app import create_app
from reseller_autoseller.config import get_settings
from reseller_autoseller.db import Database, PRODUCT_MAPPINGS_SCHEMA, SCHEMA
from reseller_autoseller.marketplaces import SaleEvent


LEGACY_PRODUCT_MAPPINGS_SCHEMA = """
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
"""


def sale_event(order_id: str = "order-1") -> SaleEvent:
    return SaleEvent(
        marketplace="plati",
        external_order_id=order_id,
        external_product_id="product-1",
        external_variant_id="",
        buyer_email=None,
        buyer_name=None,
        amount=None,
        currency=None,
        raw_payload={"inv": order_id},
    )


class DatabaseForeignKeyMigrationTests(unittest.TestCase):
    def test_legacy_product_migration_keeps_child_foreign_keys_and_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(LEGACY_PRODUCT_MAPPINGS_SCHEMA)
                conn.executescript(SCHEMA)
                conn.execute(
                    """
                    INSERT INTO product_mappings
                        (id, marketplace, external_product_id, tariff_code, title,
                         enabled, delivery_template, created_at, updated_at)
                    VALUES (7, 'plati', 'product-1', 'lite_monthly', 'Legacy',
                            1, '', '2024-01-01', '2024-01-01')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sales
                        (id, marketplace, external_order_id, external_product_id,
                         raw_payload, created_at)
                    VALUES (11, 'plati', 'order-1', 'product-1', '{}', '2024-01-01')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO deliveries
                        (id, sale_id, product_mapping_id, xyranet_order_id,
                         subscription_url, panel_username, tariff_code, action,
                         delivery_text, raw_response, created_at)
                    VALUES (21, 11, 7, 'xyra-1', 'https://sub/1', 'user-1',
                            'lite_monthly', 'create', 'delivery', '{}', '2024-01-01')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO pending_operations
                        (id, sale_id, product_mapping_id, marketplace, external_order_id,
                         action, action_params, status, created_at, updated_at)
                    VALUES (31, 11, 7, 'plati', 'order-1', 'renew', '{}',
                            'waiting_order_id', '2024-01-01', '2024-01-01')
                    """
                )

            db = Database(path)
            db.init()

            with db.connect() as conn:
                mapping = conn.execute("SELECT * FROM product_mappings WHERE id=7").fetchone()
                delivery = conn.execute("SELECT * FROM deliveries WHERE id=21").fetchone()
                pending = conn.execute("SELECT * FROM pending_operations WHERE id=31").fetchone()
                delivery_targets = {
                    row["table"] for row in conn.execute("PRAGMA foreign_key_list(deliveries)")
                }
                pending_targets = {
                    row["table"] for row in conn.execute("PRAGMA foreign_key_list(pending_operations)")
                }

                self.assertEqual(mapping["external_variant_id"], "")
                self.assertEqual(mapping["action"], "create")
                self.assertEqual(delivery["product_mapping_id"], 7)
                self.assertEqual(pending["product_mapping_id"], 7)
                self.assertIn("product_mappings", delivery_targets)
                self.assertIn("product_mappings", pending_targets)
                self.assertNotIn("product_mappings_legacy", delivery_targets | pending_targets)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_init_repairs_affected_child_schemas_without_losing_data_or_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "affected.sqlite3"
            db = Database(path)
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "product-1",
                    "tariff_code": "lite_monthly",
                    "title": "Product",
                }
            )
            sale = db.create_sale(sale_event())
            delivery = db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "xyra-1",
                    "subscription_url": "https://sub/1",
                    "panel_username": "user-1",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "saved delivery",
                    "raw_response": {"ok": True},
                },
            )
            pending = db.create_pending_operation(
                sale_id=int(sale["id"]),
                product_id=int(product["id"]),
                marketplace="plati",
                external_order_id="order-1",
                action="renew",
                action_params={"days": 30},
            )

            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute("PRAGMA legacy_alter_table=OFF")
                conn.execute("ALTER TABLE deliveries ADD COLUMN audit_marker TEXT NOT NULL DEFAULT ''")
                conn.execute(
                    "UPDATE deliveries SET audit_marker='keep-me' WHERE id=?",
                    (delivery["id"],),
                )
                conn.execute("CREATE INDEX deliveries_audit_marker_idx ON deliveries(audit_marker)")
                conn.execute("ALTER TABLE product_mappings RENAME TO product_mappings_legacy")
                conn.executescript(PRODUCT_MAPPINGS_SCHEMA)
                conn.execute("INSERT INTO product_mappings SELECT * FROM product_mappings_legacy")
                conn.execute("DROP TABLE product_mappings_legacy")

            db.init()
            db.init()  # The repair must also be safe on every later startup.

            with db.connect() as conn:
                repaired_delivery = conn.execute(
                    "SELECT * FROM deliveries WHERE id=?", (delivery["id"],)
                ).fetchone()
                repaired_pending = conn.execute(
                    "SELECT * FROM pending_operations WHERE id=?", (pending["id"],)
                ).fetchone()
                delivery_targets = {
                    row["table"] for row in conn.execute("PRAGMA foreign_key_list(deliveries)")
                }
                pending_targets = {
                    row["table"] for row in conn.execute("PRAGMA foreign_key_list(pending_operations)")
                }
                indexes = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='deliveries'"
                    )
                }

                self.assertEqual(repaired_delivery["id"], delivery["id"])
                self.assertEqual(repaired_delivery["delivery_text"], "saved delivery")
                self.assertEqual(repaired_delivery["audit_marker"], "keep-me")
                self.assertEqual(repaired_pending["id"], pending["id"])
                self.assertEqual(repaired_pending["action"], "renew")
                self.assertIn("deliveries_sale_id_unique", indexes)
                self.assertIn("deliveries_audit_marker_idx", indexes)
                self.assertIn("product_mappings", delivery_targets)
                self.assertIn("product_mappings", pending_targets)
                self.assertNotIn("product_mappings_legacy", delivery_targets | pending_targets)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_connect_enables_foreign_keys_and_blocks_referenced_mapping_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "product-1",
                    "tariff_code": "lite_monthly",
                }
            )
            sale = db.create_sale(sale_event())
            db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "xyra-1",
                    "subscription_url": "https://sub/1",
                    "panel_username": "user-1",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "delivery",
                    "raw_response": {},
                },
            )

            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.delete_product(int(product["id"]))
            self.assertIsNotNone(db.get_product(int(product["id"])))


class ProductDeleteApiTests(unittest.TestCase):
    def test_referenced_mapping_delete_returns_conflict_and_preserves_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "api.sqlite3"
            env = {
                "DATABASE_PATH": str(database_path),
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": "strong-password",
                "ADMIN_IDS": "123456789",
                "ENABLE_TELEGRAM": "false",
                "TELEGRAM_BOT_TOKEN": "",
                "APP_BASE_URL": "https://panel.example",
                "DIGISELLER_API_KEY": "TEST_DIGISELLER_KEY",
            }
            with patch.dict(os.environ, env, clear=False):
                get_settings.cache_clear()
                client = TestClient(create_app())
                try:
                    login = client.post(
                        "/admin/api/login",
                        json={"username": "admin", "password": "strong-password"},
                    )
                    headers = {"Authorization": f"Bearer {login.json()['token']}"}
                    created = client.post(
                        "/admin/api/products",
                        headers=headers,
                        json={
                            "marketplace": "plati",
                            "external_product_id": "product-1",
                            "external_variant_id": "",
                            "action": "create",
                            "action_params": {},
                            "tariff_code": "lite_monthly",
                            "title": "Product",
                            "enabled": True,
                        },
                    ).json()
                    db = Database(database_path)
                    sale = db.create_sale(sale_event("api-order-1"))
                    delivery = db.create_delivery(
                        int(sale["id"]),
                        int(created["id"]),
                        {
                            "xyranet_order_id": "xyra-api-1",
                            "subscription_url": "https://sub/api-1",
                            "panel_username": "api-user",
                            "tariff_code": "lite_monthly",
                            "delivery_text": "delivery",
                            "raw_response": {},
                        },
                    )

                    response = client.delete(
                        f"/admin/api/products/{created['id']}",
                        headers=headers,
                    )

                    self.assertEqual(response.status_code, 409)
                    self.assertIn("referenced", response.json()["detail"].lower())
                    self.assertIsNotNone(db.get_product(int(created["id"])))
                    self.assertIsNotNone(db.get_delivery(int(delivery["id"])))
                finally:
                    client.close()
                    get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
