from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


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
    delivery_text TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    created_at TEXT NOT NULL,
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


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._ensure_product_mappings(conn)
            conn.executescript(SCHEMA)
            self._ensure_sales_variant(conn)
            self._ensure_pending_operations(conn)
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

        conn.execute("ALTER TABLE product_mappings RENAME TO product_mappings_legacy")
        conn.executescript(PRODUCT_MAPPINGS_SCHEMA)
        conn.execute(
            """
            INSERT INTO product_mappings
                (id, marketplace, external_product_id, external_variant_id, tariff_code,
                 title, enabled, delivery_template, created_at, updated_at)
            SELECT id, marketplace, external_product_id, '', tariff_code,
                   title, enabled, delivery_template, created_at, updated_at
            FROM product_mappings_legacy
            """
        )
        conn.execute("DROP TABLE product_mappings_legacy")

    def _ensure_sales_variant(self, conn: sqlite3.Connection) -> None:
        if "external_variant_id" not in self._columns(conn, "sales"):
            conn.execute("ALTER TABLE sales ADD COLUMN external_variant_id TEXT NOT NULL DEFAULT ''")

    def _ensure_pending_operations(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    def _normalize_delivery_texts(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE deliveries
            SET delivery_text = REPLACE(delivery_text, 'ID заказа XyraNet:', 'ID заказа:')
            WHERE delivery_text LIKE '%ID заказа XyraNet:%'
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_products(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM product_mappings
                ORDER BY marketplace, external_product_id, external_variant_id, title, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

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
        with self.connect() as conn:
            if external_variant_id:
                row = conn.execute(
                    """
                    SELECT * FROM product_mappings
                    WHERE marketplace=? AND external_product_id=? AND external_variant_id=?
                    """,
                    (marketplace, external_product_id, external_variant_id),
                ).fetchone()
                if row:
                    return dict(row)
            row = conn.execute(
                """
                SELECT * FROM product_mappings
                WHERE marketplace=? AND external_product_id=? AND external_variant_id=''
                """,
                (marketplace, external_product_id),
            ).fetchone()
            return dict(row) if row else None

    def get_sale_with_delivery(self, marketplace: str, external_order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, d.id AS delivery_id, d.xyranet_order_id, d.subscription_url,
                       d.panel_username, d.tariff_code AS delivered_tariff_code, d.delivery_text
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
                       d.panel_username, d.tariff_code AS delivered_tariff_code, d.delivery_text
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

    def create_delivery(self, sale_id: int, product_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO deliveries
                    (sale_id, product_mapping_id, xyranet_order_id, subscription_url, panel_username,
                     tariff_code, delivery_text, raw_response, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    product_id,
                    payload["xyranet_order_id"],
                    payload["subscription_url"],
                    payload["panel_username"],
                    payload["tariff_code"],
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
                ON CONFLICT(sale_id) DO UPDATE SET
                    product_mapping_id=excluded.product_mapping_id,
                    action=excluded.action,
                    action_params=excluded.action_params,
                    status='waiting_order_id',
                    updated_at=excluded.updated_at
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

    def get_pending_operation_by_sale(self, sale_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pending_operations WHERE sale_id=?", (sale_id,)).fetchone()
            return dict(row) if row else None

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
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pending_operations
                SET status='completed',
                    target_order_id=?,
                    result_text=?,
                    raw_response=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    target_order_id,
                    result_text,
                    json.dumps(raw_response, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                    operation_id,
                ),
            )
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def fail_pending_operation(self, operation_id: int, error_text: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pending_operations SET status='error', error_text=?, updated_at=? WHERE id=?",
                (error_text, utcnow(), operation_id),
            )
            row = conn.execute("SELECT * FROM pending_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def retry_pending_operation(self, operation_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pending_operations
                SET status='waiting_order_id',
                    error_text='',
                    updated_at=?
                WHERE id=?
                """,
                (utcnow(), operation_id),
            )
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
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utcnow()),
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
