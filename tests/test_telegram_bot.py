import unittest
from unittest.mock import AsyncMock, Mock

from reseller_autoseller.telegram_bot import answer_or_edit, main_menu


class TelegramBotMenuTests(unittest.TestCase):
    def test_main_menu_contains_server_metrics_button(self) -> None:
        keyboard = main_menu(is_owner=True)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertIn("🖥 Метрики", texts)

    def test_main_menu_can_render_english(self) -> None:
        keyboard = main_menu(is_owner=True, language="en")
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertIn("🖥 Metrics", texts)
        self.assertIn("⚙️ Settings", texts)


class TelegramBotCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_is_acknowledged_before_message_edit(self) -> None:
        calls: list[str] = []

        async def answer() -> None:
            calls.append("answer")

        async def edit_text(_text: str, *, reply_markup: object) -> None:
            calls.append("edit")

        callback = Mock()
        callback.answer = AsyncMock(side_effect=answer)
        callback.message = Mock()
        callback.message.edit_text = AsyncMock(side_effect=edit_text)

        await answer_or_edit(callback, "Main menu")

        self.assertEqual(calls, ["answer", "edit"])

    async def test_callback_stays_acknowledged_when_message_edit_fails(self) -> None:
        callback = Mock()
        callback.answer = AsyncMock()
        callback.message = Mock()
        callback.message.edit_text = AsyncMock(side_effect=RuntimeError("edit failed"))

        with self.assertRaisesRegex(RuntimeError, "edit failed"):
            await answer_or_edit(callback, "Main menu")

        callback.answer.assert_awaited_once_with()

    async def test_already_acknowledged_callback_is_only_edited(self) -> None:
        callback = Mock()
        callback.answer = AsyncMock()
        callback.message = Mock()
        callback.message.edit_text = AsyncMock()

        await answer_or_edit(callback, "Updated menu", answer_callback=False)

        callback.answer.assert_not_awaited()
        callback.message.edit_text.assert_awaited_once_with("Updated menu", reply_markup=None)


if __name__ == "__main__":
    unittest.main()
