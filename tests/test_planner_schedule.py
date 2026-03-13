import os
import unittest
from unittest.mock import patch

from runtime.models import RequestContext
from runtime.planner import plan


class PlannerScheduleTests(unittest.TestCase):
    def test_heuristic_defaults_to_direct_answer_without_openai_key(self):
        ctx = RequestContext(
            sender="5511975196655@s.whatsapp.net",
            instance_name="Travis",
            message_id="m1",
            user_text="agende no n8n um lembrete para amanhã às 10:30",
        )
        catalog = [
            {"name": "direct_answer", "description": "x"},
            {"name": "n8n_schedule_alert", "description": "y"},
            {"name": "web_search", "description": "z"},
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            p = plan(ctx, available_skills=catalog)
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.steps[0].skill, "direct_answer")


if __name__ == "__main__":
    unittest.main()
