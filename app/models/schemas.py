from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    ConfidenceLevel,
    DiscussionStage,
    ProviderKind,
    ProviderMode,
    RunStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False, extra="forbid")


class ProviderConfig(Model):
    id: str = Field(default_factory=new_id)
    name: str
    kind: ProviderKind = ProviderKind.MOCK
    enabled: bool = True
    mode: ProviderMode = ProviderMode.MOCK
    timeout_seconds: float = Field(default=60.0, ge=0.1, le=3600)
    max_retries: int = Field(default=1, ge=0, le=10)
    browser_profile_dir: str = ""
    login_url: str = ""
    selector_config: str = ""
    experimental: bool = False
    display_name: str = ""
    avatar_text: str = "AI"
    avatar_path: str = ""
    accent_color: str = "#64748b"
    allowed_hosts: list[str] = Field(default_factory=list)
    browser_channel: str = "msedge"
    onboarding_required: bool = False
    allow_manual_fallback: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderSession(Model):
    id: str = Field(default_factory=new_id)
    provider_id: str
    provider_name: str
    is_logged_in: bool = False
    status: RunStatus = RunStatus.IDLE
    error_message: str = ""
    last_checked_at: datetime | None = None
    local_profile_path: str = ""


class ProjectConfig(Model):
    id: str = Field(default_factory=new_id)
    name: str = "AI Roundtable"
    database_path: str = "data/roundtable.sqlite3"
    export_directory: str = "exports"
    browser_profile_root: str = "browser_profiles"
    concurrency: int = Field(default=4, ge=1, le=16)
    rounds: int = Field(default=1, ge=1, le=5)
    anonymous_review: bool = True
    enable_revision: bool = True
    multi_judge: bool = False
    moderator_name: str = "GPT"
    judge_name: str = "Kimi"
    providers: list[ProviderConfig] = Field(default_factory=list)


class RecordBase(Model):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    provider_name: str
    stage: DiscussionStage
    status: RunStatus = RunStatus.SUCCEEDED
    raw_content: str = ""
    error_message: str = ""
    retry_count: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)


class UserQuestion(Model):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    question: str
    background: str = ""
    constraints: str = ""
    template_name: str = "自由提问"

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class AIResponse(RecordBase):
    stage: DiscussionStage = DiscussionStage.INDEPENDENT
    understanding: str = ""
    conclusion: str = ""
    reasoning: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommendation: str = ""
    distinctive_contribution: str = ""
    key_assumptions: list[str] = Field(default_factory=list)
    alias: str = ""


class ReviewComment(RecordBase):
    stage: DiscussionStage = DiscussionStage.REVIEW
    reviewer_alias: str = ""
    target_alias: str = ""
    strengths: list[str] = Field(default_factory=list)
    logical_gaps: list[str] = Field(default_factory=list)
    unverified_assumptions: list[str] = Field(default_factory=list)
    fact_conflicts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    unique_contributions: list[str] = Field(default_factory=list)
    integration_proposals: list[str] = Field(default_factory=list)
    decisive_tests: list[str] = Field(default_factory=list)


class RevisedResponse(RecordBase):
    stage: DiscussionStage = DiscussionStage.REVISION
    original_response_id: str
    kept_points: list[str] = Field(default_factory=list)
    changed_points: list[str] = Field(default_factory=list)
    change_reasons: list[str] = Field(default_factory=list)
    borrowed_ideas: list[str] = Field(default_factory=list)
    resolved_conflicts: list[str] = Field(default_factory=list)
    synergy_gains: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommendation: str = ""
    alias: str = ""


class JudgeDimensions(Model):
    correctness: float = Field(ge=0, le=10)
    logical_completeness: float = Field(ge=0, le=10)
    executability: float = Field(ge=0, le=10)
    objectivity: float = Field(ge=0, le=10)
    risk_control: float = Field(ge=0, le=10)
    constraint_alignment: float = Field(ge=0, le=10)
    evidence_grounding: float = Field(ge=0, le=10)
    uncertainty_expression: float = Field(ge=0, le=10)

    @property
    def average(self) -> float:
        values = [float(value) for value in self.model_dump().values()]
        return round(sum(values) / len(values), 2)

    @property
    def weighted_total(self) -> float:
        weights = {
            "correctness": 0.22,
            "constraint_alignment": 0.16,
            "executability": 0.16,
            "risk_control": 0.14,
            "evidence_grounding": 0.12,
            "logical_completeness": 0.10,
            "objectivity": 0.05,
            "uncertainty_expression": 0.05,
        }
        return round(
            sum(float(getattr(self, key)) * weight for key, weight in weights.items()),
            2,
        )


