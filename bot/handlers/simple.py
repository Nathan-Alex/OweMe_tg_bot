from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from ..config import Settings
from ..currency import is_currency_token, normalize_currency_code
from ..db import Database

router = Router(name="simple")
logger = logging.getLogger(__name__)

BUTTON_IN = "In"
BUTTON_BALANCE = "Balance"
BUTTON_CLOSE = "Close"

PAYLOAD_PREFIX = "pay_"
CALLBACK_PAY_APPROVE_PREFIX = "payapprove"
CALLBACK_CLOSE_PREFIX = "closefriend"

_TWO_DP = Decimal("0.01")


# TODO(refactor): Move to handlers/payments.py with the payment flow handlers.
class InFlow(StatesGroup):
    waiting_amount = State()


# TODO(refactor): Move to keyboards/common.py.
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_IN)],
            [KeyboardButton(text=BUTTON_BALANCE), KeyboardButton(text=BUTTON_CLOSE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap In, Balance, or Close",
    )


@router.message(CommandStart())
# TODO(refactor): Move to handlers/common.py.
async def handle_start(
    message: Message,
    command: CommandObject,
    db: Database,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        await message.answer("Unable to resolve user context.")
        return

    await state.clear()

    profile = db.get_or_create_profile(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=_display_name_from_message(message),
    )

    payload = (command.args or "").strip()
    if not payload.startswith(PAYLOAD_PREFIX):
        await message.answer(
            "Use the buttons below:\n"
            "• In - log money you got and forward a loan link\n"
            "• Balance - show open debts\n"
            "• Close - set balance with one person to 0\n\n"
            "Main action: tap In.",
            reply_markup=main_keyboard(),
        )
        return

    request_code = payload[len(PAYLOAD_PREFIX) :].strip().upper()
    await message.answer("Menu ready below.", reply_markup=main_keyboard())
    await _show_payment_request_for_approval(
        message=message,
        db=db,
        viewer_profile=profile,
        request_code=request_code,
    )


@router.message(F.text == BUTTON_IN)
# TODO(refactor): Move to handlers/payments.py.
async def handle_in_button(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(InFlow.waiting_amount)
    default_currency = normalize_currency_code(settings.DEFAULT_CURRENCY)
    await message.answer(
        "How much did you get?\n"
        "Send amount like: 120 or 120 USD.\n"
        f"If you send only a number, currency is {default_currency}."
    )


@router.message(
    StateFilter(InFlow.waiting_amount),
    ~F.text.in_({BUTTON_IN, BUTTON_BALANCE, BUTTON_CLOSE}),
)
# TODO(refactor): Move to handlers/payments.py.
async def handle_in_amount(
    message: Message,
    settings: Settings,
    db: Database,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        await message.answer("Unable to resolve user context.")
        return

    raw_text = (message.text or "").strip()
    if not raw_text:
        await message.answer("Send amount like: 120 or 120 USD.")
        return

    try:
        amount, currency = _parse_amount_and_currency(raw_text, settings.DEFAULT_CURRENCY)
    except ValueError as exc:
        await message.answer(f"{exc}\nSend amount like: 120 or 120 USD.")
        return

    requester_profile = db.get_or_create_profile(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=_display_name_from_message(message),
    )
    requester_id = str(requester_profile.get("id", ""))
    if not requester_id:
        await message.answer("Profile data is incomplete. Try again.")
        return

    try:
        request_row = db.create_payment_request(
            requester_id=requester_id,
            amount=amount,
            currency=currency,
        )
    except Exception:
        logger.exception("Failed to create payment request")
        await message.answer("Failed to create request. Try again.")
        return
    request_code = str(request_row.get("code", "")).upper()

    deep_link = await _build_request_link(message=message, settings=settings, request_code=request_code)
    if deep_link is None:
        await message.answer(
            "Request created but I cannot build a Telegram link. "
            "Set BOT_USERNAME in .env and restart."
        )
        await state.clear()
        return

    await message.answer(
        "Loan request is ready.\n"
        "Tap the button to forward it quickly.\n\n"
        f"{deep_link}\n\n"
        f"Amount: {_format_money(amount, currency)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Forward Loan",
                        url=_build_share_url(deep_link=deep_link, amount=amount, currency=currency),
                    )
                ]
            ]
        ),
        disable_web_page_preview=True,
    )

    await state.clear()


