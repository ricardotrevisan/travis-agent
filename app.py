from flask import Flask, request, jsonify
import os
import json
import base64
import requests
from dotenv import load_dotenv

from agent.engine import AgentEngine

load_dotenv()

EVOLUTION_URL = os.getenv("EVOLUTION_URL")
EVOLUTION_APIKEY = os.getenv("EVOLUTION_APIKEY")
INSTANCE = os.getenv("INSTANCE", "default")
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "10"))

app = Flask(__name__)

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


# ---------------------------------------------------------------------
# SEND MEDIA (IMAGE)
# ---------------------------------------------------------------------
def send_whatsapp_media(number: str, file_path: str, instance_name: str, caption: str = ""):
    url = f"{EVOLUTION_URL}/message/sendMedia/{instance_name}"

    try:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as exc:
        print(f"[media] erro lendo arquivo={file_path} err={exc}")
        return None

    file_name = os.path.basename(file_path)

    payload = {
        "number": number,
        "mediatype": "image",
        "caption": caption,
        "fileName": file_name,
        "media": b64,
        "options": {"delay": 0, "presence": "composing"},
    }

    headers = {
        "apikey": EVOLUTION_APIKEY,
        "Content-Type": "application/json",
    }

    r = requests.post(url, json=payload, headers=headers, timeout=SEARCH_TIMEOUT)
    print(f"[evolution media] status={r.status_code} body={r.text}")

    return r.json() if r.ok else None


# ---------------------------------------------------------------------
# DOWNLOAD MEDIA FROM EVOLUTION
# ---------------------------------------------------------------------
def download_evolution_media(instance_name: str, web_message_info: dict) -> str:
    url = f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{instance_name}"

    headers = {
        "apikey": EVOLUTION_APIKEY,
        "Content-Type": "application/json",
    }

    payload = {
        "message": web_message_info,
        "convertToMp4": False
    }

    r = requests.post(url, json=payload, headers=headers, timeout=SEARCH_TIMEOUT)

    if not r.ok:
        raise RuntimeError(f"Erro no download da mídia: {r.status_code} - {r.text}")

    data = r.json()

    if "base64" not in data:
        raise RuntimeError(f"Resposta inválida do Evolution API: {data}")

    return data["base64"]


# ---------------------------------------------------------------------
# ENGINE (sem parâmetros extras)
# ---------------------------------------------------------------------
engine = AgentEngine(send_media_callback=send_whatsapp_media)


# ---------------------------------------------------------------------
# WEBHOOK — delega TUDO para o Engine
# ---------------------------------------------------------------------
@app.post("/webhook")
def webhook():
    data = request.json
    if not data:
        return jsonify({"status": "empty"}), 200

    instance_name = data.get("instance") or INSTANCE

    response_text = engine.handle_inbound_whatsapp(
        wa_event=data,
        instance_name=instance_name,
        send_text=send_whatsapp_message,
        send_media=send_whatsapp_media,
        media_downloader=download_evolution_media,
    )

    return jsonify({"status": "ok", "response": response_text})


# ---------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