class JudgeScore(RecordBase):
    stage: DiscussionStage = DiscussionStage.JUDGE
    judge_name: str
    candidate_alias: str
    candidate_response_id: str
    dimensions: JudgeDimensions
    reason: str
    evidence: list[str] = Field(default_factory=list)
    base_alias: str = ""
    snapshot: str = "final"
    weighted_total: float = Field(default=0.0, ge=0, le=10)
    rank: int = Field(default=0, ge=0)
    verdict: str = ""
    fatal_flaw: str = ""
    comparative_reason: str = ""
    score_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class ProviderEffectiveness(Model):
    provider_name: str
    candidate_alias: str
    before_snapshot: str
    after_snapshot: str
    before_score: float
    after_score: float
    overall_delta: float
    dimension_deltas: dict[str, float] = Field(default_factory=dict)
    improved_dimensions: list[str] = Field(default_factory=list)
    regressed_dimensions: list[str] = Field(default_factory=list)


class RoundEffectiveness(Model):
    round_number: int = Field(ge=1)
    comparison_basis: str
    provider_results: list[ProviderEffectiveness] = Field(default_factory=list)
    average_dimension_deltas: dict[str, float] = Field(default_factory=dict)
    average_overall_delta: float = 0.0
    improved_provider_count: int = Field(default=0, ge=0)
    regressed_provider_count: int = Field(default=0, ge=0)
    verdict: str = "证据不足"
    warnings: list[str] = Field(default_factory=list)


class DiscussionRound(Model):
    id: str = Field(default_factory=new_id)
    round_number: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    responses: list[AIResponse] = Field(default_factory=list)
    reviews: list[ReviewComment] = Field(default_factory=list)
    revisions: list[RevisedResponse] = Field(default_factory=list)
    baseline_scores: list[JudgeScore] = Field(default_factory=list)
    scores: list[JudgeScore] = Field(default_factory=list)
    effectiveness: RoundEffectiveness | None = None


class FinalSynthesis(RecordBase):
    stage: DiscussionStage = DiscussionStage.SYNTHESIS
    recommendation: str
    reasons: list[str] = Field(default_factory=list)
    execution_steps: list[str] = Field(default_factory=list)
    rejected_options: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    user_confirmations: list[str] = Field(default_factory=list)
    contributions: dict[str, str] = Field(default_factory=dict)
    consensus: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    synergy_gains: list[str] = Field(default_factory=list)
    decisive_tradeoffs: list[str] = Field(default_factory=list)
    validation_plan: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    score_averages: dict[str, float] = Field(default_factory=dict)
    decision_scores: dict[str, float] = Field(default_factory=dict)
    recommended_candidate: str = ""
    candidate_ranking: list[str] = Field(default_factory=list)
    selection_rationale: list[str] = Field(default_factory=list)
    minority_report: list[str] = Field(default_factory=list)
    score_warnings: list[str] = Field(default_factory=list)
    round_effectiveness_summary: list[str] = Field(default_factory=list)
    cumulative_dimension_deltas: dict[str, float] = Field(default_factory=dict)
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class ErrorRecord(RecordBase):
    exception_type: str = ""
    recoverable: bool = True
    suggested_action: str = "切换到半自动模式、重试或跳过该 AI"


class DiscussionRecord(Model):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    status: RunStatus = RunStatus.IDLE
    current_stage: DiscussionStage = DiscussionStage.PREPARING
    question: UserQuestion
    provider_names: list[str]
    moderator_name: str
    judge_names: list[str]
    settings: dict[str, Any] = Field(default_factory=dict)
    rounds: list[DiscussionRound] = Field(default_factory=list)
    errors: list[ErrorRecord] = Field(default_factory=list)
    final_synthesis: FinalSynthesis | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    @property
    def title(self) -> str:
        compact = " ".join(self.question.question.split())
        return compact[:60] + ("…" if len(compact) > 60 else "")

    def ensure_runtime_directories(self, root: Path) -> None:
        for relative in ("data", "exports", "logs", "browser_profiles"):
            (root / relative).mkdir(parents=True, exist_ok=True)
