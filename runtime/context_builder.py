import re
from typing import Any, Dict, Optional

from runtime.models import RequestContext

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _extract_user_text(message: Dict[str, Any]) -> str:
    return (
        message.get("conversation")
        or message.get("text", {}).get("body")
        or message.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return [u.strip(").,;!?") for u in _URL_RE.findall(text)]


def build_request_context(wa_event: Dict[str, Any], instance_name: str) -> Optional[RequestContext]:
    if wa_event.get("event") != "messages.upsert":
        print(f"[v2.context] skip reason=unsupported_event event={wa_event.get('event')}")
        return None

    data = wa_event.get("data") or {}
    key = data.get("key") or {}
    message = data.get("message") or {}

    sender = key.get("remoteJid") or ""
    if not sender:
        print("[v2.context] skip reason=missing_sender")
        return None

    if bool(key.get("fromMe")):
        print(f"[v2.context] skip reason=from_me sender={sender}")
        return None

    if "imageMessage" in message:
        print(f"[v2.context] media_not_supported type=image sender={sender}")
    if "documentMessage" in message:
        print(f"[v2.context] media_not_supported type=document sender={sender}")

    user_text = _extract_user_text(message)
    if not user_text:
        print(f"[v2.context] skip reason=empty_or_non_text sender={sender}")
        return None

    return RequestContext(
        sender=sender,
        instance_name=instance_name,
        message_id=key.get("id") or "",
        user_text=user_text,
        urls=_extract_urls(user_text),
        channel_meta={
            "pushName": data.get("pushName"),
            "messageTimestamp": data.get("messageTimestamp"),
            "source": wa_event.get("source", "web"),
        },
    )
