import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
import app as app_module


class AppScheduleCallbackTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_env_alias_precedence_resolver(self):
        with patch.dict(os.environ, {"PRIMARY_X": "p", "ALIAS_X": "a"}, clear=False):
            self.assertEqual(app_module._resolve_env("PRIMARY_X", "ALIAS_X"), "a")
        with patch.dict(os.environ, {"PRIMARY_X": "p"}, clear=True):
            self.assertEqual(app_module._resolve_env("PRIMARY_X", "ALIAS_X"), "p")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app_module._resolve_env("PRIMARY_X", "ALIAS_X", "d"), "d")

    def test_allowlist_compatibility_parsing(self):
        with patch.dict(os.environ, {"WA_AGENT_ALLOWED_SENDERS": "5511975196655"}, clear=True):
            parsed = app_module._parse_sender_allowlist()
            self.assertIn("5511975196655@s.whatsapp.net", parsed)

    def test_task_callback_rejects_invalid_secret(self):
        payload = {
            "task_id": "t1",
            "message": "oi",
            "target": {"sender": "5511975196655@s.whatsapp.net", "instance": "Travis"},
        }
        with patch.object(app_module, "TASK_CALLBACK_SECRET", "secret-x"):
            resp = self.client.post("/webhook/task-callback", json=payload, headers={"X-Task-Secret": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_task_callback_requires_fields(self):
        resp = self.client.post("/webhook/task-callback", json={"message": "oi"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("task_id", resp.get_json().get("error", ""))

    def test_task_callback_duplicate_ignored(self):
        payload = {
            "task_id": "t2",
            "message": "hello",
            "target": {"sender": "5511975196655@s.whatsapp.net", "instance": "Travis"},
        }
        with (
            patch.object(app_module, "ALLOWED_SENDERS", {"5511975196655@s.whatsapp.net"}),
            patch.object(app_module.engine, "_mark_message_processed", return_value=False),
            patch.object(app_module.engine, "redis", object()),
            patch.object(app_module, "send_whatsapp_message") as send_mock,
        ):
            resp = self.client.post("/webhook/task-callback", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("status"), "duplicate_ignored")
        self.assertFalse(send_mock.called)

    def test_task_callback_valid_sends_message(self):
        payload = {
            "task_id": "t3",
            "message": "scheduled ping",
            "target": {"sender": "5511975196655@s.whatsapp.net", "instance": "Travis"},
        }
        with (
            patch.object(app_module, "ALLOWED_SENDERS", {"5511975196655@s.whatsapp.net"}),
            patch.object(app_module.engine, "_mark_message_processed", return_value=True),
            patch.object(app_module, "send_whatsapp_message") as send_mock,
        ):
            resp = self.client.post("/webhook/task-callback", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("ok"))
        send_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
