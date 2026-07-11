import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from reseller_autoseller.db import Database
from reseller_autoseller.marketplace_chat import GgselChatClient, MarketplaceMessenger


class GgselChatClientTests(unittest.TestCase):
    def test_uses_live_seller_office_api_host(self) -> None:
        client = GgselChatClient(seller_id="1", api_key="key")

        self.assertEqual(client.base_url, "https://seller.ggsel.com")

    def test_invalid_json_has_contextual_api_error(self) -> None:
        response = httpx.Response(200, text="not-json")

        with self.assertRaisesRegex(RuntimeError, "GGsel sales API returned invalid JSON"):
            GgselChatClient._json_response(response, "GGsel sales API")


class FakeDigiseller:
    async def send_order_message(self, order_id: str, text: str) -> dict[str, int]:
        return {"retval": 0}


class BrokenHistoryDatabase:
    def add_chat_message(self, **kwargs):
        raise RuntimeError("local database is temporarily unavailable")


class MarketplaceMessengerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_message_is_saved_with_its_role(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            notified: list[dict] = []
            messenger = MarketplaceMessenger(
                digiseller=FakeDigiseller(),
                db=db,
                on_message=notified.append,
            )

            sent = await messenger.send_message(
                "plati",
                "1001",
                "Здравствуйте!",
                role="admin",
                author_name="Оператор",
                source="telegram",
            )

            self.assertTrue(sent)
            rows = db.list_chat_messages("plati", "1001")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["role"], "admin")
            self.assertEqual(rows[0]["author_name"], "Оператор")
            self.assertEqual([row["id"] for row in notified], [rows[0]["id"]])

    async def test_local_history_failure_does_not_retry_remote_send(self) -> None:
        messenger = MarketplaceMessenger(
            digiseller=FakeDigiseller(),
            db=BrokenHistoryDatabase(),
        )

        self.assertTrue(await messenger.send_message("plati", "1001", "Уже отправлено"))


if __name__ == "__main__":
    unittest.main()
