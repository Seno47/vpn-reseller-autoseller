import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reseller_autoseller.db import Database
from reseller_autoseller.marketplaces import SaleEvent
from scripts.backfill_digiseller_chat_history import (
    HARD_MAX_CHATS,
    MESSAGES_PER_CHAT,
    backfill_chat_history,
    collect_known_invoice_ids,
    normalize_message,
)


class FakeMessagesClient:
    def __init__(self, messages=None, error_for=None) -> None:
        self.messages = messages or {}
        self.error_for = set(error_for or [])
        self.calls = []

    async def order_messages(self, invoice_id, **kwargs):
        self.calls.append((invoice_id, kwargs))
        if invoice_id in self.error_for:
            raise RuntimeError("quota-safe failure")
        return self.messages.get(invoice_id, [])

    async def mark_order_messages_seen(self, invoice_id):  # pragma: no cover - a safety tripwire
        raise AssertionError(f"must never mark {invoice_id} as seen")


class DigisellerBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.init()

    def _sale(self, marketplace, order_id, raw_payload):
        return self.db.create_sale(
            SaleEvent(
                marketplace=marketplace,
                external_order_id=order_id,
                external_product_id="5968452",
                external_variant_id="",
                buyer_email=None,
                buyer_name=None,
                amount=None,
                currency=None,
                raw_payload=raw_payload,
            )
        )

    def test_collects_and_deduplicates_only_plati_digiseller_invoices(self) -> None:
        self.db.set_chat_cursor("plati", "1001", "1")
        self.db.set_chat_cursor("digiseller", "1002", "2")
        self.db.set_chat_cursor("ggsel", "9999", "3")
        self._sale("plati", "1003:ABCDEF1234567890", {"inv": 1003})
        self._sale("digiseller", "sale-key", {"content": {"invoice_id": 1004}})
        self._sale("plati", "duplicate", {"id_i": 1001})
        self._sale("ggsel", "8888", {"invoice_id": 8888})

        invoice_ids = collect_known_invoice_ids(self.db, max_chats=20)

        self.assertEqual(set(invoice_ids), {"1001", "1002", "1003", "1004"})
        self.assertEqual(len(invoice_ids), 4)
        self.assertEqual(len(collect_known_invoice_ids(self.db, max_chats=2)), 2)

    def test_normalizes_roles_files_and_stable_fallback_keys(self) -> None:
        buyer = normalize_message("1001", {"id": 5, "buyer": 1, "message": "Вопрос"})
        seller = normalize_message(
            "1001",
            {
                "seller": "true",
                "text": "Ответ",
                "date_written": "2026-07-11 17:30:00",
                "files": [{"name": "guide.pdf", "url": "https://example.test/guide.pdf"}],
            },
        )
        seller_again = normalize_message(
            "1001",
            {
                "seller": "true",
                "text": "Ответ",
                "date_written": "2026-07-11 17:30:00",
                "files": [{"name": "guide.pdf", "url": "https://example.test/guide.pdf"}],
            },
        )
        system = normalize_message("1001", {"buyer": 0, "seller": 0, "info": "Системное"})

        self.assertEqual(buyer["message_key"], "remote:5")
        self.assertEqual(buyer["role"], "buyer")
        self.assertEqual(seller["role"], "seller")
        self.assertTrue(seller["is_file"])
        self.assertEqual(seller["file_name"], "guide.pdf")
        self.assertEqual(seller["message_key"], seller_again["message_key"])
        self.assertEqual(system["role"], "system")

    async def test_calls_each_chat_once_and_is_idempotent_on_rerun(self) -> None:
        client = FakeMessagesClient(
            {
                "1001": [
                    {"id": 1, "buyer": 1, "message": "Первое"},
                    {"id": 2, "seller": 1, "message": "Второе"},
                ],
                "1002": [{"id": 3, "buyer": 1, "message": "Третье"}],
            }
        )

        first = await backfill_chat_history(self.db, client, ["1001", "1001", "bad", "1002"])
        second = await backfill_chat_history(self.db, client, ["1001", "1002"])

        self.assertEqual(client.calls[:2], [("1001", {"count": MESSAGES_PER_CHAT}), ("1002", {"count": MESSAGES_PER_CHAT})])
        self.assertEqual(first.requested_chats, 2)
        self.assertEqual(first.inserted_messages, 3)
        self.assertEqual(second.inserted_messages, 0)
        self.assertEqual(second.existing_messages, 3)
        self.assertEqual(self.db.count_chat_messages("plati", "1001"), 2)
        self.assertEqual(self.db.count_chat_messages("plati", "1002"), 1)

    async def test_hard_cap_applies_to_direct_calls_and_errors_do_not_retry(self) -> None:
        invoice_ids = [str(10_000 + number) for number in range(HARD_MAX_CHATS + 25)]
        failed_invoice = invoice_ids[3]
        client = FakeMessagesClient(error_for={failed_invoice})

        summary = await backfill_chat_history(self.db, client, invoice_ids)

        self.assertEqual(summary.requested_chats, HARD_MAX_CHATS)
        self.assertEqual(len(client.calls), HARD_MAX_CHATS)
        self.assertEqual(sum(invoice == failed_invoice for invoice, _ in client.calls), 1)
        self.assertEqual(summary.failed_chats[0][0], failed_invoice)


if __name__ == "__main__":
    unittest.main()
