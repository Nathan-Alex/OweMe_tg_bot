from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import WhatsAppSettings


@dataclass(frozen=True)
class ReplyButton:
    id: str
    title: str


@dataclass(frozen=True)
class ListRow:
    id: str
    title: str
    description: str | None = None


class WhatsAppClient:
    def __init__(self, settings: WhatsAppSettings) -> None:
        self._settings = settings

    async def send_text(self, *, to: str, text: str) -> None:
        await self._post_message(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": text,
                },
            }
        )

    async def send_reply_buttons(
        self,
        *,
        to: str,
        body: str,
        buttons: list[ReplyButton],
    ) -> None:
        await self._post_message(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": button.id,
                                    "title": button.title,
                                },
                            }
                            for button in buttons[:3]
                        ]
                    },
                },
            }
        )

    async def send_list(
        self,
        *,
        to: str,
        body: str,
        button_text: str,
        rows: list[ListRow],
    ) -> None:
        await self._post_message(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {
                        "button": button_text,
                        "sections": [
                            {
                                "title": "Open balances",
                                "rows": [
                                    {
                                        "id": row.id,
                                        "title": row.title,
                                        **({"description": row.description} if row.description else {}),
                                    }
                                    for row in rows
                                ],
                            }
                        ],
                    },
                },
            }
        )

    async def _post_message(self, payload: dict[str, Any]) -> None:
        url = f"{self._settings.graph_base_url}/{self._settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {self._settings.access_token}"}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
