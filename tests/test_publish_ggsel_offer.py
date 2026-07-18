from __future__ import annotations

import copy
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from tests.ggsel_test_data import (
    TEST_OFFER_ID,
    TEST_OPTION_ID,
    TEST_VARIANT_ID_START,
    make_ggsel_offer_spec,
)

from scripts.publish_ggsel_offer import (
    GgselPublishError,
    build_notification_settings,
    build_variant_payload,
    nested_id,
    prepare_existing_variant_sync,
    publish,
    reconcile_activation_timeout,
    state_without_notification_urls,
    sync_existing_offer,
)


class PublishGgselOfferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = make_ggsel_offer_spec()

    def test_builds_all_profitable_variants_from_final_prices(self) -> None:
        payload = build_variant_payload(self.spec)

        self.assertEqual(len(payload), 12)
        self.assertEqual(payload[0]["price"], 0)
        self.assertTrue(payload[0]["is_default"])
        self.assertEqual(
            payload[-1]["price"],
            self.spec["variants"][-1]["final_price_rub"] - self.spec["offer"]["price"],
        )
        self.assertEqual(payload[-1]["impact_type"], "increase")
        self.assertEqual(
            [
                self.spec["offer"]["price"] + item["price"]
                if item["impact_type"] == "increase"
                else self.spec["offer"]["price"] - item["price"]
                for item in payload
            ],
            [item["final_price_rub"] for item in self.spec["variants"]],
        )

    def test_rejects_variant_below_category_minimum(self) -> None:
        spec = copy.deepcopy(self.spec)
        spec["variants"][0]["final_price_rub"] = 48

        with self.assertRaisesRegex(GgselPublishError, "below the 49 RUB category minimum"):
            build_variant_payload(spec)

    def test_rejects_fractional_offer_or_variant_prices(self) -> None:
        fractional_base = copy.deepcopy(self.spec)
        fractional_base["offer"]["price"] = 49.9
        with self.assertRaisesRegex(GgselPublishError, "Offer base price must be an integer"):
            build_variant_payload(fractional_base)

        fractional_variant = copy.deepcopy(self.spec)
        fractional_variant["variants"][1]["final_price_rub"] = 99.9
        with self.assertRaisesRegex(GgselPublishError, "final price must be an integer"):
            build_variant_payload(fractional_variant)

    def test_rejects_duplicate_tariff_code(self) -> None:
        spec = copy.deepcopy(self.spec)
        spec["variants"][1]["tariff_code"] = spec["variants"][0]["tariff_code"]

        with self.assertRaisesRegex(GgselPublishError, "Duplicate or empty tariff code"):
            build_variant_payload(spec)

    def test_extracts_nested_created_id(self) -> None:
        self.assertEqual(nested_id({"data": {"offer": {"id": 123}}}), 123)
        self.assertEqual(nested_id({"options": [{"option_id": 456}]}), 456)

    def test_activation_timeout_accepts_offer_that_is_already_active(self) -> None:
        class ActiveOfferClient:
            @staticmethod
            def offer(offer_id: int) -> dict[str, object]:
                self.assertEqual(offer_id, 123)
                return {"status": "active"}

        result = reconcile_activation_timeout(ActiveOfferClient(), 123)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["inferred_from_offer_status"])

    def test_builds_post_notification_settings_with_escaped_secret(self) -> None:
        payload = build_notification_settings("https://reseller.example.test/", "secret/value ?")

        self.assertEqual(
            payload,
            {
                "type": "url",
                "url": "https://reseller.example.test/api/ggsel/notify/order/secret%2Fvalue%20%3F",
                "email": None,
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            },
        )

    def test_rejects_invalid_notification_destination(self) -> None:
        with self.assertRaisesRegex(GgselPublishError, "absolute HTTP"):
            build_notification_settings("reseller.example.test", "secret")
        with self.assertRaisesRegex(GgselPublishError, "secret is not configured"):
            build_notification_settings("https://reseller.example.test", "")

    def test_state_copy_never_contains_notification_url(self) -> None:
        secret = "never-write-this-secret"
        state = {
            "created_offer_response": {
                "data": {
                    "notification_settings": build_notification_settings(
                        "https://reseller.example.test", secret
                    )
                }
            }
        }

        sanitized = state_without_notification_urls(state)
        serialized = json.dumps(sanitized)

        self.assertNotIn(secret, serialized)
        self.assertNotIn("/api/ggsel/notify/order/", serialized)
        notification = sanitized["created_offer_response"]["data"]["notification_settings"]
        self.assertTrue(notification["url_configured"])
        self.assertEqual(notification["http_method"], "POST")

    def _publish_args(
        self,
        state: Path,
        *,
        sync_notifications: bool,
        sync_offer: bool = False,
    ) -> Namespace:
        spec_path = state.parent / "spec.json"
        cover_path = state.parent / "cover.jpg"
        spec = copy.deepcopy(self.spec)
        spec["cover_image_ru_path"] = str(cover_path)
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        cover_path.write_bytes(b"synthetic-test-cover")
        return Namespace(
            spec=spec_path,
            state=state,
            base_url="https://seller.ggsel.com",
            dry_run=False,
            activate=False,
            sync_notifications=sync_notifications,
            sync_offer=sync_offer,
            activation_timeout=1.0,
        )

    def _fake_client(self, *, offer_id: int = TEST_OFFER_ID):
        spec = self.spec

        class FakeClient:
            def __init__(self) -> None:
                self.requests: list[tuple[str, str, dict[str, object]]] = []

            def offer(self, requested_offer_id: int) -> dict[str, object]:
                self_outer.assertEqual(requested_offer_id, offer_id)
                return {"id": offer_id, "status": "active"}

            @staticmethod
            def options(requested_offer_id: int) -> list[dict[str, object]]:
                return [{"id": TEST_OPTION_ID, "title_ru": spec["option"]["title_ru"]}]

            @staticmethod
            def option(requested_offer_id: int, option_id: int) -> dict[str, object]:
                return {
                    "variants": [
                        {"id": TEST_VARIANT_ID_START + index, "title_ru": item["title_ru"]}
                        for index, item in enumerate(spec["variants"])
                    ]
                }

            def request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
                self.requests.append((method, path, kwargs))
                if method == "POST" and path == "/api_sellers/v2/offers":
                    return {"data": {"id": offer_id, **dict(kwargs.get("json") or {})}}
                return {"status": "ok"}

            @staticmethod
            def close() -> None:
                return None

        self_outer = self
        return FakeClient()

    def _sync_state(self) -> dict[str, object]:
        return {
            "offer_id": TEST_OFFER_ID,
            "option_id": TEST_OPTION_ID,
            "mappings": [
                {
                    "tariff_code": item["tariff_code"],
                    "external_variant_id": str(TEST_VARIANT_ID_START + index),
                }
                for index, item in enumerate(self.spec["variants"])
            ],
        }

    def _stateful_sync_client(
        self,
        *,
        notification: dict[str, object] | None,
        fail_first_offer_patch: bool = False,
    ):
        spec = self.spec
        old_offer = {
            **copy.deepcopy(spec["offer"]),
            "id": TEST_OFFER_ID,
            "status": "active",
            "price": 59,
            "title_ru": "Old title",
            "notification_settings": copy.deepcopy(notification),
        }
        old_variants = []
        for index, (variant_spec, payload) in enumerate(
            zip(spec["variants"], build_variant_payload(spec), strict=True)
        ):
            old_variants.append(
                {
                    "id": TEST_VARIANT_ID_START + index,
                    **payload,
                    "price": max(0, int(variant_spec["final_price_rub"]) - 59),
                }
            )

        class StatefulClient:
            def __init__(self) -> None:
                self.offer_state = copy.deepcopy(old_offer)
                self.variants = copy.deepcopy(old_variants)
                self.requests: list[tuple[str, str, dict[str, object]]] = []
                self.fail_first_offer_patch = fail_first_offer_patch

            def offer(self, offer_id: int) -> dict[str, object]:
                return copy.deepcopy(self.offer_state)

            @staticmethod
            def options(offer_id: int) -> list[dict[str, object]]:
                return [{"id": TEST_OPTION_ID, "title_ru": spec["option"]["title_ru"]}]

            def option(self, offer_id: int, option_id: int) -> dict[str, object]:
                return {
                    "id": option_id,
                    "title_ru": spec["option"]["title_ru"],
                    "variants": copy.deepcopy(self.variants),
                }

            def request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
                self.requests.append((method, path, kwargs))
                payload = copy.deepcopy(dict(kwargs.get("json") or {}))
                if method == "POST" and path.endswith("/variants"):
                    by_id = {int(item["id"]): item for item in self.variants}
                    for update in payload["variants"]:
                        by_id[int(update["id"])].update(update)
                    self.variants = [by_id[int(item["id"])] for item in self.variants]
                elif method == "PATCH" and path == f"/api_sellers/v2/offers/{TEST_OFFER_ID}":
                    if self.fail_first_offer_patch:
                        self.fail_first_offer_patch = False
                        raise GgselPublishError("synthetic PATCH failure")
                    self.offer_state.update(payload)
                return {"status": "ok"}

            @staticmethod
            def close() -> None:
                return None

        return StatefulClient(), old_offer, old_variants

    def test_existing_offer_sync_preserves_ids_prices_copy_and_callback(self) -> None:
        notification = build_notification_settings(
            "https://reseller.example.test", "sync-secret"
        )
        client, _, _ = self._stateful_sync_client(notification=notification)

        synced_offer, synced_option = sync_existing_offer(
            client=client,
            offer_id=TEST_OFFER_ID,
            option_id=TEST_OPTION_ID,
            offer=client.offer(TEST_OFFER_ID),
            option_detail=client.option(TEST_OFFER_ID, TEST_OPTION_ID),
            spec=self.spec,
            state=self._sync_state(),
            notification_settings=notification,
        )

        self.assertEqual(synced_offer["price"], 49)
        self.assertEqual(synced_offer["title_ru"], self.spec["offer"]["title_ru"])
        self.assertEqual(synced_offer["notification_settings"], notification)
        self.assertEqual(
            [item["id"] for item in synced_option["variants"]],
            list(range(TEST_VARIANT_ID_START, TEST_VARIANT_ID_START + len(self.spec["variants"]))),
        )
        self.assertEqual(
            [49 + int(item["price"]) for item in synced_option["variants"]],
            [item["final_price_rub"] for item in self.spec["variants"]],
        )

    def test_existing_offer_sync_rolls_back_variants_after_offer_patch_failure(self) -> None:
        client, old_offer, old_variants = self._stateful_sync_client(
            notification=None,
            fail_first_offer_patch=True,
        )
        configured = build_notification_settings(
            "https://reseller.example.test", "rollback-secret"
        )

        with self.assertRaisesRegex(GgselPublishError, "synthetic PATCH failure"):
            sync_existing_offer(
                client=client,
                offer_id=TEST_OFFER_ID,
                option_id=TEST_OPTION_ID,
                offer=client.offer(TEST_OFFER_ID),
                option_detail=client.option(TEST_OFFER_ID, TEST_OPTION_ID),
                spec=self.spec,
                state=self._sync_state(),
                notification_settings=configured,
            )

        self.assertEqual(client.offer_state, old_offer)
        self.assertEqual(client.variants, old_variants)

    def test_variant_mapping_mismatch_fails_before_any_write(self) -> None:
        client, _, _ = self._stateful_sync_client(notification=None)
        state = self._sync_state()
        state["mappings"][0]["external_variant_id"] = "99999999"

        with self.assertRaisesRegex(GgselPublishError, "does not match saved mapping"):
            prepare_existing_variant_sync(
                self.spec,
                state,
                client.option(TEST_OFFER_ID, TEST_OPTION_ID),
            )

        self.assertEqual(client.requests, [])

    def test_sync_offer_without_existing_state_fails_before_client_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "missing-state.json"
            with patch("scripts.publish_ggsel_offer.GgselV2Client") as client_class:
                with self.assertRaisesRegex(GgselPublishError, "requires existing positive"):
                    publish(
                        self._publish_args(
                            state_path,
                            sync_notifications=False,
                            sync_offer=True,
                        )
                    )

            client_class.assert_not_called()
            self.assertFalse(state_path.exists())

    def test_sync_offer_with_missing_live_option_performs_no_write(self) -> None:
        client, _, _ = self._stateful_sync_client(notification=None)
        client.options = lambda offer_id: []
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(self._sync_state()), encoding="utf-8")
            with (
                patch("scripts.publish_ggsel_offer.configured_notification_settings", return_value=None),
                patch("scripts.publish_ggsel_offer.load_api_key", return_value="api-key"),
                patch("scripts.publish_ggsel_offer.GgselV2Client", return_value=client),
            ):
                with self.assertRaisesRegex(GgselPublishError, "missing or has a different title"):
                    publish(
                        self._publish_args(
                            state_path,
                            sync_notifications=False,
                            sync_offer=True,
                        )
                    )

        self.assertEqual(client.requests, [])

    def test_sync_notifications_patches_existing_offer_without_persisting_url(self) -> None:
        notification = build_notification_settings(
            "https://reseller.example.test", "existing-offer-secret"
        )
        fake_client = self._fake_client()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps({"offer_id": TEST_OFFER_ID, "option_id": TEST_OPTION_ID}), encoding="utf-8"
            )
            with (
                patch("scripts.publish_ggsel_offer.configured_notification_settings", return_value=notification),
                patch("scripts.publish_ggsel_offer.load_api_key", return_value="api-key"),
                patch("scripts.publish_ggsel_offer.GgselV2Client", return_value=fake_client),
            ):
                result = publish(self._publish_args(state_path, sync_notifications=True))

            self.assertEqual(result["offer_id"], TEST_OFFER_ID)
            patch_calls = [request for request in fake_client.requests if request[0] == "PATCH"]
            self.assertEqual(len(patch_calls), 1)
            self.assertEqual(patch_calls[0][1], f"/api_sellers/v2/offers/{TEST_OFFER_ID}")
            self.assertEqual(patch_calls[0][2]["json"], {"notification_settings": notification})
            self.assertTrue(patch_calls[0][2]["sensitive"])
            persisted = state_path.read_text(encoding="utf-8")
            self.assertNotIn("existing-offer-secret", persisted)
            self.assertNotIn("/api/ggsel/notify/order/", persisted)

    def test_new_offer_includes_configured_notifications_without_sync_flag(self) -> None:
        notification = build_notification_settings(
            "https://reseller.example.test", "new-offer-secret"
        )
        fake_client = self._fake_client()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                patch("scripts.publish_ggsel_offer.configured_notification_settings", return_value=notification),
                patch("scripts.publish_ggsel_offer.load_api_key", return_value="api-key"),
                patch("scripts.publish_ggsel_offer.GgselV2Client", return_value=fake_client),
            ):
                result = publish(self._publish_args(state_path, sync_notifications=False))

            self.assertEqual(result["offer_id"], TEST_OFFER_ID)
            create_calls = [
                request
                for request in fake_client.requests
                if request[0:2] == ("POST", "/api_sellers/v2/offers")
            ]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(create_calls[0][2]["json"]["notification_settings"], notification)
            self.assertTrue(create_calls[0][2]["sensitive"])
            persisted = state_path.read_text(encoding="utf-8")
            self.assertNotIn("new-offer-secret", persisted)
            self.assertNotIn("/api/ggsel/notify/order/", persisted)


if __name__ == "__main__":
    unittest.main()
