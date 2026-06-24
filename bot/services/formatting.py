from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any

from .money import format_amount_compact, to_decimal


def short_money(amount: Decimal, currency: str) -> str:
    return f"{format_amount_compact(amount)} {currency}"


def format_money(amount: Decimal, currency: str) -> str:
    return f"{format_amount_compact(amount)} {escape(currency)}"


def balance_summary_for_button(open_rows: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for row in open_rows:
        currency = str(row.get("currency", "")).upper()
        they_owe_you = to_decimal(row.get("they_owe_you"))
        you_owe = to_decimal(row.get("you_owe"))
        if you_owe > Decimal("0"):
            chunks.append(f"You owe {short_money(you_owe, currency)}")
        elif they_owe_you > Decimal("0"):
            chunks.append(f"Owes you {short_money(they_owe_you, currency)}")

    return ", ".join(chunks) if chunks else "settled"


def balance_line_for_friend(friend_label: str, row: dict[str, Any]) -> str | None:
    currency = str(row.get("currency", "")).upper()
    they_owe_you = to_decimal(row.get("they_owe_you"))
    you_owe = to_decimal(row.get("you_owe"))
    if you_owe > Decimal("0"):
        return f"You owe {friend_label} {short_money(you_owe, currency)}"
    if they_owe_you > Decimal("0"):
        return f"{friend_label} owes you {short_money(they_owe_you, currency)}"
    return None


def truncate_button_label(value: str, max_len: int = 60) -> str:
    cleaned = value.strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def profile_label(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "friend"

    display_name = str(
        profile.get("display_name")
        or profile.get("whatsapp_display_name")
        or ""
    ).strip()
    if display_name:
        return display_name

    username = str(
        profile.get("telegram_username")
        or profile.get("username")
        or ""
    ).strip()
    if username:
        return f"@{username.lstrip('@')}"

    whatsapp_user_id = profile.get("whatsapp_user_id")
    if whatsapp_user_id is not None:
        return f"WhatsApp {whatsapp_user_id}"

    telegram_user_id = profile.get("telegram_user_id")
    if telegram_user_id is not None:
        return f"user {telegram_user_id}"

    provider_user_id = profile.get("provider_user_id")
    if provider_user_id is not None:
        return f"user {provider_user_id}"

    return "friend"
