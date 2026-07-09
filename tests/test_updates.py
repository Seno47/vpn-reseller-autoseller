from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reseller_autoseller.config import Settings
from reseller_autoseller.db import Database
from reseller_autoseller.updates import UpdateManager, github_repo_parts, version_newer


class UpdateTests(unittest.TestCase):
    def test_version_compare(self) -> None:
        self.assertTrue(version_newer("0.2.0", "0.1.9"))
        self.assertTrue(version_newer("v1.0.0", "0.9.9"))
        self.assertFalse(version_newer("0.1.0", "0.1.0"))

    def test_github_repo_parts(self) -> None:
        self.assertEqual(
            github_repo_parts("https://github.com/Seno47/vpn-reseller-autoseller.git"),
            ("Seno47", "vpn-reseller-autoseller"),
        )
        self.assertEqual(github_repo_parts("Seno47/vpn-reseller-autoseller"), ("Seno47", "vpn-reseller-autoseller"))

    def test_start_update_writes_trigger_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "data.sqlite3")
            db.init()
            settings = Settings(
                database_path=str(root / "data.sqlite3"),
                app_update_trigger_file="data/update-request.json",
                app_update_status_file="data/update-status.json",
            )
            manager = UpdateManager(settings=settings, db=db, app_root=root)
            result = manager.start_update()
            self.assertEqual(result["status"], "requested")
            request_path = root / "data" / "update-request.json"
            self.assertTrue(request_path.exists())
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["repo_url"], settings.app_update_repo_url)
            self.assertEqual(db.get_setting("update_last_request_id"), result["request_id"])


if __name__ == "__main__":
    unittest.main()
