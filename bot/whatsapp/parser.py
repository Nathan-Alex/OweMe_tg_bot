from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WhatsAppMessage:
    message_id: str
    from_user_id: str
    display_name: str | None
    text: str | None
    button_id: str | None = None
    list_reply_id: str | None = None


def parse_webhook_payload(payload: dict[str, Any]) -> list[WhatsAppMessage]:
    messages: list[WhatsAppMessage] = []
    for entry in _as_list(payload.get("entry")):
        for change in _as_list(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            contact_names = _contact_display_names(value)
            for raw_message in _as_list(value.get("messages")):
                parsed = _parse_message(raw_message, contact_names)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_message(
    raw_message: Any,
    contact_names: dict[str, str],
) -> WhatsAppMessage | None:
    if not isinstance(raw_message, dict):
        return None

    message_id = str(raw_message.get("id", "")).strip()
    from_user_id = str(raw_message.get("from", "")).strip()
    if not message_id or not from_user_id:
        return None

    text: str | None = None
    button_id: str | None = None
    list_reply_id: str | None = None

    message_type = str(raw_message.get("type", "")).strip().lower()
    if message_type == "text":
        text = _nested_text(raw_message, "text", "body")
    elif message_type == "interactive":
        interactive = raw_message.get("interactive")
        if isinstance(interactive, dict):
            interactive_type = str(interactive.get("type", "")).strip().lower()
            if interactive_type == "button_reply":
                reply = interactive.get("button_reply")
                if isinstance(reply, dict):
                    button_id = _clean_text(reply.get("id"))
                    text = _clean_text(reply.get("title"))
            elif interactive_type == "list_reply":
                reply = interactive.get("list_reply")
                if isinstance(reply, dict):
                    list_reply_id = _clean_text(reply.get("id"))
                    text = _clean_text(reply.get("title"))
    elif message_type == "button":
        button = raw_message.get("button")
        if isinstance(button, dict):
            button_id = _clean_text(button.get("payload"))
            text = _clean_text(button.get("text"))

    if text is None and button_id is None and list_reply_id is None:
        return None

    return WhatsAppMessage(
        message_id=message_id,
        from_user_id=from_user_id,
        display_name=contact_names.get(from_user_id),
        text=text,
        button_id=button_id,
        list_reply_id=list_reply_id,
    )


def _contact_display_names(value: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in _as_list(value.get("contacts")):
        if not isinstance(contact, dict):
            continue
        wa_id = str(contact.get("wa_id", "")).strip()
        profile = contact.get("profile")
        if not wa_id or not isinstance(profile, dict):
            continue
        name = _clean_text(profile.get("name"))
        if name:
            names[wa_id] = name
    return names


def _nested_text(value: dict[str, Any], *keys: str) -> str | None:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _clean_text(current)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

