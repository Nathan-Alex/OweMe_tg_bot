from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from .client import WhatsAppClient
from .config import WhatsAppSettings, get_whatsapp_settings
from .db import WhatsAppDatabase
from .handlers import handle_message
from .parser import parse_webhook_payload

logger = logging.getLogger(__name__)


def create_whatsapp_app() -> FastAPI:
    app = FastAPI(title="Split a Bill WhatsApp Bot")

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {"ok": True, "service": "split-a-bill-whatsapp-bot"}

    @app.get("/health")
    async def healthcheck() -> dict[str, Any]:
        settings = get_whatsapp_settings()
        database = WhatsAppDatabase(settings)
        try:
            database.assert_ready()
        finally:
            database.close()
        return {"ok": True}

    @app.get("/webhook", response_class=PlainTextResponse)
    async def verify_webhook(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> str:
        settings = get_whatsapp_settings()
        if (
            hub_mode == "subscribe"
            and hub_challenge is not None
            and _is_valid_verify_token(settings, hub_verify_token)
        ):
            return hub_challenge

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid WhatsApp webhook verification token.",
        )

    @app.post("/webhook")
    async def webhook(payload: dict[str, Any]) -> dict[str, Any]:
        settings = get_whatsapp_settings()
        events = parse_webhook_payload(payload)
        if not events:
            return {"ok": True}

        database = WhatsAppDatabase(settings)
        client = WhatsAppClient(settings)
        try:
            for event in events:
                await handle_message(
                    event=event,
                    db=database,
                    client=client,
                    settings=settings,
                )
        finally:
            database.close()

        return {"ok": True}

    return app


def _is_valid_verify_token(settings: WhatsAppSettings, token: str | None) -> bool:
    return token is not None and secrets.compare_digest(token, settings.verify_token)

