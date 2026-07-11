from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

import httpx

from reseller_autoseller.runtime_config import RuntimeConfig


log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, runtime: RuntimeConfig) -> None:
        self.runtime = runtime

    def enabled(self) -> bool:
        return bool(self.runtime.get_bool("enable_telegram") and self.runtime.get_text("telegram_bot_token"))

    async def send_admins(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.enabled():
            return
        token = self.runtime.get_text("telegram_bot_token")
        admin_ids = sorted(self.runtime.bot_admin_ids())
        if not token or not admin_ids:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await asyncio.gather(
                *[self._send_one(client, token, telegram_id, text, reply_markup) for telegram_id in admin_ids],
                return_exceptions=True,
            )

    async def _send_one(
        self,
        client: httpx.AsyncClient,
        token: str,
        telegram_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            payload: dict[str, Any] = {
                "chat_id": telegram_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            if response.status_code >= 400:
                log.warning("Cannot send admin notification to %s: %s", telegram_id, response.text)
        except Exception:
            log.exception("Cannot send admin notification to %s", telegram_id)


def sale_title(sale: dict[str, Any] | None, fallback_order_id: str = "") -> str:
    if not sale:
        return escape(fallback_order_id or "unknown")
    return escape(f"{sale.get('marketplace')}:{sale.get('external_order_id')}")


def compact_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
