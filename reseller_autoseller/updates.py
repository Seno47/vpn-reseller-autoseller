from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from reseller_autoseller import __version__
from reseller_autoseller.config import Settings
from reseller_autoseller.db import Database


UPDATE_CACHE_KEYS = {
    "checked_at": "update_checked_at",
    "source": "update_source",
    "latest_version": "update_latest_version",
    "latest_commit": "update_latest_commit",
    "latest_url": "update_latest_url",
    "available": "update_available",
    "error": "update_error",
    "last_request_id": "update_last_request_id",
    "last_request_at": "update_last_request_at",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def version_parts(version: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+){0,3})", str(version or "").strip())
    if not match:
        return ()
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def version_newer(latest: str, current: str) -> bool:
    latest_parts = version_parts(latest)
    current_parts = version_parts(current)
    if not latest_parts or not current_parts:
        return bool(latest and current and latest.strip().lstrip("v") != current.strip().lstrip("v"))
    return latest_parts > current_parts


def github_repo_parts(repo_url: str) -> tuple[str, str] | None:
    value = str(repo_url or "").strip().removesuffix(".git")
    patterns = [
        r"^https?://github\.com/([^/]+)/([^/]+)$",
        r"^git@github\.com:([^/]+)/([^/]+)$",
        r"^([^/\s]+)/([^/\s]+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, value)
        if match:
            return match.group(1), match.group(2)
    return None


def current_git_commit(app_root: Path) -> str:
    env_commit = os.environ.get("APP_UPDATE_CURRENT_COMMIT", "").strip()
    if env_commit:
        return env_commit
    try:
        result = subprocess.run(
            ["git", "-C", str(app_root), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    return result.stdout.strip()


class UpdateManager:
    def __init__(self, *, settings: Settings, db: Database, app_root: Path | None = None) -> None:
        self.settings = settings
        self.db = db
        self.app_root = app_root or Path(__file__).resolve().parents[1]
        self._lock = asyncio.Lock()

    @property
    def current_version(self) -> str:
        return __version__

    @property
    def current_commit(self) -> str:
        return current_git_commit(self.app_root)

    @property
    def update_supported(self) -> bool:
        return bool(self.trigger_file or self.settings.app_update_command.strip())

    @property
    def trigger_file(self) -> Path | None:
        value = self.settings.app_update_trigger_file.strip()
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.app_root / path

    @property
    def status_file(self) -> Path | None:
        value = self.settings.app_update_status_file.strip()
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.app_root / path

    def cache(self) -> dict[str, str]:
        stored = self.db.list_settings()
        return {name: stored.get(key, "") for name, key in UPDATE_CACHE_KEYS.items()}

    def status(self) -> dict[str, Any]:
        cache = self.cache()
        return {
            "current_version": self.current_version,
            "current_commit": self.current_commit,
            "repo_url": self.settings.app_update_repo_url,
            "branch": self.settings.app_update_branch,
            "check_interval_hours": self.settings.app_update_check_interval_hours,
            "checked_at": cache.get("checked_at") or "",
            "source": cache.get("source") or "",
            "latest_version": cache.get("latest_version") or "",
            "latest_commit": cache.get("latest_commit") or "",
            "latest_url": cache.get("latest_url") or "",
            "update_available": str(cache.get("available") or "").lower() == "true",
            "error": cache.get("error") or "",
            "update_supported": self.update_supported,
            "trigger_configured": bool(self.trigger_file),
            "command_configured": bool(self.settings.app_update_command.strip()),
            "last_request_id": cache.get("last_request_id") or "",
            "last_request_at": cache.get("last_request_at") or "",
            "host_status": self.read_host_status(),
        }

    async def check(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            cache = self.cache()
            checked_at = parse_dt(cache.get("checked_at", ""))
            interval = max(float(self.settings.app_update_check_interval_hours or 12), 1.0)
            if (
                not force
                and checked_at
                and datetime.now(timezone.utc) - checked_at < timedelta(hours=interval)
                and (cache.get("latest_version") or cache.get("latest_commit") or cache.get("error"))
            ):
                return self.status()
            try:
                payload = await self.fetch_latest()
                self.write_cache(payload)
            except Exception as exc:
                self.write_cache(
                    {
                        "checked_at": utcnow(),
                        "source": "",
                        "latest_version": "",
                        "latest_commit": "",
                        "latest_url": "",
                        "available": False,
                        "error": str(exc),
                    }
                )
            return self.status()

    async def fetch_latest(self) -> dict[str, Any]:
        repo = github_repo_parts(self.settings.app_update_repo_url)
        if not repo:
            raise ValueError("Unsupported update repository URL")
        owner, name = repo
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "xyranet-reseller-autoseller-update-check",
        }
        base = f"https://api.github.com/repos/{owner}/{name}"
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            release_response = await client.get(f"{base}/releases/latest")
            if release_response.status_code == 200:
                release = release_response.json()
                tag = str(release.get("tag_name") or "").strip()
                latest_version = tag.lstrip("v")
                latest_commit = await self.fetch_commit_sha(client, base)
                return {
                    "checked_at": utcnow(),
                    "source": "github_release",
                    "latest_version": latest_version,
                    "latest_commit": latest_commit,
                    "latest_url": str(release.get("html_url") or ""),
                    "available": version_newer(latest_version, self.current_version),
                    "error": "",
                }
            if release_response.status_code not in {404, 403}:
                raise ValueError(f"GitHub release check failed: HTTP {release_response.status_code}")
            latest_commit = await self.fetch_commit_sha(client, base)
        current_commit = self.current_commit
        return {
            "checked_at": utcnow(),
            "source": "github_commit",
            "latest_version": "",
            "latest_commit": latest_commit,
            "latest_url": f"https://github.com/{owner}/{name}/commits/{self.settings.app_update_branch}",
            "available": bool(current_commit and latest_commit and not latest_commit.startswith(current_commit)),
            "error": "" if current_commit else "Current commit is unknown; release tags are recommended.",
        }

    async def fetch_commit_sha(self, client: httpx.AsyncClient, base: str) -> str:
        branch = self.settings.app_update_branch.strip() or "main"
        response = await client.get(f"{base}/commits/{branch}")
        if response.status_code >= 400:
            raise ValueError(f"GitHub commit check failed: HTTP {response.status_code}")
        data = response.json()
        return str(data.get("sha") or "")[:12]

    def write_cache(self, payload: dict[str, Any]) -> None:
        for name, key in UPDATE_CACHE_KEYS.items():
            if name in {"last_request_id", "last_request_at"}:
                continue
            value = payload.get(name, "")
            if isinstance(value, bool):
                value = "true" if value else "false"
            self.db.set_setting(key, str(value or ""))

    def read_host_status(self) -> dict[str, Any]:
        path = self.status_file
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"status": "error", "message": f"Cannot read update status: {exc}"}
        return data if isinstance(data, dict) else {}

    def start_update(self) -> dict[str, Any]:
        request_id = secrets.token_urlsafe(10)
        request = {
            "request_id": request_id,
            "requested_at": utcnow(),
            "repo_url": self.settings.app_update_repo_url,
            "branch": self.settings.app_update_branch,
            "current_version": self.current_version,
            "current_commit": self.current_commit,
        }
        trigger = self.trigger_file
        if trigger:
            trigger.parent.mkdir(parents=True, exist_ok=True)
            tmp = trigger.with_suffix(trigger.suffix + ".tmp")
            tmp.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, trigger)
            self.db.set_setting(UPDATE_CACHE_KEYS["last_request_id"], request_id)
            self.db.set_setting(UPDATE_CACHE_KEYS["last_request_at"], request["requested_at"])
            return {"status": "requested", "mode": "trigger_file", "request_id": request_id}
        command = self.settings.app_update_command.strip()
        if command:
            env = os.environ.copy()
            env.update(
                {
                    "APP_UPDATE_REQUEST_ID": request_id,
                    "APP_UPDATE_REPO_URL": self.settings.app_update_repo_url,
                    "APP_UPDATE_BRANCH": self.settings.app_update_branch,
                    "APP_UPDATE_PID": str(os.getpid()),
                }
            )
            subprocess.Popen(
                shlex.split(command),
                cwd=str(self.app_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.db.set_setting(UPDATE_CACHE_KEYS["last_request_id"], request_id)
            self.db.set_setting(UPDATE_CACHE_KEYS["last_request_at"], request["requested_at"])
            return {"status": "started", "mode": "command", "request_id": request_id}
        raise RuntimeError("Updates are not configured on this installation")
