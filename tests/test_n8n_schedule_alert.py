import os
import unittest
from unittest.mock import patch

from runtime.models import RequestContext
from skills.n8n_schedule_alert import N8NScheduleAlertSkill


class _FakeResponse:
    def __init__(self, ok: bool, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class N8NScheduleAlertTests(unittest.TestCase):
    def setUp(self):
        os.environ["N8N_SCHEDULE_WEBHOOK_URL"] = "http://example.com/webhook/schedule"
        os.environ["N8N_SCHEDULE_TIMEOUT"] = "15"
        os.environ["SCHEDULE_TIMEZONE"] = "America/Sao_Paulo"
        self.skill = N8NScheduleAlertSkill()
        self.ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m1",
            user_text="agende lembrete amanhã às 10:30 tomar água",
        )

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_valid_schedule_calls_webhook_with_create_payload(self, mock_post):
        mock_post.return_value = _FakeResponse(
            ok=True,
            payload={"idTask": "abc-1", "run_at": "2026-03-11T13:30:00.000Z", "status": "scheduled"},
        )
        result = self.skill.run(self.ctx, {"action": "create"})
        self.assertTrue(result.ok)
        self.assertIn("Alerta agendado", result.user_visible_text)
        self.assertTrue(mock_post.called)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["data"]["payload"]["target"]["sender"], self.ctx.sender)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_ambiguous_time_returns_clarification(self, mock_post):
        ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m2",
            user_text="agende um lembrete no n8n",
        )
        result = self.skill.run(ctx, {"action": "create"})
        self.assertTrue(result.ok)
        self.assertIn("Preciso de data e horário", result.user_visible_text)
        self.assertFalse(mock_post.called)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_webhook_error_returns_explicit_message(self, mock_post):
        mock_post.return_value = _FakeResponse(ok=False, status_code=500, text="internal error")
        result = self.skill.run(self.ctx, {"action": "create"})
        self.assertTrue(result.ok)
        self.assertIn("N8N retornou erro", result.user_visible_text)
        self.assertEqual(result.output.get("status_code"), 500)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_create_without_idtask_reports_unconfirmed(self, mock_post):
        mock_post.return_value = _FakeResponse(ok=True, payload={"status": "ok"})
        result = self.skill.run(self.ctx, {"action": "create"})
        self.assertTrue(result.ok)
        self.assertIn("sem idTask", result.user_visible_text)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_list_calls_webhook_with_list_action(self, mock_post):
        mock_post.return_value = _FakeResponse(
            ok=True,
            payload={
                "tasks": [
                    {
                        "idTask": "a1",
                        "title": "primeira tarefa",
                        "run_at": "2026-03-10T16:00:00.000Z",
                        "status": "scheduled",
                        "created_at": "2026-03-09T10:00:00.000Z",
                    },
                    {
                        "idTask": "a2",
                        "title": "segunda tarefa",
                        "run_at": "2026-03-11T16:00:00.000Z",
                        "status": "scheduled",
                        "created_at": "2026-03-09T11:00:00.000Z",
                    },
                ]
            },
        )
        ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m4",
            user_text="liste meus agendamentos no n8n",
        )
        result = self.skill.run(ctx, {"action": "list"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output.get("action"), "list")
        self.assertIn("Total encontrado: 2", result.user_visible_text)
        self.assertIn("title: primeira tarefa", result.user_visible_text)
        self.assertIn("run_at: 2026-03-10T16:00:00.000Z", result.user_visible_text)
        self.assertIn("status: scheduled", result.user_visible_text)
        self.assertIn("created_at: 2026-03-09T10:00:00.000Z", result.user_visible_text)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["action"], "list")
        self.assertEqual(payload["data"]["payload"]["target"]["sender"], ctx.sender)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_delete_calls_webhook_with_task_id(self, mock_post):
        mock_post.return_value = _FakeResponse(ok=True, payload={"status": "deleted"})
        ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m5",
            user_text="excluir idTask abc-123 no n8n",
        )
        result = self.skill.run(ctx, {"action": "delete"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output.get("action"), "delete")
        self.assertIn("Tarefa removida no n8n. idTask: abc-123", result.user_visible_text)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["action"], "delete")
        self.assertEqual(payload["data"]["idTask"], "abc-123")
        self.assertEqual(payload["data"]["payload"]["target"]["sender"], ctx.sender)

    @patch("skills.n8n_schedule_alert.requests.post")
    def test_delete_without_task_id_returns_clarification(self, mock_post):
        ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m6",
            user_text="remova meu lembrete no n8n",
        )
        result = self.skill.run(ctx, {"action": "delete"})
        self.assertTrue(result.ok)
        self.assertIn("preciso do idtask", result.user_visible_text.lower())
        self.assertFalse(mock_post.called)

    def test_invalid_sender_rejected(self):
        bad_ctx = RequestContext(
            sender="user@example.com",
            instance_name="Travis",
            message_id="m7",
            user_text="agende para 2026-03-10T16:30:00-03:00",
        )
        result = self.skill.run(bad_ctx, {"action": "create"})
        self.assertTrue(result.ok)
        self.assertIn("sender do WhatsApp está inválido", result.user_visible_text)

    def test_missing_action_returns_clarification(self):
        result = self.skill.run(self.ctx, {})
        self.assertTrue(result.ok)
        self.assertIn("confirme a ação", result.user_visible_text.lower())


if __name__ == "__main__":
    unittest.main()
