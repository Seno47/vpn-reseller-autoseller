from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from reseller_autoseller.config import Settings
from reseller_autoseller.db import Database
from reseller_autoseller.updates import UpdateManager, git_commits_match, github_repo_parts, version_newer


class UpdateTests(unittest.TestCase):
    def test_version_compare(self) -> None:
        self.assertTrue(version_newer("0.2.0", "0.1.9"))
        self.assertTrue(version_newer("v1.0.0", "0.9.9"))
        self.assertFalse(version_newer("0.1.0", "0.1.0"))

    def test_full_and_short_git_sha_are_the_same_commit(self) -> None:
        full_sha = "0123456789abcdef0123456789abcdef01234567"

        self.assertTrue(git_commits_match(full_sha, full_sha[:12]))
        self.assertTrue(git_commits_match(full_sha[:12].upper(), full_sha))
        self.assertFalse(git_commits_match(full_sha, "fedcba987654"))

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
            self.assertNotIn("repo_url", payload)
            self.assertNotIn("branch", payload)
            self.assertIn("current_version", payload)
            self.assertIn("current_commit", payload)
            self.assertEqual(db.get_setting("update_last_request_id"), result["request_id"])


class UpdateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_check_does_not_report_same_full_sha_as_update(self) -> None:
        full_sha = "0123456789abcdef0123456789abcdef01234567"
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.__aexit__.return_value = None
        http.get.side_effect = [
            httpx.Response(404, json={"message": "Not Found"}),
            httpx.Response(200, json={"sha": full_sha}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "data.sqlite3")
            db.init()
            manager = UpdateManager(settings=Settings(database_path=str(root / "data.sqlite3")), db=db, app_root=root)
            with (
                patch.dict(os.environ, {"APP_UPDATE_CURRENT_COMMIT": full_sha}),
                patch("reseller_autoseller.updates.httpx.AsyncClient", return_value=http),
            ):
                result = await manager.fetch_latest()

        self.assertFalse(result["available"])
        self.assertEqual(result["latest_commit"], full_sha[:12])


if __name__ == "__main__":
    unittest.main()
