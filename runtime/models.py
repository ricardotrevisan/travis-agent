from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RequestContext:
    sender: str
    instance_name: str
    message_id: str
    user_text: str
    urls: list[str] = field(default_factory=list)
    image_b64: Optional[str] = None
    pdf_b64: Optional[str] = None
    document_b64: Optional[str] = None
    history: list[dict[str, str]] = field(default_factory=list)
    channel_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanStep:
    skill: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    steps: list[PlanStep]
    final_response_mode: str = "skill_output"


@dataclass
class SkillResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    user_visible_text: str = ""
    error: Optional[str] = None

