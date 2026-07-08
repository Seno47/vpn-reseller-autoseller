import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent
from reseller_autoseller.services import (
    TEMPLATE_GROUPS,
    DeliveryService,
    extract_order_id_from_text,
    parse_chat_command,
    render_template,
    sale_quantity,
)
from reseller_autoseller.statistics import build_sales_statistics


class FakeXyraClient:
    def __init__(self) -> None:
        self.calls = 0
        self.create_calls = []
        self.renew_calls = []

    async def create_order(self, tariff_code: str, *, idempotency_key: str):
        self.calls += 1
        self.create_calls.append((tariff_code, idempotency_key))
        return {
            "order": {
                "order_id": f"xyra-{self.calls}",
                "panel_username": "panel_user",
                "subscription": {
                    "subscription_url": "https://x.example/sub",
                    "tariff_code": tariff_code,
                    "expire_at": "2026-08-01T00:00:00Z",
                },
            }
        }

    async def renew_order(self, order_id: str, tariff_code: str | None = None, *, idempotency_key: str):
        self.calls += 1
        self.renew_calls.append((order_id, tariff_code, idempotency_key))
        return {
            "order": {
                "order_id": order_id,
                "panel_username": "panel_user",
                "subscription": {
                    "subscription_url": "https://x.example/sub-renewed",
                    "tariff_code": tariff_code or "lite_monthly",
                    "expire_at": "2026-09-01T00:00:00Z",
                },
            }
        }

    async def get_order(self, order_id: str):
        self.calls += 1
        return {
            "order": {
                "order_id": order_id,
                "panel_username": "panel_user",
                "subscription": {
                    "subscription_url": "https://x.example/sub-status",
                    "tariff_code": "lite_monthly",
                    "expire_at": "2026-10-01T00:00:00Z",
                    "device_limit": 2,
                    "lte_quota": 10,
                },
            }
        }

    async def tariffs(self):
        return [{"code": "lite_monthly", "api_price_rub": "113"}]


class FakeMessenger:
    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, marketplace: str, external_order_id: str, text: str) -> None:
        self.messages.append((marketplace, external_order_id, text))


