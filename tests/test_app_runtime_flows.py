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


class FakeRuntimeXyra:
    def __init__(self) -> None:
        self.create_calls: list[str] = []
        self.get_calls: list[str] = []
        self.renew_calls: list[str] = []
        self.reissue_calls: list[str] = []
        self.reissue_keys: list[str] = []

    async def tariffs(self):
        return [{"code": "lite_monthly", "api_price_rub": "100"}]

    async def create_order(self, tariff_code: str, *, idempotency_key: str):
        self.create_calls.append(idempotency_key)
        number = len(self.create_calls)
        return {
            "order": {
                "order_id": f"ord_created{number:05d}",
                "panel_username": "buyer",
                "subscription": {"subscription_url": f"https://sub/{number}", "tariff_code": tariff_code},
            }
        }

    async def get_order(self, order_id: str):
        self.get_calls.append(order_id)
        return {
            "order": {
                "order_id": order_id,
                "panel_username": "buyer",
                "subscription": {"subscription_url": "https://sub/status", "tariff_code": "lite_monthly"},
            }
        }

    async def renew_order(self, order_id: str, tariff_code: str | None = None, *, idempotency_key: str):
        self.renew_calls.append(order_id)
        return {
            "order": {
                "order_id": order_id,
                "subscription": {"subscription_url": "https://sub/renew", "tariff_code": tariff_code or "lite_monthly"},
            }
        }

    async def reissue_order(self, order_id: str, *, idempotency_key: str):
        self.reissue_calls.append(order_id)
        self.reissue_keys.append(idempotency_key)
        return {"order": {"order_id": order_id, "subscription": {"subscription_url": "https://sub/reissued"}}}


class FakeRuntimeDigiseller:
    def __init__(
        self,
        *,
        purchase: dict | None = None,
        mark_outcomes: list[object] | None = None,
        send_outcomes: list[object] | None = None,
    ) -> None:
        self.messages: list[tuple[str, str]] = []
        self.purchase = purchase or {}
        self.mark_outcomes = list(mark_outcomes or [])
        self.send_outcomes = list(send_outcomes or [])
        self.mark_calls: list[str] = []

    async def send_order_message(self, invoice_id: str, message: str):
        self.messages.append((invoice_id, message))
        outcome = self.send_outcomes.pop(0) if self.send_outcomes else {"retval": 0}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def purchase_by_unique_code(self, code: str):
        return dict(self.purchase)

    async def mark_unique_code_delivered(self, code: str):
        self.mark_calls.append(code)
        outcome = self.mark_outcomes.pop(0) if self.mark_outcomes else {"retval": 0}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRuntimeGgsel:
    def __init__(self, *, sales=None, details=None, messages=None) -> None:
        self.sales = list(sales or [])
        self.details = dict(details or {})
        self.messages_by_order = dict(messages or {})
        self.order_info_calls: list[str] = []
        self.sent_messages: list[tuple[str, str]] = []
        self.message_read = threading.Event()

    def configured_for_polling(self) -> bool:
        return True

    async def last_sales(self):
        return self.sales

    async def order_info(self, order_id: str):
        self.order_info_calls.append(order_id)
        value = self.details.get(order_id, {})
        if isinstance(value, Exception):
            raise value
        return value

    async def send_order_message(self, order_id: str, message: str):
        self.sent_messages.append((order_id, message))
        return {"ok": True}

    async def order_messages(self, order_id: str):
        self.message_read.set()
        return list(self.messages_by_order.get(order_id, []))


