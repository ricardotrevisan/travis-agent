import base64
import os
import unittest
from unittest.mock import patch

from runtime.models import RequestContext
from skills.voice_note_reply import VoiceNoteReplySkill


class _FakeResponse:
    def __init__(
        self,
        ok: bool,
        status_code: int = 200,
        text: str = "",
        payload: dict | None = None,
        content: bytes = b"",
    ):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


class VoiceNoteReplySkillTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("OPENAI_API_KEY", "test-key")
        self.skill = VoiceNoteReplySkill()

    def _voice_event(self) -> dict:
        return {
            "event": "messages.upsert",
            "data": {
                "key": {"remoteJid": "5511999999999@s.whatsapp.net", "id": "m1", "fromMe": False},
                "message": {"audioMessage": {"mimetype": "audio/ogg"}},
                "pushName": "Test",
                "messageTimestamp": 123,
            },
            "instance": "Travis",
            "source": "web",
        }

    @patch("skills.voice_note_reply.requests.post")
    def test_transcribe_audio_b64_success(self, mock_post):
        mock_post.return_value = _FakeResponse(ok=True, payload={"text": "texto transcrito"})
        audio_b64 = base64.b64encode(b"fake-audio").decode("utf-8")
        out = self.skill._transcribe_audio_b64(audio_b64, filename="voice.ogg", mimetype="audio/ogg")
        self.assertEqual(out, "texto transcrito")
        self.assertTrue(mock_post.called)
        _, kwargs = mock_post.call_args
        self.assertIn("files", kwargs)
        self.assertIn("file", kwargs["files"])

    @patch("skills.voice_note_reply.requests.post")
    def test_synthesize_mp3_via_voice_api(self, mock_post):
        mock_post.return_value = _FakeResponse(ok=True, content=b"\x10\x11")
        out = self.skill._synthesize_mp3("hello", language="pt")
        self.assertEqual(out, b"\x10\x11")
        self.assertTrue(mock_post.called)
        args, kwargs = mock_post.call_args
        self.assertTrue(args[0].endswith("/tts"))
        self.assertEqual(kwargs["json"]["format"], "mp3")

    def test_run_audio_success_output_structure(self):
        ctx = RequestContext(
            sender="5511999999999@s.whatsapp.net",
            instance_name="Travis",
            message_id="m1",
            user_text="",
        )
        with (
            patch.object(self.skill, "_download_audio_base64", return_value="ZmFrZQ=="),
            patch.object(self.skill, "_transcribe_audio_b64", return_value="oi"),
            patch.object(self.skill, "_generate_reply_text", return_value="resposta"),
            patch.object(self.skill, "_synthesize_mp3", return_value=b"\x01\x02"),
        ):
            result = self.skill.run(ctx, {"wa_event": self._voice_event()})
        self.assertTrue(result.ok)
        self.assertEqual(result.output.get("mimetype"), "audio/mpeg")
        self.assertIsInstance(result.output.get("audio_bytes"), bytes)
        self.assertEqual(result.output.get("transcript"), "oi")
        self.assertEqual(result.output.get("text_reply"), "resposta")

    def test_run_tts_failure_falls_back_to_text(self):
        ctx = RequestContext(
            sender="5511999999999@s.whatsapp.net",
            instance_name="Travis",
            message_id="m1",
            user_text="",
        )
        with (
            patch.object(self.skill, "_download_audio_base64", return_value="ZmFrZQ=="),
            patch.object(self.skill, "_transcribe_audio_b64", return_value="oi"),
            patch.object(self.skill, "_generate_reply_text", return_value="resposta"),
            patch.object(self.skill, "_synthesize_mp3", side_effect=RuntimeError("boom")),
        ):
            result = self.skill.run(ctx, {"wa_event": self._voice_event()})
        self.assertTrue(result.ok)
        self.assertEqual(result.user_visible_text, "resposta")
        self.assertIsNone(result.output.get("audio_bytes"))
        self.assertEqual(result.output.get("error_stage"), "tts")


if __name__ == "__main__":
    unittest.main()
