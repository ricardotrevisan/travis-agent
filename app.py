from flask import Flask, request, jsonify
import os
import json
import base64
import re
import requests
from dotenv import load_dotenv
import redis

load_dotenv()

# ------------------------------------------------------------
# Environment compatibility aliases (travis-the-helpful -> travis-agent)
# ------------------------------------------------------------
def _resolve_env(primary: str, alias: str | None = None, default: str | None = None) -> str | None:
    if alias and os.getenv(alias):
        return os.getenv(alias)
    if os.getenv(primary):
        return os.getenv(primary)
    return default


resolved_openai = _resolve_env("OPENAI_API_KEY", "WA_AGENT_OPENAI_API_KEY")
if resolved_openai and not os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = resolved_openai

resolved_allowlist = _resolve_env("ALLOWED_WHATSAPP_NUMBERS", "WA_AGENT_ALLOWED_SENDERS")
if resolved_allowlist and not os.getenv("ALLOWED_WHATSAPP_NUMBERS"):
    os.environ["ALLOWED_WHATSAPP_NUMBERS"] = resolved_allowlist

from runtime.orchestrator import handle_webhook_v2

EVOLUTION_URL = os.getenv("EVOLUTION_URL")
EVOLUTION_APIKEY = _resolve_env("EVOLUTION_APIKEY", "WA_AGENT_EVOLUTION_APIKEY")
INSTANCE = _resolve_env("INSTANCE", "WA_AGENT_INSTANCE", "default") or "default"
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "10"))
TASK_CALLBACK_SECRET = (os.getenv("TASK_CALLBACK_SECRET") or "").strip()
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "agent")
WEBHOOK_DEDUPE_TTL_SECONDS = int(os.getenv("WEBHOOK_DEDUPE_TTL_SECONDS", "300"))

app = Flask(__name__)


def _init_redis():
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True,
        )
        r.ping()
        return r
    except Exception as exc:
        print(f"[redis] unavailable: {exc}")
        return None


_redis = _init_redis()


def _redis_key(kind: str, value: str) -> str:
    return f"{REDIS_PREFIX}:{kind}:{value}"


def _mark_message_processed(message_id: str) -> bool:
    if not message_id:
        return False
    if not _redis:
        return False
    key = _redis_key("msg", message_id)
    try:
        created = _redis.set(key, "1", ex=WEBHOOK_DEDUPE_TTL_SECONDS, nx=True)
        return bool(created)
    except Exception as exc:
        print(f"[redis] dedupe error: {exc}")
        return False

# ---------------------------------------------------------------------
# SEND WHATSAPP TEXT MESSAGE
# ---------------------------------------------------------------------
def send_whatsapp_message(number: str, message: str, instance_name: str):
    print(">>> enviando resposta para Evolution API")
    url = f"{EVOLUTION_URL}/message/sendText/{instance_name}"

    payload = {
        "number": number,
        "text": message,
        "options": {
            "delay": 0,
            "presence": "composing",
            "linkPreview": True,
            "mentions": {"everyOne": False, "mentioned": []},
        },
        "textMessage": {"text": message},
    }

    headers = {
        "apikey": EVOLUTION_APIKEY,
        "Content-Type": "application/json",
    }

    r = requests.post(url, json=payload, headers=headers, timeout=SEARCH_TIMEOUT)
    print(f"[evolution] status={r.status_code} body={r.text}")

    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "body": r.text}


def send_whatsapp_voice(number: str, audio_bytes: bytes, instance_name: str, mimetype: str = "audio/mpeg"):
    url = f"{EVOLUTION_URL}/message/sendMedia/{instance_name}"
    payload = {
        "number": number,
        "mediatype": "audio",
        "fileName": "reply.mp3",
        "mimetype": mimetype,
        "media": base64.b64encode(audio_bytes).decode("utf-8"),
        "options": {"delay": 0, "presence": "recording"},
    }
    headers = {
        "apikey": EVOLUTION_APIKEY,
        "Content-Type": "application/json",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=SEARCH_TIMEOUT)
    print(f"[evolution voice] status={r.status_code} body={r.text}")
    return r.json() if r.ok else None


