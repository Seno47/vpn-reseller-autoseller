from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


class RuntimeMappingSyncError(RuntimeError):
    pass


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeMappingSyncError(f"Cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeMappingSyncError(f"{label} must contain a JSON object")
    return value


def expected_mapping_set(
    spec: dict[str, Any],
    state: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    offer_id = str(state.get("offer_id") or "").strip()
    if not offer_id.isdigit() or int(offer_id) <= 0:
        raise RuntimeMappingSyncError("Publish state has no positive GGSEL offer_id")

    variants = spec.get("variants")
    mappings = state.get("mappings")
    if not isinstance(variants, list) or not variants:
        raise RuntimeMappingSyncError("Offer spec has no variants")
    if not isinstance(mappings, list) or not mappings:
        raise RuntimeMappingSyncError("Publish state has no variant mappings")

    expected_codes = [str(item.get("tariff_code") or "").strip() for item in variants]
    if not all(expected_codes) or len(expected_codes) != len(set(expected_codes)):
        raise RuntimeMappingSyncError("Offer spec contains duplicate or empty tariff codes")

    by_code: dict[str, str] = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise RuntimeMappingSyncError("Publish state contains a non-object mapping")
        code = str(mapping.get("tariff_code") or "").strip()
        product_id = str(mapping.get("external_product_id") or offer_id).strip()
        variant_id = str(mapping.get("external_variant_id") or "").strip()
        if product_id != offer_id:
            raise RuntimeMappingSyncError("Publish-state mapping references another GGSEL offer")
        if not code or code in by_code or not variant_id.isdigit():
            raise RuntimeMappingSyncError("Publish state contains duplicate or invalid variant mappings")
        by_code[code] = variant_id

    if set(by_code) != set(expected_codes):
        raise RuntimeMappingSyncError("Publish-state tariffs differ from the offer specification")
    return offer_id, by_code


def synchronize_runtime_mappings(
    *,
    database_path: Path,
    spec_path: Path,
    state_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    spec = load_json_object(spec_path, label="offer specification")
    state = load_json_object(state_path, label="publish state")
    offer_id, expected_by_code = expected_mapping_set(spec, state)
    title = str((spec.get("offer") or {}).get("title_ru") or "").strip()
    delivery_template = str(spec.get("delivery_template_ru") or "").strip()
    if not title or not delivery_template:
        raise RuntimeMappingSyncError("Offer specification has no title or delivery template")
    if not database_path.is_file():
        raise RuntimeMappingSyncError(f"Runtime database does not exist: {database_path}")

    conn = sqlite3.connect(database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise RuntimeMappingSyncError("Runtime database failed PRAGMA quick_check")
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, external_variant_id, tariff_code, enabled
            FROM product_mappings
            WHERE marketplace='ggsel' AND external_product_id=?
            ORDER BY id
            """,
            (offer_id,),
        ).fetchall()
        actual_by_code = {str(row["tariff_code"]): str(row["external_variant_id"]) for row in rows}
        if len(rows) != len(actual_by_code) or actual_by_code != expected_by_code:
            raise RuntimeMappingSyncError(
                "Runtime GGSEL mappings differ from the exact publish-state tariff/variant set"
            )
        if not all(int(row["enabled"]) == 1 for row in rows):
            raise RuntimeMappingSyncError("One or more runtime GGSEL mappings are disabled")

        updated_at = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            UPDATE product_mappings
            SET title=?, delivery_template=?, updated_at=?
            WHERE marketplace='ggsel' AND external_product_id=?
            """,
            (title, delivery_template, updated_at, offer_id),
        )
        if cursor.rowcount != len(expected_by_code):
            raise RuntimeMappingSyncError("Runtime mapping update touched an unexpected row count")

        verified = conn.execute(
            """
            SELECT COUNT(*)
            FROM product_mappings
            WHERE marketplace='ggsel' AND external_product_id=?
              AND title=? AND delivery_template=? AND enabled=1
            """,
            (offer_id, title, delivery_template),
        ).fetchone()[0]
        if int(verified) != len(expected_by_code):
            raise RuntimeMappingSyncError("Runtime mapping readback verification failed")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            if str(conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                raise RuntimeMappingSyncError("Runtime database failed quick_check after commit")
        return {
            "offer_id": int(offer_id),
            "mapping_count": len(expected_by_code),
            "dry_run": dry_run,
            "committed": not dry_run,
            "database_quick_check": "ok",
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atomically synchronize GGSEL runtime mapping copy from the reviewed offer spec"
    )
    parser.add_argument("--database", type=Path, default=Path("data/reseller.sqlite3"))
    parser.add_argument("--spec", type=Path, required=True, help="Private offer spec JSON")
    parser.add_argument("--state", type=Path, default=Path("data/ggsel-publish-state.json"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        result = synchronize_runtime_mappings(
            database_path=arguments.database.resolve(),
            spec_path=arguments.spec.resolve(),
            state_path=arguments.state.resolve(),
            dry_run=arguments.dry_run,
        )
    except RuntimeMappingSyncError as exc:
        raise SystemExit(f"GGSEL runtime mapping sync failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
