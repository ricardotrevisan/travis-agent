import base64
import os
import tempfile
from typing import Any

import requests
from openai import OpenAI

from runtime.models import RequestContext, SkillResult
from runtime.persona import build_system_persona
from skills.base import BaseSkill

VOICE_LANG_ALLOWED = {"pt", "pt-br", "en"}


class VoiceNoteReplySkill(BaseSkill):
    name = "voice_note_reply"
    description = "Processar áudio do WhatsApp, transcrever e responder com áudio MP3."
    planner_visible = False

    def __init__(self) -> None:
        self.evolution_url = (os.getenv("EVOLUTION_URL") or "").rstrip("/")
        self.evolution_apikey = (os.getenv("WA_AGENT_EVOLUTION_APIKEY") or os.getenv("EVOLUTION_APIKEY") or "").strip()
        self.voice_api_url = (os.getenv("VOICE_API_URL", "http://127.0.0.1:8000") or "").rstrip("/")
        self.voice_api_timeout = float(os.getenv("VOICE_API_TIMEOUT", "180"))
        self.language_default = self._normalize_language(os.getenv("VOICE_LANGUAGE_DEFAULT") or os.getenv("LANGUAGE") or "pt")
        self.openai_model = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    @staticmethod
    def _normalize_language(language: str | None) -> str:
        raw = (language or "").strip().lower()
        if raw in ("pt_br", "ptbr", "pt-br"):
            return "pt-br"
        if raw in VOICE_LANG_ALLOWED:
            return raw
        return "pt"

    @staticmethod
    def _is_voice_event(wa_event: dict[str, Any]) -> bool:
        if wa_event.get("event") != "messages.upsert":
            return False
        msg = ((wa_event.get("data") or {}).get("message") or {})
        return "audioMessage" in msg

    @staticmethod
    def _build_web_message_info(wa_event: dict[str, Any]) -> dict[str, Any]:
        data = wa_event.get("data") or {}
        return {
            "key": data.get("key") or {},
            "message": data.get("message") or {},
            "pushName": data.get("pushName"),
            "messageTimestamp": data.get("messageTimestamp"),
            "instanceId": wa_event.get("instanceId") or wa_event.get("instance"),
            "source": wa_event.get("source", "web"),
        }

    def _download_audio_base64(self, instance_name: str, wa_event: dict[str, Any]) -> str:
        if not self.evolution_url:
            raise RuntimeError("EVOLUTION_URL not configured")
        if not self.evolution_apikey:
            raise RuntimeError("EVOLUTION_APIKEY not configured")

        url = f"{self.evolution_url}/chat/getBase64FromMediaMessage/{instance_name}"
        headers = {
            "apikey": self.evolution_apikey,
            "Content-Type": "application/json",
        }
        payload = {
            "message": self._build_web_message_info(wa_event),
            "convertToMp4": False,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=self.voice_api_timeout)
        if not r.ok:
            raise RuntimeError(f"media download failed: {r.status_code} {r.text}")
        data = r.json()
        audio_b64 = data.get("base64") or ""
        if not audio_b64:
            raise RuntimeError("media download returned empty base64")
        return audio_b64

    def _transcribe_audio_b64(self, audio_b64: str, filename: str, mimetype: str) -> str:
        raw_b64 = audio_b64.split(",", 1)[1] if audio_b64.lstrip().startswith("data:") and "," in audio_b64 else audio_b64
        audio_raw = base64.b64decode(raw_b64)
        suffix = os.path.splitext(filename)[1] or ".ogg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp.write(audio_raw)
            temp_path = temp.name
        try:
            with open(temp_path, "rb") as f:
                files = {"file": (filename, f, mimetype)}
                r = requests.post(f"{self.voice_api_url}/transcribe", files=files, timeout=self.voice_api_timeout)
            if not r.ok:
                raise RuntimeError(f"stt failed: {r.status_code} {r.text}")
            data = r.json()
            text = (data.get("text") or "").strip()
            if not text:
                raise RuntimeError("stt returned empty text")
            return text
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _generate_reply_text(self, transcript: str) -> str:
        response = self.openai_client.responses.create(
            model=self.openai_model,
            input=[
                {
                    "role": "system",
                    "content": build_system_persona(channel="voice"),
                },
                {"role": "user", "content": transcript},
            ],
        )
        text = (response.output_text or "").strip()
        if not text:
            raise RuntimeError("llm returned empty text")
        return text

    def _synthesize_mp3(self, text: str, language: str) -> bytes:
        payload = {
            "text": text,
            "language": self._normalize_language(language),
            "format": "mp3",
        }
        r = requests.post(
            f"{self.voice_api_url}/tts",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.voice_api_timeout,
        )
        if not r.ok:
            raise RuntimeError(f"tts failed: {r.status_code} {r.text}")
        return r.content

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        wa_event = args.get("wa_event") if isinstance(args, dict) else None
        if not isinstance(wa_event, dict):
            return SkillResult(ok=False, error="voice_note_reply missing wa_event")
        if not self._is_voice_event(wa_event):
            return SkillResult(ok=False, error="voice_note_reply called for non-audio event")

        message = ((wa_event.get("data") or {}).get("message") or {})
        audio_meta = message.get("audioMessage") or {}
        mimetype = audio_meta.get("mimetype") or "audio/ogg"
        file_ext = ".mp3" if "mpeg" in mimetype else ".ogg"
        filename = f"voice_input{file_ext}"

        try:
            audio_b64 = audio_meta.get("base64") or self._download_audio_base64(ctx.instance_name, wa_event)
            transcript = self._transcribe_audio_b64(audio_b64, filename=filename, mimetype=mimetype)
            reply_text = self._generate_reply_text(transcript)
        except Exception as exc:
            fallback = "Nao consegui processar seu audio agora. Tente novamente em instantes."
            return SkillResult(
                ok=True,
                output={"voice_mode": True, "error_stage": "stt_or_generation", "error": str(exc)},
                user_visible_text=fallback,
            )

        try:
            mp3_bytes = self._synthesize_mp3(reply_text, language=self.language_default)
            return SkillResult(
                ok=True,
                output={
                    "voice_mode": True,
                    "mimetype": "audio/mpeg",
                    "audio_bytes": mp3_bytes,
                    "transcript": transcript,
                    "text_reply": reply_text,
                    "tts_provider": "voice_api",
                },
                user_visible_text=reply_text,
            )
        except Exception as exc:
            return SkillResult(
                ok=True,
                output={
                    "voice_mode": True,
                    "error_stage": "tts",
                    "error": str(exc),
                    "text_reply": reply_text,
                },
                user_visible_text=reply_text,
            )
