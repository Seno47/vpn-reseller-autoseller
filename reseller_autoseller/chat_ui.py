from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_TEXT_LIMIT = 3900


ROLE_DETAILS: dict[str, dict[str, tuple[str, str]]] = {
    "buyer": {"ru": ("👤", "Покупатель"), "en": ("👤", "Buyer")},
    "seller": {"ru": ("🧑‍💻", "Продавец"), "en": ("🧑‍💻", "Seller")},
    "admin": {"ru": ("✍️", "Ответ из Telegram"), "en": ("✍️", "Telegram reply")},
    "bot": {"ru": ("🤖", "Бот"), "en": ("🤖", "Bot")},
    "system": {"ru": ("⚙️", "Система"), "en": ("⚙️", "System")},
}

MARKETPLACE_LABELS = {
    "plati": "Plati.Market · DigiSeller",
    "digiseller": "DigiSeller",
    "ggsel": "GGsel",
}


def normalized_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    return role if role in ROLE_DETAILS else "system"


def role_details(role: Any, language: str = "ru") -> tuple[str, str]:
    selected = normalized_role(role)
    locale = "en" if language == "en" else "ru"
    return ROLE_DETAILS[selected][locale]


def marketplace_label(value: Any) -> str:
    raw = str(value or "").strip()
    return MARKETPLACE_LABELS.get(raw.lower(), raw or "DigiSeller")


def _message_role_details(row: dict[str, Any], language: str) -> tuple[str, str]:
    icon, label = role_details(row.get("role"), language)
    author_name = compact_plain_text(row.get("author_name"), 80)
    if author_name and normalized_role(row.get("role")) in {"admin", "seller"}:
        label = f"{label} · {author_name}"
    return icon, label


def compact_plain_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _display_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed.strftime("%d.%m %H:%M")
    except ValueError:
        return raw[:16]


def digiseller_chat_url(external_order_id: Any) -> str:
    # DigiSeller does not document the seller-cabinet deep link for a buyer
    # debate. Use the verified "My sales" entry point instead of inventing a
    # route that may break or lead to another correspondence section.
    return "https://my.digiseller.com/inside/account.asp"


def chat_actions_reply_markup(message_id: int, external_order_id: Any) -> dict[str, Any]:
    anchor = max(1, int(message_id))
    return {
        "inline_keyboard": [
            [
                {"text": "✍️ Ответить", "callback_data": f"chat:reply:{anchor}"},
                {"text": "📚 Больше", "callback_data": f"chat:more:{anchor}"},
            ],
            [{"text": "⚡ Заготовки", "callback_data": f"chat:templates:{anchor}"}],
            [{"text": "🔗 Открыть продажи DigiSeller", "url": digiseller_chat_url(external_order_id)}],
        ]
    }


def _message_body(row: dict[str, Any], *, limit: int) -> str:
    text = str(row.get("text") or "").strip()
    file_name = str(row.get("file_name") or "").strip()
    file_url = str(row.get("file_url") or "").strip()
    parts = [text] if text else []
    if file_name:
        parts.append(f"📎 {file_name}")
    if file_url:
        parts.append(file_url)
    return compact_plain_text("\n".join(parts) or "(пустое сообщение)", max(1, limit))


def format_chat_notification(row: dict[str, Any], language: str = "ru") -> str:
    icon, role_label = _message_role_details(row, language)
    marketplace = escape(marketplace_label(row.get("marketplace")))
    order_id = escape(str(row.get("external_order_id") or ""))
    when = _display_time(row.get("message_date") or row.get("created_at"))
    body = escape(_message_body(row, limit=1200))
    role = normalized_role(row.get("role"))
    titles = {
        "buyer": ("Новое сообщение покупателя", "New buyer message"),
        "seller": ("Сообщение продавца", "Seller message"),
        "admin": ("Ответ оператора", "Operator reply"),
        "bot": ("Сообщение бота", "Bot message"),
        "system": ("Системное сообщение", "System message"),
    }
    title = titles[role][1 if language == "en" else 0]
    order_label = "Заказ" if language != "en" else "Order"
    source_label = "Площадка" if language != "en" else "Marketplace"
    lines = [
        f"💬 <b>{title}</b>",
        f"{icon} <b>{escape(role_label)}</b>",
        f"🧾 {order_label}: <code>{order_id}</code>",
        f"🛒 {source_label}: <b>{marketplace}</b>",
    ]
    if when:
        lines.append(f"🕒 <code>{escape(when)}</code>")
    lines.append(f"\n<blockquote>{body}</blockquote>")
    return "\n".join(lines)


