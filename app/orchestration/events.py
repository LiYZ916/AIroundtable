from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.enums import DiscussionStage, RunStatus


@dataclass(slots=True)
class EngineEvent:
    event_type: str
    message: str
    stage: DiscussionStage
    provider_name: str = ""
    status: RunStatus = RunStatus.RUNNING
    payload: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    transient: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