def _normalize_whatsapp_number(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if re.fullmatch(r"\d{8,16}@s\.whatsapp\.net", lowered):
        return lowered
    if re.fullmatch(r"\d{8,16}", raw):
        return f"{raw}@s.whatsapp.net"
    return ""


def _parse_sender_allowlist() -> set[str]:
    raw = os.getenv("WA_AGENT_ALLOWED_SENDERS") or os.getenv("ALLOWED_WHATSAPP_NUMBERS") or ""
    parts = re.split(r"[,;\s\n]+", raw)
    out: set[str] = set()
    for part in parts:
        normalized = _normalize_whatsapp_number(part)
        if normalized:
            out.add(normalized)
    return out


ALLOWED_SENDERS = _parse_sender_allowlist()


def _is_sender_allowed(sender: str) -> bool:
    if not ALLOWED_SENDERS:
        return True
    return _normalize_whatsapp_number(sender) in ALLOWED_SENDERS


def handle_webhook_v2_text(wa_event: dict, instance_name: str) -> str:
    return handle_webhook_v2(
        wa_event=wa_event,
        instance_name=instance_name,
        send_text=send_whatsapp_message,
        send_voice=send_whatsapp_voice,
    )


# ---------------------------------------------------------------------
# WEBHOOK — fluxo v2
# ---------------------------------------------------------------------
@app.post("/webhook")
def webhook():
    data = request.json
    if not data:
        return jsonify({"status": "empty"}), 200

    sender = str((((data.get("data") or {}).get("key") or {}).get("remoteJid") or "")).strip()
    if sender and not _is_sender_allowed(sender):
        print(f"[webhook] sender blocked by allowlist sender={sender}")
        return jsonify({"status": "ignored", "reason": "sender_not_allowed"}), 200

    instance_name = data.get("instance") or INSTANCE
    print(f"[webhook] mode=v2 instance={instance_name}")
    event = str(data.get("event") or "")
    key = (data.get("data") or {}).get("key") or {}
    message = (data.get("data") or {}).get("message") or {}
    from_me = bool(key.get("fromMe"))
    has_text = bool(message.get("conversation") or message.get("text", {}).get("body"))
    print(
        "[webhook] v2 payload "
        f"event={event} sender={sender or '-'} fromMe={from_me} has_text={has_text} "
        f"message_keys={list(message.keys())}"
    )

    response_text = handle_webhook_v2_text(
        wa_event=data,
        instance_name=instance_name,
    )

    return jsonify({"status": "ok", "response": response_text or ""})


@app.post("/webhook/task-callback")
def task_callback():
    data = request.json or {}
    header_secret = (request.headers.get("X-Task-Secret") or "").strip()

    if TASK_CALLBACK_SECRET and header_secret != TASK_CALLBACK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    task_id = str(data.get("task_id") or data.get("idTask") or "").strip()
    register_type = str(data.get("register_type") or "message").strip().lower()
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    sender = str(
        target.get("sender")
        or data.get("sender")
        or data.get("remoteJid")
        or ""
    ).strip()
    sender = _normalize_whatsapp_number(sender)
    instance_name = str(
        target.get("instance")
        or data.get("instance")
        or INSTANCE
    ).strip() or INSTANCE
    message = str(data.get("message") or data.get("prompt") or "").strip()

    if register_type != "message":
        return jsonify({"ok": False, "error": "register_type não suportado para este endpoint"}), 400
    if not task_id:
        return jsonify({"ok": False, "error": "task_id obrigatório"}), 400
    if not sender:
        return jsonify({"ok": False, "error": "sender obrigatório"}), 400
    if not _is_sender_allowed(sender):
        print(f"[task-callback] sender blocked by allowlist sender={sender}")
        return jsonify({"ok": False, "error": "sender_not_allowed"}), 403
    if not message:
        return jsonify({"ok": False, "error": "message obrigatório"}), 400

    idempotency_key = str(data.get("idempotency_key") or "").strip()
    dedupe_message_id = idempotency_key or f"task:{task_id}"
    if _mark_message_processed(dedupe_message_id):
        send_whatsapp_message(sender, message, instance_name)
    elif _redis:
        return jsonify(
            {
                "ok": True,
                "status": "duplicate_ignored",
                "task_id": task_id,
                "idempotency_key": dedupe_message_id,
            }
        ), 200
    else:
        # Without Redis dedupe we still deliver.
        send_whatsapp_message(sender, message, instance_name)

    return jsonify(
        {
            "ok": True,
            "task_id": task_id,
            "idempotency_key": dedupe_message_id,
            "sender": sender,
            "instance": instance_name,
        }
    )


# ---------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
