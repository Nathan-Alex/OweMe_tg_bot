from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal
from typing import Any

from pydantic import SecretStr

from bot.whatsapp.client import ListRow, ReplyButton
from bot.whatsapp.config import WhatsAppSettings
from bot.whatsapp.handlers import ACTION_CLOSE_PREFIX, SESSION_WAITING_AMOUNT, handle_message
from bot.whatsapp.parser import WhatsAppMessage


class FakeWhatsAppDatabase:
    def __init__(self) -> None:
        self.profile = {
            "id": "11111111-1111-1111-1111-111111111111",
            "whatsapp_user_id": "15550001111",
            "display_name": "Alice",
        }
        self.friend_profile = {
            "id": "22222222-2222-2222-2222-222222222222",
            "whatsapp_user_id": "15550002222",
            "display_name": "Bob",
        }
        self.sessions: dict[str, str] = {}
        self.processed_events: set[str] = set()
        self.created_requests: list[dict[str, Any]] = []
        self.closed_args: dict[str, str] | None = None

    def mark_event_processed(self, event_id: str) -> bool:
        if event_id in self.processed_events:
            return False
        self.processed_events.add(event_id)
        return True

    def get_or_create_profile(
        self,
        *,
        whatsapp_user_id: str,
        display_name: str | None,
    ) -> dict[str, Any]:
        self.profile["whatsapp_user_id"] = whatsapp_user_id
        self.profile["display_name"] = display_name
        return self.profile

    def get_profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        if profile_id == self.friend_profile["id"]:
            return self.friend_profile
        if profile_id == self.profile["id"]:
            return self.profile
        return None

    def get_session_state(self, whatsapp_user_id: str) -> str | None:
        return self.sessions.get(whatsapp_user_id)

    def set_session_state(self, whatsapp_user_id: str, state: str) -> None:
        self.sessions[whatsapp_user_id] = state

    def clear_session(self, whatsapp_user_id: str) -> None:
        self.sessions.pop(whatsapp_user_id, None)

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


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.texts: list[dict[str, str]] = []
        self.buttons: list[dict[str, Any]] = []
        self.lists: list[dict[str, Any]] = []

    async def send_text(self, *, to: str, text: str) -> None:
        self.texts.append({"to": to, "text": text})

    async def send_reply_buttons(
        self,
        *,
        to: str,
        body: str,
        buttons: list[ReplyButton],
    ) -> None:
        self.buttons.append({"to": to, "body": body, "buttons": buttons})

    async def send_list(
        self,
        *,
        to: str,
        body: str,
        button_text: str,
        rows: list[ListRow],
    ) -> None:
        self.lists.append(
            {
                "to": to,
                "body": body,
                "button_text": button_text,
                "rows": rows,
            }
        )


def make_settings() -> WhatsAppSettings:
    return WhatsAppSettings(
        WHATSAPP_DATABASE_URL=SecretStr("postgresql://postgres:postgres@localhost:5432/whatsapp"),
        WHATSAPP_ACCESS_TOKEN=SecretStr("token"),
        WHATSAPP_PHONE_NUMBER_ID="phone-number-id",
        WHATSAPP_BUSINESS_PHONE="15559998888",
        WHATSAPP_VERIFY_TOKEN=SecretStr("verify"),
        DEFAULT_CURRENCY="ILS",
    )


def make_event(
    *,
    message_id: str = "wamid.1",
    text: str | None = "In",
    button_id: str | None = None,
    list_reply_id: str | None = None,
) -> WhatsAppMessage:
    return WhatsAppMessage(
        message_id=message_id,
        from_user_id="15550001111",
        display_name="Alice",
        text=text,
        button_id=button_id,
        list_reply_id=list_reply_id,
    )


class WhatsAppHandlersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeWhatsAppDatabase()
        self.client = FakeWhatsAppClient()
        self.settings = make_settings()

    def run_handler(self, event: WhatsAppMessage) -> None:
        asyncio.run(
            handle_message(
                event=event,
                db=self.db,  # type: ignore[arg-type]
                client=self.client,  # type: ignore[arg-type]
                settings=self.settings,
            )
        )

    def test_in_sets_waiting_amount_session(self) -> None:
        self.run_handler(make_event(text="In"))

        self.assertEqual(self.db.sessions["15550001111"], SESSION_WAITING_AMOUNT)
        self.assertIn("How much did you get?", self.client.texts[0]["text"])

    def test_amount_creates_payment_request_and_sends_approval_link(self) -> None:
        self.db.sessions["15550001111"] = SESSION_WAITING_AMOUNT

        self.run_handler(make_event(message_id="wamid.amount", text="120 USD"))

        self.assertEqual(self.db.created_requests[0]["amount"], Decimal("120.00"))
        self.assertEqual(self.db.created_requests[0]["currency"], "USD")
        self.assertNotIn("15550001111", self.db.sessions)
        self.assertIn("https://wa.me/15559998888?text=approve%20ABC123", self.client.texts[0]["text"])

    def test_balance_sends_open_balances(self) -> None:
        self.run_handler(make_event(message_id="wamid.balance", text="Balance"))

        self.assertEqual(self.client.texts[0]["text"], "Your balance:\nBob + 15 USD")

    def test_close_sends_interactive_list(self) -> None:
        self.run_handler(make_event(message_id="wamid.close", text="Close"))

        self.assertEqual(len(self.client.lists), 1)
        self.assertEqual(self.client.lists[0]["rows"][0].title, "Bob")
        self.assertEqual(
            self.client.lists[0]["rows"][0].id,
            f"{ACTION_CLOSE_PREFIX}{self.db.friend_profile['id']}",
        )

    def test_close_list_reply_closes_balance(self) -> None:
        self.run_handler(
            make_event(
                message_id="wamid.close-reply",
                text="Bob",
                list_reply_id=f"{ACTION_CLOSE_PREFIX}{self.db.friend_profile['id']}",
            )
        )

        self.assertEqual(self.db.closed_args["friend_id"], self.db.friend_profile["id"])  # type: ignore[index]
        self.assertIn("Closed balance with Bob for USD.", self.client.texts[0]["text"])

    def test_approve_code_confirms_request(self) -> None:
        self.run_handler(make_event(message_id="wamid.approve", text="approve abc123"))

        self.assertIn("Confirmed. The debt was saved.", self.client.texts[0]["text"])
        self.assertIn("Loan confirmed by Alice.", self.client.texts[1]["text"])

    def test_duplicate_message_id_is_skipped(self) -> None:
        event = make_event(message_id="wamid.dup", text="Balance")

        self.run_handler(event)
        self.run_handler(event)

        self.assertEqual(len(self.client.texts), 1)


if __name__ == "__main__":
    unittest.main()
