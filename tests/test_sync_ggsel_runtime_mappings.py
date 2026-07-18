from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reseller_autoseller.db import Database
from scripts.sync_ggsel_runtime_mappings import (
    RuntimeMappingSyncError,
    synchronize_runtime_mappings,
)
from tests.ggsel_test_data import (
    TEST_OFFER_ID,
    TEST_VARIANT_ID_START,
    make_ggsel_offer_spec,
)


class SyncGgselRuntimeMappingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = make_ggsel_offer_spec()

    def prepare_files(self, root: Path) -> tuple[Path, Path, Path]:
        database_path = root / "runtime.sqlite3"
        spec_path = root / "spec.json"
        state_path = root / "state.json"
        spec_path.write_text(json.dumps(self.spec, ensure_ascii=False), encoding="utf-8")
        mappings = []
        database = Database(database_path)
        database.init()
        for index, variant in enumerate(self.spec["variants"]):
            variant_id = str(TEST_VARIANT_ID_START + index)
            mappings.append(
                {
                    "external_product_id": str(TEST_OFFER_ID),
                    "external_variant_id": variant_id,
                    "tariff_code": variant["tariff_code"],
                }
            )
            database.upsert_product(
                {
                    "marketplace": "ggsel",
                    "external_product_id": str(TEST_OFFER_ID),
                    "external_variant_id": variant_id,
                    "tariff_code": variant["tariff_code"],
                    "title": "Old title",
                    "delivery_template": "Old delivery",
                }
            )
        state_path.write_text(
            json.dumps({"offer_id": TEST_OFFER_ID, "mappings": mappings}),
            encoding="utf-8",
        )
        return database_path, spec_path, state_path

    def test_dry_run_rolls_back_and_commit_updates_exact_twelve_rows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path, spec_path, state_path = self.prepare_files(Path(temp_dir))

            dry_run = synchronize_runtime_mappings(
                database_path=database_path,
                spec_path=spec_path,
                state_path=state_path,
                dry_run=True,
            )
            self.assertFalse(dry_run["committed"])
            self.assertTrue(all(row["title"] == "Old title" for row in Database(database_path).list_products()))

            result = synchronize_runtime_mappings(
                database_path=database_path,
                spec_path=spec_path,
                state_path=state_path,
            )

            rows = Database(database_path).list_products()
            self.assertEqual(result["mapping_count"], 12)
            self.assertTrue(result["committed"])
            self.assertTrue(all(row["title"] == self.spec["offer"]["title_ru"] for row in rows))
            self.assertTrue(
                all(row["delivery_template"] == self.spec["delivery_template_ru"] for row in rows)
            )

    def test_mismatched_runtime_mapping_rolls_back_without_partial_copy_change(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path, spec_path, state_path = self.prepare_files(Path(temp_dir))
            database = Database(database_path)
            row = database.list_products()[0]
            database.update_product(
                int(row["id"]),
                {**row, "external_variant_id": "99999999"},
            )

            with self.assertRaisesRegex(RuntimeMappingSyncError, "differ from the exact"):
                synchronize_runtime_mappings(
                    database_path=database_path,
                    spec_path=spec_path,
                    state_path=state_path,
                )

            self.assertTrue(
                all(row["title"] == "Old title" for row in Database(database_path).list_products())
            )


if __name__ == "__main__":
    unittest.main()
