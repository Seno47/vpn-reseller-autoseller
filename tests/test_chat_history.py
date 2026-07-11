import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reseller_autoseller.chat_ui import (
    chat_actions_reply_markup,
    format_chat_history,
    format_chat_notification,
)
from reseller_autoseller.db import Database


class ChatHistoryDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.init()

    def test_chat_messages_are_idempotent_and_returned_chronologically(self) -> None:
        first, first_created = self.db.add_chat_message(
            marketplace="plati",
            external_order_id="1001",
            external_message_id="10",
            message_key="remote:10",
            role="buyer",
            text="Первое",
            raw_payload={"buyer": 1},
        )
        duplicate, duplicate_created = self.db.add_chat_message(
            marketplace="plati",
            external_order_id="1001",
            external_message_id="10",
            message_key="remote:10",
            role="buyer",
            text="Не должно заменить первое",
        )
        second, second_created = self.db.add_chat_message(
            marketplace="plati",
            external_order_id="1001",
            external_message_id="11",
            message_key="remote:11",
            role="unknown-role",
            text="Второе",
        )

        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertTrue(second_created)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["text"], "Первое")
        self.assertEqual(second["role"], "system")
        self.assertEqual(self.db.count_chat_messages("plati", "1001"), 2)
        self.assertEqual(
            [row["external_message_id"] for row in self.db.list_chat_messages("plati", "1001")],
            ["10", "11"],
        )
        self.assertEqual(self.db.get_chat_message(first["id"])["text"], "Первое")

    def test_finds_recent_outgoing_message_but_not_buyer_message(self) -> None:
        self.db.add_chat_message(
            marketplace="plati",
            external_order_id="1001",
            role="bot",
            text="Готово",
        )
        self.db.add_chat_message(
            marketplace="plati",
            external_order_id="1001",
            role="buyer",
            text="Вопрос",
        )

        self.assertIsNotNone(self.db.find_recent_chat_message_by_text("plati", "1001", "Готово"))
        self.assertIsNone(self.db.find_recent_chat_message_by_text("plati", "1001", "Вопрос"))

    def test_quick_reply_templates_support_crud_and_keep_copied_draft(self) -> None:
        template = self.db.create_quick_reply_template("Приветствие", "Здравствуйте!", created_by=42)
        updated = self.db.update_quick_reply_template(
            template["id"],
            title="Ответ покупателю",
            body="Здравствуйте! Чем помочь?",
            enabled=False,
        )
        draft = self.db.create_chat_reply_draft(
            marketplace="plati",
            external_order_id="1001",
            telegram_user_id=42,
            author_name="Администратор",
            body=updated["body"],
            template_id=template["id"],
        )

        self.assertEqual(self.db.list_quick_reply_templates(enabled_only=True), [])
        self.assertEqual(updated["title"], "Ответ покупателю")
        self.assertFalse(updated["enabled"])
        self.assertTrue(self.db.delete_quick_reply_template(template["id"]))
        self.assertIsNone(self.db.get_quick_reply_template(template["id"]))
        saved_draft = self.db.get_chat_reply_draft(draft["id"])
        self.assertEqual(saved_draft["body"], "Здравствуйте! Чем помочь?")
        self.assertIsNone(saved_draft["template_id"])

    def test_reply_draft_can_only_be_claimed_once_by_its_owner(self) -> None:
        draft = self.db.create_chat_reply_draft(
            marketplace="plati",
            external_order_id="1001",
            telegram_user_id=42,
            body="Ответ",
        )

        self.assertIsNone(self.db.update_chat_reply_draft_body(draft["id"], 7, "Чужая правка"))
        self.assertIsNone(self.db.claim_chat_reply_draft(draft["id"], 7))
        updated = self.db.update_chat_reply_draft_body(draft["id"], 42, "Исправленный ответ")
        claimed = self.db.claim_chat_reply_draft(draft["id"], 42)

        self.assertEqual(updated["body"], "Исправленный ответ")
        self.assertIsNotNone(claimed)
        claimed_row, token = claimed
        self.assertEqual(claimed_row["status"], "sending")
        self.assertIsNone(self.db.claim_chat_reply_draft(draft["id"], 42))
        self.assertFalse(self.db.finish_chat_reply_draft(draft["id"], "wrong-token", status="sent"))
        self.assertTrue(self.db.finish_chat_reply_draft(draft["id"], token, status="sent"))
        self.assertEqual(self.db.get_chat_reply_draft(draft["id"])["status"], "sent")
        self.assertFalse(self.db.cancel_chat_reply_draft(draft["id"], 42))


class ChatHistoryFormattingTests(unittest.TestCase):
    def test_notification_escapes_untrusted_html(self) -> None:
        rendered = format_chat_notification(
            {
                "marketplace": "Digi<Seller>",
                "external_order_id": "12&34",
                "role": "buyer",
                "text": "<script>alert('x')</script> & вопрос",
                "message_date": "2026-07-11T17:30:00+00:00",
            }
        )

        self.assertIn("👤 <b>Покупатель</b>", rendered)
        self.assertIn("Digi&lt;Seller&gt;", rendered)
        self.assertIn("12&amp;34", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))

    def test_history_keeps_latest_local_messages_within_limit_and_valid_tags(self) -> None:
        messages = [
            {
                "role": "buyer" if number % 2 else "bot",
                "text": f"Сообщение {number}: <b>не HTML</b> & " + ("длинный текст " * 70),
                "message_date": f"2026-07-11T17:{number:02d}:00+00:00",
            }
            for number in range(1, 9)
        ]

        rendered = format_chat_history(
            messages,
            marketplace="plati",
            external_order_id="1001",
            limit=620,
        )

        self.assertLessEqual(len(rendered), 620)
        self.assertIn("Сообщение 8", rendered)
        self.assertIn("старых сообщений не поместилось", rendered)
        self.assertIn("&lt;b&gt;не HTML&lt;/b&gt;", rendered)
        self.assertNotIn("<b>не HTML</b>", rendered)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))
        self.assertEqual(rendered.count("<code>"), rendered.count("</code>"))
        self.assertEqual(rendered.count("<b>"), rendered.count("</b>"))

    def test_tiny_custom_limit_never_cuts_html(self) -> None:
        rendered = format_chat_history(
            [{"role": "buyer", "text": "x" * 5000}],
            marketplace="plati",
            external_order_id="1001",
            limit=60,
        )

        self.assertLessEqual(len(rendered), 60)
        self.assertEqual(rendered.count("<b>"), rendered.count("</b>"))
        self.assertEqual(rendered.count("<code>"), rendered.count("</code>"))
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))

    def test_inline_actions_are_anchored_to_local_message(self) -> None:
        markup = chat_actions_reply_markup(17, "12/34")
        buttons = [button for row in markup["inline_keyboard"] for button in row]

        self.assertIn("chat:reply:17", {button.get("callback_data") for button in buttons})
        self.assertIn("chat:more:17", {button.get("callback_data") for button in buttons})
        link = next(button["url"] for button in buttons if "url" in button)
        self.assertEqual(link, "https://my.digiseller.com/inside/account.asp")


if __name__ == "__main__":
    unittest.main()
