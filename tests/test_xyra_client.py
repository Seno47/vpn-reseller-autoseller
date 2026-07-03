import unittest

from reseller_autoseller.xyra_client import XyraNetClient


class XyraNetClientTests(unittest.TestCase):
    def test_uses_documented_wholesale_base_url(self) -> None:
        client = XyraNetClient(base_url="https://xyranet.pro/api/wholesale", api_key="key")

        self.assertEqual(client.base_url, "https://xyranet.pro/api/wholesale")

    def test_accepts_legacy_origin_base_url(self) -> None:
        client = XyraNetClient(base_url="https://xyranet.pro", api_key="key")

        self.assertEqual(client.base_url, "https://xyranet.pro/api/wholesale")


if __name__ == "__main__":
    unittest.main()
