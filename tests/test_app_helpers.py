import unittest

from reseller_autoseller.app import digiseller_invoice_matches_chat


class AppHelperTests(unittest.TestCase):
    def test_digiseller_invoice_must_match_chat_invoice(self) -> None:
        self.assertTrue(digiseller_invoice_matches_chat("296106996", "296106996"))
        self.assertTrue(digiseller_invoice_matches_chat(" 296106996 ", "296106996"))
        self.assertFalse(digiseller_invoice_matches_chat("296106996", "295956496"))
        self.assertFalse(digiseller_invoice_matches_chat("296106996", ""))
        self.assertFalse(digiseller_invoice_matches_chat("", "296106996"))


if __name__ == "__main__":
    unittest.main()
