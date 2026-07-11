from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from reseller_autoseller.config import Settings  # noqa: E402
from reseller_autoseller.db import Database  # noqa: E402
from reseller_autoseller.digiseller_client import RuntimeDigisellerClient  # noqa: E402
from reseller_autoseller.runtime_config import RuntimeConfig  # noqa: E402


HARD_MAX_CHATS = 200
MESSAGES_PER_CHAT = 200
KNOWN_MARKETPLACES = {"plati", "digiseller"}
TRUE_VALUES = {"1", "true", "yes", "on"}


class MessagesClient(Protocol):
    async def order_messages(
        self,
        invoice_id: str,
        *,
        count: int = MESSAGES_PER_CHAT,
        newer: bool = False,
        old_id: str = "",
    ) -> list[dict[str, Any]]: ...


@dataclass
class BackfillSummary:
    selected_chats: int
    requested_chats: int = 0
    received_messages: int = 0
    inserted_messages: int = 0
    existing_messages: int = 0
    failed_chats: list[tuple[str, str]] = field(default_factory=list)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _first_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return str(payload[key]).strip()
    return ""


def _message_role(payload: dict[str, Any]) -> str:
    seller_markers = ("seller", "is_seller", "from_seller", "seller_message")
    buyer_markers = ("buyer", "is_buyer", "from_buyer", "customer", "is_customer")
    if any(key in payload and _truthy(payload.get(key)) for key in seller_markers):
        return "seller"
    if any(key in payload and _truthy(payload.get(key)) for key in buyer_markers):
        return "buyer"
    if any(key in payload for key in (*seller_markers, *buyer_markers)):
        return "system"
    return "buyer"


def _file_details(payload: dict[str, Any]) -> tuple[bool, str, str]:
    file_name = _first_value(payload, "filename", "file_name")
    file_url = _first_value(payload, "url", "file_url", "file_link")
    files = payload.get("files")
    if isinstance(files, list) and files:
        first_file = files[0] if isinstance(files[0], dict) else {}
        file_name = file_name or _first_value(first_file, "filename", "file_name", "name")
        file_url = file_url or _first_value(first_file, "url", "file_url", "link")
    is_file = _truthy(payload.get("is_file")) or bool(file_name or file_url or (isinstance(files, list) and files))
    return is_file, file_name, file_url


def normalize_message(invoice_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    remote_id = _first_value(payload, "id", "message_id", "id_d", "id_msg", "id_debate")
    text = _first_value(payload, "message", "text", "body", "content", "info")
    message_date = _first_value(payload, "date_written", "MessageDate", "message_date", "date")
    author_name = _first_value(payload, "author_name", "author", "sender_name", "username", "name")
    is_file, file_name, file_url = _file_details(payload)
    if remote_id:
        message_key = f"remote:{remote_id}"
    else:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        fingerprint = f"{invoice_id}\x1f{serialized}"
        message_key = "backfill:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return {
        "marketplace": "plati",
        "external_order_id": invoice_id,
        "role": _message_role(payload),
        "text": text,
        "external_message_id": remote_id,
        "message_key": message_key,
        "author_name": author_name,
        "source": "digiseller_backfill",
        "message_date": message_date,
        "is_file": is_file,
        "file_name": file_name,
        "file_url": file_url,
        "raw_payload": payload,
    }


def _invoice_from_sale(external_order_id: Any, raw_payload: Any) -> str:
    try:
        payload = json.loads(str(raw_payload or "{}"))
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    invoice_id = _first_value(payload, "inv", "id_i", "invoice_id", "order_id")
    invoice_id = invoice_id or _first_value(content, "inv", "id_i", "invoice_id", "order_id")
    invoice_id = invoice_id or str(external_order_id or "").strip().partition(":")[0]
    return invoice_id if invoice_id.isdigit() else ""


