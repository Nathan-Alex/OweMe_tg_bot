from __future__ import annotations

import unittest
from decimal import Decimal

from bot.services.formatting import balance_summary_for_button, profile_label
from bot.services.money import parse_amount_and_currency


class ServiceMoneyFormattingTest(unittest.TestCase):
    def test_parse_amount_uses_default_currency(self) -> None:
        amount, currency = parse_amount_and_currency("100", "ils")
        self.assertEqual(amount, Decimal("100.00"))
        self.assertEqual(currency, "ILS")

    def test_parse_amount_accepts_currency(self) -> None:
        amount, currency = parse_amount_and_currency("12.5 usd", "ILS")
        self.assertEqual(amount, Decimal("12.50"))
        self.assertEqual(currency, "USD")

    def test_parse_amount_accepts_comma(self) -> None:
        amount, currency = parse_amount_and_currency("12,5 EUR", "ILS")
        self.assertEqual(amount, Decimal("12.50"))
        self.assertEqual(currency, "EUR")

    def test_parse_amount_rejects_extra_tokens(self) -> None:
        with self.assertRaises(ValueError):
            parse_amount_and_currency("100 USD lunch", "ILS")

    def test_balance_summary_says_when_you_owe(self) -> None:
        summary = balance_summary_for_button(
            [
                {
                    "currency": "USD",
                    "they_owe_you": "0",
                    "you_owe": "15.00",
                }
            ]
        )
        self.assertEqual(summary, "You owe 15 USD")

    def test_balance_summary_says_when_friend_owes_you(self) -> None:
        summary = balance_summary_for_button(
            [
                {
                    "currency": "ILS",
                    "they_owe_you": "20.50",
                    "you_owe": "0",
                }
            ]
        )
        self.assertEqual(summary, "Owes you 20.5 ILS")

    def test_profile_label_supports_generic_username(self) -> None:
        self.assertEqual(profile_label({"username": "alice"}), "@alice")

    def test_profile_label_supports_whatsapp_identity(self) -> None:
        self.assertEqual(profile_label({"whatsapp_user_id": "15551234567"}), "WhatsApp 15551234567")

    def test_profile_label_supports_whatsapp_display_name(self) -> None:
        self.assertEqual(
            profile_label(
                {
                    "whatsapp_user_id": "15551234567",
                    "whatsapp_display_name": "Alice",
                }
            ),
            "Alice",
        )


if __name__ == "__main__":
    unittest.main()
