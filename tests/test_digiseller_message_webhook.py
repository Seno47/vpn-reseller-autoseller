import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from reseller_autoseller.app import create_app
from reseller_autoseller.config import Settings, get_settings
from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent


class RecordingDigiseller:
    def __init__(self) -> None:
        self.purchase_codes: list[str] = []
        self.sent_messages: list[tuple[str, str]] = []
        self.order_message_reads: list[str] = []
        self.order_chat_list_reads = 0
        self.seller_sales_reads = 0
        self.unread_chat_listed = threading.Event()

    async def purchase_by_unique_code(self, code: str):
        self.purchase_codes.append(code)
        return {}

    async def mark_unique_code_delivered(self, code: str):
        return {"retval": 0}

    async def send_order_message(self, invoice_id: str, message: str):
        self.sent_messages.append((invoice_id, message))
        return {"retval": 0}

    async def order_chats(self, *, filter_new: bool = True, page: int = 1, rows: int = 100):
        self.order_chat_list_reads += 1
        self.unread_chat_listed.set()
        return []

    async def order_messages(
        self,
        invoice_id: str,
        *,
        count: int = 100,
        newer: bool = False,
        old_id: str = "",
    ):
        self.order_message_reads.append(invoice_id)
        return []

    async def mark_order_messages_seen(self, invoice_id: str):
        return {"retval": 0}

    async def seller_sales(self, **kwargs):
        self.seller_sales_reads += 1
        return []

    async def purchase_info(self, invoice_id: str):
        return {}


class DigisellerMessageWebhookTests(unittest.TestCase):
    def env(self, tmp: str, **overrides: str) -> dict[str, str]:
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
            "DIGISELLER_POLLING_FALLBACK_ENABLED": "false",
            "GGSEL_SELLER_ID": "",
            "GGSEL_API_KEY": "",
        }
        values.update(overrides)
        return values

    def test_administration_message_without_invoice_is_acknowledged(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            digiseller = RecordingDigiseller()

            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                response = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={
                        "id": 11223344,
                        "id_to": 955297,
                        "id_from": 0,
                        "date": "2026-07-11 17:13:00",
                        "text": "Digiseller service notification",
                        "is_read": False,
                        "shop_id": 0,
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ignored")
            self.assertIn("no buyer invoice", response.json()["reason"])
            self.assertEqual(db.count_chat_messages("plati", ""), 0)
            self.assertEqual(digiseller.purchase_codes, [])
            self.assertEqual(digiseller.order_message_reads, [])

    def test_invoice_message_is_stored_in_full_and_duplicate_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            digiseller = RecordingDigiseller()
            full_text = "Buyer message: " + ("0123456789" * 80)
            payload = {
                "InvoiceId": "296211150",
                "Message": full_text,
                "MessageId": "56627081",
                "MessageDate": "2026-07-11 17:14:00",
                "buyer": 1,
                "seller": 0,
            }

            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                first = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json=payload,
                )
                duplicate = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={**payload, "Message": "must not replace the first payload"},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["status"], "ok")
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["status"], "ignored")
            self.assertEqual(duplicate.json()["reason"], "duplicate message notification")
            self.assertEqual(db.count_chat_messages("plati", "296211150"), 1)
            stored = db.list_chat_messages("plati", "296211150")[0]
            self.assertEqual(stored["external_message_id"], "56627081")
            self.assertEqual(stored["role"], "buyer")
            self.assertEqual(stored["text"], full_text)
            self.assertEqual(stored["source"], "digiseller_webhook")
            self.assertEqual(stored["message_date"], "2026-07-11 17:14:00")
            self.assertEqual(digiseller.order_message_reads, [])

    def test_seller_and_system_messages_are_stored_but_never_run_buyer_flow(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            digiseller = RecordingDigiseller()

            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                seller = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={
                        "InvoiceId": "296211150",
                        "Message": "ABCDEFGHIJKLMNOP",
                        "MessageId": "seller-1",
                        "Seller": 1,
                        "Buyer": 0,
                    },
                )
                system = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={
                        "InvoiceId": "296211150",
                        "Message": "QRSTUVWXYZABCDEF",
                        "MessageId": "system-1",
                        "seller": 0,
                        "buyer": 0,
                    },
                )

            self.assertEqual(seller.status_code, 200)
            self.assertEqual(seller.json()["role"], "seller")
            self.assertEqual(seller.json()["handled"], False)
            self.assertEqual(system.status_code, 200)
            self.assertEqual(system.json()["role"], "system")
            self.assertEqual(system.json()["handled"], False)
            self.assertEqual(digiseller.purchase_codes, [])
            self.assertEqual(digiseller.sent_messages, [])
            self.assertEqual(
                [row["role"] for row in db.list_chat_messages("plati", "296211150")],
                ["seller", "system"],
            )

    def test_invoice_only_notification_reads_that_chat_once_and_deduplicates_retry(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            digiseller = RecordingDigiseller()

            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                first = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296211150", "MessageId": "event-without-body"},
                )
                duplicate = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296211150", "MessageId": "event-without-body"},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["action"], "chat_synced")
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["status"], "ignored")
            self.assertEqual(digiseller.order_message_reads, ["296211150"])

    def test_invoice_only_notification_returns_retryable_error_when_sync_fails(self) -> None:
        class FailingDigiseller(RecordingDigiseller):
            async def order_messages(self, invoice_id: str, **kwargs):
                self.order_message_reads.append(invoice_id)
                raise RuntimeError("temporary DigiSeller read failure")

        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            digiseller = FailingDigiseller()

            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                response = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296211150", "MessageId": "event-without-body"},
                )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(digiseller.order_message_reads, ["296211150"])

    def test_polling_fallback_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertFalse(settings.digiseller_polling_fallback_enabled)
        self.assertEqual(settings.digiseller_unique_code_request_delay_minutes, 0)

    def test_fallback_reads_unread_chat_list_once_not_every_pending_plati_chat(self) -> None:
        pending_loop_completed = threading.Event()
        original_list_pending = Database.list_pending_operations

        def observed_pending_operations(
            database: Database,
            status: str | None = "waiting_order_id",
        ):
            rows = original_list_pending(database, status)

            def iterator():
                yield from rows
                pending_loop_completed.set()

            return iterator()

        with TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            self.env(
                tmp,
                DIGISELLER_SELLER_ID="955297",
                DIGISELLER_API_KEY="test-key",
                DIGISELLER_POLLING_FALLBACK_ENABLED="true",
            ),
            clear=False,
        ):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "tariff_code": "lite_monthly",
                }
            )
            for number in range(3):
                invoice_id = f"29621115{number}"
                sale = db.create_sale(
                    SaleEvent(
                        "plati",
                        invoice_id,
                        "5968452",
                        "",
                        None,
                        None,
                        None,
                        None,
                        {"invoice_id": invoice_id},
                    )
                )
                db.create_pending_operation(
                    sale_id=int(sale["id"]),
                    product_id=int(product["id"]),
                    marketplace="plati",
                    external_order_id=invoice_id,
                    action="renew",
                    action_params={},
                )

            digiseller = RecordingDigiseller()
            with (
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                patch.object(Database, "list_pending_operations", new=observed_pending_operations),
                TestClient(create_app()),
            ):
                self.assertTrue(pending_loop_completed.wait(timeout=2))

            self.assertEqual(digiseller.order_chat_list_reads, 1)
            self.assertEqual(digiseller.order_message_reads, [])


if __name__ == "__main__":
    unittest.main()
