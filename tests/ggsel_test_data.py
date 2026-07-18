from __future__ import annotations

from typing import Any


TEST_OFFER_ID = 5001
TEST_OPTION_ID = 6001
TEST_VARIANT_ID_START = 7001


def make_ggsel_offer_spec() -> dict[str, Any]:
    """Return synthetic publisher data with no seller or live-product content."""

    tariff_codes = [
        "lite_weekly",
        "pro_weekly",
        "lite_monthly",
        "pro_monthly",
        "lite_3m",
        "pro_3m",
        "lite_6m",
        "pro_6m",
        "lite_1y",
        "pro_1y",
        "lite_2y",
        "pro_2y",
    ]
    variants = [
        {
            "tariff_code": tariff_code,
            "title_ru": f"Тестовый тариф {index + 1}",
            "title_en": f"Test plan {index + 1}",
            "final_price_rub": 49 + index * 50,
        }
        for index, tariff_code in enumerate(tariff_codes)
    ]
    return {
        "category_id": 1,
        "category_tree": [1],
        "category_fee": 0,
        "minimum_variant_price_rub": 49,
        "cover_image_ru_path": "cover.jpg",
        "offer": {
            "title_ru": "Тестовый товар",
            "title_en": "Test product",
            "description_ru": "Синтетическое описание для тестов.",
            "description_en": "Synthetic description for tests.",
            "instructions_ru": "Синтетическая инструкция для тестов.",
            "instructions_en": "Synthetic instructions for tests.",
            "price": 49,
            "currency": "RUB",
            "is_autoselling": True,
            "min_quantity": 1,
            "max_quantity": 1,
            "is_unlimited_quantity": True,
            "post_payment_url": None,
            "delivery": "manual",
        },
        "option": {
            "type": "select",
            "status": "active",
            "has_splitted_products": False,
            "title_ru": "Тестовый вариант",
            "title_en": "Test variant",
            "comment_ru": "",
            "comment_en": "",
            "is_required": True,
            "is_price_modifier_hidden": True,
            "position": 1,
        },
        "variants": variants,
        "delivery_template_ru": "Заказ: {ORDER_ID}\n{SUBSCRIPTION_URL}",
    }
