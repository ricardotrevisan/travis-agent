from skills.base import BaseSkill
from skills.direct_answer import DirectAnswerSkill
from skills.garmin_tracking import GarminTrackingSkill
from skills.gmail_list import GmailListSkill
from skills.n8n_schedule_alert import N8NScheduleAlertSkill
from skills.summarize_url import SummarizeURLSkill
from skills.voice_note_reply import VoiceNoteReplySkill
from skills.web_search import WebSearchSkill
from skills.mcp_tools import load_mcp_skills


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, BaseSkill] = {
            "direct_answer": DirectAnswerSkill(),
            "garmin_tracking": GarminTrackingSkill(),
            "gmail_list": GmailListSkill(),
            "n8n_schedule_alert": N8NScheduleAlertSkill(),
            "web_search": WebSearchSkill(),
            "summarize_url": SummarizeURLSkill(),
            "voice_note_reply": VoiceNoteReplySkill(),
        }
        try:
            mcp_skills = load_mcp_skills(set(self._skills.keys()))
            if mcp_skills:
                print(f"[v2.registry] loaded {len(mcp_skills)} MCP skills")
                self._skills.update(mcp_skills)
        except Exception as exc:
            print(f"[v2.registry] mcp skills unavailable: {exc}")

    def get(self, name: str) -> BaseSkill | None:
        skill = self._skills.get(name)
        if not skill or not skill.enabled:
            return None
        return skill

    def planner_catalog(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for skill in self._skills.values():
            if skill.enabled and skill.planner_visible:
                items.append({"name": skill.name, "description": skill.description})
        return items
