from __future__ import annotations

import logging
import re
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID

from ..services.formatting import balance_summary_for_button, format_money, profile_label, truncate_button_label
from ..services.money import parse_amount_and_currency, to_decimal
from .client import ListRow, ReplyButton, WhatsAppClient
from .config import WhatsAppSettings
from .db import WhatsAppDatabase
from .parser import WhatsAppMessage

logger = logging.getLogger(__name__)

BUTTON_IN = "In"
BUTTON_BALANCE = "Balance"
BUTTON_CLOSE = "Close"

ACTION_IN = "menu:in"
ACTION_BALANCE = "menu:balance"
ACTION_CLOSE = "menu:close"
ACTION_CLOSE_PREFIX = "close:"
SESSION_WAITING_AMOUNT = "waiting_amount"

_APPROVE_RE = re.compile(r"^\s*approve\s+([A-Za-z0-9]+)\s*$", re.IGNORECASE)


async def handle_message(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
    settings: WhatsAppSettings,
) -> None:
    if not db.mark_event_processed(event.message_id):
        logger.info("Skipping duplicate WhatsApp message_id=%s", event.message_id)
        return

    action = _action_from_event(event)
    text = (event.text or "").strip()
    session_state = db.get_session_state(event.from_user_id)

    try:
        if action == ACTION_IN or _is_text_command(text, BUTTON_IN):
            await _handle_in(event=event, db=db, client=client)
            return

        if action == ACTION_BALANCE or _is_text_command(text, BUTTON_BALANCE):
            await _handle_balance(event=event, db=db, client=client)
            return

        if action == ACTION_CLOSE or _is_text_command(text, BUTTON_CLOSE):
            await _handle_close(event=event, db=db, client=client)
            return

        if event.list_reply_id and event.list_reply_id.startswith(ACTION_CLOSE_PREFIX):
            await _handle_close_reply(event=event, db=db, client=client)
            return

        approval_code = _parse_approval_code(text)
        if approval_code is not None:
            await _handle_approve(event=event, db=db, client=client, request_code=approval_code)
            return

        if session_state == SESSION_WAITING_AMOUNT:
            await _handle_amount(event=event, db=db, client=client, settings=settings, raw_text=text)
            return

        await _send_main_menu(
            client=client,
            to=event.from_user_id,
            body="Use the buttons below.",
        )
    except Exception:
        logger.exception("Failed to handle WhatsApp message_id=%s", event.message_id)
        await client.send_text(to=event.from_user_id, text="Something went wrong. Try again.")


async def _handle_in(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
) -> None:
    db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    db.set_session_state(event.from_user_id, SESSION_WAITING_AMOUNT)
    await client.send_text(
        to=event.from_user_id,
        text="How much did you get?\nSend amount like: 120 or 120 USD.",
    )


async def _handle_amount(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
    settings: WhatsAppSettings,
    raw_text: str,
) -> None:
    if not raw_text:
        await client.send_text(to=event.from_user_id, text="Send amount like: 120 or 120 USD.")
        return

    try:
        amount, currency = parse_amount_and_currency(raw_text, settings.DEFAULT_CURRENCY)
    except ValueError as exc:
        await client.send_text(
            to=event.from_user_id,
            text=f"{exc}\nSend amount like: 120 or 120 USD.",
        )
        return

    requester_profile = db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    requester_id = str(requester_profile.get("id", ""))
    if not requester_id:
        await client.send_text(to=event.from_user_id, text="Profile data is incomplete. Try again.")
        return

    request_row = db.create_payment_request(
        requester_id=requester_id,
        amount=amount,
        currency=currency,
    )
    request_code = str(request_row.get("code", "")).upper()
    db.clear_session(event.from_user_id)

    approval_link = _build_approval_link(
        business_phone=settings.WHATSAPP_BUSINESS_PHONE,
        request_code=request_code,
    )
    await client.send_text(
        to=event.from_user_id,
        text=(
            "Loan request is ready.\n"
            "Forward this link to the person who gave you money:\n\n"
            f"{approval_link}\n\n"
            f"Amount: {format_money(amount, currency)}"
        ),
    )


async def _handle_balance(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
) -> None:
    viewer_profile = db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await client.send_text(to=event.from_user_id, text="Profile data is incomplete. Try again.")
        return

    open_balances = db.list_open_balances(viewer_id)
    if not open_balances:
        await client.send_text(
            to=event.from_user_id,
            text="No open balances.\nTap In when someone lends you money.",
        )
        return

    lines = ["Your balance:"]
    for item in sorted(
        open_balances,
        key=lambda row: profile_label(row.get("friend_profile", {})).lower(),
    ):
        friend_label = profile_label(item.get("friend_profile", {}))
        for row in item.get("open_rows", []):
            currency = str(row.get("currency", "")).upper()
            they_owe_you = to_decimal(row.get("they_owe_you"))
            you_owe = to_decimal(row.get("you_owe"))
            if you_owe > Decimal("0"):
                lines.append(f"{friend_label} + {format_money(you_owe, currency)}")
            elif they_owe_you > Decimal("0"):
                lines.append(f"{friend_label} - {format_money(they_owe_you, currency)}")

    await client.send_text(to=event.from_user_id, text="\n".join(lines))