@router.message(F.text == BUTTON_BALANCE)
# TODO(refactor): Move to handlers/balances.py.
async def handle_balance_button(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        await message.answer("Unable to resolve user context.")
        return

    await state.clear()

    viewer_profile = db.get_or_create_profile(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=_display_name_from_message(message),
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await message.answer("Profile data is incomplete. Try again.")
        return

    open_balances = db.list_open_balances(viewer_id)
    if not open_balances:
        await message.answer("No open balances.\nTap In when someone lends you money.")
        return

    lines = ["Your balance:"]
    for item in sorted(
        open_balances,
        key=lambda row: _profile_label(row.get("friend_profile", {})).lower(),
    ):
        friend_profile = item.get("friend_profile", {})
        friend_label = _profile_label(friend_profile)
        for row in item.get("open_rows", []):
            currency = str(row.get("currency", "")).upper()
            they_owe_you = _to_decimal(row.get("they_owe_you"))
            you_owe = _to_decimal(row.get("you_owe"))
            if you_owe > Decimal("0"):
                lines.append(f"{escape(friend_label)} + {_format_money(you_owe, currency)}")
            elif they_owe_you > Decimal("0"):
                lines.append(f"{escape(friend_label)} - {_format_money(they_owe_you, currency)}")

    if len(lines) == 1:
        await message.answer("No open balances.\nTap In when someone lends you money.")
        return

    await message.answer("\n".join(lines))


@router.message(F.text == BUTTON_CLOSE)
# TODO(refactor): Move to handlers/settlements.py.
async def handle_close_button(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        await message.answer("Unable to resolve user context.")
        return

    await state.clear()

    viewer_profile = db.get_or_create_profile(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=_display_name_from_message(message),
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await message.answer("Profile data is incomplete. Try again.")
        return

    open_balances = db.list_open_balances(viewer_id)
    if not open_balances:
        await message.answer("No open balances to close.")
        return

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for item in sorted(
        open_balances,
        key=lambda row: _profile_label(row.get("friend_profile", {})).lower(),
    ):
        friend_profile = item.get("friend_profile", {})
        friend_id = str(friend_profile.get("id", ""))
        if not friend_id:
            continue

        summary = _balance_summary_for_button(item.get("open_rows", []))
        label = _truncate_button_label(f"{_profile_label(friend_profile)} ({summary})")
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CALLBACK_CLOSE_PREFIX}:{friend_id}",
                )
            ]
        )

    if not keyboard_rows:
        await message.answer("No open balances to close.")
        return

    await message.answer(
        "Choose a person to close.\nThis sets your mutual balance to 0.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@router.callback_query(F.data.startswith(f"{CALLBACK_PAY_APPROVE_PREFIX}:"))
# TODO(refactor): Move to handlers/payments.py; delegate approval to DebtService.
async def handle_pay_approve_callback(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user is None:
        await callback.answer("Unable to resolve user.", show_alert=True)
        return

    request_code = _parse_callback_suffix(callback.data, CALLBACK_PAY_APPROVE_PREFIX)
    if request_code is None:
        await callback.answer("Invalid approval link.", show_alert=True)
        return

    actor_profile = db.get_or_create_profile(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        display_name=_display_name_from_callback(callback),
    )
    actor_id = str(actor_profile.get("id", ""))
    if not actor_id:
        await callback.answer("Profile data is incomplete.", show_alert=True)
        return

    try:
        request_row, tx_row, changed = db.approve_payment_request(
            code=request_code,
            approver_id=actor_id,
        )
    except ValueError as exc:
        error_code = str(exc)
        if error_code == "REQUEST_NOT_FOUND":
            await callback.answer("Request not found.", show_alert=True)
            return
        if error_code == "REQUEST_SELF_APPROVAL":
            await callback.answer("You cannot approve your own request.", show_alert=True)
            return
        if error_code == "REQUEST_PROCESSING":
            await callback.answer("This request is being approved now. Try again in a moment.", show_alert=True)
            return
        await callback.answer("This request is no longer pending.", show_alert=True)
        return
    except Exception:
        logger.exception("Failed to approve payment request")
        await callback.answer("Failed to approve request.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)

    requester_id = str(request_row.get("requester_id", ""))
    requester_profile = db.get_profile_by_id(requester_id) if requester_id else None
    amount = _to_decimal(request_row.get("amount"))
    currency = str(request_row.get("currency", "")).upper()

    if changed:
        await callback.answer("Confirmed")
        if callback.message:
            await callback.message.answer(
                "Confirmed. The debt was saved."
            )
        await _notify_requester_about_approval(
            callback=callback,
            requester_profile=requester_profile,
            approver_profile=actor_profile,
            amount=amount,
            currency=currency,
            tx_row=tx_row,
        )
        return

    await callback.answer("Already confirmed")
    if callback.message:
        await callback.message.answer("This request was already confirmed.")


@router.callback_query(F.data.startswith(f"{CALLBACK_CLOSE_PREFIX}:"))
# TODO(refactor): Move to handlers/settlements.py; delegate settlement to DebtService.
async def handle_close_callback(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user is None:
        await callback.answer("Unable to resolve user.", show_alert=True)
        return

    friend_id = _parse_callback_suffix(callback.data, CALLBACK_CLOSE_PREFIX)
    if friend_id is None:
        await callback.answer("Invalid selection.", show_alert=True)
        return

    try:
        friend_uuid = str(UUID(friend_id))
    except ValueError:
        await callback.answer("Invalid selection.", show_alert=True)
        return

    viewer_profile = db.get_or_create_profile(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        display_name=_display_name_from_callback(callback),
    )
    viewer_id = str(viewer_profile.get("id", ""))
    if not viewer_id:
        await callback.answer("Profile data is incomplete.", show_alert=True)
        return

    try:
        closed_currencies = db.close_friend_balances(
            viewer_id=viewer_id,
            friend_id=friend_uuid,
        )
    except ValueError:
        await callback.answer("Friendship not found.", show_alert=True)
        return
    except Exception:
        logger.exception("Failed to close balances")
        await callback.answer("Failed to close balance.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)

    friend_profile = db.get_profile_by_id(friend_uuid)
    friend_label = _profile_label(friend_profile)

    if not closed_currencies:
        await callback.answer("Already settled")
        if callback.message:
            await callback.message.answer(f"{escape(friend_label)} is already settled.")
        return

    await callback.answer("Closed")
    if callback.message:
        await callback.message.answer(
            f"Closed balance with {escape(friend_label)} for {', '.join(closed_currencies)}."
        )


@router.message(F.text)
# TODO(refactor): Move to handlers/common.py.
async def handle_unknown_text(message: Message) -> None:
    await message.answer(
        "Use the 3 buttons below:\n"
        "• In - log money you got\n"
        "• Balance - show open debts\n"
        "• Close - set one balance to 0"
    )


# TODO(refactor): Move to handlers/payments.py.
async def _show_payment_request_for_approval(
    *,
    message: Message,
    db: Database,
    viewer_profile: dict[str, Any],
    request_code: str,
) -> None:
    request_row = db.get_payment_request_by_code(request_code)
    if request_row is None:
        await message.answer("This loan link is invalid or expired.")
        return

    requester_id = str(request_row.get("requester_id", ""))
    viewer_id = str(viewer_profile.get("id", ""))

    amount = _to_decimal(request_row.get("amount"))
    currency = str(request_row.get("currency", "")).upper()
    status = str(request_row.get("status", "")).lower()

    requester_profile = db.get_profile_by_id(requester_id) if requester_id else None
    requester_label = _profile_label(requester_profile)

    if requester_id == viewer_id:
        await message.answer(
            "This is your loan link.\n"
            f"Forward it to the person who gave you {_format_money(amount, currency)}."
        )
        return

    if status == "approved":
        await message.answer(
            "This loan is already confirmed.\n"
            f"Amount: {_format_money(amount, currency)}"
        )
        return

    if status != "pending":
        await message.answer("This loan request is no longer pending.")
        return

    await message.answer(
        f"{escape(requester_label)} says you owe him {_format_money(amount, currency)}.\n"
        "Tap Approve only if this is correct.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Approve",
                        callback_data=f"{CALLBACK_PAY_APPROVE_PREFIX}:{request_code}",
                    )
                ]
            ]
        ),
    )


# TODO(refactor): Move to handlers/payments.py or a Telegram notification module.
async def _notify_requester_about_approval(
    *,
    callback: CallbackQuery,
    requester_profile: dict[str, Any] | None,
    approver_profile: dict[str, Any],
    amount: Decimal,
    currency: str,
    tx_row: dict[str, Any] | None,
) -> None:
    if not requester_profile:
        return

    requester_tg_id = requester_profile.get("telegram_user_id")
    if requester_tg_id is None:
        return

    approver_label = _profile_label(approver_profile)
    tx_id = str(tx_row.get("id", "")) if tx_row else ""
    tx_suffix = tx_id[-8:] if tx_id else "--------"

    text = (
        f"Loan confirmed by {escape(approver_label)}.\n"
        f"Saved: {_format_money(amount, currency)}\n"
        f"Transaction: {escape(tx_suffix)}"
    )

    try:
        await callback.bot.send_message(chat_id=int(requester_tg_id), text=text)
    except TelegramForbiddenError:
        logger.info("Requester has blocked bot telegram_user_id=%s", requester_tg_id)
    except Exception:
        logger.info("Failed to notify requester telegram_user_id=%s", requester_tg_id, exc_info=True)


# TODO(refactor): Move to utils/links.py and pass the bot username explicitly.
async def _build_request_link(message: Message, settings: Settings, request_code: str) -> str | None:
    bot_username = settings.BOT_USERNAME
    if not bot_username:
        me = await message.bot.get_me()
        bot_username = me.username

    if not bot_username:
        return None

    clean_username = bot_username.strip().lstrip("@")
    if not clean_username:
        return None

    return f"https://t.me/{clean_username}?start={PAYLOAD_PREFIX}{request_code}"


# TODO(refactor): Move to utils/links.py.
def _build_share_url(*, deep_link: str, amount: Decimal, currency: str) -> str:
    text = f"You owe me {_short_money(amount, currency)}"
    query = urlencode({"url": deep_link, "text": text}, quote_via=quote)
    return f"https://t.me/share/url?{query}"


# TODO(refactor): Move to utils/callbacks.py.
def _parse_callback_suffix(data: str | None, prefix: str) -> str | None:
    if not data:
        return None

    parts = data.split(":", maxsplit=1)
    if len(parts) != 2:
        return None

    received_prefix, suffix = parts
    if received_prefix != prefix:
        return None

    cleaned = suffix.strip()
    return cleaned if cleaned else None


# TODO(refactor): Move to utils/money.py.
def _parse_amount_and_currency(raw_text: str, default_currency: str) -> tuple[Decimal, str]:
    tokens = raw_text.strip().split()
    if not tokens:
        raise ValueError("Amount is required.")

    if len(tokens) > 2:
        raise ValueError("Use only amount and optional currency.")

    amount = _parse_amount(tokens[0])
    currency = normalize_currency_code(default_currency)

    if len(tokens) == 2:
        if not is_currency_token(tokens[1]):
            raise ValueError("Currency must be 3 letters, like USD.")
        currency = normalize_currency_code(tokens[1])

    return amount, currency


# TODO(refactor): Move to utils/money.py.
def _parse_amount(raw_value: str) -> Decimal:
    cleaned = raw_value.strip().replace(" ", "")
    if not cleaned:
        raise ValueError("Amount is required.")

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    elif "," in cleaned:
        if cleaned.count(",") == 1 and len(cleaned.split(",", maxsplit=1)[1]) <= 2:
            normalized = cleaned.replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    else:
        normalized = cleaned

    try:
        amount = Decimal(normalized).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid amount.") from exc

    if amount <= Decimal("0"):
        raise ValueError("Amount must be greater than zero.")
    return amount


# TODO(refactor): Move to utils/formatting.py.
def _balance_summary_for_button(open_rows: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for row in open_rows:
        currency = str(row.get("currency", "")).upper()
        they_owe_you = _to_decimal(row.get("they_owe_you"))
        you_owe = _to_decimal(row.get("you_owe"))
        if you_owe > Decimal("0"):
            chunks.append(f"+{_short_money(you_owe, currency)}")
        elif they_owe_you > Decimal("0"):
            chunks.append(f"-{_short_money(they_owe_you, currency)}")

    return ", ".join(chunks) if chunks else "settled"


# TODO(refactor): Move to utils/formatting.py.
def _short_money(amount: Decimal, currency: str) -> str:
    return f"{_format_amount_compact(amount)} {currency}"


# TODO(refactor): Move to utils/formatting.py.
def _truncate_button_label(value: str, max_len: int = 60) -> str:
    cleaned = value.strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


# TODO(refactor): Move to utils/formatting.py.
def _format_money(amount: Decimal, currency: str) -> str:
    return f"{_format_amount_compact(amount)} {escape(currency)}"


# TODO(refactor): Move to utils/money.py.
def _format_amount_compact(amount: Decimal) -> str:
    normalized = amount.quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


# TODO(refactor): Move to utils/money.py.
def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


# TODO(refactor): Move to utils/telegram_users.py.
def _display_name_from_message(message: Message) -> str | None:
    if message.from_user is None:
        return None
    first_name = (message.from_user.first_name or "").strip()
    last_name = (message.from_user.last_name or "").strip()
    full = f"{first_name} {last_name}".strip()
    return full or None


# TODO(refactor): Move to utils/telegram_users.py.
def _display_name_from_callback(callback: CallbackQuery) -> str | None:
    first_name = (callback.from_user.first_name or "").strip()
    last_name = (callback.from_user.last_name or "").strip()
    full = f"{first_name} {last_name}".strip()
    return full or None


# TODO(refactor): Move to utils/formatting.py.
def _profile_label(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "friend"

    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name

    username = str(profile.get("telegram_username", "")).strip()
    if username:
        return f"@{username}"

    telegram_user_id = profile.get("telegram_user_id")
    if telegram_user_id is not None:
        return f"user {telegram_user_id}"

    return "friend"
