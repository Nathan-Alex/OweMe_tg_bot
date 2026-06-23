from __future__ import annotations

import logging
import secrets
import string
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from ..currency import normalize_currency_code
from .config import WhatsAppSettings

logger = logging.getLogger(__name__)

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 10
_TWO_DP = Decimal("0.01")
_ZERO = Decimal("0.00")


class WhatsAppDatabase:
    def __init__(self, settings: WhatsAppSettings) -> None:
        self._conn = psycopg.connect(
            conninfo=settings.database_url,
            autocommit=True,
            row_factory=dict_row,
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            logger.exception("Failed to close PostgreSQL connection")

    def assert_ready(self) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute("select 1 from public.profiles limit 1")
                cur.execute("select 1 from public.payment_requests limit 1")
                cur.execute("select 1 from public.user_sessions limit 1")
        except Exception as exc:
            raise RuntimeError(f"WhatsApp PostgreSQL connectivity check failed: {exc}") from exc

    def get_or_create_profile(
        self,
        *,
        whatsapp_user_id: str,
        display_name: str | None,
    ) -> dict[str, Any]:
        normalized_user_id = _normalize_text(whatsapp_user_id)
        if normalized_user_id is None:
            raise ValueError("WhatsApp user id is required")

        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into public.profiles (
                    whatsapp_user_id,
                    whatsapp_display_name,
                    display_name
                )
                values (%s, %s, %s)
                on conflict (whatsapp_user_id)
                do update set
                    whatsapp_display_name = excluded.whatsapp_display_name,
                    display_name = excluded.display_name
                returning *
                """,
                (
                    normalized_user_id,
                    _normalize_text(display_name),
                    _normalize_text(display_name),
                ),
            )
            row = _fetchone_row(cur)

        if row is None:
            raise RuntimeError("Failed to create or fetch profile")
        return row

    def get_profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select
                    id,
                    whatsapp_user_id,
                    whatsapp_display_name,
                    display_name,
                    default_currency
                from public.profiles
                where id = %s::uuid
                limit 1
                """,
                (_normalize_uuid(profile_id),),
            )
            return _fetchone_row(cur)

    def create_payment_request(
        self,
        requester_id: str,
        amount: Decimal | str | int | float,
        currency: str,
    ) -> dict[str, Any]:
        requester_id = _normalize_uuid(requester_id)
        normalized_amount = _normalize_amount(amount)
        normalized_currency = normalize_currency_code(currency)

        for _ in range(8):
            code = _generate_code()
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into public.payment_requests (
                            code,
                            requester_id,
                            amount,
                            currency,
                            status,
                            approved_by,
                            approved_at,
                            friendship_id,
                            transaction_id
                        )
                        values (%s, %s::uuid, %s, %s, 'pending', null, null, null, null)
                        returning *
                        """,
                        (
                            code,
                            requester_id,
                            _decimal_to_str(normalized_amount),
                            normalized_currency,
                        ),
                    )
                    row = _fetchone_row(cur)
            except Exception as exc:
                if _is_unique_violation(exc):
                    continue
                raise

            if row is not None:
                return row

        raise RuntimeError("Unable to create a unique payment request code")

    def get_payment_request_by_code(self, code: str) -> dict[str, Any] | None:
        normalized_code = code.strip().upper()
        if not normalized_code:
            return None

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select *
                from public.payment_requests
                where code = %s
                limit 1
                """,
                (normalized_code,),
            )
            return _fetchone_row(cur)

    def approve_payment_request(
        self,
        code: str,
        approver_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        normalized_code = code.strip().upper()
        if not normalized_code:
            raise ValueError("REQUEST_NOT_FOUND")

        approver_id = _normalize_uuid(approver_id)

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                request_row = self._get_payment_request_by_code_tx(
                    cur,
                    normalized_code,
                    for_update=True,
                )
                if request_row is None:
                    raise ValueError("REQUEST_NOT_FOUND")

                requester_id = str(request_row.get("requester_id", ""))
                if requester_id == approver_id:
                    raise ValueError("REQUEST_SELF_APPROVAL")

                status = str(request_row.get("status", "")).lower()
                if status == "approved":
                    existing_tx = self._get_transaction_by_id_tx(
                        cur,
                        str(request_row.get("transaction_id", "")),
                    )
                    return request_row, existing_tx, False

                if status == "processing":
                    raise ValueError("REQUEST_PROCESSING")

                if status != "pending":
                    raise ValueError("REQUEST_NOT_PENDING")

                request_id = str(request_row.get("id", ""))
                if not request_id:
                    raise RuntimeError("Payment request ID is missing")

                cur.execute(
                    """
                    update public.payment_requests
                    set
                        status = 'processing',
                        approved_by = %s::uuid,
                        approved_at = now()
                    where id = %s::uuid
                      and status = 'pending'
                    returning *
                    """,
                    (approver_id, request_id),
                )
                locked_row = _fetchone_row(cur)
                if locked_row is None:
                    latest = self._get_payment_request_by_code_tx(cur, normalized_code)
                    if latest is None:
                        raise ValueError("REQUEST_NOT_FOUND")
                    latest_status = str(latest.get("status", "")).lower()
                    if latest_status == "approved":
                        existing_tx = self._get_transaction_by_id_tx(
                            cur,
                            str(latest.get("transaction_id", "")),
                        )
                        return latest, existing_tx, False
                    if latest_status == "processing":
                        raise ValueError("REQUEST_PROCESSING")
                    raise ValueError("REQUEST_NOT_PENDING")

                amount = _to_decimal(locked_row.get("amount"))
                currency = str(locked_row.get("currency", "")).upper()

                friendship = self._ensure_accepted_friendship_tx(
                    cur,
                    left_id=requester_id,
                    right_id=approver_id,
                    invited_by=requester_id,
                )
                friendship_id = str(friendship.get("id", ""))
                if not friendship_id:
                    raise RuntimeError("Friendship ID is missing")

                tx = self._create_confirmed_transaction_tx(
                    cur,
                    friendship=friendship,
                    friendship_id=friendship_id,
                    created_by=requester_id,
                    direction="in",
                    amount=amount,
                    currency=currency,
                    confirmed_by=approver_id,
                    note=f"Approved via request code {normalized_code}",
                )

                cur.execute(
                    """
                    update public.payment_requests
                    set
                        status = 'approved',
                        approved_by = %s::uuid,
                        approved_at = now(),
                        friendship_id = %s::uuid,
                        transaction_id = %s::uuid
                    where id = %s::uuid
                      and status = 'processing'
                    returning *
                    """,
                    (
                        approver_id,
                        friendship_id,
                        str(tx.get("id", "")),
                        request_id,
                    ),
                )
                final_row = _fetchone_row(cur)
                if final_row is None:
                    raise RuntimeError("Failed to finalize payment request")

                return final_row, tx, True

    def list_open_balances(self, viewer_id: str) -> list[dict[str, Any]]:
        viewer_id = _normalize_uuid(viewer_id)

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select
                    f.id::text as friendship_id,
                    f.user_low::text as user_low,
                    f.user_high::text as user_high,
                    p.id::text as friend_id,
                    p.whatsapp_user_id,
                    p.whatsapp_display_name,
                    p.display_name,
                    b.currency,
                    b.net_amount
                from public.friendships f
                join public.balances b
                  on b.friendship_id = f.id
                 and b.net_amount <> 0
                join public.profiles p
                  on p.id = case
                        when f.user_low = %s::uuid then f.user_high
                        else f.user_low
                    end
                where f.status = 'accepted'
                  and (f.user_low = %s::uuid or f.user_high = %s::uuid)
                order by
                    lower(coalesce(p.display_name, p.whatsapp_display_name, p.whatsapp_user_id, p.id::text)),
                    b.currency
                """,
                (viewer_id, viewer_id, viewer_id),
            )
            rows = _fetchall_rows(cur)

        by_friendship: dict[str, dict[str, Any]] = {}
        for row in rows:
            friendship_id = str(row.get("friendship_id", ""))
            friend_id = str(row.get("friend_id", ""))
            if not friendship_id or not friend_id:
                continue

            item = by_friendship.get(friendship_id)
            if item is None:
                item = {
                    "friendship_id": friendship_id,
                    "friend_profile": {
                        "id": friend_id,
                        "whatsapp_user_id": row.get("whatsapp_user_id"),
                        "whatsapp_display_name": row.get("whatsapp_display_name"),
                        "display_name": row.get("display_name"),
                    },
                    "open_rows": [],
                }
                by_friendship[friendship_id] = item

            user_low = str(row.get("user_low", ""))
            user_high = str(row.get("user_high", ""))
            net_amount = _to_decimal(row.get("net_amount"))

            if viewer_id == user_low:
                they_owe_you = max(net_amount, _ZERO)
                you_owe = max(-net_amount, _ZERO)
            elif viewer_id == user_high:
                they_owe_you = max(-net_amount, _ZERO)
                you_owe = max(net_amount, _ZERO)
            else:
                continue

            if they_owe_you == _ZERO and you_owe == _ZERO:
                continue

            item["open_rows"].append(
                {
                    "currency": str(row.get("currency", "")).upper(),
                    "they_owe_you": they_owe_you,
                    "you_owe": you_owe,
                }
            )

        results: list[dict[str, Any]] = []
        for item in by_friendship.values():
            open_rows = item.get("open_rows", [])
            open_rows.sort(key=lambda entry: str(entry.get("currency", "")))
            if open_rows:
                results.append(item)

        return results

    def close_friend_balances(self, viewer_id: str, friend_id: str) -> list[str]:
        viewer_id = _normalize_uuid(viewer_id)
        friend_id = _normalize_uuid(friend_id)
        user_low, user_high = _canonical_pair(viewer_id, friend_id)

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    select id, status
                    from public.friendships
                    where user_low = %s::uuid
                      and user_high = %s::uuid
                    limit 1
                    """,
                    (user_low, user_high),
                )
                friendship = _fetchone_row(cur)
                if friendship is None or str(friendship.get("status", "")).lower() != "accepted":
                    raise ValueError("FRIENDSHIP_NOT_FOUND")

                friendship_id = str(friendship.get("id", ""))
                if not friendship_id:
                    raise RuntimeError("Friendship ID is missing")

                cur.execute(
                    """
                    update public.balances
                    set net_amount = 0
                    where friendship_id = %s::uuid
                      and net_amount <> 0
                    returning currency
                    """,
                    (friendship_id,),
                )
                rows = _fetchall_rows(cur)

        return sorted(str(row.get("currency", "")).upper() for row in rows if row.get("currency"))

    def get_session_state(self, whatsapp_user_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select state
                from public.user_sessions
                where whatsapp_user_id = %s
                limit 1
                """,
                (_normalize_required_text(whatsapp_user_id),),
            )
            row = _fetchone_row(cur)
        return str(row.get("state")) if row and row.get("state") else None

    def set_session_state(self, whatsapp_user_id: str, state: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into public.user_sessions (whatsapp_user_id, state, data)
                values (%s, %s, '{}'::jsonb)
                on conflict (whatsapp_user_id)
                do update set state = excluded.state, updated_at = now()
                """,
                (_normalize_required_text(whatsapp_user_id), _normalize_required_text(state)),
            )

    def clear_session(self, whatsapp_user_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "delete from public.user_sessions where whatsapp_user_id = %s",
                (_normalize_required_text(whatsapp_user_id),),
            )

    def mark_event_processed(self, event_id: str) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "insert into public.processed_events (event_id) values (%s)",
                    (_normalize_required_text(event_id),),
                )
            return True
        except Exception as exc:
            if _is_unique_violation(exc):
                return False
            raise

    def _get_payment_request_by_code_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        code: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " for update" if for_update else ""
        cur.execute(
            (
                "select * from public.payment_requests "
                "where code = %s "
                "limit 1"
                f"{suffix}"
            ),
            (code,),
        )
        return _fetchone_row(cur)

    def _get_transaction_by_id_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        tx_id: str,
    ) -> dict[str, Any] | None:
        if not tx_id:
            return None
        cur.execute(
            """
            select *
            from public.transactions
            where id = %s::uuid
            limit 1
            """,
            (_normalize_uuid(tx_id),),
        )
        return _fetchone_row(cur)

    def _ensure_accepted_friendship_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        *,
        left_id: str,
        right_id: str,
        invited_by: str,
    ) -> dict[str, Any]:
        invited_by = _normalize_uuid(invited_by)
        user_low, user_high = _canonical_pair(left_id, right_id)

        cur.execute(
            """
            insert into public.friendships (
                user_low,
                user_high,
                status,
                invited_by,
                accepted_at
            )
            values (%s::uuid, %s::uuid, 'accepted', %s::uuid, now())
            on conflict (user_low, user_high)
            do update set
                status = 'accepted',
                accepted_at = coalesce(public.friendships.accepted_at, excluded.accepted_at),
                invited_by = excluded.invited_by
            returning *
            """,
            (user_low, user_high, invited_by),
        )
        row = _fetchone_row(cur)
        if row is None:
            raise RuntimeError("Failed to create or fetch friendship")
        return row

    def _create_confirmed_transaction_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        *,
        friendship: Mapping[str, Any],
        friendship_id: str,
        created_by: str,
        direction: str,
        amount: Decimal,
        currency: str,
        confirmed_by: str,
        note: str | None,
    ) -> dict[str, Any]:
        friendship_id = _normalize_uuid(friendship_id)
        created_by = _normalize_uuid(created_by)
        confirmed_by = _normalize_uuid(confirmed_by)

        direction = direction.strip().lower()
        if direction not in {"in", "out"}:
            raise ValueError("Invalid direction")

        members = {str(friendship.get("user_low", "")), str(friendship.get("user_high", ""))}
        if created_by not in members:
            raise ValueError("Transaction creator must belong to friendship")
        if confirmed_by not in members:
            raise ValueError("Confirmer must belong to friendship")

        cur.execute(
            """
            insert into public.transactions (
                friendship_id,
                created_by,
                direction,
                amount,
                currency,
                note,
                status,
                confirmed_by,
                confirmed_at,
                rejected_at,
                reversed_at,
                reverses_transaction_id
            )
            values (
                %s::uuid,
                %s::uuid,
                %s,
                %s,
                %s,
                %s,
                'confirmed',
                %s::uuid,
                now(),
                null,
                null,
                null
            )
            returning *
            """,
            (
                friendship_id,
                created_by,
                direction,
                _decimal_to_str(_normalize_amount(amount)),
                normalize_currency_code(currency),
                _normalize_text(note),
                confirmed_by,
            ),
        )
        row = _fetchone_row(cur)
        if row is None:
            raise RuntimeError("Failed to create transaction")

        balance_row = self._apply_balance_delta_tx(
            cur,
            friendship_id=str(row.get("friendship_id", "")),
            currency=str(row.get("currency", "")),
            delta=_transaction_effect_on_net(tx=row, friendship=friendship),
        )
        if balance_row is not None:
            row["net_amount_after"] = balance_row.get("net_amount")

        return row

    def _get_balance_row_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        friendship_id: str,
        currency: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            select friendship_id, currency, net_amount
            from public.balances
            where friendship_id = %s::uuid
              and currency = %s
            limit 1
            """,
            (_normalize_uuid(friendship_id), normalize_currency_code(currency)),
        )
        return _fetchone_row(cur)

    def _apply_balance_delta_tx(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        *,
        friendship_id: str,
        currency: str,
        delta: Decimal | str | int | float,
    ) -> dict[str, Any] | None:
        normalized_friendship_id = _normalize_uuid(friendship_id)
        normalized_currency = normalize_currency_code(currency)
        normalized_delta = _to_decimal(delta)

        if normalized_delta == _ZERO:
            return self._get_balance_row_tx(cur, normalized_friendship_id, normalized_currency)

        cur.execute(
            """
            insert into public.balances (friendship_id, currency, net_amount)
            values (%s::uuid, %s, %s)
            on conflict (friendship_id, currency)
            do update set
                net_amount = public.balances.net_amount + excluded.net_amount
            returning friendship_id, currency, net_amount
            """,
            (
                normalized_friendship_id,
                normalized_currency,
                _decimal_to_str(normalized_delta),
            ),
        )
        return _fetchone_row(cur)


def _transaction_effect_on_net(tx: Mapping[str, Any], friendship: Mapping[str, Any]) -> Decimal:
    amount = _to_decimal(tx.get("amount"))
    direction = str(tx.get("direction", "")).lower()
    if direction not in {"in", "out"}:
        raise ValueError(f"Invalid transaction direction: {direction}")

    created_by = str(tx.get("created_by", ""))
    user_low = str(friendship.get("user_low", ""))
    user_high = str(friendship.get("user_high", ""))

    if created_by not in {user_low, user_high}:
        raise ValueError("Transaction creator is not part of friendship")

    if created_by == user_low:
        return amount if direction == "out" else -amount

    return -amount if direction == "out" else amount


def _fetchone_row(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    return dict(row)


def _fetchall_rows(cur: psycopg.Cursor[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in cur.fetchall()]


def _normalize_uuid(value: str | UUID) -> str:
    return str(UUID(str(value)))


def _canonical_pair(user_a: str | UUID, user_b: str | UUID) -> tuple[str, str]:
    left = _normalize_uuid(user_a)
    right = _normalize_uuid(user_b)
    if left <= right:
        return left, right
    return right, left


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _normalize_required_text(value: str) -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError("Value is required")
    return normalized


def _normalize_amount(value: Decimal | str | int | float) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid amount") from exc

    if amount <= _ZERO:
        raise ValueError("Amount must be greater than zero")
    return amount


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return _ZERO


def _decimal_to_str(value: Decimal) -> str:
    return str(value.quantize(_TWO_DP, rounding=ROUND_HALF_UP))


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _is_unique_violation(exc: Exception) -> bool:
    return str(getattr(exc, "sqlstate", "")).upper() == "23505"