async def _handle_close(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
) -> None:
    viewer_profile = db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await client.send_text(to=event.from_user_id, text="Profile data is incomplete. Try again.")
        return

    open_balances = db.list_open_balances(viewer_id)
    rows: list[ListRow] = []
    for item in sorted(
        open_balances,
        key=lambda row: profile_label(row.get("friend_profile", {})).lower(),
    ):
        friend_profile = item.get("friend_profile", {})
        friend_id = str(friend_profile.get("id", ""))
        if not friend_id:
            continue

        summary = balance_summary_for_button(item.get("open_rows", []))
        title = truncate_button_label(profile_label(friend_profile), max_len=24)
        description = truncate_button_label(summary, max_len=72)
        rows.append(
            ListRow(
                id=f"{ACTION_CLOSE_PREFIX}{friend_id}",
                title=title,
                description=description,
            )
        )
        if len(rows) >= 10:
            break

    if not rows:
        await client.send_text(to=event.from_user_id, text="No open balances to close.")
        return

    await client.send_list(
        to=event.from_user_id,
        body="Choose a person to close.\nThis sets your mutual balance to 0.",
        button_text="Choose",
        rows=rows,
    )


async def _handle_close_reply(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
) -> None:
    friend_id = (event.list_reply_id or "")[len(ACTION_CLOSE_PREFIX) :].strip()
    try:
        friend_uuid = str(UUID(friend_id))
    except ValueError:
        await client.send_text(to=event.from_user_id, text="Invalid selection.")
        return

    viewer_profile = db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await client.send_text(to=event.from_user_id, text="Profile data is incomplete. Try again.")
        return

    try:
        closed_currencies = db.close_friend_balances(
            viewer_id=viewer_id,
            friend_id=friend_uuid,
        )
    except ValueError:
        await client.send_text(to=event.from_user_id, text="Friendship not found.")
        return

    friend_profile = db.get_profile_by_id(friend_uuid)
    friend_label = profile_label(friend_profile)
    if not closed_currencies:
        await client.send_text(to=event.from_user_id, text=f"{friend_label} is already settled.")
        return

    await client.send_text(
        to=event.from_user_id,
        text=f"Closed balance with {friend_label} for {', '.join(closed_currencies)}.",
    )


async def _handle_approve(
    *,
    event: WhatsAppMessage,
    db: WhatsAppDatabase,
    client: WhatsAppClient,
    request_code: str,
) -> None:
    actor_profile = db.get_or_create_profile(
        whatsapp_user_id=event.from_user_id,
        display_name=event.display_name,
    )
    actor_id = str(actor_profile.get("id", ""))
    if not actor_id:
        await client.send_text(to=event.from_user_id, text="Profile data is incomplete. Try again.")
        return

    try:
        request_row, tx_row, changed = db.approve_payment_request(
            code=request_code,
            approver_id=actor_id,
        )
    except ValueError as exc:
        await client.send_text(to=event.from_user_id, text=_approval_error_text(str(exc)))
        return

    amount = to_decimal(request_row.get("amount"))
    currency = str(request_row.get("currency", "")).upper()
    if changed:
        await client.send_text(
            to=event.from_user_id,
            text=f"Confirmed. The debt was saved.\nAmount: {format_money(amount, currency)}",
        )
    else:
        await client.send_text(to=event.from_user_id, text="This request was already confirmed.")

    requester_id = str(request_row.get("requester_id", ""))
    requester_profile = db.get_profile_by_id(requester_id) if requester_id else None
    requester_whatsapp_id = requester_profile.get("whatsapp_user_id") if requester_profile else None
    if changed and requester_whatsapp_id:
        approver_label = profile_label(actor_profile)
        tx_id = str(tx_row.get("id", "")) if tx_row else ""
        tx_suffix = tx_id[-8:] if tx_id else "--------"
        try:
            await client.send_text(
                to=str(requester_whatsapp_id),
                text=(
                    f"Loan confirmed by {approver_label}.\n"
                    f"Saved: {format_money(amount, currency)}\n"
                    f"Transaction: {tx_suffix}"
                ),
            )
        except Exception:
            logger.info("Failed to notify requester whatsapp_user_id=%s", requester_whatsapp_id, exc_info=True)


async def _send_main_menu(*, client: WhatsAppClient, to: str, body: str) -> None:
    await client.send_reply_buttons(
        to=to,
        body=body,
        buttons=[
            ReplyButton(id=ACTION_IN, title=BUTTON_IN),
            ReplyButton(id=ACTION_BALANCE, title=BUTTON_BALANCE),
            ReplyButton(id=ACTION_CLOSE, title=BUTTON_CLOSE),
        ],
    )


def _action_from_event(event: WhatsAppMessage) -> str | None:
    if event.button_id in {ACTION_IN, ACTION_BALANCE, ACTION_CLOSE}:
        return event.button_id
    return None


def _is_text_command(text: str, command: str) -> bool:
    return text.strip().casefold() == command.casefold()


def _parse_approval_code(text: str) -> str | None:
    match = _APPROVE_RE.match(text)
    if match is None:
        return None
    return match.group(1).upper()


def _approval_error_text(error_code: str) -> str:
    if error_code == "REQUEST_NOT_FOUND":
        return "Request not found."
    if error_code == "REQUEST_SELF_APPROVAL":
        return "You cannot approve your own request."
    if error_code == "REQUEST_PROCESSING":
        return "This request is being approved now. Try again in a moment."
    return "This request is no longer pending."


def _build_approval_link(*, business_phone: str, request_code: str) -> str:
    text = quote(f"approve {request_code}")
    return f"https://wa.me/{business_phone}?text={text}"
