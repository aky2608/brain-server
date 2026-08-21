from abc import ABC, abstractmethod
from enum import Enum
from typing import ClassVar, Optional, Type

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict


class InterruptTier(str, Enum):
    always = "always"
    morning_brief = "morning_brief"
    log_only = "log_only"


class CostTier(str, Enum):
    free = "free"
    flash = "flash"
    haiku = "haiku"
    claude = "claude"


class GraphState(TypedDict):
    """Fixed shared state. Specialists receive and return narrow slices — never this whole dict."""
    raw_input: str
    source: str
    capture_uuid: Optional[str]
    routed_to: Optional[str]
    specialist_result: Optional[dict]


class NarrowModel(BaseModel):
    """Base for all agent I/O schemas. Extra keys are forbidden — enforced at Pydantic level."""
    model_config = ConfigDict(extra="forbid")


class BaseAgent(ABC):
    interrupt_tier: ClassVar[InterruptTier]
    cost_tier: ClassVar[CostTier]
    requires_context: ClassVar[list[str]]

    InputSchema: ClassVar[Type[NarrowModel]]
    OutputSchema: ClassVar[Type[NarrowModel]]

    @abstractmethod
    def handle(self, input: NarrowModel) -> NarrowModel: ...