def _history_block(row: dict[str, Any], language: str, *, body_limit: int = 1800) -> str:
    icon, role_label = _message_role_details(row, language)
    when = _display_time(row.get("message_date") or row.get("created_at"))
    meta = f"{icon} <b>{escape(role_label)}</b>"
    if when:
        meta += f" · <code>{escape(when)}</code>"
    body = escape(_message_body(row, limit=body_limit))
    return f"{meta}\n<blockquote>{body}</blockquote>"


def _fitted_history_block(row: dict[str, Any], language: str, max_length: int) -> str:
    """Return the largest complete HTML block that fits, never cutting an HTML entity/tag."""
    if max_length <= 0:
        return ""
    smallest = _history_block(row, language, body_limit=1)
    if len(smallest) > max_length:
        return ""
    low, high = 1, 1800
    best = smallest
    while low <= high:
        middle = (low + high) // 2
        candidate = _history_block(row, language, body_limit=middle)
        if len(candidate) <= max_length:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def format_chat_history(
    messages: list[dict[str, Any]],
    *,
    marketplace: str,
    external_order_id: str,
    language: str = "ru",
    limit: int = TELEGRAM_SAFE_TEXT_LIMIT,
) -> str:
    selected_limit = max(1, min(int(limit), TELEGRAM_TEXT_LIMIT))
    title = "Диалог с покупателем" if language != "en" else "Buyer conversation"
    order_label = "Заказ" if language != "en" else "Order"
    empty_label = "История пока пуста." if language != "en" else "The history is empty."
    header = (
        f"📚 <b>{title}</b>\n"
        f"🛒 <b>{escape(compact_plain_text(marketplace_label(marketplace), 80))}</b> · {order_label} "
        f"<code>{escape(compact_plain_text(external_order_id, 160))}</code>"
    )
    if len(header) > selected_limit:
        # A deliberately tiny custom limit cannot fit the decorated header.
        # Plain escaped text remains valid Telegram HTML and still honors it.
        return escape(compact_plain_text(f"📚 {title} · {marketplace} · {order_label} {external_order_id}", selected_limit))
    if not messages:
        rendered_empty = f"{header}\n\n{empty_label}"
        if len(rendered_empty) <= selected_limit:
            return rendered_empty
        return header

    selected_reversed: list[str] = []
    used = len(header) + 2
    # When older messages are omitted, reserve space for that fact before
    # filling the message. This keeps every emitted HTML tag complete.
    reserve = 0
    if len(messages) > 1:
        maximum_suffix = (
            f"\n\n… ещё {len(messages)} старых сообщений не поместилось"
            if language != "en"
            else f"\n\n… {len(messages)} older messages did not fit"
        )
        reserve = len(maximum_suffix)
    for row in reversed(messages):
        block = _history_block(row, language)
        extra = len(block) + 2
        if used + extra + reserve <= selected_limit:
            selected_reversed.append(block)
            used += extra
            continue
        if not selected_reversed:
            available = selected_limit - used - reserve
            fitted = _fitted_history_block(row, language, available)
            if fitted:
                selected_reversed.append(fitted)
        break

    selected = list(reversed(selected_reversed))
    omitted = max(0, len(messages) - len(selected))
    suffix = ""
    if omitted:
        suffix = (
            f"\n\n… ещё {omitted} старых сообщений не поместилось"
            if language != "en"
            else f"\n\n… {omitted} older messages did not fit"
        )
    rendered = f"{header}\n\n" + "\n\n".join(selected) + suffix
    if len(rendered) <= selected_limit:
        return rendered
    # This can only occur for very small custom limits where even the
    # decorated block could not fit. Returning the header keeps valid HTML.
    return header
