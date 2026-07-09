import hashlib
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from reseller_autoseller.app import create_app
from reseller_autoseller.config import get_settings


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


if __name__ == "__main__":
    unittest.main()
