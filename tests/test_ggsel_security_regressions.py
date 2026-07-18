import asyncio
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from reseller_autoseller.app import create_app
from reseller_autoseller.config import get_settings
from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent
from reseller_autoseller.services import DeliveryService
from tests.test_app_runtime_flows import FakeRuntimeGgsel, FakeRuntimeXyra


class GgselSecurityRegressionTests(unittest.TestCase):
    @staticmethod
    def env(tmp: str, **overrides: str) -> dict[str, str]:
        values = {
            "DATABASE_PATH": os.path.join(tmp, "test.sqlite3"),
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "strong-password",
            "ADMIN_IDS": "[]",
            "ENABLE_TELEGRAM": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "DIGISELLER_SELLER_ID": "",
            "DIGISELLER_API_KEY": "",
            "DIGISELLER_NOTIFICATION_SECRET": "notification-secret",
            "DIGISELLER_UNIQUE_CODE_REQUEST_DELAY_MINUTES": "0",
            "GGSEL_SELLER_ID": "seller",
            "GGSEL_API_KEY": "key",
            "GGSEL_NOTIFICATION_SECRET": "ggsel-notification-secret",
            "GGSEL_SALES_POLLING_FALLBACK_INTERVAL_SECONDS": "3600",
        }
        values.update(overrides)
        return values

    @staticmethod
    def seed_cross_chat_pending(db: Database) -> tuple[dict, dict, SaleEvent]:
        create_product = db.upsert_product(
            {
                "marketplace": "ggsel",
                "external_product_id": "p-create",
                "action": "create",
                "tariff_code": "lite_monthly",
            }
        )
        victim_sale = db.create_sale(
            SaleEvent("ggsel", "victim-chat", "p-create", "", None, None, None, None, {})
        )
        db.create_delivery(
            int(victim_sale["id"]),
            int(create_product["id"]),
            {
                "xyranet_order_id": "ord_victim12345",
                "subscription_url": "https://sub/victim-secret",
                "panel_username": "victim",
                "tariff_code": "lite_monthly",
                "action": "create",
                "delivery_text": "victim secret delivery: https://sub/victim-secret",
                "raw_response": {},
            },
        )

        reissue_product = db.upsert_product(
            {
                "marketplace": "ggsel",
                "external_product_id": "p-reissue",
                "action": "reissue",
                "tariff_code": "lite_monthly",
            }
        )
        attacker_event = SaleEvent(
            "ggsel",
            "attacker-chat",
            "p-reissue",
            "",
            None,
            None,
            None,
            None,
            {},
        )
        attacker_sale = db.create_sale(attacker_event)
        pending = db.create_pending_operation(
            sale_id=int(attacker_sale["id"]),
            product_id=int(reissue_product["id"]),
            marketplace="ggsel",
            external_order_id="attacker-chat",
            action="reissue",
            action_params={},
        )
        return victim_sale, pending, attacker_event

    def test_ggsel_pending_reissue_cannot_target_create_delivery_from_another_chat(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            _, pending, _ = self.seed_cross_chat_pending(db)
            ggsel = FakeRuntimeGgsel(
                sales=[],
                unread_chats=[{"id_i": "attacker-chat", "cnt_new": 1}],
                unread_once=True,
                messages={
                    "attacker-chat": [
                        {
                            "id": "m-attacker",
                            "message": "!reissue ord_victim12345",
                            "buyer": True,
                        }
                    ]
                },
            )
            xyranet = FakeRuntimeXyra()

            with (
                patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                patch("reseller_autoseller.app.MARKETPLACE_POLL_LOOP_SECONDS", 0.05),
                patch("reseller_autoseller.app.GGSEL_CHAT_POLL_INTERVAL_SECONDS", 0.05),
                TestClient(create_app()),
            ):
                deadline = time.time() + 3
                while db.get_chat_cursor("ggsel", "attacker-chat") != "m-attacker" and time.time() < deadline:
                    time.sleep(0.02)

            self.assertEqual(db.get_chat_cursor("ggsel", "attacker-chat"), "m-attacker")
            self.assertEqual(xyranet.reissue_calls, [])
            self.assertNotIn("https://", "\n".join(text for _, text in ggsel.sent_messages))
            attacker_sale = db.get_sale_with_delivery("ggsel", "attacker-chat")
            self.assertFalse(attacker_sale.get("delivery_id"))
            self.assertNotEqual(db.get_pending_operation(int(pending["id"]))["status"], "completed")
            events = db.list_order_events(marketplace="ggsel", external_order_id="attacker-chat")
            self.assertTrue(any(event["event_type"] == "pending_order_ownership_rejected" for event in events))

    def test_delivery_service_rejects_cross_chat_pending_target_before_xyranet(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                _, pending, _ = self.seed_cross_chat_pending(db)
                xyranet = FakeRuntimeXyra()
                service = DeliveryService(db=db, xyranet=xyranet)

                with self.assertRaises(ValueError):
                    await service.complete_pending_operation(pending, "ord_victim12345")

                self.assertEqual(xyranet.reissue_calls, [])
                attacker_sale = db.get_sale_with_delivery("ggsel", "attacker-chat")
                self.assertFalse(attacker_sale.get("delivery_id"))

        asyncio.run(scenario())

    def test_delivery_service_rejects_corrupted_completed_pending_without_delivery(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                reissue_product = db.upsert_product(
                    {
                        "marketplace": "ggsel",
                        "external_product_id": "p-reissue",
                        "action": "reissue",
                        "tariff_code": "lite_monthly",
                    }
                )
                event = SaleEvent(
                    "ggsel",
                    "corrupted-chat",
                    "p-reissue",
                    "",
                    None,
                    None,
                    None,
                    None,
                    {},
                )
                sale = db.create_sale(event)
                pending = db.create_pending_operation(
                    sale_id=int(sale["id"]),
                    product_id=int(reissue_product["id"]),
                    marketplace="ggsel",
                    external_order_id="corrupted-chat",
                    action="reissue",
                    action_params={},
                )
                with db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE pending_operations
                        SET status='completed',
                            target_order_id='ord_victim12345',
                            result_text='forged delivery: https://sub/victim-secret',
                            raw_response='{}'
                        WHERE id=?
                        """,
                        (pending["id"],),
                    )
                xyranet = FakeRuntimeXyra()
                service = DeliveryService(db=db, xyranet=xyranet)

                with self.assertRaises(ValueError):
                    await service.handle_sale(event, notify_marketplace=False)

                self.assertEqual(xyranet.reissue_calls, [])
                self.assertFalse(db.get_sale_with_delivery("ggsel", "corrupted-chat").get("delivery_id"))

        asyncio.run(scenario())

    def test_first_ggsel_poll_watermarks_historical_sales_without_delivery(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product(
                {"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            ggsel = FakeRuntimeGgsel(
                sales=[{"invoice_id": "old-2"}, {"invoice_id": "old-1"}],
                details={
                    "old-1": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                    "old-2": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                },
            )
            xyranet = FakeRuntimeXyra()

            with (
                patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                patch("reseller_autoseller.app.MARKETPLACE_POLL_LOOP_SECONDS", 0.05),
                TestClient(create_app()),
            ):
                deadline = time.time() + 3
                while db.get_chat_cursor("ggsel", "_last_sales") != "old-2" and time.time() < deadline:
                    time.sleep(0.02)

            self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "old-2")
            self.assertEqual(ggsel.order_info_calls, [])
            self.assertEqual(xyranet.create_calls, [])
            self.assertEqual(ggsel.sent_messages, [])
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-1"))
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-2"))

    def test_callback_before_first_poll_cannot_fulfill_historical_sale(self) -> None:
        class FirstPollGateGgsel(FakeRuntimeGgsel):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self.first_call_started = threading.Event()
                self.release_first_call = threading.Event()

            async def last_sales(self):
                self.last_sales_calls += 1
                self.sales_read.set()
                if self.last_sales_calls == 1:
                    self.first_call_started.set()
                    while not self.release_first_call.is_set():
                        await asyncio.sleep(0.01)
                return self.sales

        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product(
                {"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            ggsel = FirstPollGateGgsel(
                sales=[{"invoice_id": "old-2"}, {"invoice_id": "old-1"}],
                details={
                    "old-1": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                    "old-2": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                },
            )
            xyranet = FakeRuntimeXyra()

            with (
                patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                TestClient(create_app()) as client,
            ):
                self.assertTrue(ggsel.first_call_started.wait(timeout=3))
                response = client.post(
                    "/api/ggsel/notify/order/ggsel-notification-secret",
                    json={"id_i": "old-1"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "accepted")
                ggsel.release_first_call.set()

                deadline = time.time() + 3
                while ggsel.last_sales_calls < 2 and time.time() < deadline:
                    time.sleep(0.02)
                while db.get_chat_cursor("ggsel", "_last_sales") != "old-2" and time.time() < deadline:
                    time.sleep(0.02)

            self.assertGreaterEqual(ggsel.last_sales_calls, 2)
            self.assertEqual(db.get_setting("_ggsel_sales_cursor_initialized"), "1")
            self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "old-2")
            self.assertEqual(ggsel.order_info_calls, [])
            self.assertEqual(xyranet.create_calls, [])
            self.assertEqual(ggsel.sent_messages, [])
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-1"))
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-2"))

    def test_delayed_callback_for_historical_sale_is_safe_after_both_retries(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product(
                {"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            ggsel = FakeRuntimeGgsel(
                sales=[{"invoice_id": "old-2"}, {"invoice_id": "old-1"}],
                details={
                    "old-1": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                    "old-2": {"content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"}},
                },
            )
            xyranet = FakeRuntimeXyra()

            with (
                patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                TestClient(create_app()) as client,
            ):
                deadline = time.time() + 3
                while db.get_chat_cursor("ggsel", "_last_sales") != "old-2" and time.time() < deadline:
                    time.sleep(0.02)
                self.assertEqual(db.get_setting("_ggsel_sales_cursor_initialized"), "1")
                self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "old-2")
                calls_after_bootstrap = ggsel.last_sales_calls

                response = client.post(
                    "/api/ggsel/notify/order/ggsel-notification-secret",
                    json={"id_i": "old-1"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "accepted")
                deadline = time.time() + 6
                while ggsel.last_sales_calls < calls_after_bootstrap + 2 and time.time() < deadline:
                    time.sleep(0.02)

                self.assertGreaterEqual(ggsel.last_sales_calls, calls_after_bootstrap + 2)

            self.assertEqual(db.get_setting("_ggsel_sales_cursor_initialized"), "1")
            self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "old-2")
            self.assertEqual(ggsel.order_info_calls, [])
            self.assertEqual(xyranet.create_calls, [])
            self.assertEqual(ggsel.sent_messages, [])
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-1"))
            self.assertIsNone(db.get_sale_with_delivery("ggsel", "old-2"))

    def test_empty_ggsel_bootstrap_then_first_new_sale_is_delivered(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product(
                {"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            ggsel = FakeRuntimeGgsel(sales=[])
            xyranet = FakeRuntimeXyra()

            with (
                patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                TestClient(create_app()) as client,
            ):
                deadline = time.time() + 3
                while db.get_setting("_ggsel_sales_cursor_initialized") != "1" and time.time() < deadline:
                    time.sleep(0.02)
                self.assertEqual(db.get_setting("_ggsel_sales_cursor_initialized"), "1")
                self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "")

                ggsel.sales = [{"invoice_id": "new-1", "product": {"id": "p1"}}]
                ggsel.details["new-1"] = {
                    "retval": 0,
                    "content": {"invoice_state": 3, "owner": "seller", "item_id": "p1"},
                }
                response = client.post(
                    "/api/ggsel/notify/order/ggsel-notification-secret",
                    json={"id_i": "new-1"},
                )
                self.assertEqual(response.status_code, 200)
                deadline = time.time() + 3
                while not xyranet.create_calls and time.time() < deadline:
                    time.sleep(0.02)

            self.assertEqual(len(xyranet.create_calls), 1)
            self.assertEqual(len(ggsel.sent_messages), 1)
            self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "new-1")
            delivered = db.get_sale_with_delivery("ggsel", "new-1")
            self.assertTrue(delivered.get("delivery_id"))


if __name__ == "__main__":
    unittest.main()
