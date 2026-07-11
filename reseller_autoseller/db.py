from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


PRODUCT_MAPPINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_product_id TEXT NOT NULL,
    external_variant_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT 'create',
    action_params TEXT NOT NULL DEFAULT '{}',
    tariff_code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    delivery_template TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (marketplace, external_product_id, external_variant_id)
);
"""

SCHEMA = """

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    external_product_id TEXT NOT NULL,
    external_variant_id TEXT NOT NULL DEFAULT '',
    buyer_email TEXT,
    buyer_name TEXT,
    amount TEXT,
    currency TEXT,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processing_token TEXT NOT NULL DEFAULT '',
    processing_started_at TEXT NOT NULL DEFAULT '',
    UNIQUE (marketplace, external_order_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_mapping_id INTEGER NOT NULL,
    xyranet_order_id TEXT NOT NULL,
    subscription_url TEXT NOT NULL,
    panel_username TEXT NOT NULL,
    tariff_code TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    delivery_text TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    marketplace_message_status TEXT NOT NULL DEFAULT 'pending',
    marketplace_message_claim_token TEXT NOT NULL DEFAULT '',
    marketplace_message_claimed_at TEXT NOT NULL DEFAULT '',
    marketplace_message_sent_at TEXT NOT NULL DEFAULT '',
    marketplace_message_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (sale_id) REFERENCES sales(id),
    FOREIGN KEY (product_mapping_id) REFERENCES product_mappings(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS deliveries_sale_id_unique
ON deliveries (sale_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_users (
    telegram_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_mapping_id INTEGER NOT NULL,
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    action TEXT NOT NULL,
    action_params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    target_order_id TEXT NOT NULL DEFAULT '',
    last_message_id TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    processing_token TEXT NOT NULL DEFAULT '',
    request_message_status TEXT NOT NULL DEFAULT 'pending',
    request_message_claim_token TEXT NOT NULL DEFAULT '',
    request_message_claimed_at TEXT NOT NULL DEFAULT '',
    request_message_sent_at TEXT NOT NULL DEFAULT '',
    request_message_error TEXT NOT NULL DEFAULT '',
    UNIQUE (sale_id),
    FOREIGN KEY (sale_id) REFERENCES sales(id),
    FOREIGN KEY (product_mapping_id) REFERENCES product_mappings(id)
);

CREATE TABLE IF NOT EXISTS marketplace_chat_cursors (
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (marketplace, external_order_id)
);

CREATE TABLE IF NOT EXISTS marketplace_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    external_message_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    author_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    is_file INTEGER NOT NULL DEFAULT 0,
    file_name TEXT NOT NULL DEFAULT '',
    file_url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    message_date TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (marketplace, external_order_id, message_key)
);

CREATE INDEX IF NOT EXISTS marketplace_chat_messages_order_idx
ON marketplace_chat_messages (marketplace, external_order_id, id);

CREATE TABLE IF NOT EXISTS quick_reply_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_reply_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    author_name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    template_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft',
    claim_token TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (template_id) REFERENCES quick_reply_templates(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS chat_reply_drafts_user_idx
ON chat_reply_drafts (telegram_user_id, status, id);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_order_id TEXT NOT NULL,
    sale_id INTEGER,
    pending_operation_id INTEGER,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS order_events_order_idx
ON order_events (marketplace, external_order_id, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_before(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=max(0, seconds))).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Schema migrations deliberately run with FK enforcement disabled: a
        # product_mappings upgrade has to replace the parent table while its
        # historical rows are still referenced. Normal application
        # connections enable enforcement in ``connect`` below.
        with self._connect(enforce_foreign_keys=False) as conn:
            self._ensure_product_mappings(conn)
            conn.executescript(SCHEMA)
            self._ensure_sales_variant(conn)
            self._ensure_sales_processing(conn)
            self._ensure_delivery_action(conn)
            self._ensure_delivery_message_state(conn)
            self._ensure_pending_operations(conn)
            self._repair_product_mapping_foreign_keys(conn)
            self._normalize_delivery_texts(conn)

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_product_mappings(self, conn: sqlite3.Connection) -> None:
        if not self._table_exists(conn, "product_mappings"):
            conn.executescript(PRODUCT_MAPPINGS_SCHEMA)
            return

        columns = self._columns(conn, "product_mappings")
        if "external_variant_id" in columns:
            if "action" not in columns:
                conn.execute("ALTER TABLE product_mappings ADD COLUMN action TEXT NOT NULL DEFAULT 'create'")
            if "action_params" not in columns:
                conn.execute("ALTER TABLE product_mappings ADD COLUMN action_params TEXT NOT NULL DEFAULT '{}'")
            return

        # Do not rename the old parent table. On modern SQLite versions a
        # parent rename also rewrites child-table FK declarations, which left
        # them pointing at product_mappings_legacy after that table was
        # dropped. Build the replacement alongside it instead.
        replacement = f"product_mappings_migration_{uuid4().hex}"
        replacement_schema = PRODUCT_MAPPINGS_SCHEMA.replace(
            "CREATE TABLE IF NOT EXISTS product_mappings",
            f'CREATE TABLE "{replacement}"',
            1,
        )
        conn.execute(replacement_schema)
        action_expr = "action" if "action" in columns else "'create'"
        action_params_expr = "action_params" if "action_params" in columns else "'{}'"
        conn.execute(
            f"""
            INSERT INTO "{replacement}"
                (id, marketplace, external_product_id, external_variant_id, action, action_params,
                 tariff_code, title, enabled, delivery_template, created_at, updated_at)
            SELECT id, marketplace, external_product_id, '', {action_expr}, {action_params_expr},
                   tariff_code, title, enabled, delivery_template, created_at, updated_at
            FROM product_mappings
            """
        )
        conn.execute("DROP TABLE product_mappings")
        conn.execute(f'ALTER TABLE "{replacement}" RENAME TO product_mappings')

    def _ensure_sales_variant(self, conn: sqlite3.Connection) -> None:
        if "external_variant_id" not in self._columns(conn, "sales"):
            conn.execute("ALTER TABLE sales ADD COLUMN external_variant_id TEXT NOT NULL DEFAULT ''")

    def _ensure_sales_processing(self, conn: sqlite3.Connection) -> None:
        columns = self._columns(conn, "sales")
        if "processing_token" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN processing_token TEXT NOT NULL DEFAULT ''")
        if "processing_started_at" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN processing_started_at TEXT NOT NULL DEFAULT ''")

    def _ensure_delivery_action(self, conn: sqlite3.Connection) -> None:
        columns = self._columns(conn, "deliveries")
        if "action" in columns:
            return
        conn.execute("ALTER TABLE deliveries ADD COLUMN action TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE deliveries
            SET action=COALESCE(
                (SELECT pm.action FROM product_mappings pm WHERE pm.id=deliveries.product_mapping_id),
                ''
            )
            """
        )

    def _ensure_delivery_message_state(self, conn: sqlite3.Connection) -> None:
        columns = self._columns(conn, "deliveries")
        existing_rows_have_unknown_state = "marketplace_message_status" not in columns
        additions = {
            "marketplace_message_status": "TEXT NOT NULL DEFAULT 'pending'",
            "marketplace_message_claim_token": "TEXT NOT NULL DEFAULT ''",
            "marketplace_message_claimed_at": "TEXT NOT NULL DEFAULT ''",
            "marketplace_message_sent_at": "TEXT NOT NULL DEFAULT ''",
            "marketplace_message_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE deliveries ADD COLUMN {name} {definition}")
        if existing_rows_have_unknown_state:
            # Old versions saved a delivery only after attempting the marketplace
            # response. Treat historical rows as sent so an upgrade cannot spam
            # every old buyer on the next duplicate notification.
            cursor = conn.execute(
                """
                UPDATE deliveries
                SET marketplace_message_status='sent',
                    marketplace_message_sent_at=created_at
                """
            )

    def _ensure_pending_operations(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)
        columns = self._columns(conn, "pending_operations")
        if "processing_token" not in columns:
            conn.execute("ALTER TABLE pending_operations ADD COLUMN processing_token TEXT NOT NULL DEFAULT ''")
        existing_rows_have_unknown_message_state = "request_message_status" not in columns
        additions = {
            "request_message_status": "TEXT NOT NULL DEFAULT 'pending'",
            "request_message_claim_token": "TEXT NOT NULL DEFAULT ''",
            "request_message_claimed_at": "TEXT NOT NULL DEFAULT ''",
            "request_message_sent_at": "TEXT NOT NULL DEFAULT ''",
            "request_message_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE pending_operations ADD COLUMN {name} {definition}")
        if existing_rows_have_unknown_message_state:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET request_message_status='sent',
                    request_message_sent_at=created_at
                """
            )

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _repair_product_mapping_foreign_keys(self, conn: sqlite3.Connection) -> None:
        # Releases affected by the old parent-table migration can have one or
        # both child tables referencing a table that no longer exists. Rebuild
        # only those tables; the original CREATE statement keeps every column
        # and constraint, while explicit indexes/triggers are restored below.
        for table in ("deliveries", "pending_operations"):
            if not self._table_exists(conn, table):
                continue
            targets = {
                str(row["table"])
                for row in conn.execute(
                    f"PRAGMA foreign_key_list({self._quote_identifier(table)})"
                ).fetchall()
            }
            if "product_mappings_legacy" in targets:
                self._rebuild_table_with_repaired_product_mapping_fk(conn, table)

    def _rebuild_table_with_repaired_product_mapping_fk(
        self,
        conn: sqlite3.Connection,
        table: str,
    ) -> None:
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not table_row or not table_row["sql"]:
            raise RuntimeError(f"Cannot repair SQLite schema for {table}")

        original_sql = str(table_row["sql"])
        reference_pattern = re.compile(
            r"(\bREFERENCES\s+)"
            r"(?:\"product_mappings_legacy\"|`product_mappings_legacy`|"
            r"\[product_mappings_legacy\]|'product_mappings_legacy'|product_mappings_legacy)"
            r"(?=\s*\()",
            re.IGNORECASE,
        )
        corrected_sql, replacement_count = reference_pattern.subn(
            r'\1"product_mappings"',
            original_sql,
        )
        if replacement_count == 0:
            raise RuntimeError(f"Cannot find broken product mapping reference in {table}")

        temporary_table = f"__{table}_fk_repair_{uuid4().hex}"
        quoted_table_pattern = (
            rf'(?:"{re.escape(table)}"|`{re.escape(table)}`|'
            rf"\[{re.escape(table)}\]|'{re.escape(table)}'|{re.escape(table)})"
        )
        create_pattern = re.compile(
            rf"^(\s*CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?{quoted_table_pattern}",
            re.IGNORECASE,
        )
        temporary_sql, create_replacement_count = create_pattern.subn(
            lambda match: match.group(1) + self._quote_identifier(temporary_table),
            corrected_sql,
            count=1,
        )
        if create_replacement_count != 1:
            raise RuntimeError(f"Cannot prepare repaired SQLite schema for {table}")

        schema_objects = conn.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE tbl_name=? AND type IN ('index', 'trigger') AND sql IS NOT NULL
            ORDER BY type, name
            """,
            (table,),
        ).fetchall()
        table_info = conn.execute(
            f"PRAGMA table_xinfo({self._quote_identifier(table)})"
        ).fetchall()
        copied_columns = [
            str(row["name"])
            for row in table_info
            if int(row["hidden"] if "hidden" in row.keys() else 0) == 0
        ]
        if not copied_columns:
            raise RuntimeError(f"Cannot copy SQLite rows while repairing {table}")

        original_row_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {self._quote_identifier(table)}"
            ).fetchone()[0]
        )
        conn.execute(temporary_sql)
        column_list = ", ".join(self._quote_identifier(name) for name in copied_columns)
        conn.execute(
            f"INSERT INTO {self._quote_identifier(temporary_table)} ({column_list}) "
            f"SELECT {column_list} FROM {self._quote_identifier(table)}"
        )
        copied_row_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {self._quote_identifier(temporary_table)}"
            ).fetchone()[0]
        )
        if copied_row_count != original_row_count:
            raise RuntimeError(
                f"SQLite schema repair for {table} copied {copied_row_count} "
                f"of {original_row_count} rows"
            )
        conn.execute(f"DROP TABLE {self._quote_identifier(table)}")
        conn.execute(
            f"ALTER TABLE {self._quote_identifier(temporary_table)} "
            f"RENAME TO {self._quote_identifier(table)}"
        )
        for schema_object in schema_objects:
            conn.execute(str(schema_object["sql"]))

    def _normalize_delivery_texts(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE deliveries
            SET delivery_text = REPLACE(delivery_text, 'ID заказа XyraNet:', 'ID заказа:')
            WHERE delivery_text LIKE '%ID заказа XyraNet:%'
            """
        )

    @contextmanager
    def _connect(self, *, enforce_foreign_keys: bool) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA foreign_keys={'ON' if enforce_foreign_keys else 'OFF'}")
        actual_foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if actual_foreign_keys != int(enforce_foreign_keys):
            conn.close()
            raise RuntimeError("SQLite did not apply the requested foreign-key mode")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._connect(enforce_foreign_keys=True) as conn:
            yield conn

    def list_products(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM product_mappings
                ORDER BY marketplace, external_product_id, external_variant_id, title, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM product_mappings WHERE id=?", (product_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _json_text(value: Any) -> str:
        if isinstance(value, str):
            try:
                parsed = json.loads(value or "{}")
                return json.dumps(parsed if isinstance(parsed, dict) else {}, ensure_ascii=False, sort_keys=True)
            except ValueError:
                return "{}"
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return "{}"

    def upsert_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO product_mappings
                    (marketplace, external_product_id, external_variant_id, action, action_params, tariff_code,
                     title, enabled, delivery_template, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(marketplace, external_product_id, external_variant_id) DO UPDATE SET
                    action=excluded.action,
                    action_params=excluded.action_params,
                    tariff_code=excluded.tariff_code,
                    title=excluded.title,
                    enabled=excluded.enabled,
                    delivery_template=excluded.delivery_template,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["marketplace"],
                    payload["external_product_id"],
                    payload.get("external_variant_id") or "",
                    payload.get("action") or "create",
                    self._json_text(payload.get("action_params")),
                    payload["tariff_code"],
                    payload.get("title") or payload["external_product_id"],
                    1 if payload.get("enabled", True) else 0,
                    payload.get("delivery_template") or "",
                    now,
                    now,
                ),
            )

            row = conn.execute(
                """
                SELECT * FROM product_mappings
                WHERE marketplace=? AND external_product_id=? AND external_variant_id=?
                """,
                (payload["marketplace"], payload["external_product_id"], payload.get("external_variant_id") or ""),
            ).fetchone()
            return dict(row)

    def set_product_enabled(self, product_id: int, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE product_mappings SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, utcnow(), product_id),
            )
            row = conn.execute("SELECT * FROM product_mappings WHERE id=?", (product_id,)).fetchone()
            return dict(row) if row else None

    def update_product(self, product_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE product_mappings
                SET marketplace=?,
                    external_product_id=?,
                    external_variant_id=?,
                    action=?,
                    action_params=?,
                    tariff_code=?,
                    title=?,
                    enabled=?,
                    delivery_template=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    payload["marketplace"],
                    payload["external_product_id"],
                    payload.get("external_variant_id") or "",
                    payload.get("action") or "create",
                    self._json_text(payload.get("action_params")),
                    payload["tariff_code"],
                    payload.get("title") or payload["external_product_id"],
                    1 if payload.get("enabled", True) else 0,
                    payload.get("delivery_template") or "",
                    now,
                    product_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM product_mappings WHERE id=?", (product_id,)).fetchone()
            return dict(row) if row else None

    def delete_product(self, product_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM product_mappings WHERE id=?", (product_id,))
            return cursor.rowcount > 0

    def get_product_by_external(
        self,
        marketplace: str,
        external_product_id: str,
        external_variant_id: str = "",
    ) -> dict[str, Any] | None:
        # Digiseller is the API behind Plati.Market.  Older releases allowed
        # mappings to be stored under either name, while incoming sales are
        # canonicalized to ``plati``.  Prefer the canonical row, but keep old
        # databases working without a destructive migration (there may be a
        # row under both names).
        marketplaces = (
            ("plati", "digiseller")
            if str(marketplace).strip().lower() in {"plati", "digiseller"}
            else (marketplace,)
        )
        with self.connect() as conn:
            if external_variant_id:
                for candidate in marketplaces:
                    row = conn.execute(
                        """
                        SELECT * FROM product_mappings
                        WHERE marketplace=? AND external_product_id=? AND external_variant_id=?
                        """,
                        (candidate, external_product_id, external_variant_id),
                    ).fetchone()
                    if row:
                        return dict(row)
            for candidate in marketplaces:
                row = conn.execute(
                    """
                    SELECT * FROM product_mappings
                    WHERE marketplace=? AND external_product_id=? AND external_variant_id=''
                    """,
                    (candidate, external_product_id),
                ).fetchone()
                if row:
                    return dict(row)
            return None

    def get_sale_with_delivery(self, marketplace: str, external_order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, d.id AS delivery_id, d.xyranet_order_id, d.subscription_url,
                       d.panel_username, d.tariff_code AS delivered_tariff_code, d.delivery_text,
                       d.raw_response AS delivery_raw_response,
                       d.marketplace_message_status, d.marketplace_message_claim_token,
                       d.marketplace_message_claimed_at, d.marketplace_message_sent_at,
                       d.marketplace_message_error
                FROM sales s
                LEFT JOIN deliveries d ON d.sale_id = s.id
                WHERE s.marketplace=? AND s.external_order_id=?
                """,
                (marketplace, external_order_id),
            ).fetchone()
            return dict(row) if row else None

    def get_sale_with_delivery_by_id(self, sale_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, d.id AS delivery_id, d.xyranet_order_id, d.subscription_url,
                       d.panel_username, d.tariff_code AS delivered_tariff_code, d.delivery_text,
                       d.raw_response AS delivery_raw_response,
                       d.marketplace_message_status, d.marketplace_message_claim_token,
                       d.marketplace_message_claimed_at, d.marketplace_message_sent_at,
                       d.marketplace_message_error
                FROM sales s
                LEFT JOIN deliveries d ON d.sale_id = s.id
                WHERE s.id=?
                """,
                (sale_id,),
            ).fetchone()
            return dict(row) if row else None

    def digiseller_invoice_has_delivery(self, invoice_id: str) -> bool:
        invoice = str(invoice_id or "").strip()
        if not invoice:
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM sales s
                JOIN deliveries d ON d.sale_id = s.id
                WHERE s.marketplace IN ('plati', 'digiseller')
                  AND (s.external_order_id=? OR s.external_order_id LIKE ?)
                LIMIT 1
                """,
                (invoice, f"{invoice}:%"),
            ).fetchone()
            return row is not None

    def marketplace_chat_owns_order(self, marketplace: str, external_order_id: str, xyranet_order_id: str) -> bool:
        selected_marketplace = str(marketplace or "").strip().lower()
        chat_id = str(external_order_id or "").strip()
        order_id = str(xyranet_order_id or "").strip()
        if not selected_marketplace or not chat_id or not order_id:
            return False
        marketplaces = ("plati", "digiseller") if selected_marketplace in {"plati", "digiseller"} else (selected_marketplace,)
        placeholders = ", ".join("?" for _ in marketplaces)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM sales s
                JOIN deliveries d ON d.sale_id=s.id
                WHERE s.marketplace IN ({placeholders})
                  AND d.xyranet_order_id=?
                  AND d.action='create'
                  AND (
                    s.external_order_id=?
                    OR substr(s.external_order_id, 1, length(?) + 1)=? || ':'
                  )
                LIMIT 1
                """,
                (*marketplaces, order_id, chat_id, chat_id, chat_id),
            ).fetchone()
            return row is not None

    def create_sale(self, event: Any) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sales
                    (marketplace, external_order_id, external_product_id, external_variant_id, buyer_email, buyer_name,
                     amount, currency, raw_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.marketplace,
                    event.external_order_id,
                    event.external_product_id,
                    event.external_variant_id,
                    event.buyer_email,
                    event.buyer_name,
                    event.amount,
                    event.currency,
                    json.dumps(event.raw_payload, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM sales WHERE marketplace=? AND external_order_id=?",
                (event.marketplace, event.external_order_id),
            ).fetchone()
            return dict(row)

    def claim_sale_processing(self, sale_id: int, *, stale_after_seconds: int = 7200) -> str | None:
        token = uuid4().hex
        now = utcnow()
        stale_before = utc_before(stale_after_seconds)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sales
                SET processing_token=?, processing_started_at=?
                WHERE id=?
                  AND (
                    processing_token=''
                    OR processing_started_at=''
                    OR processing_started_at<=?
                  )
                """,
                (token, now, sale_id, stale_before),
            )
            return token if cursor.rowcount == 1 else None

    def refresh_sale_processing(self, sale_id: int, token: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE sales SET processing_started_at=? WHERE id=? AND processing_token=?",
                (utcnow(), sale_id, token),
            )
            return cursor.rowcount == 1

    def release_sale_processing(self, sale_id: int, token: str) -> None:
        if not token:
            return
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sales
                SET processing_token='', processing_started_at=''
                WHERE id=? AND processing_token=?
                """,
                (sale_id, token),
            )

    def create_delivery(self, sale_id: int, product_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO deliveries
                    (sale_id, product_mapping_id, xyranet_order_id, subscription_url, panel_username,
                     tariff_code, action, delivery_text, raw_response, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    product_id,
                    payload["xyranet_order_id"],
                    payload["subscription_url"],
                    payload["panel_username"],
                    payload["tariff_code"],
                    payload.get("action")
                    or str(
                        (
                            conn.execute("SELECT action FROM product_mappings WHERE id=?", (product_id,)).fetchone()
                            or {"action": ""}
                        )["action"]
                    ),
                    payload["delivery_text"],
                    json.dumps(payload["raw_response"], ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM deliveries WHERE sale_id=? ORDER BY id LIMIT 1",
                (sale_id,),
            ).fetchone()
            return dict(row)

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            return dict(row) if row else None

    def claim_delivery_message(self, delivery_id: int, *, stale_after_seconds: int = 600) -> str | None:
        token = uuid4().hex
        now = utcnow()
        stale_before = utc_before(stale_after_seconds)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE deliveries
                SET marketplace_message_status='sending',
                    marketplace_message_claim_token=?,
                    marketplace_message_claimed_at=?,
                    marketplace_message_error=''
                WHERE id=?
                  AND marketplace_message_status!='sent'
                  AND (
                    marketplace_message_status!='sending'
                    OR marketplace_message_claimed_at=''
                    OR marketplace_message_claimed_at<=?
                  )
                """,
                (token, now, delivery_id, stale_before),
            )
            return token if cursor.rowcount == 1 else None

    def mark_delivery_message_sent(self, delivery_id: int, token: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE deliveries
                SET marketplace_message_status='sent',
                    marketplace_message_claim_token='',
                    marketplace_message_claimed_at='',
                    marketplace_message_sent_at=?,
                    marketplace_message_error=''
                WHERE id=? AND marketplace_message_claim_token=?
                """,
                (utcnow(), delivery_id, token),
            )
            return cursor.rowcount == 1

    def mark_delivery_message_failed(self, delivery_id: int, token: str, error_text: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE deliveries
                SET marketplace_message_status='pending',
                    marketplace_message_claim_token='',
                    marketplace_message_claimed_at='',
                    marketplace_message_error=?
                WHERE id=? AND marketplace_message_claim_token=?
                """,
                (error_text, delivery_id, token),
            )
            return cursor.rowcount == 1

    def create_pending_operation(
        self,
        *,
        sale_id: int,
        product_id: int,
        marketplace: str,
        external_order_id: str,
        action: str,
        action_params: dict[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_operations
                    (sale_id, product_mapping_id, marketplace, external_order_id, action,
                     action_params, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'waiting_order_id', ?, ?)
                ON CONFLICT(sale_id) DO NOTHING
                """,
                (
                    sale_id,
                    product_id,
                    marketplace,
                    external_order_id,
                    action,
                    json.dumps(action_params, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM pending_operations WHERE sale_id=?", (sale_id,)).fetchone()
            return dict(row)

    def claim_pending_request_message(self, operation_id: int, *, stale_after_seconds: int = 600) -> str | None:
        token = uuid4().hex
        now = utcnow()
        stale_before = utc_before(stale_after_seconds)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET request_message_status='sending',
                    request_message_claim_token=?,
                    request_message_claimed_at=?,
                    request_message_error=''
                WHERE id=?
                  AND request_message_status!='sent'
                  AND (
                    request_message_status!='sending'
                    OR request_message_claimed_at=''
                    OR request_message_claimed_at<=?
                  )
                """,
                (token, now, operation_id, stale_before),
            )
            return token if cursor.rowcount == 1 else None

    def mark_pending_request_message_sent(self, operation_id: int, token: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET request_message_status='sent',
                    request_message_claim_token='',
                    request_message_claimed_at='',
                    request_message_sent_at=?,
                    request_message_error=''
                WHERE id=? AND request_message_claim_token=?
                """,
                (utcnow(), operation_id, token),
            )
            return cursor.rowcount == 1

    def mark_pending_request_message_failed(self, operation_id: int, token: str, error_text: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET request_message_status='pending',
                    request_message_claim_token='',
                    request_message_claimed_at='',
                    request_message_error=?
                WHERE id=? AND request_message_claim_token=?
                """,
                (error_text, operation_id, token),
            )
            return cursor.rowcount == 1

    def get_pending_operation_by_sale(self, sale_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pending_operations WHERE sale_id=?", (sale_id,)).fetchone()
            return dict(row) if row else None

    def get_pending_operation(self, operation_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def claim_pending_operation(
        self,
        operation_id: int,
        *,
        target_order_id: str,
        stale_after_seconds: int = 7200,
    ) -> dict[str, Any] | None:
        token = uuid4().hex
        now = utcnow()
        stale_before = utc_before(stale_after_seconds)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET status='processing',
                    target_order_id=?,
                    processing_token=?,
                    error_text='',
                    updated_at=?
                WHERE id=?
                  AND (
                    status IN ('waiting_order_id', 'error')
                    OR (status='processing' AND updated_at<=?)
                  )
                """,
                (target_order_id, token, now, operation_id, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def refresh_pending_processing(self, operation_id: int, token: str) -> bool:
        if not token:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE pending_operations SET updated_at=? WHERE id=? AND processing_token=? AND status='processing'",
                (utcnow(), operation_id, token),
            )
            return cursor.rowcount == 1

    def list_pending_operations(self, status: str | None = "waiting_order_id") -> list[dict[str, Any]]:
        with self.connect() as conn:
            where = "WHERE p.status=?" if status else ""
            params: tuple[Any, ...] = (status,) if status else ()
            rows = conn.execute(
                f"""
                SELECT p.*, s.raw_payload
                FROM pending_operations p
                JOIN sales s ON s.id = p.sale_id
                {where}
                ORDER BY p.id
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def recover_stale_pending_operations(self, *, stale_after_seconds: int = 7200) -> list[dict[str, Any]]:
        stale_before = utc_before(stale_after_seconds)
        now = utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_operations
                WHERE status='processing' AND updated_at<=?
                ORDER BY id
                """,
                (stale_before,),
            ).fetchall()
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE pending_operations
                SET status='waiting_order_id',
                    processing_token='',
                    error_text='Recovered stale processing claim',
                    updated_at=?
                WHERE status='processing'
                  AND updated_at<=?
                  AND id IN ({placeholders})
                """,
                (now, stale_before, *ids),
            )
            recovered = conn.execute(
                f"SELECT * FROM pending_operations WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
            return [dict(row) for row in recovered if str(row["status"]) == "waiting_order_id"]

    def update_pending_last_message(self, operation_id: int, last_message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pending_operations SET last_message_id=?, updated_at=? WHERE id=?",
                (last_message_id, utcnow(), operation_id),
            )

    def complete_pending_operation(
        self,
        operation_id: int,
        *,
        target_order_id: str,
        result_text: str,
        raw_response: Any,
        claim_token: str = "",
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            token_clause = " AND processing_token=?" if claim_token else ""
            params: list[Any] = [
                target_order_id,
                result_text,
                json.dumps(raw_response, ensure_ascii=False, sort_keys=True),
                utcnow(),
                operation_id,
            ]
            if claim_token:
                params.append(claim_token)
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET status='completed',
                    target_order_id=?,
                    result_text=?,
                    raw_response=?,
                    error_text='',
                    processing_token='',
                    updated_at=?
                WHERE id=?
                """ + token_clause,
                params,
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def fail_pending_operation(self, operation_id: int, error_text: str, *, claim_token: str = "") -> dict[str, Any] | None:
        with self.connect() as conn:
            token_clause = " AND processing_token=?" if claim_token else ""
            params: list[Any] = [error_text, utcnow(), operation_id]
            if claim_token:
                params.append(claim_token)
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET status='error', error_text=?, processing_token='', updated_at=?
                WHERE id=?
                """ + token_clause,
                params,
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def retry_pending_operation(self, operation_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_operations
                SET status='waiting_order_id',
                    error_text='',
                    processing_token='',
                    updated_at=?
                WHERE id=? AND status='error'
                """,
                (utcnow(), operation_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def get_chat_cursor(self, marketplace: str, external_order_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT last_message_id FROM marketplace_chat_cursors
                WHERE marketplace=? AND external_order_id=?
                """,
                (marketplace, external_order_id),
            ).fetchone()
            return str(row["last_message_id"]) if row else ""

    def set_chat_cursor(self, marketplace: str, external_order_id: str, last_message_id: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO marketplace_chat_cursors
                    (marketplace, external_order_id, last_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(marketplace, external_order_id) DO UPDATE SET
                    last_message_id=excluded.last_message_id,
                    updated_at=excluded.updated_at
                """,
                (marketplace, external_order_id, last_message_id, now),
            )

    def list_chat_cursors(self, marketplace: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        selected_limit = max(1, min(int(limit), 1000))
        if marketplace:
            query = """
                SELECT marketplace, external_order_id, last_message_id, updated_at
                FROM marketplace_chat_cursors
                WHERE marketplace=?
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (marketplace, selected_limit)
        else:
            query = """
                SELECT marketplace, external_order_id, last_message_id, updated_at
                FROM marketplace_chat_cursors
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params = (selected_limit,)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def add_chat_message(
        self,
        *,
        marketplace: str,
        external_order_id: str,
        role: str,
        text: str = "",
        external_message_id: str = "",
        message_key: str = "",
        author_name: str = "",
        source: str = "",
        message_date: str = "",
        is_file: bool = False,
        file_name: str = "",
        file_url: str = "",
        raw_payload: Any | None = None,
    ) -> tuple[dict[str, Any], bool]:
        selected_marketplace = str(marketplace or "").strip()
        selected_order_id = str(external_order_id or "").strip()
        if not selected_marketplace or not selected_order_id:
            raise ValueError("marketplace and external_order_id are required")
        selected_role = str(role or "system").strip().lower()
        if selected_role not in {"buyer", "seller", "admin", "bot", "system"}:
            selected_role = "system"
        remote_id = str(external_message_id or "").strip()
        selected_key = str(message_key or "").strip()
        if not selected_key:
            selected_key = f"remote:{remote_id}" if remote_id else f"local:{uuid4().hex}"
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO marketplace_chat_messages
                    (marketplace, external_order_id, message_key, external_message_id,
                     role, author_name, text, is_file, file_name, file_url, source,
                     message_date, raw_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selected_marketplace,
                    selected_order_id,
                    selected_key,
                    remote_id,
                    selected_role,
                    str(author_name or "").strip(),
                    str(text or ""),
                    1 if is_file else 0,
                    str(file_name or "").strip(),
                    str(file_url or "").strip(),
                    str(source or "").strip(),
                    str(message_date or "").strip(),
                    json.dumps(raw_payload or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM marketplace_chat_messages
                WHERE marketplace=? AND external_order_id=? AND message_key=?
                """,
                (selected_marketplace, selected_order_id, selected_key),
            ).fetchone()
            if not row:
                raise RuntimeError("Cannot save marketplace chat message")
            return dict(row), cursor.rowcount > 0

    def get_chat_message(self, message_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM marketplace_chat_messages WHERE id=?",
                (int(message_id),),
            ).fetchone()
            return dict(row) if row else None

    def list_chat_messages(
        self,
        marketplace: str,
        external_order_id: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        selected_limit = max(1, min(int(limit), 2000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM marketplace_chat_messages
                WHERE marketplace=? AND external_order_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(marketplace), str(external_order_id), selected_limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def count_chat_messages(self, marketplace: str, external_order_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(1) AS cnt FROM marketplace_chat_messages
                WHERE marketplace=? AND external_order_id=?
                """,
                (str(marketplace), str(external_order_id)),
            ).fetchone()
            return int(row["cnt"] if row else 0)

    def find_recent_chat_message_by_text(
        self,
        marketplace: str,
        external_order_id: str,
        text: str,
        *,
        roles: tuple[str, ...] = ("bot", "admin", "seller"),
        within_seconds: int = 300,
    ) -> dict[str, Any] | None:
        if not roles:
            return None
        placeholders = ",".join("?" for _ in roles)
        query = f"""
            SELECT * FROM marketplace_chat_messages
            WHERE marketplace=? AND external_order_id=? AND text=?
              AND role IN ({placeholders}) AND created_at>=?
            ORDER BY id DESC LIMIT 1
        """
        params: tuple[Any, ...] = (
            str(marketplace),
            str(external_order_id),
            str(text),
            *roles,
            utc_before(within_seconds),
        )
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def list_quick_reply_templates(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM quick_reply_templates"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query)]

    def get_quick_reply_template(self, template_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quick_reply_templates WHERE id=?",
                (int(template_id),),
            ).fetchone()
            return dict(row) if row else None

    def create_quick_reply_template(self, title: str, body: str, *, created_by: int | None = None) -> dict[str, Any]:
        selected_title = str(title or "").strip()
        selected_body = str(body or "").strip()
        if not selected_title or not selected_body:
            raise ValueError("template title and body are required")
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO quick_reply_templates
                    (title, body, enabled, created_by, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (selected_title, selected_body, created_by, now, now),
            )
            row = conn.execute("SELECT * FROM quick_reply_templates WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def update_quick_reply_template(
        self,
        template_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_quick_reply_template(template_id)
        if not current:
            return None
        selected_title = str(current["title"] if title is None else title).strip()
        selected_body = str(current["body"] if body is None else body).strip()
        if not selected_title or not selected_body:
            raise ValueError("template title and body are required")
        selected_enabled = int(current["enabled"]) if enabled is None else (1 if enabled else 0)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE quick_reply_templates
                SET title=?, body=?, enabled=?, updated_at=? WHERE id=?
                """,
                (selected_title, selected_body, selected_enabled, utcnow(), int(template_id)),
            )
            row = conn.execute("SELECT * FROM quick_reply_templates WHERE id=?", (int(template_id),)).fetchone()
            return dict(row) if row else None

    def delete_quick_reply_template(self, template_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM quick_reply_templates WHERE id=?", (int(template_id),))
            return cursor.rowcount > 0

    def create_chat_reply_draft(
        self,
        *,
        marketplace: str,
        external_order_id: str,
        telegram_user_id: int,
        body: str,
        author_name: str = "",
        template_id: int | None = None,
    ) -> dict[str, Any]:
        selected_body = str(body or "").strip()
        if not selected_body:
            raise ValueError("reply body is required")
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_reply_drafts
                    (marketplace, external_order_id, telegram_user_id, author_name,
                     body, template_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    str(marketplace),
                    str(external_order_id),
                    int(telegram_user_id),
                    str(author_name or "").strip(),
                    selected_body,
                    template_id,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM chat_reply_drafts WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def get_chat_reply_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM chat_reply_drafts WHERE id=?", (int(draft_id),)).fetchone()
            return dict(row) if row else None

    def update_chat_reply_draft_body(self, draft_id: int, telegram_user_id: int, body: str) -> dict[str, Any] | None:
        selected_body = str(body or "").strip()
        if not selected_body:
            raise ValueError("reply body is required")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_reply_drafts SET body=?, updated_at=?
                WHERE id=? AND telegram_user_id=? AND status='draft'
                """,
                (selected_body, utcnow(), int(draft_id), int(telegram_user_id)),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM chat_reply_drafts WHERE id=?", (int(draft_id),)).fetchone()
            return dict(row) if row else None

    def claim_chat_reply_draft(self, draft_id: int, telegram_user_id: int) -> tuple[dict[str, Any], str] | None:
        token = uuid4().hex
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_reply_drafts
                SET status='sending', claim_token=?, error_text='', updated_at=?
                WHERE id=? AND telegram_user_id=? AND status='draft'
                """,
                (token, utcnow(), int(draft_id), int(telegram_user_id)),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM chat_reply_drafts WHERE id=?", (int(draft_id),)).fetchone()
            return (dict(row), token) if row else None

    def finish_chat_reply_draft(self, draft_id: int, token: str, *, status: str, error_text: str = "") -> bool:
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("unsupported reply draft status")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_reply_drafts
                SET status=?, error_text=?, claim_token='', updated_at=?
                WHERE id=? AND claim_token=? AND status='sending'
                """,
                (status, str(error_text or "")[:2000], utcnow(), int(draft_id), str(token)),
            )
            return cursor.rowcount > 0

    def cancel_chat_reply_draft(self, draft_id: int, telegram_user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_reply_drafts SET status='cancelled', updated_at=?
                WHERE id=? AND telegram_user_id=? AND status='draft'
                """,
                (utcnow(), int(draft_id), int(telegram_user_id)),
            )
            return cursor.rowcount > 0

    def add_order_event(
        self,
        *,
        marketplace: str,
        external_order_id: str,
        event_type: str,
        status: str = "info",
        message: str = "",
        sale_id: int | None = None,
        pending_operation_id: int | None = None,
        payload: Any | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_events
                    (marketplace, external_order_id, sale_id, pending_operation_id,
                     event_type, status, message, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    marketplace,
                    external_order_id,
                    sale_id,
                    pending_operation_id,
                    event_type,
                    status,
                    message,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )
            row = conn.execute("SELECT * FROM order_events WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def list_order_events(
        self,
        *,
        marketplace: str | None = None,
        external_order_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM order_events"
        params: list[Any] = []
        clauses: list[str] = []
        if marketplace:
            clauses.append("marketplace=?")
            params.append(marketplace)
        if external_order_id:
            clauses.append("external_order_id=?")
            params.append(external_order_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def list_sales(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, d.xyranet_order_id, d.subscription_url, d.tariff_code AS delivered_tariff_code
                FROM sales s
                LEFT JOIN deliveries d ON d.sale_id = s.id
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_sales_for_statistics(self, limit: int = 10000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.*,
                    d.id AS delivery_id,
                    d.xyranet_order_id,
                    d.subscription_url,
                    d.tariff_code AS delivered_tariff_code,
                    d.raw_response AS delivery_raw_response,
                    pm.action AS product_action,
                    pm.tariff_code AS mapped_tariff_code,
                    pm.title AS product_title
                FROM sales s
                LEFT JOIN deliveries d ON d.sale_id = s.id
                LEFT JOIN product_mappings pm ON pm.id = d.product_mapping_id
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def list_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings ORDER BY key").fetchall()
            return {str(row["key"]): str(row["value"]) for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, str]) -> None:
        if not values:
            return
        now = utcnow()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                [(key, value, now) for key, value in values.items()],
            )

    def list_bot_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM bot_users ORDER BY telegram_id").fetchall()
            return [dict(row) for row in rows]

    def upsert_bot_user(self, telegram_id: int, label: str = "", added_by: int | None = None) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_users (telegram_id, label, enabled, added_by, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    label=excluded.label,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, label, added_by, now, now),
            )
            row = conn.execute("SELECT * FROM bot_users WHERE telegram_id=?", (telegram_id,)).fetchone()
            return dict(row)

    def set_bot_user_enabled(self, telegram_id: int, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE bot_users SET enabled=?, updated_at=? WHERE telegram_id=?",
                (1 if enabled else 0, utcnow(), telegram_id),
            )
            row = conn.execute("SELECT * FROM bot_users WHERE telegram_id=?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    def delete_bot_user(self, telegram_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM bot_users WHERE telegram_id=?", (telegram_id,))
            return cursor.rowcount > 0
