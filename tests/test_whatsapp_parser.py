from __future__ import annotations

import unittest

from bot.whatsapp.parser import parse_webhook_payload


class WhatsAppParserTest(unittest.TestCase):
    def test_parses_text_message(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [
                                    {
                                        "wa_id": "15551234567",
                                        "profile": {"name": "Alice"},
                                    }
                                ],
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "15551234567",
                                        "type": "text",
                                        "text": {"body": "Balance"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        events = parse_webhook_payload(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].message_id, "wamid.1")
        self.assertEqual(events[0].from_user_id, "15551234567")
        self.assertEqual(events[0].display_name, "Alice")
        self.assertEqual(events[0].text, "Balance")

    def test_parses_button_reply(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.2",
                                        "from": "15551234567",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": "menu:in",
                                                "title": "In",
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        events = parse_webhook_payload(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].button_id, "menu:in")
        self.assertEqual(events[0].text, "In")

    def test_parses_list_reply(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.3",
                                        "from": "15551234567",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "list_reply",
                                            "list_reply": {
                                                "id": "close:22222222-2222-2222-2222-222222222222",
                                                "title": "Bob",
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        events = parse_webhook_payload(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].list_reply_id, "close:22222222-2222-2222-2222-222222222222")
        self.assertEqual(events[0].text, "Bob")

    def test_ignores_status_only_payload(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.status",
                                        "status": "delivered",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        self.assertEqual(parse_webhook_payload(payload), [])


if __name__ == "__main__":
    unittest.main()