class DeliveryServiceTests(unittest.TestCase):
    def test_sale_quantity_reads_digiseller_count(self) -> None:
        self.assertEqual(sale_quantity({"cnt_goods": "10"}), 10)
        self.assertEqual(sale_quantity({"order": {"quantity": "2.2"}}), 3)
        self.assertEqual(sale_quantity({"cnt_goods": "bad"}), 1)

    def test_chat_command_parser_reads_action_and_order_id(self) -> None:
        command = parse_chat_command("please !renew ord_abc12345")

        self.assertIsNotNone(command)
        self.assertEqual(command["action"], "renew")
        self.assertEqual(command["order_id"], "ord_abc12345")

    def test_expected_command_can_be_changed(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            service = DeliveryService(db=db, xyranet=FakeXyraClient())

            service.set_expected_command("renew", "!extend")

            self.assertEqual(service.expected_command("renew"), "!extend")
            self.assertEqual(service.action_for_command("!extend"), "renew")

    def test_status_command_can_be_changed_and_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            service = DeliveryService(db=db, xyranet=FakeXyraClient())

            service.set_expected_command("status", "!info")

            self.assertEqual(service.expected_command("status"), "!info")
            self.assertEqual(service.action_for_command("!info"), "status")

    def test_command_help_keeps_status_and_respects_free_reissue_toggle(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()

            disabled_service = DeliveryService(db=db, xyranet=FakeXyraClient(), free_reissue_enabled=False)
            disabled_text = disabled_service.render_system_text("command_help")
            self.assertIn("!status {ORDER_ID}", disabled_text)
            self.assertNotIn("!reissue", disabled_text)
            self.assertNotIn("{STATUS_COMMAND_EXAMPLE}", disabled_text)

            enabled_service = DeliveryService(db=db, xyranet=FakeXyraClient(), free_reissue_enabled=True)
            enabled_text = enabled_service.render_system_text("command_help")
            self.assertIn("!status {ORDER_ID}", enabled_text)
            self.assertIn("!reissue {ORDER_ID}", enabled_text)

    def test_purchase_template_group_has_no_reissue_command(self) -> None:
        group = next(item for item in TEMPLATE_GROUPS if item["key"] == "create")

        self.assertNotIn("command_action", group)
        self.assertEqual([stage["key"] for stage in group["stages"]], ["create"])

    def test_digiseller_template_group_has_unique_code_request(self) -> None:
        group = next(item for item in TEMPLATE_GROUPS if item["key"] == "digiseller")

        self.assertNotIn("command_action", group)
        self.assertEqual([stage["key"] for stage in group["stages"]], ["request_unique_code"])

    def test_reissue_template_group_owns_free_reissue_templates(self) -> None:
        group = next(item for item in TEMPLATE_GROUPS if item["key"] == "reissue")
        stage_keys = {stage["key"] for stage in group["stages"]}

        self.assertEqual(group["command_action"], "reissue")
        self.assertIn("free_reissue_help", stage_keys)
        self.assertIn("free_reissue_disabled", stage_keys)

    def test_status_template_group_has_command_and_status_templates(self) -> None:
        group = next(item for item in TEMPLATE_GROUPS if item["key"] == "status")
        stage_keys = {stage["key"] for stage in group["stages"]}

        self.assertEqual(group["command_action"], "status")
        self.assertIn("status_help", stage_keys)
        self.assertIn("status", stage_keys)
        self.assertIn("status_error", stage_keys)

    def test_order_id_parser_ignores_placeholder(self) -> None:
        self.assertEqual(extract_order_id_from_text("!renew {ORDER_ID}"), "")
        self.assertEqual(extract_order_id_from_text("!ip ord_real12345"), "ord_real12345")

    def test_template_supports_uppercase_variables(self) -> None:
        text = render_template(
            "ID {ORDER_ID} devices {DEVICE_LIMIT} quota {LTE_QUOTA} legacy ${order_id}",
            {"order_id": "ord-1", "device_limit": "2", "lte_quota": "10 ГБ"},
        )

        self.assertEqual(text, "ID ord-1 devices 2 quota 10 ГБ legacy ord-1")

    def test_custom_complex_variable_expands_inside_delivery_template(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            service = DeliveryService(db=db, xyranet=FakeXyraClient())

            service.save_complex_variable(
                key="CUSTOM_NOTICE",
                label="Custom notice",
                template="ID {ORDER_ID}, тариф {TARIFF_CODE}",
            )

            text = service.render_template_with_complex_variables(
                "Готово\n{CUSTOM_NOTICE}",
                {"order_id": "ord-1", "tariff_code": "lite_monthly"},
            )

            self.assertEqual(text, "Готово\nID ord-1, тариф lite_monthly")

    def test_sales_statistics_sums_revenue_and_expense(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            product = db.upsert_product(
                {
                    "marketplace": "plati",
                    "external_product_id": "p1",
                    "action": "create",
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                }
            )
            sale = db.create_sale(
                SaleEvent("plati", "o1", "p1", "", None, None, "299", "RUR", {"invoice_id": "o1"})
            )
            db.create_delivery(
                int(sale["id"]),
                int(product["id"]),
                {
                    "xyranet_order_id": "ord-1",
                    "subscription_url": "https://x.example/sub",
                    "panel_username": "panel_user",
                    "tariff_code": "lite_monthly",
                    "delivery_text": "ok",
                    "raw_response": {"order": {"subscription": {"api_price_rub": 113}}},
                },
            )

            stats = build_sales_statistics(db.list_sales_for_statistics(), period="all")

            self.assertEqual(stats["totals"]["sales_count"], 1)
            self.assertEqual(stats["totals"]["revenue_rub"]["text"], "299")
            self.assertEqual(stats["totals"]["expense_rub"]["text"], "113")
            self.assertEqual(stats["totals"]["profit_rub"]["text"], "186")

    def test_delivery_is_idempotent(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                db.upsert_product(
                    {
                        "marketplace": "plati",
                        "external_product_id": "p1",
                        "tariff_code": "lite_monthly",
                        "title": "Lite",
                    }
                )
                client = FakeXyraClient()
                service = DeliveryService(db=db, xyranet=client)
                event = SaleEvent("plati", "o1", "p1", "", None, None, None, None, {"invoice_id": "o1", "id_goods": "p1"})

                first = await service.handle_sale(event)
                second = await service.handle_sale(event)

                self.assertEqual(first["status"], "delivered")
                self.assertEqual(second["status"], "duplicate")
                self.assertEqual(client.calls, 1)

        asyncio.run(scenario())

    def test_create_quantity_renews_single_subscription(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                db.upsert_product(
                    {
                        "marketplace": "plati",
                        "external_product_id": "p1",
                        "tariff_code": "lite_monthly",
                        "title": "Lite",
                    }
                )
                client = FakeXyraClient()
                service = DeliveryService(db=db, xyranet=client)
                event = SaleEvent(
                    "plati",
                    "o1",
                    "p1",
                    "",
                    None,
                    None,
                    None,
                    None,
                    {"invoice_id": "o1", "id_goods": "p1", "cnt_goods": "3"},
                )

                result = await service.handle_sale(event)

                self.assertEqual(result["status"], "delivered")
                self.assertEqual(len(client.create_calls), 1)
                self.assertEqual(len(client.renew_calls), 2)
                self.assertEqual(client.renew_calls[0][0], "xyra-1")
                self.assertIn(":quantity-renew:2", client.renew_calls[0][2])
                self.assertEqual(result["delivery"]["xyranet_order_id"], "xyra-1")
                raw_response = json.loads(result["delivery"]["raw_response"])
                self.assertEqual(raw_response["statistics_expense_rub"], "339")

        asyncio.run(scenario())

    def test_renew_waits_for_order_id_in_marketplace_chat(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                db.upsert_product(
                    {
                        "marketplace": "plati",
                        "external_product_id": "p-renew",
                        "external_variant_id": "renew-button",
                        "action": "renew",
                        "tariff_code": "lite_monthly",
                        "title": "Renew",
                    }
                )
                client = FakeXyraClient()
                messenger = FakeMessenger()
                service = DeliveryService(db=db, xyranet=client, messenger=messenger)
                event = SaleEvent("plati", "o-renew", "p-renew", "renew-button", None, None, None, None, {"invoice_id": "o-renew", "id_goods": "p-renew"})

                pending = await service.handle_sale(event)
                self.assertEqual(pending["status"], "waiting_order_id")
                self.assertEqual(client.calls, 0)
                self.assertEqual(len(messenger.messages), 1)

                operation = db.list_pending_operations()[0]
                completed = await service.complete_pending_operation(operation, "xyra-order-1")

                self.assertEqual(completed["status"], "delivered")
                self.assertIn("Подписка продлена", completed["delivery_text"])
                self.assertEqual(client.calls, 1)

        asyncio.run(scenario())

    def test_subscription_status_renders_order_details(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "test.sqlite3")
                db.init()
                client = FakeXyraClient()
                service = DeliveryService(db=db, xyranet=client)

                result = await service.subscription_status("xyra-order-1")

                self.assertEqual(result["status"], "delivered")
                self.assertIn("Статус подписки", result["delivery_text"])
                self.assertIn("xyra-order-1", result["delivery_text"])
                self.assertIn("lite_monthly", result["delivery_text"])
                self.assertIn("https://x.example/sub-status", result["delivery_text"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
