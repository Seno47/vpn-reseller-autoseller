import unittest

import httpx

from reseller_autoseller.marketplace_chat import GgselChatClient


class GgselChatClientTests(unittest.TestCase):
    def test_uses_live_seller_office_api_host(self) -> None:
        client = GgselChatClient(seller_id="1", api_key="key")

        self.assertEqual(client.base_url, "https://seller.ggsel.com")

    def test_invalid_json_has_contextual_api_error(self) -> None:
        response = httpx.Response(200, text="not-json")

        with self.assertRaisesRegex(RuntimeError, "GGsel sales API returned invalid JSON"):
            GgselChatClient._json_response(response, "GGsel sales API")


if __name__ == "__main__":
    unittest.main()