def collect_known_invoice_ids(db: Database, *, max_chats: int) -> list[str]:
    selected_limit = max(1, min(int(max_chats), HARD_MAX_CHATS))
    candidates: list[str] = []
    for cursor in db.list_chat_cursors(limit=1000):
        if str(cursor.get("marketplace") or "").lower() not in KNOWN_MARKETPLACES:
            continue
        invoice_id = str(cursor.get("external_order_id") or "").strip().partition(":")[0]
        if invoice_id.isdigit():
            candidates.append(invoice_id)

    # Filtering in SQL prevents unrelated marketplace sales from consuming
    # the bounded candidate window before Plati/DigiSeller rows are examined.
    with db.connect() as conn:
        sales = conn.execute(
            """
            SELECT external_order_id, raw_payload
            FROM sales
            WHERE lower(marketplace) IN ('plati', 'digiseller')
            ORDER BY id DESC
            LIMIT 5000
            """
        ).fetchall()
    for sale in sales:
        invoice_id = _invoice_from_sale(sale["external_order_id"], sale["raw_payload"])
        if invoice_id:
            candidates.append(invoice_id)

    return list(dict.fromkeys(candidates))[:selected_limit]


async def backfill_chat_history(
    db: Database,
    client: MessagesClient,
    invoice_ids: list[str],
) -> BackfillSummary:
    # A second guard makes the API budget safe even if this function is called
    # directly instead of through the CLI/collector.
    selected = list(dict.fromkeys(str(value).strip() for value in invoice_ids if str(value).strip().isdigit()))[
        :HARD_MAX_CHATS
    ]
    summary = BackfillSummary(selected_chats=len(selected))
    for invoice_id in selected:
        summary.requested_chats += 1
        try:
            # Deliberately one call, one page, and no `mark_order_messages_seen`.
            messages = await client.order_messages(invoice_id, count=MESSAGES_PER_CHAT)
        except Exception as exc:
            summary.failed_chats.append((invoice_id, str(exc)))
            continue
        summary.received_messages += len(messages)
        try:
            for payload in messages:
                if not isinstance(payload, dict):
                    continue
                _, created = db.add_chat_message(**normalize_message(invoice_id, payload))
                if created:
                    summary.inserted_messages += 1
                else:
                    summary.existing_messages += 1
        except Exception as exc:
            summary.failed_chats.append((invoice_id, f"database: {exc}"))
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-time, quota-safe import of the latest DigiSeller chat messages into the local database. "
            "At most one read call is made per selected chat."
        )
    )
    parser.add_argument(
        "--max-chats",
        type=int,
        default=HARD_MAX_CHATS,
        help=f"maximum chats to read (hard cap: {HARD_MAX_CHATS})",
    )
    parser.add_argument("--database", type=Path, help="override DATABASE_PATH")
    parser.add_argument("--dry-run", action="store_true", help="show selected invoice IDs without API calls")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings(database_path=str(args.database) if args.database else "data/reseller.sqlite3") if args.database else Settings()
    db = Database(settings.database_file)
    db.init()
    invoice_ids = collect_known_invoice_ids(db, max_chats=args.max_chats)
    print(f"Selected DigiSeller chats: {len(invoice_ids)} (hard cap {HARD_MAX_CHATS})")
    if args.dry_run:
        for invoice_id in invoice_ids:
            print(invoice_id)
        return 0
    if not invoice_ids:
        print("Nothing to backfill.")
        return 0

    runtime = RuntimeConfig(settings=settings, db=db)
    if not runtime.get_text("digiseller_seller_id") or not runtime.get_text("digiseller_api_key"):
        print("DigiSeller seller ID/API key are not configured.", file=sys.stderr)
        return 2
    summary = await backfill_chat_history(db, RuntimeDigisellerClient(runtime), invoice_ids)
    print(
        "Backfill finished: "
        f"requests={summary.requested_chats}, received={summary.received_messages}, "
        f"inserted={summary.inserted_messages}, existing={summary.existing_messages}, "
        f"failed={len(summary.failed_chats)}"
    )
    for invoice_id, error in summary.failed_chats:
        print(f"FAILED {invoice_id}: {error}", file=sys.stderr)
    return 1 if summary.failed_chats else 0


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
