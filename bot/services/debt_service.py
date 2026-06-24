from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .formatting import (
    balance_line_for_friend,
    balance_summary_for_button,
    format_money,
    profile_label,
    truncate_button_label,
)
from .money import parse_amount_and_currency, to_decimal

if TYPE_CHECKING:
    from ..db import Database


@dataclass(frozen=True)
class UserIdentity:
    provider: str
    provider_user_id: str
    username: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class PaymentRequestResult:
    requester_profile: dict[str, Any]
    code: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class BalanceTextResult:
    profile: dict[str, Any]
    text: str
    has_open_balances: bool


@dataclass(frozen=True)
class CloseOption:
    friend_id: str
    label: str


@dataclass(frozen=True)
class CloseOptionsResult:
    profile: dict[str, Any]
    options: list[CloseOption]


@dataclass(frozen=True)
class CloseBalanceResult:
    friend_profile: dict[str, Any] | None
    closed_currencies: list[str]


@dataclass(frozen=True)
class ApprovalResult:
    actor_profile: dict[str, Any]
    requester_profile: dict[str, Any] | None
    request_row: dict[str, Any]
    transaction_row: dict[str, Any] | None
    changed: bool
    amount: Decimal
    currency: str


class DebtService:
    """Transport-neutral debt operations backed by the existing Database facade."""

    def __init__(self, db: "Database") -> None:
        self._db = db

    def create_payment_request(
        self,
        *,
        identity: UserIdentity,
        raw_amount: str,
        default_currency: str,
    ) -> PaymentRequestResult:
        amount, currency = parse_amount_and_currency(raw_amount, default_currency)
        requester_profile = self.get_or_create_profile(identity)
        requester_id = str(requester_profile.get("id", ""))
        if not requester_id:
            raise RuntimeError("Profile data is incomplete.")

        request_row = self._db.create_payment_request(
            requester_id=requester_id,
            amount=amount,
            currency=currency,
        )
        return PaymentRequestResult(
            requester_profile=requester_profile,
            code=str(request_row.get("code", "")).upper(),
            amount=amount,
            currency=currency,
        )

    def build_balance_text(self, *, identity: UserIdentity) -> BalanceTextResult:
        viewer_profile = self.get_or_create_profile(identity)
        viewer_id = str(viewer_profile.get("id", ""))
        if not viewer_id:
            raise RuntimeError("Profile data is incomplete.")

        open_balances = self._db.list_open_balances(viewer_id)
        if not open_balances:
            return BalanceTextResult(
                profile=viewer_profile,
                text="No open balances.\nTap In when someone lends you money.",
                has_open_balances=False,
            )

        lines = ["Open balances:"]
        for item in sorted(
            open_balances,
            key=lambda row: profile_label(row.get("friend_profile", {})).lower(),
        ):
            friend_profile = item.get("friend_profile", {})
            friend_label = profile_label(friend_profile)
            for row in item.get("open_rows", []):
                line = balance_line_for_friend(friend_label, row)
                if line is not None:
                    lines.append(line)

        if len(lines) == 1:
            return BalanceTextResult(
                profile=viewer_profile,
                text="No open balances.\nTap In when someone lends you money.",
                has_open_balances=False,
            )

        return BalanceTextResult(
            profile=viewer_profile,
            text="\n".join(lines),
            has_open_balances=True,
        )

    def build_close_options(self, *, identity: UserIdentity) -> CloseOptionsResult:
        viewer_profile = self.get_or_create_profile(identity)
        viewer_id = str(viewer_profile.get("id", ""))
        if not viewer_id:
            raise RuntimeError("Profile data is incomplete.")

        open_balances = self._db.list_open_balances(viewer_id)
        options: list[CloseOption] = []
        for item in sorted(
            open_balances,
            key=lambda row: profile_label(row.get("friend_profile", {})).lower(),
        ):
            friend_profile = item.get("friend_profile", {})
            friend_id = str(friend_profile.get("id", ""))
            if not friend_id:
                continue

            summary = balance_summary_for_button(item.get("open_rows", []))
            label = truncate_button_label(f"{profile_label(friend_profile)} ({summary})")
            options.append(CloseOption(friend_id=friend_id, label=label))

        return CloseOptionsResult(profile=viewer_profile, options=options)

    def close_friend_balance(self, *, identity: UserIdentity, friend_id: str) -> CloseBalanceResult:
        friend_uuid = str(UUID(friend_id))
        viewer_profile = self.get_or_create_profile(identity)
        viewer_id = str(viewer_profile.get("id", ""))
        if not viewer_id:
            raise RuntimeError("Profile data is incomplete.")

        closed_currencies = self._db.close_friend_balances(
            viewer_id=viewer_id,
            friend_id=friend_uuid,
        )
        friend_profile = self._db.get_profile_by_id(friend_uuid)
        return CloseBalanceResult(
            friend_profile=friend_profile,
            closed_currencies=closed_currencies,
        )

    def approve_payment_request(self, *, identity: UserIdentity, request_code: str) -> ApprovalResult:
        actor_profile = self.get_or_create_profile(identity)
        actor_id = str(actor_profile.get("id", ""))
        if not actor_id:
            raise RuntimeError("Profile data is incomplete.")

        request_row, tx_row, changed = self._db.approve_payment_request(
            code=request_code,
            approver_id=actor_id,
        )
        requester_id = str(request_row.get("requester_id", ""))
        requester_profile = self._db.get_profile_by_id(requester_id) if requester_id else None
        amount = to_decimal(request_row.get("amount"))
        currency = str(request_row.get("currency", "")).upper()

        return ApprovalResult(
            actor_profile=actor_profile,
            requester_profile=requester_profile,
            request_row=request_row,
            transaction_row=tx_row,
            changed=changed,
            amount=amount,
            currency=currency,
        )

    def get_or_create_profile(self, identity: UserIdentity) -> dict[str, Any]:
        provider = identity.provider.strip().lower()
        if provider != "telegram":
            raise NotImplementedError(
                "The current Database facade only supports Telegram profile identities."
            )

        return self._db.get_or_create_profile(
            telegram_user_id=int(identity.provider_user_id),
            username=identity.username,
            display_name=identity.display_name,
        )
