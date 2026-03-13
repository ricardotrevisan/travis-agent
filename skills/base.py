from abc import ABC, abstractmethod
from typing import Any

from runtime.models import RequestContext, SkillResult


class BaseSkill(ABC):
    name: str = ""
    description: str = ""
    enabled: bool = True
    planner_visible: bool = True

    @abstractmethod
    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        raise NotImplementedError

