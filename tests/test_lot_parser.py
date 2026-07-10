import unittest

from reseller_autoseller.lot_parser import detect_marketplace, extract_product_id, is_allowed_lot_url, parse_lot_html


class LotParserTests(unittest.TestCase):
    def test_lot_url_allowlist_rejects_internal_and_unknown_hosts(self):
        self.assertTrue(is_allowed_lot_url("https://plati.io/itm/demo/5968452"))
        self.assertTrue(is_allowed_lot_url("https://my.digiseller.com/inside/lot?id_goods=5968452"))
        self.assertTrue(is_allowed_lot_url("https://ggsel.net/catalog/demo"))

        self.assertFalse(is_allowed_lot_url("http://127.0.0.1:8095/admin/api/settings"))
        self.assertFalse(is_allowed_lot_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(is_allowed_lot_url("https://plati.io.evil.example/itm/demo/5968452"))

    def test_parses_plati_radio_options(self):
        html = """
        <html><head><title>Купить Test VPN</title></head><body>
          <input class="chips__input cl_checked_option id_delta_rb" id="CheckedOption_1"
            type="radio" value="23468281" data-item-id="5968452" data-delta-price="0" checked="checked">
          <label class="chips__label" for="CheckedOption_1">
            <span>Lite 1 месяц | 2 устройства</span><span>Выбран</span>
          </label>
          <input class="chips__input cl_checked_option id_delta_rb" id="CheckedOption_2"
            type="radio" value="23469997" data-item-id="5968452" data-delta-price="130">
          <label class="chips__label" for="CheckedOption_2">
            <span>Premium 1 месяц | 6 устройств</span><span>+130 ₽ за Lite 1 мес</span>
          </label>
        </body></html>
        """

        parsed = parse_lot_html("https://plati.io/itm/demo/5968452", html)

        self.assertEqual(parsed["marketplace"], "plati")
        self.assertEqual(parsed["productId"], "5968452")
        self.assertEqual(
            parsed["variants"],
            [
                {"id": "23468281", "label": "Lite 1 месяц | 2 устройства"},
                {"id": "23469997", "label": "Premium 1 месяц | 6 устройств (130 ₽)"},
            ],
        )

    def test_marketplace_comes_from_hostname_not_query_text(self):
        self.assertEqual(
            detect_marketplace("https://plati.io/itm/demo/5968452?return_url=https://ggsel.net"),
            "plati",
        )
        self.assertEqual(detect_marketplace("https://my.digiseller.com/inside/lot?id_goods=5968452"), "plati")

    def test_html_prices_are_not_mistaken_for_product_id(self):
        html = "<html><head><title>Demo 2026</title></head><body>Price: 1499 RUB</body></html>"

        self.assertEqual(extract_product_id(html), "")
        self.assertEqual(parse_lot_html("https://ggsel.net/catalog/demo", html)["productId"], "")

    def test_structured_html_product_id_marker_is_supported(self):
        html = '<div class="product" data-product-id="5968452">Price: 1499 RUB</div>'

        self.assertEqual(extract_product_id(html), "5968452")
        self.assertEqual(extract_product_id('<div data-not-product-id="1234">Price: 1499 RUB</div>'), "")

    def test_only_explicit_variant_select_contributes_options(self):
        html = """
        <select name="language">
          <option value="ru">Russian</option>
          <option value="en">English</option>
        </select>
        <select class="product-variants" name="variant_id">
          <option value="42">Lite</option>
          <option value="43">Premium</option>
        </select>
        """

        parsed = parse_lot_html("https://ggsel.net/catalog/demo", html)

        self.assertEqual(parsed["variants"], [{"id": "42", "label": "42"}, {"id": "43", "label": "43"}])


if __name__ == "__main__":
    unittest.main()
