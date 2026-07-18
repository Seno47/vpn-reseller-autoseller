from __future__ import annotations

import argparse
import base64
from decimal import Decimal, InvalidOperation
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reseller_autoseller.config import Settings
from reseller_autoseller.db import Database
from reseller_autoseller.runtime_config import RuntimeConfig


DEFAULT_BASE_URL = "https://seller.ggsel.com"


class GgselPublishError(RuntimeError):
    pass


def exact_int(value: Any, *, field: str) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GgselPublishError(f"{field} must be an integer") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise GgselPublishError(f"{field} must be an integer")
    return int(number)


def nested_id(payload: Any) -> int | None:
    if isinstance(payload, dict):
        for key in ("id", "offer_id", "option_id", "variant_id"):
            value = payload.get(key)
            if str(value or "").isdigit():
                return int(value)
        for key in ("data", "offer", "option", "options", "variant", "variants", "items", "result"):
            found = nested_id(payload.get(key))
            if found is not None:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = nested_id(item)
            if found is not None:
                return found
    return None


def response_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "options", "variants", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = response_items(value)
            if nested:
                return nested
    return []


def image_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_notification_settings(app_base_url: str, secret: str) -> dict[str, Any]:
    """Build GGSEL's new-order URL notification object without logging its secret URL."""

    normalized_base_url = str(app_base_url or "").strip().rstrip("/")
    parsed = urlsplit(normalized_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GgselPublishError("GGSEL notification app base URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise GgselPublishError("GGSEL notification app base URL must not contain a query or fragment")
    normalized_secret = str(secret or "").strip()
    if not normalized_secret:
        raise GgselPublishError("GGSEL notification secret is not configured")
    callback_url = (
        f"{normalized_base_url}/api/ggsel/notify/order/{quote(normalized_secret, safe='')}"
    )
    return {
        "type": "url",
        "url": callback_url,
        "email": None,
        "http_method": "POST",
        "is_disabled": False,
        "is_default": False,
    }


def configured_notification_settings(*, required: bool = False) -> dict[str, Any] | None:
    """Load notification settings from env/runtime DB, optionally requiring a usable config."""

    settings = Settings()
    db = Database(settings.database_path)
    runtime = RuntimeConfig(settings=settings, db=db)
    if not runtime.get_bool("ggsel_sale_notifications_enabled"):
        if required:
            raise GgselPublishError("GGSEL order notifications are disabled in runtime settings")
        return None
    secret = runtime.get_text("ggsel_notification_secret")
    if not secret:
        if required:
            raise GgselPublishError("GGSEL notification secret is not configured")
        return None
    return build_notification_settings(runtime.get_text("app_base_url"), secret)


def build_variant_payload(spec: dict[str, Any]) -> list[dict[str, Any]]:
    base_price = exact_int(spec["offer"]["price"], field="Offer base price")
    minimum = exact_int(spec["minimum_variant_price_rub"], field="Category minimum price")
    variants: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    seen_titles: set[str] = set()
    for position, item in enumerate(spec["variants"]):
        tariff_code = str(item["tariff_code"]).strip()
        title_ru = str(item["title_ru"]).strip()
        final_price = exact_int(item["final_price_rub"], field=f"Variant {tariff_code} final price")
        if not tariff_code or tariff_code in seen_codes:
            raise GgselPublishError(f"Duplicate or empty tariff code: {tariff_code!r}")
        if not title_ru or title_ru in seen_titles:
            raise GgselPublishError(f"Duplicate or empty RU variant title: {title_ru!r}")
        if final_price < minimum:
            raise GgselPublishError(
                f"Variant {tariff_code} costs {final_price} RUB, below the {minimum} RUB category minimum"
            )
        delta = final_price - base_price
        variants.append(
            {
                "title_ru": title_ru,
                "title_en": str(item["title_en"]).strip(),
                "price": abs(delta),
                "discount_type": "fixed",
                "impact_type": "increase" if delta >= 0 else "decrease",
                "is_default": position == 0,
                "status": "active",
                "position": position,
            }
        )
        seen_codes.add(tariff_code)
        seen_titles.add(title_ru)
    return variants


VARIANT_REQUEST_FIELDS = (
    "id",
    "title_ru",
    "title_en",
    "price",
    "discount_type",
    "impact_type",
    "is_default",
    "status",
    "position",
)


def _variant_request_from_live(item: dict[str, Any]) -> dict[str, Any]:
    payload = {key: item.get(key) for key in VARIANT_REQUEST_FIELDS}
    if not str(payload.get("id") or "").isdigit():
        raise GgselPublishError("A live GGSEL variant has no numeric ID")
    return payload


def _variant_final_price(base_price: int, item: dict[str, Any]) -> int:
    modifier = exact_int(
        item.get("price") or 0,
        field="Live GGSEL variant price modifier",
    )
    impact = str(item.get("impact_type") or "increase")
    if impact == "increase":
        return base_price + modifier
    if impact == "decrease":
        return base_price - modifier
    raise GgselPublishError("A live GGSEL variant has an invalid impact type")


def _notification_matches(actual: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    return all(
        actual.get(key) == expected.get(key)
        for key in ("type", "url", "email", "http_method", "is_disabled", "is_default")
    )


def prepare_existing_variant_sync(
    spec: dict[str, Any],
    state: dict[str, Any],
    option_detail: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build update and rollback payloads only after exact live/state reconciliation."""

    expected_specs = list(spec["variants"])
    expected_payloads = build_variant_payload(spec)
    expected_titles = [str(item["title_ru"]).strip() for item in expected_specs]
    expected_codes = [str(item["tariff_code"]).strip() for item in expected_specs]

    live_variants = response_items(option_detail.get("variants", []))
    live_by_title = {str(item.get("title_ru") or "").strip(): item for item in live_variants}
    if len(live_variants) != len(live_by_title) or set(live_by_title) != set(expected_titles):
        raise GgselPublishError(
            "Live GGSEL variants differ from the exact expected title set; refusing to update"
        )

    mappings = state.get("mappings")
    if not isinstance(mappings, list):
        raise GgselPublishError("Publish state has no GGSEL variant mappings; refusing to update")
    mapped_by_code = {
        str(item.get("tariff_code") or "").strip(): item
        for item in mappings
        if isinstance(item, dict)
    }
    if len(mappings) != len(mapped_by_code) or set(mapped_by_code) != set(expected_codes):
        raise GgselPublishError(
            "Publish-state mappings differ from the exact expected tariff set; refusing to update"
        )

    updates: list[dict[str, Any]] = []
    rollback: list[dict[str, Any]] = []
    for item, expected in zip(expected_specs, expected_payloads, strict=True):
        title = str(item["title_ru"]).strip()
        code = str(item["tariff_code"]).strip()
        live = live_by_title[title]
        live_id = exact_int(live.get("id") or 0, field=f"Live variant ID for {code}")
        mapped_id = str(mapped_by_code[code].get("external_variant_id") or "").strip()
        if not live_id or mapped_id != str(live_id):
            raise GgselPublishError(
                f"Live GGSEL variant ID does not match saved mapping for tariff {code}"
            )
        updates.append({"id": live_id, **expected})
        rollback.append(_variant_request_from_live(live))
    return updates, rollback


def verify_existing_offer_sync(
    *,
    offer: dict[str, Any],
    option_detail: dict[str, Any],
    spec: dict[str, Any],
    expected_updates: list[dict[str, Any]],
    notification_settings: dict[str, Any] | None,
) -> None:
    if str(offer.get("status") or "") != "active":
        raise GgselPublishError("GGSEL offer is not active after synchronization")
    if exact_int(offer.get("price") or 0, field="Live offer base price") != exact_int(
        spec["offer"]["price"], field="Offer base price"
    ):
        raise GgselPublishError("GGSEL offer base price does not match the specification")
    for key in ("title_ru", "title_en", "description_ru", "description_en", "instructions_ru", "instructions_en"):
        if str(offer.get(key) or "") != str(spec["offer"][key]):
            raise GgselPublishError(f"GGSEL offer field {key} does not match the specification")
    if notification_settings is not None and not _notification_matches(
        offer.get("notification_settings"), notification_settings
    ):
        raise GgselPublishError("GGSEL notification settings do not match after synchronization")

    live_variants = response_items(option_detail.get("variants", []))
    live_by_id = {int(item.get("id") or 0): item for item in live_variants}
    expected_ids = {int(item["id"]) for item in expected_updates}
    if len(live_variants) != len(live_by_id) or set(live_by_id) != expected_ids:
        raise GgselPublishError("GGSEL variant IDs changed during synchronization")
    base_price = int(spec["offer"]["price"])
    expected_final_prices = [int(item["final_price_rub"]) for item in spec["variants"]]
    actual_final_prices: list[int] = []
    for expected in expected_updates:
        live = live_by_id[int(expected["id"])]
        for key in ("title_ru", "title_en", "discount_type", "impact_type", "status"):
            if str(live.get(key) or "") != str(expected[key]):
                raise GgselPublishError(f"GGSEL variant {expected['id']} field {key} did not synchronize")
        for key in ("price", "position"):
            if exact_int(live.get(key) or 0, field=f"Live variant {expected['id']} {key}") != exact_int(
                expected[key], field=f"Expected variant {expected['id']} {key}"
            ):
                raise GgselPublishError(f"GGSEL variant {expected['id']} field {key} did not synchronize")
        if bool(live.get("is_default")) != bool(expected["is_default"]):
            raise GgselPublishError(f"GGSEL variant {expected['id']} default flag did not synchronize")
        actual_final_prices.append(_variant_final_price(base_price, live))
    if actual_final_prices != expected_final_prices:
        raise GgselPublishError("GGSEL final variant prices do not match the specification")


def verify_rollback_snapshot(
    *,
    offer: dict[str, Any],
    option_detail: dict[str, Any],
    offer_snapshot: dict[str, Any],
    variant_snapshot: list[dict[str, Any]],
) -> None:
    for key, expected in offer_snapshot.items():
        actual = offer.get(key)
        if key == "notification_settings":
            if expected is None:
                if actual is not None:
                    raise GgselPublishError("GGSEL offer callback did not roll back")
            elif actual != expected:
                raise GgselPublishError("GGSEL offer callback did not roll back")
        elif key == "price":
            if exact_int(actual or 0, field="Rolled-back offer base price") != exact_int(
                expected, field="Offer snapshot base price"
            ):
                raise GgselPublishError("GGSEL offer base price did not roll back")
        elif actual != expected:
            raise GgselPublishError(f"GGSEL offer field {key} did not roll back")

    live_variants = response_items(option_detail.get("variants", []))
    live_by_id = {
        exact_int(item.get("id") or 0, field="Rolled-back variant ID"): item
        for item in live_variants
    }
    expected_by_id = {int(item["id"]): item for item in variant_snapshot}
    if set(live_by_id) != set(expected_by_id):
        raise GgselPublishError("GGSEL variant IDs changed during rollback")
    for variant_id, expected in expected_by_id.items():
        actual = live_by_id[variant_id]
        for key in ("title_ru", "title_en", "discount_type", "impact_type", "status"):
            if str(actual.get(key) or "") != str(expected.get(key) or ""):
                raise GgselPublishError(f"GGSEL variant {variant_id} field {key} did not roll back")
        for key in ("price", "position"):
            if exact_int(actual.get(key) or 0, field=f"Rolled-back variant {variant_id} {key}") != exact_int(
                expected.get(key) or 0, field=f"Variant snapshot {variant_id} {key}"
            ):
                raise GgselPublishError(f"GGSEL variant {variant_id} field {key} did not roll back")
        if bool(actual.get("is_default")) != bool(expected.get("is_default")):
            raise GgselPublishError(f"GGSEL variant {variant_id} default flag did not roll back")


def sync_existing_offer(
    *,
    client: Any,
    offer_id: int,
    option_id: int,
    offer: dict[str, Any],
    option_detail: dict[str, Any],
    spec: dict[str, Any],
    state: dict[str, Any],
    notification_settings: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updates, rollback_variants = prepare_existing_variant_sync(spec, state, option_detail)
    offer_payload = dict(spec["offer"])
    previous_offer_payload = {
        key: offer.get(key)
        for key in offer_payload
        if key in offer
    }
    live_notification = offer.get("notification_settings")
    preserved_notification = notification_settings or (
        dict(live_notification) if isinstance(live_notification, dict) else None
    )
    if preserved_notification is not None:
        offer_payload["notification_settings"] = preserved_notification
        previous_offer_payload["notification_settings"] = (
            dict(live_notification) if isinstance(live_notification, dict) else None
        )

    variants_path = f"/api_sellers/v2/offers/{offer_id}/options/{option_id}/variants"
    offer_path = f"/api_sellers/v2/offers/{offer_id}"
    variants_attempted = False
    offer_attempted = False
    try:
        variants_attempted = True
        client.request("POST", variants_path, json={"variants": updates})
        offer_attempted = True
        client.request(
            "PATCH",
            offer_path,
            json=offer_payload,
            sensitive=preserved_notification is not None,
        )
        synced_offer = client.offer(offer_id)
        synced_option = client.option(offer_id, option_id)
        verify_existing_offer_sync(
            offer=synced_offer,
            option_detail=synced_option,
            spec=spec,
            expected_updates=updates,
            notification_settings=preserved_notification,
        )
        return synced_offer, synced_option
    except Exception as exc:
        rollback_errors: list[str] = []
        if offer_attempted:
            try:
                client.request(
                    "PATCH",
                    offer_path,
                    json=previous_offer_payload,
                    sensitive=isinstance(live_notification, dict),
                )
            except Exception:
                rollback_errors.append("offer")
        if variants_attempted:
            try:
                client.request("POST", variants_path, json={"variants": rollback_variants})
            except Exception:
                rollback_errors.append("variants")
        if not rollback_errors:
            try:
                verify_rollback_snapshot(
                    offer=client.offer(offer_id),
                    option_detail=client.option(offer_id, option_id),
                    offer_snapshot=previous_offer_payload,
                    variant_snapshot=rollback_variants,
                )
            except Exception:
                rollback_errors.append("verification")
        if rollback_errors:
            raise GgselPublishError(
                "GGSEL offer synchronization failed and rollback was incomplete: "
                + ", ".join(rollback_errors)
            ) from exc
        raise


class GgselV2Client:
    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": api_key, "Accept": "application/json", "locale": "ru"},
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, *, sensitive: bool = False, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        try:
            payload = response.json() if response.content else {}
        except ValueError as exc:
            raise GgselPublishError(
                f"GGSEL {method} {path} returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            detail: Any = (
                "response omitted because the request contains a secret" if sensitive else payload
            )
            raise GgselPublishError(
                f"GGSEL {method} {path} failed with HTTP {response.status_code}: {detail}"
            )
        if isinstance(payload, dict):
            retval = payload.get("retval")
            if retval not in (None, 0, "0"):
                if sensitive:
                    raise GgselPublishError(
                        f"GGSEL {method} {path} failed; response omitted because the request contains a secret"
                    )
                raise GgselPublishError(
                    f"GGSEL {method} {path} failed: {payload.get('retdesc') or payload.get('desc') or payload}"
                )
        return payload

    def offer(self, offer_id: int) -> dict[str, Any]:
        payload = self.request("GET", f"/api_sellers/v2/offers/{offer_id}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        if not isinstance(payload, dict):
            raise GgselPublishError("GGSEL offer response is not an object")
        return payload

    def options(self, offer_id: int) -> list[dict[str, Any]]:
        return response_items(self.request("GET", f"/api_sellers/v2/offers/{offer_id}/options"))

    def option(self, offer_id: int, option_id: int) -> dict[str, Any]:
        payload = self.request("GET", f"/api_sellers/v2/offers/{offer_id}/options/{option_id}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        if not isinstance(payload, dict):
            raise GgselPublishError("GGSEL option response is not an object")
        return payload


def load_api_key() -> str:
    settings = Settings()
    db = Database(settings.database_path)
    runtime = RuntimeConfig(settings=settings, db=db)
    key = runtime.get_text("ggsel_api_key")
    if not key:
        raise GgselPublishError("GGSEL API key is not configured")
    return key


def state_without_notification_urls(value: Any) -> Any:
    """Return a state-safe copy with notification destinations reduced to non-secret metadata."""

    if isinstance(value, list):
        return [state_without_notification_urls(item) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "notification_settings" and isinstance(item, dict):
            sanitized[key] = {
                field: item[field]
                for field in ("type", "http_method", "is_disabled", "is_default")
                if field in item
            }
            sanitized[key]["url_configured"] = bool(item.get("url"))
            continue
        sanitized[key] = state_without_notification_urls(item)
    return sanitized


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_state = state_without_notification_urls(state)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(safe_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def required_existing_sync_ids(state: dict[str, Any]) -> tuple[int, int]:
    try:
        offer_id = exact_int(state.get("offer_id") or 0, field="Publish-state offer ID")
        option_id = exact_int(state.get("option_id") or 0, field="Publish-state option ID")
    except GgselPublishError:
        raise
    if offer_id <= 0 or option_id <= 0:
        raise GgselPublishError(
            "--sync-offer requires existing positive offer_id and option_id values in publish state"
        )
    mappings = state.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise GgselPublishError("--sync-offer requires existing variant mappings in publish state")
    return offer_id, option_id


def find_option(options: list[dict[str, Any]], title_ru: str) -> dict[str, Any] | None:
    for option in options:
        if str(option.get("title_ru") or "").strip() == title_ru:
            return option
    return None


def reconcile_activation_timeout(client: GgselV2Client, offer_id: int) -> dict[str, Any]:
    """Treat a slow async job as successful when the offer is already active."""

    offer_status = str(client.offer(offer_id).get("status") or "")
    if offer_status == "active":
        return {
            "status": "completed",
            "offer_status": offer_status,
            "inferred_from_offer_status": True,
        }
    raise GgselPublishError("Timed out waiting for offer activation")


def publish(args: argparse.Namespace) -> dict[str, Any]:
    spec_path = args.spec.resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cover_path = (Path.cwd() / spec["cover_image_ru_path"]).resolve()
    if not cover_path.is_file():
        raise GgselPublishError(f"Cover image not found: {cover_path}")
    variants_payload = build_variant_payload(spec)
    if args.dry_run:
        return {
            "dry_run": True,
            "category_id": spec["category_id"],
            "title_ru": spec["offer"]["title_ru"],
            "base_price_rub": spec["offer"]["price"],
            "variant_count": len(variants_payload),
            "minimum_final_price_rub": min(item["final_price_rub"] for item in spec["variants"]),
            "maximum_final_price_rub": max(item["final_price_rub"] for item in spec["variants"]),
            "cover_bytes": cover_path.stat().st_size,
            "notification_sync_requested": bool(getattr(args, "sync_notifications", False)),
            "offer_sync_requested": bool(getattr(args, "sync_offer", False)),
        }

    state_path = args.state.resolve()
    state: dict[str, Any] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    notification_sync_requested = bool(getattr(args, "sync_notifications", False))
    offer_sync_requested = bool(getattr(args, "sync_offer", False))
    required_sync_ids = required_existing_sync_ids(state) if offer_sync_requested else None
    notification_settings = configured_notification_settings(required=notification_sync_requested)
    client = GgselV2Client(load_api_key(), base_url=args.base_url)
    try:
        offer_id = required_sync_ids[0] if required_sync_ids else int(state.get("offer_id") or 0)
        existing_offer = bool(offer_id)
        if offer_id:
            offer = client.offer(offer_id)
        else:
            offer_payload = {
                **spec["offer"],
                "category_id": int(spec["category_id"]),
                "cover_image_ru": image_data_uri(cover_path),
            }
            if notification_settings is not None:
                offer_payload["notification_settings"] = notification_settings
            created = client.request(
                "POST",
                "/api_sellers/v2/offers",
                json=offer_payload,
                sensitive=notification_settings is not None,
            )
            offer_id = nested_id(created) or 0
            if not offer_id:
                safe_created = state_without_notification_urls(created)
                raise GgselPublishError(
                    f"Cannot find created offer ID in response: {safe_created}"
                )
            state.update({"offer_id": offer_id, "created_offer_response": created})
            write_state(state_path, state)
            offer = client.offer(offer_id)

        if existing_offer and notification_sync_requested and not offer_sync_requested:
            assert notification_settings is not None
            client.request(
                "PATCH",
                f"/api_sellers/v2/offers/{offer_id}",
                json={"notification_settings": notification_settings},
                sensitive=True,
            )
            state["notification_sync"] = {
                "synced": True,
                "type": "url",
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            }
            write_state(state_path, state)
            offer = client.offer(offer_id)
        elif not existing_offer and notification_settings is not None:
            state["notification_sync"] = {
                "included_on_create": True,
                "type": "url",
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            }
            write_state(state_path, state)

        option_title = str(spec["option"]["title_ru"])
        live_options = client.options(offer_id)
        if required_sync_ids:
            option_id = required_sync_ids[1]
            option = next(
                (
                    item
                    for item in live_options
                    if exact_int(item.get("id") or 0, field="Live option ID") == option_id
                ),
                None,
            )
            if option is None or str(option.get("title_ru") or "").strip() != option_title:
                raise GgselPublishError(
                    "The saved GGSEL option is missing or has a different title; refusing to update"
                )
        else:
            option = find_option(live_options, option_title)
            option_id = int(state.get("option_id") or (option or {}).get("id") or 0)
        if not option_id:
            created_option = client.request(
                "POST",
                f"/api_sellers/v2/offers/{offer_id}/options",
                json={"options": [spec["option"]]},
            )
            option_id = nested_id(created_option) or 0
            if not option_id:
                option = find_option(client.options(offer_id), option_title)
                option_id = int((option or {}).get("id") or 0)
            if not option_id:
                raise GgselPublishError(f"Cannot find created option ID in response: {created_option}")
            state.update({"option_id": option_id, "created_option_response": created_option})
            write_state(state_path, state)

        option_detail = client.option(offer_id, option_id)
        if existing_offer and offer_sync_requested:
            if str(offer.get("status") or "") != "active":
                raise GgselPublishError("Refusing to synchronize an existing GGSEL offer that is not active")
            offer, option_detail = sync_existing_offer(
                client=client,
                offer_id=offer_id,
                option_id=option_id,
                offer=offer,
                option_detail=option_detail,
                spec=spec,
                state=state,
                notification_settings=notification_settings,
            )
            state["last_offer_sync"] = {
                "synced": True,
                "base_price_rub": int(spec["offer"]["price"]),
                "variant_count": len(spec["variants"]),
                "notification_configured": isinstance(
                    offer.get("notification_settings"), dict
                ),
            }
            if notification_settings is not None:
                state["notification_sync"] = {
                    "synced": True,
                    "type": "url",
                    "http_method": "POST",
                    "is_disabled": False,
                    "is_default": False,
                }
            write_state(state_path, state)
        existing_variants = response_items(option_detail.get("variants", []))
        existing_by_title = {str(item.get("title_ru") or "").strip(): item for item in existing_variants}
        expected_titles = [str(item["title_ru"]).strip() for item in spec["variants"]]
        if not all(title in existing_by_title for title in expected_titles):
            if existing_variants:
                raise GgselPublishError(
                    "The option already has a partial or different variant set; refusing to append duplicates"
                )
            created_variants = client.request(
                "POST",
                f"/api_sellers/v2/offers/{offer_id}/options/{option_id}/variants",
                json={"variants": variants_payload},
            )
            state["created_variants_response"] = created_variants
            write_state(state_path, state)
            option_detail = client.option(offer_id, option_id)
            existing_variants = response_items(option_detail.get("variants", []))
            existing_by_title = {str(item.get("title_ru") or "").strip(): item for item in existing_variants}

        missing_titles = [title for title in expected_titles if title not in existing_by_title]
        if missing_titles:
            raise GgselPublishError(f"Created offer is missing variants: {missing_titles}")

        mappings = []
        for item in spec["variants"]:
            title = str(item["title_ru"]).strip()
            external_variant_id = int(existing_by_title[title].get("id") or 0)
            if not external_variant_id:
                raise GgselPublishError(f"Variant {title!r} has no ID")
            mappings.append(
                {
                    "marketplace": "ggsel",
                    "external_product_id": str(offer_id),
                    "external_variant_id": str(external_variant_id),
                    "tariff_code": item["tariff_code"],
                    "title": spec["offer"]["title_ru"],
                    "action": "create",
                    "action_params": {},
                    "enabled": True,
                    "delivery_template": spec["delivery_template_ru"],
                    "final_price_rub": item["final_price_rub"],
                }
            )
        state.update(
            {
                "offer_id": offer_id,
                "option_id": option_id,
                "offer_status": offer.get("status"),
                "mappings": mappings,
            }
        )
        write_state(state_path, state)

        if args.activate:
            if str(offer.get("status")) == "active":
                state["activation"] = {"already_active": True}
            else:
                activation = client.request(
                    "POST", "/api_sellers/v2/offers/batch_activate", json={"offer_ids": [offer_id]}
                )
                job_id = str((activation or {}).get("job_id") or "")
                state["activation"] = activation
                write_state(state_path, state)
                if job_id:
                    deadline = time.monotonic() + args.activation_timeout
                    while time.monotonic() < deadline:
                        result = client.request("GET", f"/api_sellers/v2/async_job_results/{job_id}")
                        state["activation_result"] = result
                        write_state(state_path, state)
                        status = str((result or {}).get("status") or "")
                        if status == "completed":
                            break
                        if status == "failed":
                            raise GgselPublishError(f"Offer activation failed: {result}")
                        time.sleep(1)
                    else:
                        state["activation_result"] = reconcile_activation_timeout(client, offer_id)
                        write_state(state_path, state)

        return {
            "offer_id": offer_id,
            "option_id": option_id,
            "status": client.offer(offer_id).get("status"),
            "variant_count": len(mappings),
            "state_path": str(state_path),
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or resume the neutral GGSEL VLESS offer")
    parser.add_argument(
        "--spec", type=Path, default=Path("output/ggsel-vless/offer-spec.json"), help="Offer spec JSON"
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("output/ggsel-vless/publish-state.json"),
        help="Idempotency and mapping state JSON",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--activate", action="store_true", help="Activate only after draft validation")
    parser.add_argument(
        "--sync-notifications",
        action="store_true",
        help="PATCH the existing offer with the configured secret new-order callback URL",
    )
    parser.add_argument(
        "--sync-offer",
        action="store_true",
        help="Safely reconcile an existing offer's copy, base price, variants, and callback",
    )
    parser.add_argument("--activation-timeout", type=float, default=120.0)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        print(json.dumps(publish(parse_args()), ensure_ascii=False, indent=2))
    except (GgselPublishError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"GGSEL publish failed: {exc}") from exc