class AppRuntimeFlowTests(unittest.TestCase):
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
            "GGSEL_SELLER_ID": "",
            "GGSEL_API_KEY": "",
        }
        values.update(overrides)
        return values

    def test_free_commands_cannot_access_order_from_another_chat(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {"marketplace": "plati", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            sale = db.create_sale(
                SaleEvent(
                    "plati",
                    "296100001:ABCDEFGHIJKLMNOP",
                    "p1",
                    "",
                    None,
                    None,
                    None,
                    None,
                    {"inv": "296100001"},
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
                    "delivery_text": "secret victim delivery",
                    "raw_response": {},
                },
            )
            xyranet = FakeRuntimeXyra()
            digiseller = FakeRuntimeDigiseller()
            with (
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                attacker_status = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100002", "Message": "!status ord_victim12345", "MessageId": "m1"},
                )
                attacker_reissue = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100002", "Message": "!reissue ord_victim12345", "MessageId": "m2"},
                )
                self.assertEqual(attacker_status.status_code, 200)
                self.assertEqual(attacker_reissue.status_code, 200)
                self.assertEqual(xyranet.get_calls, [])
                self.assertEqual(xyranet.reissue_calls, [])
                self.assertTrue(all("подтвердить" in text for _, text in digiseller.messages[-2:]))

                owner_status = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100001", "Message": "!status ord_victim12345", "MessageId": "m3"},
                )
                owner_reissue = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100001", "Message": "!reissue ord_victim12345", "MessageId": "m4"},
                )
                self.assertEqual(owner_status.status_code, 200)
                self.assertEqual(owner_reissue.status_code, 200)
                self.assertEqual(xyranet.get_calls, ["ord_victim12345"])
                self.assertEqual(xyranet.reissue_calls, ["ord_victim12345"])

    def test_ggsel_cursor_stops_at_first_failed_sale(self) -> None:
        env = self.env(tmp="placeholder", GGSEL_SELLER_ID="seller", GGSEL_API_KEY="key")
        with TemporaryDirectory() as tmp:
            env["DATABASE_PATH"] = os.path.join(tmp, "test.sqlite3")
            with patch.dict(os.environ, env, clear=False):
                get_settings.cache_clear()
                self.addCleanup(get_settings.cache_clear)
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                db.set_chat_cursor("ggsel", "_last_sales", "o0")
                db.upsert_product({"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"})
                ggsel = FakeRuntimeGgsel(
                    sales=[{"invoice_id": "o3"}, {"invoice_id": "o2"}, {"invoice_id": "o1"}, {"invoice_id": "o0"}],
                    details={
                        "o1": {"invoice_id": "o1", "product_id": "p1"},
                        "o2": RuntimeError("temporary GGsel failure"),
                        "o3": {"invoice_id": "o3", "product_id": "p1"},
                    },
                )
                xyranet = FakeRuntimeXyra()
                with (
                    patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                    patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                    TestClient(create_app()),
                ):
                    deadline = time.time() + 3
                    while "o2" not in ggsel.order_info_calls and time.time() < deadline:
                        time.sleep(0.02)
                    time.sleep(0.05)

                self.assertEqual(ggsel.order_info_calls, ["o1", "o2", "o3"])
                self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "o1")
                self.assertEqual(len(xyranet.create_calls), 2)
                self.assertEqual(len(ggsel.sent_messages), 2)

    def test_free_reissue_message_failure_is_not_acknowledged_and_reuses_idempotency_key(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {"marketplace": "plati", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            sale = db.create_sale(
                SaleEvent("plati", "296100001:ABCDEFGHIJKLMNOP", "p1", "", None, None, None, None, {"inv": "296100001"})
            )
            db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "ord_owner12345",
                    "subscription_url": "https://sub/owner",
                    "panel_username": "owner",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "owner delivery",
                    "raw_response": {},
                },
            )
            xyranet = FakeRuntimeXyra()
            digiseller = FakeRuntimeDigiseller(
                send_outcomes=[RuntimeError("temporary chat failure"), {"retval": 0}]
            )
            with (
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app(), raise_server_exceptions=False) as client,
            ):
                payload = {"InvoiceId": "296100001", "Message": "!reissue ord_owner12345", "MessageId": "m1"}
                failed = client.post("/api/digiseller/notify/message/notification-secret", json=payload)
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(db.get_chat_cursor("plati", "296100001"), "")

                retried = client.post("/api/digiseller/notify/message/notification-secret", json=payload)
                self.assertEqual(retried.status_code, 200)
                self.assertEqual(db.get_chat_cursor("plati", "296100001"), "m1")

            self.assertEqual(xyranet.reissue_calls, ["ord_owner12345", "ord_owner12345"])
            self.assertEqual(len(set(xyranet.reissue_keys)), 1)

    def test_digiseller_mark_failure_does_not_report_delivery_failure_and_retries(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_product({"marketplace": "plati", "external_product_id": "p1", "tariff_code": "lite_monthly"})
            xyranet = FakeRuntimeXyra()
            digiseller = FakeRuntimeDigiseller(
                purchase={
                    "retval": 0,
                    "inv": "296100001",
                    "id_goods": "p1",
                    "amount": "299",
                    "type_curr": "RUR",
                    "unique_code_state": {"state": 1},
                },
                mark_outcomes=[RuntimeError("temporary mark failure"), {"retval": 0}],
            )
            with (
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                first = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100001", "Message": "ABCDEFGHIJKLMNOP", "MessageId": "m1"},
                )
                second = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100001", "Message": "ABCDEFGHIJKLMNOP", "MessageId": "m2"},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(len(xyranet.create_calls), 1)
            self.assertEqual(digiseller.mark_calls, ["ABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"])
            self.assertEqual(len(digiseller.messages), 1)
            self.assertNotIn("Не удалось проверить", digiseller.messages[0][1])
            external_order_id = "296100001:ABCDEFGHIJKLMNOP"
            events = db.list_order_events(marketplace="plati", external_order_id=external_order_id)
            self.assertTrue(any(item["event_type"] == "unique_code_mark_delivery_failed" for item in events))
            self.assertTrue(any(item["event_type"] == "unique_code_marked_delivered" for item in events))

    def test_manual_resend_marks_unsent_delivery_and_automatic_retry_does_not_duplicate(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {"marketplace": "plati", "external_product_id": "p1", "tariff_code": "lite_monthly"}
            )
            sale = db.create_sale(
                SaleEvent(
                    "plati",
                    "296100001:ABCDEFGHIJKLMNOP",
                    "p1",
                    "",
                    None,
                    None,
                    None,
                    None,
                    {"inv": "296100001"},
                )
            )
            db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "ord_owner12345",
                    "subscription_url": "https://sub/owner",
                    "panel_username": "owner",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "saved delivery",
                    "raw_response": {},
                },
            )
            digiseller = FakeRuntimeDigiseller(
                purchase={
                    "retval": 0,
                    "inv": "296100001",
                    "id_goods": "p1",
                    "unique_code_state": {"state": 2},
                }
            )
            with (
                patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=FakeRuntimeXyra()),
                patch("reseller_autoseller.app.RuntimeDigisellerClient", return_value=digiseller),
                TestClient(create_app()) as client,
            ):
                login = client.post(
                    "/admin/api/login",
                    json={"username": "admin", "password": "strong-password"},
                )
                headers = {"Authorization": f"Bearer {login.json()['token']}"}
                resent = client.post(f"/admin/api/sales/{sale['id']}/resend", headers=headers)
                self.assertEqual(resent.status_code, 200)
                self.assertEqual(db.get_sale_with_delivery_by_id(int(sale["id"]))["marketplace_message_status"], "sent")

                duplicate = client.post(
                    "/api/digiseller/notify/message/notification-secret",
                    json={"InvoiceId": "296100001", "Message": "ABCDEFGHIJKLMNOP", "MessageId": "m1"},
                )
                self.assertEqual(duplicate.status_code, 200)

            self.assertEqual(len(digiseller.messages), 1)

    def test_pending_poll_ignores_seller_messages(self) -> None:
        env = self.env(tmp="placeholder", GGSEL_SELLER_ID="seller", GGSEL_API_KEY="key")
        with TemporaryDirectory() as tmp:
            env["DATABASE_PATH"] = os.path.join(tmp, "test.sqlite3")
            with patch.dict(os.environ, env, clear=False):
                get_settings.cache_clear()
                self.addCleanup(get_settings.cache_clear)
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                product = db.upsert_product(
                    {
                        "marketplace": "ggsel",
                        "external_product_id": "p-renew",
                        "action": "renew",
                        "tariff_code": "lite_monthly",
                    }
                )
                sale = db.create_sale(SaleEvent("ggsel", "chat1", "p-renew", "", None, None, None, None, {}))
                pending = db.create_pending_operation(
                    sale_id=int(sale["id"]),
                    product_id=int(product["id"]),
                    marketplace="ggsel",
                    external_order_id="chat1",
                    action="renew",
                    action_params={},
                )
                ggsel = FakeRuntimeGgsel(
                    messages={"chat1": [{"id": "m1", "message": "!renew {ORDER_ID}", "is_seller": True}]}
                )
                xyranet = FakeRuntimeXyra()
                with (
                    patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                    patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                    TestClient(create_app()),
                ):
                    self.assertTrue(ggsel.message_read.wait(timeout=3))
                    time.sleep(0.05)

                refreshed = db.get_pending_operation(int(pending["id"]))
                self.assertEqual(refreshed["status"], "waiting_order_id")
                self.assertEqual(refreshed["last_message_id"], "m1")
                self.assertEqual(ggsel.sent_messages, [])
                self.assertEqual(xyranet.renew_calls, [])

    def test_ggsel_unmapped_gap_does_not_block_newer_delivery_or_advance_cursor(self) -> None:
        env = self.env(tmp="placeholder", GGSEL_SELLER_ID="seller", GGSEL_API_KEY="key")
        with TemporaryDirectory() as tmp:
            env["DATABASE_PATH"] = os.path.join(tmp, "test.sqlite3")
            with patch.dict(os.environ, env, clear=False):
                get_settings.cache_clear()
                self.addCleanup(get_settings.cache_clear)
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                db.set_chat_cursor("ggsel", "_last_sales", "o0")
                db.upsert_product({"marketplace": "ggsel", "external_product_id": "p1", "tariff_code": "lite_monthly"})
                ggsel = FakeRuntimeGgsel(
                    sales=[{"invoice_id": "o2"}, {"invoice_id": "o1"}, {"invoice_id": "o0"}],
                    details={
                        "o1": {"invoice_id": "o1", "product_id": "missing-product"},
                        "o2": {"invoice_id": "o2", "product_id": "p1"},
                    },
                )
                xyranet = FakeRuntimeXyra()
                with (
                    patch("reseller_autoseller.app.RuntimeGgselClient", return_value=ggsel),
                    patch("reseller_autoseller.app.RuntimeXyraNetClient", return_value=xyranet),
                    TestClient(create_app()),
                ):
                    deadline = time.time() + 3
                    while len(xyranet.create_calls) < 1 and time.time() < deadline:
                        time.sleep(0.02)

                self.assertEqual(ggsel.order_info_calls, ["o1", "o2"])
                self.assertEqual(db.get_chat_cursor("ggsel", "_last_sales"), "o0")
                self.assertEqual(len(xyranet.create_calls), 1)
                skipped = db.list_order_events(marketplace="ggsel", external_order_id="o1")
                self.assertTrue(any(item["event_type"] == "polling_sale_deferred" for item in skipped))

    def test_login_rate_limit_cannot_be_bypassed_with_unique_usernames(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            get_settings.cache_clear()
            self.addCleanup(get_settings.cache_clear)
            with TestClient(create_app()) as client:
                for number in range(8):
                    response = client.post(
                        "/admin/api/login",
                        json={"username": f"attacker-{number}", "password": "wrong-password"},
                    )
                    self.assertEqual(response.status_code, 401)
                blocked = client.post(
                    "/admin/api/login",
                    json={"username": "another-username", "password": "wrong-password"},
                )
                self.assertEqual(blocked.status_code, 429)


if __name__ == "__main__":
    unittest.main()
