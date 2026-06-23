from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any

from bot.services.debt_service import DebtService, UserIdentity


class FakeDatabase:
    def __init__(self) -> None:
        self.profile = {
            "id": "11111111-1111-1111-1111-111111111111",
            "telegram_user_id": 123,
            "telegram_username": "alice",
            "display_name": "Alice",
        }
        self.friend_profile = {
            "id": "22222222-2222-2222-2222-222222222222",
            "telegram_user_id": 456,
            "telegram_username": "bob",
            "display_name": "Bob",
        }
        self.created_requests: list[dict[str, Any]] = []
        self.closed_args: dict[str, str] | None = None

    def get_or_create_profile(
        self,
        telegram_user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> dict[str, Any]:
        self.profile["telegram_user_id"] = telegram_user_id
        self.profile["telegram_username"] = username
        self.profile["display_name"] = display_name
        return self.profile

    def create_payment_request(
        self,
        requester_id: str,
        amount: Decimal,
        currency: str,
    ) -> dict[str, Any]:
        payload = {
            "requester_id": requester_id,
            "amount": amount,
            "currency": currency,
        }
        self.created_requests.append(payload)
        return {"code": "abc123", **payload}

    def list_open_balances(self, viewer_id: str) -> list[dict[str, Any]]:
        return [
            {
                "friend_profile": self.friend_profile,
                "open_rows": [
                    {
                        "currency": "USD",
                        "they_owe_you": "0",
                        "you_owe": "15.00",
                    }
                ],
            }
        ]

    def close_friend_balances(self, viewer_id: str, friend_id: str) -> list[str]:
        self.closed_args = {"viewer_id": viewer_id, "friend_id": friend_id}
        return ["USD"]

    def get_profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        if profile_id == self.friend_profile["id"]:
            return self.friend_profile
        return self.profile

    def approve_payment_request(
        self,
        code: str,
        approver_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        return (
            {
                "code": code,
                "requester_id": self.friend_profile["id"],
                "amount": "12.50",
                "currency": "EUR",
            },
            {"id": "33333333-3333-3333-3333-333333333333"},
            True,
        )


class DebtServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeDatabase()
        self.service = DebtService(self.db)  # type: ignore[arg-type]
        self.identity = UserIdentity(
            provider="telegram",
            provider_user_id="123",
            username="alice",
            display_name="Alice",
        )

    def test_create_payment_request_parses_amount_and_calls_database(self) -> None:
        result = self.service.create_payment_request(
            identity=self.identity,
            raw_amount="10 usd",
            default_currency="ILS",
        )

        self.assertEqual(result.code, "ABC123")
        self.assertEqual(result.amount, Decimal("10.00"))
        self.assertEqual(result.currency, "USD")
        self.assertEqual(self.db.created_requests[0]["requester_id"], self.db.profile["id"])

    def test_build_balance_text_formats_open_balances(self) -> None:
        result = self.service.build_balance_text(identity=self.identity)

        self.assertTrue(result.has_open_balances)
        self.assertEqual(result.text, "Your balance:\nBob + 15 USD")

    def test_build_close_options_returns_friend_labels(self) -> None:
        result = self.service.build_close_options(identity=self.identity)

        self.assertEqual(len(result.options), 1)
        self.assertEqual(result.options[0].friend_id, self.db.friend_profile["id"])
        self.assertEqual(result.options[0].label, "Bob (+15 USD)")

    def test_close_friend_balance_calls_database(self) -> None:
        result = self.service.close_friend_balance(
            identity=self.identity,
            friend_id=self.db.friend_profile["id"],
        )

        self.assertEqual(result.closed_currencies, ["USD"])
        self.assertEqual(self.db.closed_args["viewer_id"], self.db.profile["id"])  # type: ignore[index]

    def test_approve_payment_request_returns_request_context(self) -> None:
        result = self.service.approve_payment_request(
            identity=self.identity,
            request_code="abc123",
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.amount, Decimal("12.50"))
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.requester_profile, self.db.friend_profile)

    def test_non_telegram_identity_is_explicitly_unsupported_until_schema_changes(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.service.build_balance_text(
                identity=UserIdentity(provider="whatsapp", provider_user_id="15551234567")
            )


if __name__ == "__main__":
    unittest.main()
