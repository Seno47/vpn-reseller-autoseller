from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reseller_autoseller.config import Settings
from reseller_autoseller.db import Database
from reseller_autoseller.xyra_client import XyraNetClient


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    kind: str = "text"
    sensitive: bool = False
    restart_required: bool = False
    description: str = ""


SETTING_DEFINITIONS = [
    SettingDefinition("app_base_url", "Base URL"),
    SettingDefinition("xyranet_api_base_url", "XyraNet API URL"),
    SettingDefinition("xyranet_api_key", "XyraNet API key", sensitive=True),
    SettingDefinition("xyranet_timeout_seconds", "XyraNet timeout", kind="number"),
    SettingDefinition("digiseller_seller_id", "Digiseller seller ID"),
    SettingDefinition("digiseller_api_key", "Digiseller API key", sensitive=True),
    SettingDefinition("ggsel_seller_id", "GGsel seller ID"),
    SettingDefinition("ggsel_api_key", "GGsel API key", sensitive=True),
    SettingDefinition("enable_telegram", "Telegram enabled", kind="boolean", restart_required=True),
    SettingDefinition("telegram_bot_token", "Telegram bot token", sensitive=True, restart_required=True),
    SettingDefinition("notify_new_purchases", "Notify: new purchases", kind="boolean"),
    SettingDefinition("notify_chat_messages", "Notify: chat messages", kind="boolean"),
    SettingDefinition("notify_errors", "Notify: errors", kind="boolean"),
    SettingDefinition("notify_pending", "Notify: pending actions", kind="boolean"),
    SettingDefinition("notify_daily_statistics", "Notify: daily statistics", kind="boolean"),
    SettingDefinition("free_reissue_enabled", "Free reissue command", kind="boolean"),
    SettingDefinition("admin_username", "Web admin username"),
    SettingDefinition("admin_password", "Web admin password", sensitive=True),
]

SETTING_BY_KEY = {item.key: item for item in SETTING_DEFINITIONS}


class RuntimeConfig:
    def __init__(self, *, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    def get_raw(self, key: str) -> str:
        stored = self.db.get_setting(key)
        value = stored if stored is not None else getattr(self.settings, key)
        if key == "xyranet_api_base_url":
            return XyraNetClient.normalize_base_url(str(value))
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def get_text(self, key: str) -> str:
        return self.get_raw(key).strip()

    def get_bool(self, key: str) -> bool:
        return self.get_raw(key).strip().lower() in {"1", "true", "yes", "on"}

    def get_float(self, key: str) -> float:
        try:
            return float(self.get_raw(key))
        except ValueError:
            return float(getattr(self.settings, key))

    def is_env_admin(self, telegram_id: int) -> bool:
        return telegram_id in set(self.settings.admin_ids)

    def bot_admin_ids(self) -> set[int]:
        ids = set(self.settings.admin_ids)
        ids.update(int(row["telegram_id"]) for row in self.db.list_bot_users() if int(row["enabled"]))
        return ids

    def is_bot_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.bot_admin_ids()

    def list_bot_users(self) -> list[dict[str, Any]]:
        env_admins = set(self.settings.admin_ids)
        rows: dict[int, dict[str, Any]] = {
            int(row["telegram_id"]): {
                **row,
                "telegram_id": int(row["telegram_id"]),
                "enabled": bool(row["enabled"]),
                "source": "database",
                "locked": int(row["telegram_id"]) in env_admins,
            }
            for row in self.db.list_bot_users()
        }
        for telegram_id in sorted(env_admins):
            rows[telegram_id] = {
                "telegram_id": telegram_id,
                "label": "ENV admin",
                "enabled": True,
                "source": "env",
                "locked": True,
                "added_by": None,
                "created_at": "",
                "updated_at": "",
            }
        return sorted(rows.values(), key=lambda row: int(row["telegram_id"]))

    def setting_payload(self) -> list[dict[str, Any]]:
        result = []
        stored = self.db.list_settings()
        for definition in SETTING_DEFINITIONS:
            value = self.get_raw(definition.key)
            has_value = bool(value)
            result.append(
                {
                    "key": definition.key,
                    "label": definition.label,
                    "kind": definition.kind,
                    "value": "" if definition.sensitive else value,
                    "configured": has_value,
                    "source": "database" if definition.key in stored else "env",
                    "sensitive": definition.sensitive,
                    "restart_required": definition.restart_required,
                    "description": definition.description,
                }
            )
        return result

    def set_value(self, key: str, value: Any) -> None:
        if key not in SETTING_BY_KEY:
            raise ValueError(f"Unknown setting: {key}")
        definition = SETTING_BY_KEY[key]
        if definition.sensitive and str(value).strip() == "":
            return
        if definition.kind == "boolean":
            stored = "true" if self._as_bool(value) else "false"
        elif definition.kind == "number":
            stored = str(float(value))
        else:
            stored = str(value).strip()
        if key == "xyranet_api_base_url":
            stored = XyraNetClient.normalize_base_url(stored)
        self.db.set_setting(key, stored)

    def set_many(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set_value(key, value)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


class RuntimeXyraNetClient:
    def __init__(self, runtime: RuntimeConfig) -> None:
        self.runtime = runtime

    def client(self) -> XyraNetClient:
        return XyraNetClient(
            base_url=self.runtime.get_text("xyranet_api_base_url"),
            api_key=self.runtime.get_text("xyranet_api_key"),
            timeout=self.runtime.get_float("xyranet_timeout_seconds"),
        )

    async def summary(self) -> dict[str, Any]:
        return await self.client().summary()

    async def tariffs(self) -> list[dict[str, Any]]:
        return await self.client().tariffs()

    async def create_order(self, tariff_code: str, *, idempotency_key: str) -> dict[str, Any]:
        return await self.client().create_order(tariff_code, idempotency_key=idempotency_key)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self.client().get_order(order_id)

    async def renew_order(self, order_id: str, tariff_code: str | None = None, *, idempotency_key: str) -> dict[str, Any]:
        return await self.client().renew_order(order_id, tariff_code, idempotency_key=idempotency_key)

    async def reissue_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return await self.client().reissue_order(order_id, idempotency_key=idempotency_key)

    async def traffic_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.client().traffic_quote(order_id, payload)

    async def traffic_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self.client().traffic_purchase(order_id, payload, idempotency_key=idempotency_key)

    async def ip_limit_quote(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.client().ip_limit_quote(order_id, payload)

    async def ip_limit_purchase(self, order_id: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self.client().ip_limit_purchase(order_id, payload, idempotency_key=idempotency_key)
