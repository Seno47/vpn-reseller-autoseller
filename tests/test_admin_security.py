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
        token: str = "abcdefghijklmnopqrstuvwxyz123456",
    ) -> TestClient:
        env = {
            "DATABASE_PATH": os.path.join(tmp, "test.sqlite3"),
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": password,
            "ADMIN_TOKEN": token,
            "ADMIN_IDS": "123456789",
            "ENABLE_TELEGRAM": "false",
            "TELEGRAM_BOT_TOKEN": "",
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        return TestClient(create_app())

    def test_default_admin_secrets_block_login(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp, password="change-me", token="change-me")

            response = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "change-me"},
            )

            self.assertEqual(response.status_code, 503)

    def test_login_returns_token_for_valid_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_client(tmp)

            response = client.post(
                "/admin/api/login",
                json={"username": "admin", "password": "strong-password"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["token"], "abcdefghijklmnopqrstuvwxyz123456")

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


if __name__ == "__main__":
    unittest.main()
