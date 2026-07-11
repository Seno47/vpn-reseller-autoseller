import inspect
import unittest

from reseller_autoseller.telegram_bot import (
    build_dispatcher,
    chat_draft_keyboard,
    chat_draft_preview,
    escaped_excerpt,
    main_menu,
)


class TelegramChatUiTests(unittest.TestCase):
    def test_quick_replies_have_a_separate_main_menu_entry(self) -> None:
        keyboard = main_menu(is_owner=True)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertIn("💬 Быстрые ответы", texts)
        self.assertIn("📝 Шаблоны", texts)

    def test_reply_preview_escapes_buyer_chat_content(self) -> None:
        rendered = chat_draft_preview(
            {
                "marketplace": "plati<script>",
                "external_order_id": "1<&",
                "body": "Здравствуйте <b>покупатель</b> & спасибо!",
            }
        )

        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<b>покупатель</b>", rendered)
        self.assertIn("&lt;b&gt;покупатель&lt;/b&gt;", rendered)
        self.assertIn("1&lt;&amp;", rendered)

    def test_escaped_excerpt_stays_within_limit_without_cutting_an_entity(self) -> None:
        rendered = escaped_excerpt("<&" * 100, limit=31)

        self.assertLessEqual(len(rendered), 31)
        self.assertFalse(rendered.endswith("&"))
        self.assertFalse(rendered.endswith("&l"))

    def test_draft_keyboard_requires_explicit_send_confirmation(self) -> None:
        keyboard = chat_draft_keyboard(42)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("chat:draft_send:42", callbacks)
        self.assertIn("chat:draft_edit:42", callbacks)
        self.assertIn("chat:draft_cancel:42", callbacks)

    def test_dispatcher_accepts_a_shared_marketplace_messenger(self) -> None:
        self.assertIn("messenger", inspect.signature(build_dispatcher).parameters)


if __name__ == "__main__":
    unittest.main()
