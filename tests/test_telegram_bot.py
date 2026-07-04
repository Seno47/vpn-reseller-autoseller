import unittest

from reseller_autoseller.telegram_bot import main_menu


class TelegramBotMenuTests(unittest.TestCase):
    def test_main_menu_contains_server_metrics_button(self) -> None:
        keyboard = main_menu(is_owner=True)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertIn("🖥 Метрики", texts)


if __name__ == "__main__":
    unittest.main()
