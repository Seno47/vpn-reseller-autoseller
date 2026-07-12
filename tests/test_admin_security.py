import asyncio
import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from reseller_autoseller.app import create_app
from reseller_autoseller.config import get_settings
from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent


class AdminSecurityTests(unittest.TestCase):
    def make_client(
        self,
        tmp: str,
        *,
        password: str = "strong-password",
    ) -> TestClient:
        env = {
            "DATABASE_PATH": os.path.join(tmp, "test.sqlite3"),
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": password,
            "ADMIN_IDS": "123456789",
            "ENABLE_TELEGRAM": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "APP_BASE_URL": "https://panel.example",
            "DIGISELLER_SELLER_ID": "",
            "DIGISELLER_API_KEY": "TEST_DIGISELLER_KEY",
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        return TestClient(create_app())

    def test_default_admin_secrets_block_login(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp, password="change-me")

            response = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "change-me"},
            )

            self.assertEqual(response.status_code, 503)

    def test_login_returns_session_token_for_valid_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)

            response = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertGreater(len(response.json()["token"]), 24)

    def test_admin_responses_set_security_and_no_store_headers(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)

            page = client.get("/")
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )

            self.assertEqual(page.headers["x-frame-options"], "DENY")
            self.assertEqual(page.headers["x-content-type-options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual(login.headers["cache-control"], "no-store")

    def test_password_change_invalidates_active_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            old_token = login.json()["token"]
            headers = {"Authorization": f"Bearer {old_token}"}

            response = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"admin_password": "new-strong-password"}},
            )

            self.assertEqual(response.status_code, 200)
            old_session = client.get("/admin/api/status", headers=headers)
            self.assertEqual(old_session.status_code, 401)

            old_password = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            self.assertEqual(old_password.status_code, 401)

            new_password = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "new-strong-password"},
            )
            self.assertEqual(new_password.status_code, 200)

    def test_logout_revokes_admin_session(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            logout = client.post("/admin/api/logout", headers=headers)
            after_logout = client.get("/admin/api/status", headers=headers)

            self.assertEqual(logout.status_code, 204)
            self.assertEqual(after_logout.status_code, 401)

    def test_settings_reject_credentials_that_would_lock_out_admin(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            empty_username = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"admin_username": ""}},
            )
            weak_password = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"admin_password": "short"}},
            )

            self.assertEqual(empty_username.status_code, 400)
            self.assertEqual(weak_password.status_code, 400)
            self.assertEqual(client.get("/admin/api/status", headers=headers).status_code, 200)

    def test_settings_validation_is_atomic(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            response = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"panel_language": "en", "unknown_setting": "value"}},
            )

            self.assertEqual(response.status_code, 400)
            settings = client.get("/admin/api/settings", headers=headers).json()
            language = next(item for item in settings if item["key"] == "panel_language")
            self.assertEqual(language["value"], "ru")

    def test_settings_reject_invalid_timeouts(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            zero = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"xyranet_timeout_seconds": 0}},
            )
            not_finite = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"xyranet_timeout_seconds": "NaN"}},
            )

            self.assertEqual(zero.status_code, 400)
            self.assertEqual(not_finite.status_code, 400)

    def test_restart_invalidates_active_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            token = login.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            self.assertEqual(client.get("/admin/api/status", headers=headers).status_code, 200)
            client.close()

            restarted_client = self.make_client(tmp)
            response = restarted_client.get("/admin/api/status", headers=headers)

            self.assertEqual(response.status_code, 401)

    def test_failed_logins_are_rate_limited(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)

            for _ in range(8):
                response = client.post(
                    "/admin/api/login",
                    json={"username": "admin", "password": "bad-password"},
                )
                self.assertEqual(response.status_code, 401)

            response = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "bad-password"},
            )

            self.assertEqual(response.status_code, 429)

    def test_system_metrics_endpoint_returns_server_load(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            token = login.json()["token"]

            response = client.get("/admin/api/system", headers={"Authorization": f"Bearer {token}"})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("cpu", data)
            self.assertIn("memory", data)
            self.assertIn("disk", data)
            self.assertIn("process", data)
            self.assertGreaterEqual(data["cpu"]["cores"], 1)

    def test_settings_can_switch_between_russian_and_english_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            ru_response = client.get("/admin/api/settings", headers=headers)
            ru_labels = {item["key"]: item["label"] for item in ru_response.json()}
            self.assertEqual(ru_labels["panel_language"], "Язык интерфейса")
            self.assertEqual(ru_labels["enable_telegram"], "Telegram включён")

            en_response = client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"panel_language": "en"}},
            )
            en_labels = {item["key"]: item["label"] for item in en_response.json()["settings"]}

            self.assertEqual(en_labels["panel_language"], "Interface language")
            self.assertEqual(en_labels["enable_telegram"], "Telegram enabled")

    def test_status_chat_command_can_be_updated(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            response = client.put(
                "/admin/api/chat-command/status",
                headers=headers,
                json={"command": "!info"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["command"], "!info")

    def test_digiseller_notification_urls_are_available(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            response = client.get("/admin/api/digiseller/notification-urls", headers=headers)

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["sale_url"].startswith("https://panel.example/api/digiseller/notify/sale/"))
            self.assertTrue(data["message_url"].startswith("https://panel.example/api/digiseller/notify/message/"))

    def test_digiseller_sale_notification_validates_secret_and_sha(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            urls = client.get("/admin/api/digiseller/notification-urls", headers=headers).json()
            secret = urls["sale_url"].rstrip("/").split("/")[-1]
            invoice_id = "296211150"
            product_id = "5968452"
            valid_sha = hashlib.sha256(f"test_digiseller_key;{invoice_id};{product_id}".encode("utf-8")).hexdigest()

            bad_secret = client.post(
                "/api/digiseller/notify/sale/bad-secret",
                json={"ID_I": invoice_id, "ID_D": product_id, "SHA256": valid_sha},
            )
            bad_sha = client.post(
                f"/api/digiseller/notify/sale/{secret}",
                json={"ID_I": invoice_id, "ID_D": product_id, "SHA256": "bad"},
            )
            valid = client.post(
                f"/api/digiseller/notify/sale/{secret}",
                json={"ID_I": invoice_id, "ID_D": product_id, "SHA256": valid_sha},
            )

            self.assertEqual(bad_secret.status_code, 404)
            self.assertEqual(bad_sha.status_code, 403)
            self.assertEqual(valid.status_code, 200)
            self.assertEqual(valid.json()["status"], "ignored")

    def test_digiseller_sale_notification_sends_unique_code_request_immediately_by_default(self) -> None:
        send_message = AsyncMock(return_value=True)
        with TemporaryDirectory() as tmp, patch(
            "reseller_autoseller.app.MarketplaceMessenger.send_message",
            new=send_message,
        ):
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            client.patch(
                "/admin/api/settings",
                headers=headers,
                json={"settings": {"digiseller_validate_sale_sha256": False}},
            )
            product = client.post(
                "/admin/api/products",
                headers=headers,
                json={
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "action": "create",
                    "action_params": {},
                    "tariff_code": "lite_monthly",
                    "title": "Lite 1 month",
                    "enabled": True,
                },
            )
            self.assertEqual(product.status_code, 200)
            urls = client.get("/admin/api/digiseller/notification-urls", headers=headers).json()
            secret = urls["sale_url"].rstrip("/").split("/")[-1]

            response = client.post(
                f"/api/digiseller/notify/sale/{secret}",
                json={"ID_I": "296240253", "ID_D": "5968452", "Amount": "119", "Currency": "WMR"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["action"], "unique_code_request_sent")
            self.assertEqual(response.json()["delay_minutes"], 0)
            send_message.assert_awaited_once()
            self.assertEqual(send_message.await_args.args[:2], ("plati", "296240253"))

            duplicate = client.post(
                f"/api/digiseller/notify/sale/{secret}",
                json={"ID_I": "296240253", "ID_D": "5968452", "Amount": "119", "Currency": "WMR"},
            )

            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["status"], "ignored")
            self.assertEqual(duplicate.json()["reason"], "request was already sent")
            send_message.assert_awaited_once()

    def test_digiseller_sale_notification_preserves_configured_delay(self) -> None:
        send_message = AsyncMock(return_value=True)
        with TemporaryDirectory() as tmp, patch(
            "reseller_autoseller.app.MarketplaceMessenger.send_message",
            new=send_message,
        ):
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            client.patch(
                "/admin/api/settings",
                headers=headers,
                json={
                    "settings": {
                        "digiseller_validate_sale_sha256": False,
                        "digiseller_unique_code_request_delay_minutes": 7,
                    }
                },
            )
            client.post(
                "/admin/api/products",
                headers=headers,
                json={
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "action": "create",
                    "action_params": {},
                    "tariff_code": "lite_monthly",
                    "title": "Lite 1 month",
                    "enabled": True,
                },
            )
            urls = client.get("/admin/api/digiseller/notification-urls", headers=headers).json()
            secret = urls["sale_url"].rstrip("/").split("/")[-1]

            response = client.post(
                f"/api/digiseller/notify/sale/{secret}",
                json={"ID_I": "296240254", "ID_D": "5968452", "Amount": "119", "Currency": "WMR"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["action"], "unique_code_request_scheduled")
            self.assertEqual(response.json()["delay_minutes"], 7)
            send_message.assert_not_awaited()

    def test_concurrent_sale_notifications_send_unique_code_request_once(self) -> None:
        async def slow_send(*_args, **_kwargs) -> bool:
            await asyncio.sleep(0.05)
            return True

        send_message = AsyncMock(side_effect=slow_send)
        with TemporaryDirectory() as tmp, patch(
            "reseller_autoseller.app.MarketplaceMessenger.send_message",
            new=send_message,
        ):
            with self.make_client(tmp) as client:
                login = client.post(
                    "/admin/api/login",
                    json={"username": "admin", "password": "strong-password"},
                )
                headers = {"Authorization": f"Bearer {login.json()['token']}"}
                client.patch(
                    "/admin/api/settings",
                    headers=headers,
                    json={"settings": {"digiseller_validate_sale_sha256": False}},
                )
                client.post(
                    "/admin/api/products",
                    headers=headers,
                    json={
                        "marketplace": "plati",
                        "external_product_id": "5968452",
                        "external_variant_id": "23468281",
                        "action": "create",
                        "action_params": {},
                        "tariff_code": "lite_monthly",
                        "title": "Lite 1 month",
                        "enabled": True,
                    },
                )
                secret = client.get(
                    "/admin/api/digiseller/notification-urls",
                    headers=headers,
                ).json()["sale_url"].rstrip("/").split("/")[-1]
                url = f"/api/digiseller/notify/sale/{secret}"
                payload = {
                    "ID_I": "296240255",
                    "ID_D": "5968452",
                    "Amount": "119",
                    "Currency": "WMR",
                }

                with ThreadPoolExecutor(max_workers=2) as executor:
                    responses = list(executor.map(lambda _: client.post(url, json=payload), range(2)))

            self.assertEqual([response.status_code for response in responses], [200, 200])
            self.assertEqual(send_message.await_count, 1)
            self.assertEqual(
                {response.json().get("action") for response in responses},
                {"unique_code_request_sent", "unique_code_request_skipped"},
            )

    def test_digiseller_product_alias_is_saved_as_canonical_plati(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            response = client.post(
                "/admin/api/products",
                headers=headers,
                json={
                    "marketplace": "digiseller",
                    "external_product_id": "5968452",
                    "external_variant_id": "",
                    "action": "create",
                    "action_params": {},
                    "tariff_code": "lite_monthly",
                    "title": "Legacy alias",
                    "enabled": True,
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["marketplace"], "plati")

    def test_delete_referenced_product_returns_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
            login = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            product_response = client.post(
                "/admin/api/products",
                headers=headers,
                json={
                    "marketplace": "plati",
                    "external_product_id": "5968452",
                    "external_variant_id": "23468281",
                    "action": "create",
                    "action_params": {},
                    "tariff_code": "lite_monthly",
                    "title": "Lite",
                    "enabled": True,
                },
            )
            product = product_response.json()
            db = Database(Path(tmp) / "test.sqlite3")
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
            db.create_delivery(
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

            response = client.delete(f"/admin/api/products/{product['id']}", headers=headers)

            self.assertEqual(response.status_code, 409)
            self.assertIsNotNone(db.get_product(product["id"]))


if __name__ == "__main__":
    unittest.main()
