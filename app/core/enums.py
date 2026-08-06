from __future__ import annotations

from enum import StrEnum


class ProviderMode(StrEnum):
    AUTOMATIC = "automatic"
    SEMI_AUTOMATIC = "semi_automatic"
    MOCK = "mock"


class ProviderKind(StrEnum):
    MOCK = "mock"
    WEB = "web"
    MANUAL = "manual"
    TEMPLATE = "template"


class RunStatus(StrEnum):
    IDLE = "idle"
    WAITING = "waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class DiscussionStage(StrEnum):
    PREPARING = "preparing"
    INDEPENDENT = "independent_answer"
    REVIEW = "anonymous_review"
    REVISION = "revision"
    JUDGE = "judge_scoring"
    SYNTHESIS = "final_synthesis"
    COMPLETED = "completed"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

