import importlib
import os
import unittest
from unittest.mock import patch

from runtime.models import RequestContext
from skills.direct_answer import DirectAnswerSkill
from skills.summarize_url import _synthesize_summary
from skills.voice_note_reply import VoiceNoteReplySkill


class _FakeLLMResponse:
    def __init__(self, output_text: str):
        self.output_text = output_text


class PersonaLayerTests(unittest.TestCase):
    def test_build_system_persona_default_text(self):
        with patch.dict(os.environ, {}, clear=True):
            persona_module = importlib.import_module("runtime.persona")
            persona_module = importlib.reload(persona_module)
            text = persona_module.build_system_persona("text")
        self.assertIn("Você é Travis.", text)
        self.assertIn("estoico, direto", text)
        self.assertIn("Sarcasmo leve", text)

    def test_build_system_persona_voice_with_moderate_sarcasm(self):
        with patch.dict(
            os.environ,
            {"AGENT_NAME": "Travis", "AGENT_PERSONA_SARCASM": "moderate"},
            clear=True,
        ):
            persona_module = importlib.import_module("runtime.persona")
            persona_module = importlib.reload(persona_module)
            text = persona_module.build_system_persona("voice")
        self.assertIn("Você é Travis.", text)
        self.assertIn("Sarcasmo moderado", text)
        self.assertIn("A resposta será entregue como áudio", text)
        self.assertIn("Nunca diga que não consegue enviar áudio", text)

    def test_direct_answer_uses_persona_system_prompt(self):
        with patch("skills.direct_answer.client.responses.create") as mock_create:
            mock_create.return_value = _FakeLLMResponse("ok")
            skill = DirectAnswerSkill()
            ctx = RequestContext(
                sender="5511999999999@s.whatsapp.net",
                instance_name="Travis",
                message_id="m1",
                user_text="oi",
            )
            result = skill.run(ctx, {})
        self.assertTrue(result.ok)
        _, kwargs = mock_create.call_args
        system_content = kwargs["input"][0]["content"]
        self.assertIn("Você é", system_content)
        self.assertIn("estoico, direto", system_content)

    def test_voice_note_reply_uses_persona_system_prompt(self):
        os.environ.setdefault("OPENAI_API_KEY", "test-key")
        skill = VoiceNoteReplySkill()
        with patch.object(skill.openai_client.responses, "create") as mock_create:
            mock_create.return_value = _FakeLLMResponse("resposta")
            out = skill._generate_reply_text("transcrito")
        self.assertEqual(out, "resposta")
        _, kwargs = mock_create.call_args
        system_content = kwargs["input"][0]["content"]
        self.assertIn("Você é", system_content)
        self.assertIn("A resposta será entregue como áudio", system_content)

    def test_summarize_synthesis_includes_persona_preamble(self):
        with patch("skills.summarize_url._client.responses.create") as mock_create:
            mock_create.return_value = _FakeLLMResponse("resumo final")
            out = _synthesize_summary(
                user_goal="resumir",
                url="https://example.com",
                title="titulo",
                base_summary="base",
            )
        self.assertEqual(out, "resumo final")
        _, kwargs = mock_create.call_args
        prompt = kwargs["input"][0]["content"]
        self.assertIn("Você é", prompt)
        self.assertIn("Você vai gerar um resumo final", prompt)


if __name__ == "__main__":
    unittest.main()
