from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from statistics import mean
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from app.core.enums import (
    ConfidenceLevel,
    DiscussionStage,
    ProviderKind,
    ProviderMode,
    RunStatus,
)
from app.core.exceptions import DiscussionCancelled, EmptyResponseError
from app.models import (
    AIResponse,
    DiscussionRecord,
    DiscussionRound,
    ErrorRecord,
    FinalSynthesis,
    JudgeScore,
    ProviderEffectiveness,
    ReviewComment,
    RevisedResponse,
    RoundEffectiveness,
    UserQuestion,
)
from app.models.schemas import JudgeDimensions
from app.orchestration.events import EngineEvent
from app.prompts import PromptFactory
from app.providers import AIProvider, ProviderRegistry
from app.utils.logging import record_engine_event

T = TypeVar("T")
EventHandler = Callable[[EngineEvent], None]


@dataclass(slots=True)
class ProviderCall:
    provider: AIProvider
    call_id: str = ""
    raw: str = ""
    retry_count: int = 0
    elapsed_seconds: float = 0.0
    error: ErrorRecord | None = None


class RoundtableEngine:
    def __init__(
        self,
        registry: ProviderRegistry,
        storage: Any | None = None,
        event_handler: EventHandler | None = None,
        interactive_recovery: bool = False,
        log_directory: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.storage = storage
        self.event_handler = event_handler
        self.interactive_recovery = interactive_recovery
        if log_directory is None and getattr(storage, "path", None):
            log_directory = Path(storage.path).parent.parent / "logs"
        self.log_directory = Path(log_directory) if log_directory else None
        self.run_log_path: Path | None = None
        self._cancel_event = asyncio.Event()
        self._recovery_futures: dict[str, asyncio.Future[str]] = {}
        self.current_record: DiscussionRecord | None = None
        self._concurrency = 4

    def _emit(
        self,
        event_type: str,
        message: str,
        stage: DiscussionStage,
        provider_name: str = "",
        status: RunStatus = RunStatus.RUNNING,
        call_id: str = "",
        transient: bool = False,
        **payload: Any,
    ) -> None:
        event = EngineEvent(
            event_type=event_type,
            message=message,
            stage=stage,
            provider_name=provider_name,
            status=status,
            payload=payload,
            call_id=call_id,
            transient=transient,
        )
        if self.event_handler:
            try:
                self.event_handler(event)
            except Exception:
                # UI/event reporting must never break orchestration.
                pass
        if self.storage:
            try:
                self.storage.append_event(self.current_record.id if self.current_record else "", event)
            except Exception:
                pass
        if self.log_directory and self.current_record:
            try:
                path = record_engine_event(self.log_directory, self.current_record.id, event)
                if path:
                    self.run_log_path = path
            except Exception:
                # Diagnostics must not change the discussion result.
                pass

    def _save(self) -> None:
        if self.current_record:
            self.current_record.touch()
            if self.storage:
                self.storage.save_discussion(self.current_record)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise DiscussionCancelled("用户已终止本轮讨论")

    async def cancel(self) -> None:
        self._cancel_event.set()
        for future in self._recovery_futures.values():
            if not future.done():
                future.cancel()
        if self.current_record:
            self.current_record.status = RunStatus.CANCELLED
            self._emit(
                "cancelled",
                "正在停止所有参与者",
                self.current_record.current_stage,
                status=RunStatus.CANCELLED,
            )
        await asyncio.gather(
            *(provider.stop_generation() for provider in self.registry.all()),
            return_exceptions=True,
        )
        self._save()

    async def resolve_provider_action(self, call_id: str, action: str) -> bool:
        if action not in {"retry", "manual", "skip"}:
            raise ValueError(f"未知恢复操作：{action}")
        future = self._recovery_futures.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(action)
        return True

    async def run(
        self,
        question: UserQuestion,
        provider_names: list[str],
        moderator_name: str,
        judge_names: list[str],
        *,
        rounds: int = 1,
        concurrency: int = 4,
        anonymous_review: bool = True,
        enable_revision: bool = True,
        multi_judge: bool = False,
    ) -> DiscussionRecord:
        unique_names = list(dict.fromkeys(provider_names))
        if len(unique_names) < 2:
            raise ValueError("至少选择两个 AI 参与讨论")
        for name in [*unique_names, moderator_name, *judge_names]:
            self.registry.get(name)
        if not judge_names:
            judge_names = [moderator_name]
        if not multi_judge:
            judge_names = judge_names[:1]

        self._cancel_event = asyncio.Event()
        self._concurrency = max(1, min(concurrency, 16))
        effective_rounds = max(1, min(rounds, 3))
        self.current_record = DiscussionRecord(
            status=RunStatus.RUNNING,
            current_stage=DiscussionStage.PREPARING,
            question=question,
            provider_names=unique_names,
            moderator_name=moderator_name,
            judge_names=judge_names,
            settings={
                "rounds": effective_rounds,
                "requested_rounds": rounds,
                "concurrency": self._concurrency,
                "anonymous_review": anonymous_review,
                "enable_revision": enable_revision,
                "multi_judge": multi_judge,
            },
        )
        self._save()
        self._emit("discussion_started", "圆桌讨论已开始", DiscussionStage.PREPARING)

        try:
            responses = await self._independent(question, unique_names)
            if len(responses) < 2:
                raise RuntimeError(
                    f"有效独立回答仅 {len(responses)} 份；至少需要 2 份，无法形成真实圆桌讨论"
                )

            current_candidates: dict[str, AIResponse | RevisedResponse] = {
                response.provider_name: response for response in responses
            }
            aliases = {name: f"方案 {chr(65 + index)}" for index, name in enumerate(unique_names)}
            all_reviews: list[ReviewComment] = []
            all_revisions: list[RevisedResponse] = []
            discussion_rounds: list[DiscussionRound] = []
            baseline_candidates: dict[str, AIResponse | RevisedResponse] = dict(
                current_candidates
            )
            baseline_scores: list[JudgeScore] = []
            previous_scores: list[JudgeScore] = []
            all_effectiveness: list[RoundEffectiveness] = []

            for round_number in range(1, effective_rounds + 1):
                self._check_cancelled()
                round_model = DiscussionRound(
                    round_number=round_number,
                    responses=responses if round_number == 1 else [],
                )
                reviews = await self._reviews(
                    question, unique_names, current_candidates, aliases, anonymous_review
                )
                round_model.reviews = reviews
                all_reviews.extend(reviews)

                if enable_revision:
                    revisions = await self._revisions(
                        question,
                        unique_names,
                        current_candidates,
                        reviews,
                        aliases,
                        round_number,
                    )
                    round_model.revisions = revisions
                    all_revisions.extend(revisions)
                    for revision in revisions:
                        current_candidates[revision.provider_name] = revision

                scored = await self._judge(
                    question,
                    judge_names,
                    current_candidates,
                    aliases,
                    moderator_name,
                    round_number=round_number,
                    baseline_candidates=(
                        baseline_candidates if round_number == 1 and enable_revision else None
                    ),
                )
                round_model.baseline_scores = [
                    item for item in scored if item.snapshot == "independent"
                ]
                round_model.scores = [
                    item for item in scored if item.snapshot != "independent"
                ]
                if round_number == 1:
                    baseline_scores = round_model.baseline_scores or round_model.scores
                comparison_scores = previous_scores or baseline_scores
                round_model.effectiveness = self._measure_effectiveness(
                    round_number,
                    comparison_scores,
                    round_model.scores,
                    {alias: name for name, alias in aliases.items()},
                )
                all_effectiveness.append(round_model.effectiveness)
                previous_scores = round_model.scores

                discussion_rounds.append(round_model)
                self.current_record.rounds = discussion_rounds
                self._save()

            scores = previous_scores

            synthesis = await self._synthesis(
                question,
                moderator_name,
                responses,
                all_reviews,
                all_revisions,
                scores,
                all_effectiveness,
            )
            self.current_record.final_synthesis = synthesis
            self.current_record.current_stage = DiscussionStage.COMPLETED
            self.current_record.status = RunStatus.SUCCEEDED
            self._save()
            self._emit(
                "discussion_completed",
                "圆桌讨论完成",
                DiscussionStage.COMPLETED,
                status=RunStatus.SUCCEEDED,
                discussion_id=self.current_record.id,
            )
            return self.current_record
        except (DiscussionCancelled, asyncio.CancelledError):
            self.current_record.status = RunStatus.CANCELLED
            self._save()
            self._emit(
                "discussion_cancelled",
                "本轮讨论已终止",
                self.current_record.current_stage,
                status=RunStatus.CANCELLED,
            )
            return self.current_record
        except Exception as exc:
            self.current_record.status = RunStatus.FAILED
            self.current_record.errors.append(
                ErrorRecord(
                    provider_name="orchestrator",
                    stage=self.current_record.current_stage,
                    status=RunStatus.FAILED,
                    error_message=str(exc),
                    exception_type=type(exc).__name__,
                    recoverable=False,
                    suggested_action="检查错误后重新开始讨论",
                )
            )
            self._save()
            self._emit(
                "discussion_failed",
                str(exc),
                self.current_record.current_stage,
                status=RunStatus.FAILED,
            )
            raise

    async def _call_provider(
        self, provider: AIProvider, stage: DiscussionStage, prompt: str, **context: Any
    ) -> ProviderCall:
        started = time.perf_counter()
        last_error: Exception | None = None
        last_error_message = ""
        call_id = str(uuid4())
        retries_remaining = provider.config.max_retries
        retry_count = 0
        recovery_used = False
        while True:
            self._check_cancelled()

            def manual_callback() -> None:
                self._emit(
                    "manual_input_required",
                    f"{provider.name} 需要人工发送并粘贴回答",
                    stage,
                    provider.name,
                    status=RunStatus.WAITING,
                    call_id=call_id,
                    prompt=prompt,
                    attempt=retry_count,
                )

            def progress_callback(text: str) -> None:
                self._emit(
                    "provider_progress",
                    f"{provider.name} 正在生成",
                    stage,
                    provider.name,
                    call_id=call_id,
                    transient=True,
                    raw=text,
                    elapsed_seconds=time.perf_counter() - started,
                )

            try:
                self._emit(
                    "provider_started",
                    f"{provider.name} 开始处理",
                    stage,
                    provider.name,
                    call_id=call_id,
                    attempt=retry_count,
                )
                raw = await asyncio.wait_for(
                    provider.ask(
                        prompt,
                        stage,
                        _manual_callback=manual_callback,
                        _progress_callback=progress_callback,
                        _cancel_check=self._check_cancelled,
                        **context,
                    ),
                    timeout=provider.config.timeout_seconds,
                )
                if not raw.strip():
                    raise EmptyResponseError("平台返回空回答")
                elapsed = time.perf_counter() - started
                self._emit(
                    "provider_completed",
                    f"{provider.name} 完成，用时 {elapsed:.2f}s",
                    stage,
                    provider.name,
                    status=RunStatus.SUCCEEDED,
                    call_id=call_id,
                    elapsed_seconds=elapsed,
                    raw=raw,
                )
                return ProviderCall(provider, call_id, raw, retry_count, elapsed)
            except (asyncio.CancelledError, DiscussionCancelled):
                raise
            except Exception as exc:
                last_error = exc
                last_error_message = (
                    f"超过 {provider.config.timeout_seconds:.0f} 秒仍未完成"
                    if isinstance(exc, asyncio.TimeoutError)
                    else str(exc).strip() or type(exc).__name__
                )
                provider.session.status = RunStatus.FAILED
                provider.session.error_message = last_error_message
                if retries_remaining > 0:
                    retries_remaining -= 1
                    retry_count += 1
                    if (
                        provider.config.kind == ProviderKind.WEB
                        and provider.config.mode == ProviderMode.AUTOMATIC
                        and provider.config.allow_manual_fallback
                    ):
                        provider.config.mode = ProviderMode.SEMI_AUTOMATIC
                    self._emit(
                        "provider_retry",
                        f"{provider.name} 失败，准备第 {retry_count} 次重试：{last_error_message}",
                        stage,
                        provider.name,
                        status=RunStatus.FAILED,
                        call_id=call_id,
                        attempt=retry_count,
                    )
                    await asyncio.sleep(min(0.25 * (2 ** (retry_count - 1)), 2))
                    if provider.config.kind == ProviderKind.WEB:
                        try:
                            await provider.reset_conversation()
                        except Exception:
                            pass
                    continue

                if (
                    self.interactive_recovery
                    and not recovery_used
                    and provider.config.allow_manual_fallback
                ):
                    recovery_used = True
                    action_future = asyncio.get_running_loop().create_future()
                    self._recovery_futures[call_id] = action_future
                    self._emit(
                        "provider_action_required",
                        f"{provider.name} 需要处理：{last_error_message}",
                        stage,
                        provider.name,
                        status=RunStatus.WAITING,
                        call_id=call_id,
                        error=last_error_message,
                        actions=["manual", "retry", "skip"],
                    )
                    try:
                        action = await action_future
                    finally:
                        self._recovery_futures.pop(call_id, None)
                    if action == "skip":
                        break
                    if action == "manual" and provider.config.kind == ProviderKind.WEB:
                        provider.config.mode = ProviderMode.SEMI_AUTOMATIC
                    retry_count += 1
                    continue
                break

        elapsed = time.perf_counter() - started
        error = ErrorRecord(
            provider_name=provider.name,
            stage=stage,
            status=RunStatus.FAILED,
            raw_content="",
            error_message=last_error_message,
            retry_count=retry_count,
            elapsed_seconds=elapsed,
            exception_type=type(last_error).__name__ if last_error else "UnknownError",
            recoverable=True,
        )
        self.current_record.errors.append(error)
        self._emit(
            "provider_failed",
            f"{provider.name} 失败，但其他 AI 将继续：{last_error_message}",
            stage,
            provider.name,
            status=RunStatus.FAILED,
            call_id=call_id,
            retry_count=retry_count,
        )
        self._save()
        return ProviderCall(
            provider,
            call_id=call_id,
            retry_count=retry_count,
            elapsed_seconds=elapsed,
            error=error,
        )

    async def _parallel(self, jobs: list[Callable[[], Coroutine[Any, Any, T]]]) -> list[T]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded(job: Callable[[], Coroutine[Any, Any, T]]) -> T:
            async with semaphore:
                return await job()

        return await asyncio.gather(*(guarded(job) for job in jobs))

    @staticmethod
    def _json(raw: str) -> dict[str, Any]:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
            if clean.endswith("```"):
                clean = clean[:-3].rstrip()

        def unwrap(value: Any) -> dict[str, Any] | None:
            for _ in range(3):
                if isinstance(value, dict):
                    return value
                if not isinstance(value, str):
                    return None
                try:
                    value = json.loads(value.strip())
                except (json.JSONDecodeError, TypeError):
                    return None
            return value if isinstance(value, dict) else None

        try:
            direct = unwrap(json.loads(clean))
            if direct is not None:
                return direct
        except (json.JSONDecodeError, TypeError):
            pass

        decoder = json.JSONDecoder()
        for index, character in enumerate(clean):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(clean[index:])
            except json.JSONDecodeError:
                continue
            embedded = unwrap(value)
            if embedded is not None:
                return embedded
        raise ValueError("回答中没有可解析的 JSON 对象")

    @staticmethod
    def _bounded(value: object, limit: int = 3200) -> str:
        clean = " ".join(str(value or "").split())
        return clean if len(clean) <= limit else clean[:limit] + "…"

    @staticmethod
    def _strings(value: object, *, count: int = 8, limit: int = 600) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        result: list[str] = []
        for item in value:
            clean = RoundtableEngine._bounded(item, limit)
            if clean and clean not in result:
                result.append(clean)
            if len(result) >= count:
                break
        return result

    async def _independent(
        self, question: UserQuestion, provider_names: list[str]
    ) -> list[AIResponse]:
        stage = DiscussionStage.INDEPENDENT
        self.current_record.current_stage = stage
        self._save()
        self._emit("stage_started", "第一阶段：独立回答", stage)
        jobs = []
        for index, name in enumerate(provider_names):
            perspective = PromptFactory.PERSPECTIVES[
                index % len(PromptFactory.PERSPECTIVES)
            ]
            prompt = PromptFactory.independent(question, perspective)
            jobs.append(
                lambda provider=self.registry.get(name), prompt=prompt: self._call_provider(
                    provider, stage, prompt
                )
            )
        calls = await self._parallel(jobs)
        responses: list[AIResponse] = []
        for index, call in enumerate(calls):
            if call.error:
                continue
            try:
                data = self._json(call.raw)
            except Exception:
                plain = self._bounded(call.raw)
                data = {"conclusion": plain, "recommendation": plain}
            array_keys = {
                "reasoning",
                "actions",
                "risks",
                "uncertainties",
                "key_assumptions",
            }
            responses.append(
                AIResponse(
                    provider_name=call.provider.name,
                    raw_content=self._bounded(call.raw, 20_000),
                    retry_count=call.retry_count,
                    elapsed_seconds=call.elapsed_seconds,
                    alias=f"方案 {chr(65 + index)}",
                    **{
                        key: self._strings(data.get(key))
                        if key in array_keys
                        else self._bounded(data.get(key), 3200)
                        for key in (
                            "understanding",
                            "conclusion",
                            "reasoning",
                            "actions",
                            "risks",
                            "uncertainties",
                            "recommendation",
                            "distinctive_contribution",
                            "key_assumptions",
                        )
                    },
                )
            )
        self._emit(
            "stage_barrier",
            f"已等待全部 {len(provider_names)} 个 AI 停止生成；完整回答 {len(responses)} 份，现在开始交叉讨论",
            stage,
            status=RunStatus.SUCCEEDED,
        )
        self._emit("stage_completed", f"独立回答完成：{len(responses)}/{len(provider_names)}", stage)
        return responses

    async def _reviews(
        self,
        question: UserQuestion,
        provider_names: list[str],
        candidates: dict[str, AIResponse | RevisedResponse],
        aliases: dict[str, str],
        anonymous: bool,
    ) -> list[ReviewComment]:
        stage = DiscussionStage.REVIEW
        self.current_record.current_stage = stage
        self._save()
        self._emit("stage_started", "第二阶段：匿名交叉评审", stage)
        jobs: list[
            Callable[
                [],
                Coroutine[Any, Any, tuple[ProviderCall, str, list[str]]],
            ]
        ] = []
        for reviewer_name in provider_names:
            if reviewer_name not in candidates:
                continue
            targets = [
                (aliases[name] if anonymous else name, candidate)
                for name, candidate in candidates.items()
                if name != reviewer_name
            ]
            target_aliases = [alias for alias, _ in targets]
            prompt = PromptFactory.review_batch(question, targets)

            async def job(
                reviewer_name: str = reviewer_name,
                target_aliases: list[str] = target_aliases,
                prompt: str = prompt,
            ) -> tuple[ProviderCall, str, list[str]]:
                call = await self._call_provider(
                    self.registry.get(reviewer_name),
                    stage,
                    prompt,
                    batch=True,
                    candidate_aliases=target_aliases,
                )
                return call, reviewer_name, target_aliases

            jobs.append(job)
        results = await self._parallel(jobs)
        reviews: list[ReviewComment] = []
        array_keys = (
            "strengths",
            "logical_gaps",
            "unverified_assumptions",
            "fact_conflicts",
            "risks",
            "improvements",
            "unique_contributions",
            "integration_proposals",
            "decisive_tests",
        )
        for call, reviewer_name, target_aliases in results:
            if call.error:
                continue
            try:
                data = self._json(call.raw)
                review_items = data.get("reviews", [])
                if not isinstance(review_items, list):
                    raise ValueError("reviews 必须是数组")
            except Exception:
                review_items = [
                    {
                        "target_alias": alias,
                        "improvements": [self._bounded(call.raw, 1200)],
                    }
                    for alias in target_aliases
                ]
            by_alias = {
                str(item.get("target_alias", "")): item
                for item in review_items
                if isinstance(item, dict)
            }
            for target_alias in target_aliases:
                item = by_alias.get(
                    target_alias,
                    {
                        "target_alias": target_alias,
                        "improvements": ["该方案缺少可解析的独立评审项"],
                    },
                )
                reviews.append(
                    ReviewComment(
                        provider_name=reviewer_name,
                        reviewer_alias="匿名评审者" if anonymous else reviewer_name,
                        target_alias=target_alias,
                        raw_content=self._bounded(
                            json.dumps(item, ensure_ascii=False), 8_000
                        ),
                        retry_count=call.retry_count,
                        elapsed_seconds=call.elapsed_seconds,
                        **{key: self._strings(item.get(key), count=6) for key in array_keys},
                    )
                )
        self._emit("stage_completed", f"交叉评审完成：{len(reviews)} 条", stage)
        return reviews

    async def _revisions(
        self,
        question: UserQuestion,
        provider_names: list[str],
        candidates: dict[str, AIResponse | RevisedResponse],
        reviews: list[ReviewComment],
        aliases: dict[str, str],
        round_number: int,
    ) -> list[RevisedResponse]:
        stage = DiscussionStage.REVISION
        self.current_record.current_stage = stage
        self._save()
        self._emit("stage_started", f"第三阶段：第 {round_number} 轮修订", stage)
        jobs: list[Callable[[], Coroutine[Any, Any, tuple[ProviderCall, str]]]] = []
        for name in provider_names:
            own = candidates.get(name)
            if own is None:
                continue
            own_reviews = [review for review in reviews if review.target_alias == aliases[name]]
            peers = [
                (aliases[peer_name], candidate)
                for peer_name, candidate in candidates.items()
                if peer_name != name
            ]
            integration_brief = {
                "integration_proposals": list(
                    dict.fromkeys(
                        item
                        for review in reviews
                        for item in review.integration_proposals
                    )
                )[:12],
                "decisive_tests": list(
                    dict.fromkeys(
                        item for review in reviews for item in review.decisive_tests
                    )
                )[:10],
            }
            prompt = PromptFactory.revision(
                question,
                own,
                own_reviews,
                round_number,
                peers=peers,
                integration_brief=integration_brief,
            )

            async def job(name: str = name, prompt: str = prompt) -> tuple[ProviderCall, str]:
                return await self._call_provider(self.registry.get(name), stage, prompt), name

            jobs.append(job)
        results = await self._parallel(jobs)
        revisions: list[RevisedResponse] = []
        for call, name in results:
            if call.error:
                continue
            try:
                data = self._json(call.raw)
            except Exception:
                data = {"recommendation": self._bounded(call.raw)}
            array_keys = (
                "kept_points",
                "changed_points",
                "change_reasons",
                "borrowed_ideas",
                "resolved_conflicts",
                "synergy_gains",
                "uncertainties",
            )
            revisions.append(
                RevisedResponse(
                    provider_name=name,
                    original_response_id=candidates[name].id,
                    alias=aliases[name],
                    raw_content=self._bounded(call.raw, 20_000),
                    retry_count=call.retry_count,
                    elapsed_seconds=call.elapsed_seconds,
                    recommendation=self._bounded(data.get("recommendation", ""), 4000),
                    **{key: self._strings(data.get(key), count=8) for key in array_keys},
                )
            )
        self._emit("stage_completed", f"方案修订完成：{len(revisions)} 份", stage)
        return revisions

    @classmethod
    def _score_items(cls, raw: str) -> list[dict[str, Any]]:
        """Parse score objects even when a model adds extra braces around array items."""
        try:
            data = cls._json(raw)
            items = data.get("scores", [])
            if isinstance(items, list) and items:
                return [item for item in items if isinstance(item, dict)]
        except (ValueError, TypeError):
            pass

        decoder = json.JSONDecoder()
        matches = list(re.finditer(r'"candidate_alias"\s*:', raw))
        recovered: list[dict[str, Any]] = []

        def field_value(segment: str, field: str) -> Any:
            match = re.search(rf'"{re.escape(field)}"\s*:', segment)
            if not match:
                return None
            remainder = segment[match.end() :].lstrip()
            try:
                value, _ = decoder.raw_decode(remainder)
                return value
            except json.JSONDecodeError:
                return None

        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            segment = raw[match.start() : end]
            alias = field_value(segment, "candidate_alias")
            dimensions = field_value(segment, "dimensions")
            if not isinstance(alias, str) or not isinstance(dimensions, dict):
                continue
            recovered.append(
                {
                    "candidate_alias": alias,
                    "dimensions": dimensions,
                    "rank": field_value(segment, "rank") or 0,
                    "verdict": field_value(segment, "verdict") or "",
                    "reason": field_value(segment, "reason") or "未提供理由",
                    "comparative_reason": field_value(segment, "comparative_reason") or "",
                    "fatal_flaw": field_value(segment, "fatal_flaw") or "",
                    "evidence": field_value(segment, "evidence") or [],
                    "score_confidence": field_value(segment, "score_confidence") or "medium",
                }
            )
        return recovered

    async def _judge(
        self,
        question: UserQuestion,
        judge_names: list[str],
        candidates: dict[str, AIResponse | RevisedResponse],
        aliases: dict[str, str],
        moderator_name: str,
        *,
        round_number: int = 1,
        baseline_candidates: dict[str, AIResponse | RevisedResponse] | None = None,
    ) -> list[JudgeScore]:
        stage = DiscussionStage.JUDGE
        self.current_record.current_stage = stage
        self._save()
        self._emit(
            "stage_started",
            f"第四阶段：第 {round_number} 轮效果评分",
            stage,
        )

        candidate_items: list[tuple[str, AIResponse | RevisedResponse]] = []
        alias_meta: dict[str, tuple[str, str, AIResponse | RevisedResponse]] = {}
        if baseline_candidates:
            for name, candidate in baseline_candidates.items():
                version_alias = f"{aliases[name]}·讨论前"
                candidate_items.append((version_alias, candidate))
                alias_meta[version_alias] = (aliases[name], "independent", candidate)
        snapshot = f"round_{round_number}"
        for name, candidate in candidates.items():
            version_alias = f"{aliases[name]}·第{round_number}轮"
            candidate_items.append((version_alias, candidate))
            alias_meta[version_alias] = (aliases[name], snapshot, candidate)

        selected_judges = list(dict.fromkeys(judge_names))
        conflicted_judges = [name for name in selected_judges if name in candidates]
        evaluator_names = selected_judges
        if conflicted_judges:
            # A participating judge can often recognise its own answer despite aliases.
            # Use every successful participant as a juror and hard-exclude each juror's
            # own candidate. This gives every candidate the same N-1 independent ballots.
            evaluator_names = list(dict.fromkeys([*selected_judges, *candidates]))
            self.current_record.settings["judge_conflict_policy"] = "leave_one_out_panel"
            self.current_record.settings["effective_judge_names"] = evaluator_names
            self._save()
            self._emit(
                "judge_conflict_free_panel",
                "参赛裁判已切换为无利益冲突评审团；每位评审者均看不到自己的方案",
                stage,
                status=RunStatus.WAITING,
                selected_judges=selected_judges,
                conflicted_judges=conflicted_judges,
                effective_judges=evaluator_names,
            )

        author_by_base_alias = {
            alias: name for name, alias in aliases.items() if name in candidates
        }

        def eligible_items(
            judge_name: str,
            items: list[tuple[str, AIResponse | RevisedResponse]],
        ) -> list[tuple[str, AIResponse | RevisedResponse]]:
            own_alias = aliases.get(judge_name, "") if judge_name in candidates else ""
            return [
                item
                for item in items
                if not own_alias or alias_meta[item[0]][0] != own_alias
            ]

        async def run_judge(
            judge_name: str,
            requested_items: list[tuple[str, AIResponse | RevisedResponse]] | None = None,
        ) -> tuple[ProviderCall, str, set[str]]:
            items = eligible_items(judge_name, requested_items or candidate_items)
            allowed_aliases = {alias for alias, _ in items}
            prompt = PromptFactory.judge_batch(question, items)
            call = await self._call_provider(
                self.registry.get(judge_name),
                stage,
                prompt,
                batch=True,
                candidate_aliases=[alias for alias, _ in items],
            )
            return call, judge_name, allowed_aliases

        def parse_call(
            call: ProviderCall,
            judge_name: str,
            allowed_aliases: set[str],
        ) -> list[JudgeScore]:
            parsed: list[JudgeScore] = []
            seen_aliases: set[str] = set()
            for item in self._score_items(call.raw):
                candidate_alias = str(item.get("candidate_alias", ""))
                # Do not trust model output to honour the exclusion. An attempted
                # self-score or duplicate ballot is discarded at the parser boundary.
                if candidate_alias not in allowed_aliases or candidate_alias in seen_aliases:
                    continue
                meta = alias_meta.get(candidate_alias)
                if meta is None:
                    continue
                base_alias, item_snapshot, candidate = meta
                try:
                    dimensions = JudgeDimensions.model_validate(item.get("dimensions", {}))
                except ValidationError:
                    continue
                confidence_value = str(item.get("score_confidence", "medium"))
                try:
                    confidence = ConfidenceLevel(confidence_value)
                except ValueError:
                    confidence = ConfidenceLevel.MEDIUM
                model_rank = item.get("rank", 0)
                parsed.append(
                    JudgeScore(
                        provider_name=judge_name,
                        judge_name=judge_name,
                        candidate_alias=candidate_alias,
                        base_alias=base_alias,
                        snapshot=item_snapshot,
                        candidate_response_id=candidate.id,
                        dimensions=dimensions,
                        weighted_total=dimensions.weighted_total,
                        rank=int(model_rank) if str(model_rank).isdigit() else 0,
                        verdict=self._bounded(item.get("verdict", ""), 40),
                        fatal_flaw=self._bounded(item.get("fatal_flaw", ""), 500),
                        reason=self._bounded(item.get("reason", "未提供理由"), 800),
                        comparative_reason=self._bounded(
                            item.get("comparative_reason", ""), 800
                        ),
                        evidence=self._strings(item.get("evidence"), count=2, limit=500),
                        score_confidence=confidence,
                        raw_content=self._bounded(
                            json.dumps(item, ensure_ascii=False), 5_000
                        ),
                        retry_count=call.retry_count,
                        elapsed_seconds=call.elapsed_seconds,
                    )
                )
                seen_aliases.add(candidate_alias)
            return parsed

        results = await self._parallel(
            [lambda name=name: run_judge(name) for name in evaluator_names]
        )
        scores: list[JudgeScore] = []
        for call, judge_name, allowed_aliases in results:
            if not call.error:
                scores.extend(parse_call(call, judge_name, allowed_aliases))

        expected = set(alias_meta)
        scored_aliases = {score.candidate_alias for score in scores}
        missing = expected - scored_aliases
        if missing:
            self._emit(
                "judge_fallback",
                f"评审团结果缺少 {len(missing)} 个候选，改由无利益冲突的评审者补评",
                stage,
                status=RunStatus.WAITING,
                missing_aliases=sorted(missing),
            )
            successful_judges = list(dict.fromkeys(score.judge_name for score in scores))
            fallback_pool = list(
                dict.fromkeys(
                    [moderator_name, *successful_judges, *evaluator_names, *candidates]
                )
            )
            fallback_groups: dict[str, list[str]] = {}
            for candidate_alias in sorted(missing):
                base_alias = alias_meta[candidate_alias][0]
                author = author_by_base_alias.get(base_alias, "")
                fallback_name = next(
                    (name for name in fallback_pool if name != author),
                    "",
                )
                if fallback_name:
                    fallback_groups.setdefault(fallback_name, []).append(candidate_alias)

            fallback_jobs = []
            for fallback_name, aliases_to_score in fallback_groups.items():
                missing_items = [
                    item for item in candidate_items if item[0] in aliases_to_score
                ]
                fallback_jobs.append(
                    lambda name=fallback_name, items=missing_items: run_judge(name, items)
                )
            for fallback_call, fallback_name, allowed_aliases in await self._parallel(
                fallback_jobs
            ):
                if not fallback_call.error:
                    scores.extend(
                        parse_call(fallback_call, fallback_name, allowed_aliases)
                    )

        remaining = expected - {score.candidate_alias for score in scores}
        if remaining:
            self._emit(
                "judge_incomplete",
                f"仍有 {len(remaining)} 个候选缺少有效评分；不再伪造统一中性分",
                stage,
                status=RunStatus.FAILED,
                missing_aliases=sorted(remaining),
            )

        if conflicted_judges and not remaining:
            ballots_by_candidate = {
                candidate_alias: [
                    score for score in scores if score.candidate_alias == candidate_alias
                ]
                for candidate_alias in expected
            }
            ballots_per_candidate = min(
                len(ballots) for ballots in ballots_by_candidate.values()
            )
            evaluator_priority = {
                name: index for index, name in enumerate(evaluator_names)
            }
            balanced_scores: list[JudgeScore] = []
            discarded_ballots = 0
            for candidate_alias in sorted(ballots_by_candidate):
                ballots = sorted(
                    ballots_by_candidate[candidate_alias],
                    key=lambda score: (
                        evaluator_priority.get(score.judge_name, len(evaluator_priority)),
                        score.judge_name,
                    ),
                )
                balanced_scores.extend(ballots[:ballots_per_candidate])
                discarded_ballots += max(0, len(ballots) - ballots_per_candidate)
            scores = balanced_scores
            panel_counts = self.current_record.settings.setdefault(
                "judge_ballots_per_candidate_by_round", {}
            )
            panel_counts[str(round_number)] = ballots_per_candidate
            self._save()
            self._emit(
                "judge_panel_balanced",
                f"无利益冲突评分已按每个候选 {ballots_per_candidate} 票对齐",
                stage,
                status=(
                    RunStatus.WAITING if discarded_ballots else RunStatus.SUCCEEDED
                ),
                ballots_per_candidate=ballots_per_candidate,
                discarded_ballots=discarded_ballots,
            )

        groups: dict[tuple[str, str], list[JudgeScore]] = {}
        for score in scores:
            groups.setdefault((score.judge_name, score.snapshot), []).append(score)
        for (_, group_snapshot), group in groups.items():
            for score in group:
                gated = min(
                    score.dimensions.correctness,
                    score.dimensions.constraint_alignment,
                    score.dimensions.risk_control,
                ) < 4
                if gated:
                    score.weighted_total = min(score.weighted_total, 4.9)
                    score.verdict = "淘汰"
                    if not score.fatal_flaw:
                        score.fatal_flaw = "正确性、约束匹配或风险控制未通过硬门槛"
            group.sort(
                key=lambda item: (
                    item.verdict == "淘汰",
                    -item.weighted_total,
                    item.rank or 999,
                    -item.dimensions.correctness,
                    -item.dimensions.constraint_alignment,
                    -item.dimensions.risk_control,
                    item.candidate_alias,
                )
            )
            for rank, score in enumerate(group, 1):
                score.rank = rank
                if score.verdict != "淘汰":
                    score.verdict = (
                        "推荐" if rank == 1 else "淘汰" if rank == len(group) else "备选"
                    )
            spread = (
                max(item.weighted_total for item in group)
                - min(item.weighted_total for item in group)
                if len(group) > 1
                else 0
            )
            if len(group) > 1 and spread < 0.75:
                self._emit(
                    "judge_low_discrimination",
                    f"{group_snapshot} 评分区分度仅 {spread:.2f} 分，已强制排序并标记复核",
                    stage,
                    status=RunStatus.WAITING,
                    spread=round(spread, 2),
                )

        self._emit("stage_completed", f"效果评分完成：{len(scores)} 份", stage)
        return scores

    @staticmethod
    def _measure_effectiveness(
        round_number: int,
        before_scores: list[JudgeScore],
        after_scores: list[JudgeScore],
        alias_to_provider: dict[str, str],
    ) -> RoundEffectiveness:
        dimension_names = tuple(JudgeDimensions.model_fields)

        def grouped(items: list[JudgeScore]) -> dict[str, list[JudgeScore]]:
            result: dict[str, list[JudgeScore]] = {}
            for item in items:
                result.setdefault(item.base_alias or item.candidate_alias, []).append(item)
            return result

        def dimension_means(items: list[JudgeScore]) -> dict[str, float]:
            return {
                key: round(mean(float(getattr(item.dimensions, key)) for item in items), 2)
                for key in dimension_names
            }

        before_by_alias = grouped(before_scores)
        after_by_alias = grouped(after_scores)
        provider_results: list[ProviderEffectiveness] = []
        warnings: list[str] = []
        for alias in sorted(set(before_by_alias) & set(after_by_alias)):
            before_group = before_by_alias[alias]
            after_group = after_by_alias[alias]
            before_dimensions = dimension_means(before_group)
            after_dimensions = dimension_means(after_group)
            deltas = {
                key: round(after_dimensions[key] - before_dimensions[key], 2)
                for key in dimension_names
            }
            before_total = round(
                mean(item.weighted_total or item.dimensions.weighted_total for item in before_group),
                2,
            )
            after_total = round(
                mean(item.weighted_total or item.dimensions.weighted_total for item in after_group),
                2,
            )
            provider_results.append(
                ProviderEffectiveness(
                    provider_name=alias_to_provider.get(alias, alias),
                    candidate_alias=alias,
                    before_snapshot=before_group[0].snapshot,
                    after_snapshot=after_group[0].snapshot,
                    before_score=before_total,
                    after_score=after_total,
                    overall_delta=round(after_total - before_total, 2),
                    dimension_deltas=deltas,
                    improved_dimensions=[key for key, value in deltas.items() if value >= 0.25],
                    regressed_dimensions=[key for key, value in deltas.items() if value <= -0.25],
                )
            )
        missing_aliases = (set(before_by_alias) | set(after_by_alias)) - {
            item.candidate_alias for item in provider_results
        }
        if missing_aliases:
            warnings.append(
                "以下方案缺少前后配对评分：" + "、".join(sorted(missing_aliases))
            )
        average_deltas = {
            key: round(mean(item.dimension_deltas[key] for item in provider_results), 2)
            for key in dimension_names
        } if provider_results else {}
        overall_delta = round(
            mean(item.overall_delta for item in provider_results), 2
        ) if provider_results else 0.0
        improved_count = sum(item.overall_delta >= 0.25 for item in provider_results)
        regressed_count = sum(item.overall_delta <= -0.25 for item in provider_results)
        verdict = (
            "有效提升"
            if overall_delta >= 0.25 and improved_count > regressed_count
            else "出现退步"
            if overall_delta <= -0.25 or regressed_count > improved_count
            else "基本持平"
            if provider_results
            else "证据不足"
        )
        basis = (
            "独立答案 → 第1轮修订"
            if round_number == 1
            else f"第{round_number - 1}轮修订 → 第{round_number}轮修订"
        )
        return RoundEffectiveness(
            round_number=round_number,
            comparison_basis=basis,
            provider_results=provider_results,
            average_dimension_deltas=average_deltas,
            average_overall_delta=overall_delta,
            improved_provider_count=improved_count,
            regressed_provider_count=regressed_count,
            verdict=verdict,
            warnings=warnings,
        )

    async def _synthesis(
        self,
        question: UserQuestion,
        moderator_name: str,
        responses: list[AIResponse],
        reviews: list[ReviewComment],
        revisions: list[RevisedResponse],
        scores: list[JudgeScore],
        effectiveness: list[RoundEffectiveness],
    ) -> FinalSynthesis:
        stage = DiscussionStage.SYNTHESIS
        self.current_record.current_stage = stage
        self._save()
        self._emit("stage_started", "第五阶段：主持人综合", stage)
        prompt = PromptFactory.synthesis(
            question, responses, reviews, revisions, scores, effectiveness
        )

        latest_candidates: dict[str, AIResponse | RevisedResponse] = {
            item.provider_name: item for item in responses
        }
        latest_candidates.update({item.provider_name: item for item in revisions})
        judge_names = set(self.current_record.judge_names if self.current_record else [])
        online_backups = [
            name
            for name, item in sorted(
                latest_candidates.items(),
                key=lambda pair: (
                    pair[0] in judge_names,
                    pair[1].elapsed_seconds <= 0,
                    pair[1].elapsed_seconds,
                    pair[0],
                ),
            )
            if name != moderator_name
            and self.registry.get(name).config.kind == ProviderKind.WEB
        ]

        actual_moderator = moderator_name
        preferred_provider = self.registry.get(moderator_name)
        fallback_notice = ""
        call: ProviderCall

        if (
            preferred_provider.config.kind == ProviderKind.WEB
            and moderator_name not in latest_candidates
            and online_backups
        ):
            actual_moderator = online_backups[0]
            fallback_notice = (
                f"原主持人 {moderator_name} 未产生有效方案，已由 {actual_moderator} 在线接力综合"
            )
            self._emit(
                "moderator_fallback",
                fallback_notice,
                stage,
                actual_moderator,
                status=RunStatus.RUNNING,
                from_provider=moderator_name,
                to_provider=actual_moderator,
                reason="missing_valid_candidate",
            )
            call = await self._call_provider(
                self.registry.get(actual_moderator), stage, prompt
            )
        else:
            call = await self._call_provider(preferred_provider, stage, prompt)
            if call.error and online_backups:
                actual_moderator = online_backups[0]
                fallback_notice = (
                    f"原主持人 {moderator_name} 综合失败，已由 {actual_moderator} 在线接力综合"
                )
                self._emit(
                    "moderator_fallback",
                    fallback_notice,
                    stage,
                    actual_moderator,
                    status=RunStatus.RUNNING,
                    from_provider=moderator_name,
                    to_provider=actual_moderator,
                    reason=call.error.error_message,
                )
                call = await self._call_provider(
                    self.registry.get(actual_moderator), stage, prompt
                )

        if call.error:
            result = self._fallback_synthesis(
                moderator_name, responses, revisions, scores, effectiveness, call.error
            )
            self._emit(
                "stage_completed",
                "在线主持综合失败，已生成低可信度本地降级结果",
                stage,
                status=RunStatus.SUCCEEDED,
            )
            return result
        try:
            data = self._json(call.raw)
            confidence = ConfidenceLevel(data.get("confidence", "medium"))
            if fallback_notice:
                confidence = {
                    ConfidenceLevel.HIGH: ConfidenceLevel.MEDIUM,
                    ConfidenceLevel.MEDIUM: ConfidenceLevel.LOW,
                    ConfidenceLevel.LOW: ConfidenceLevel.LOW,
                }[confidence]
            contributions_data = data.get("contributions", {})
            contributions = (
                {
                    self._bounded(name, 80): self._bounded(value, 800)
                    for name, value in contributions_data.items()
                    if self._bounded(name, 80) and self._bounded(value, 800)
                }
                if isinstance(contributions_data, dict)
                else {}
            )
            def list_field(key: str) -> list[str]:
                return self._strings(data.get(key), count=14, limit=900)
            decision_scores = self._decision_scores(scores)
            system_ranking = self._candidate_ranking(scores)
            valid_aliases = set(decision_scores)
            requested_ranking = [
                self._bounded(item, 100)
                for item in data.get("candidate_ranking", [])
                if isinstance(item, str) and self._bounded(item, 100) in valid_aliases
            ] if isinstance(data.get("candidate_ranking"), list) else []
            candidate_ranking = list(
                dict.fromkeys([*requested_ranking, *system_ranking])
            )
            top_alias = system_ranking[0] if system_ranking else ""
            recommended_candidate = self._bounded(
                data.get("recommended_candidate", ""), 100
            )
            selection_rationale = list_field("selection_rationale")
            score_warnings = self._score_warnings(scores)
            expected_aliases = {
                getattr(item, "alias", "") for item in latest_candidates.values()
            } - {""}
            missing_score_aliases = expected_aliases - valid_aliases
            if missing_score_aliases:
                score_warnings.append(
                    "以下方案缺少有效评分且未以 5 分伪造："
                    + "、".join(sorted(missing_score_aliases))
                )
            final_confidence = confidence if scores else ConfidenceLevel.LOW
            if missing_score_aliases:
                final_confidence = ConfidenceLevel.LOW
            elif score_warnings and final_confidence == ConfidenceLevel.HIGH:
                final_confidence = ConfidenceLevel.MEDIUM
            if recommended_candidate not in valid_aliases:
                recommended_candidate = top_alias
                if top_alias:
                    score_warnings.append("主持人未给出有效主方案，系统采用裁判第一名")
            elif (
                top_alias
                and recommended_candidate != top_alias
                and len(selection_rationale) < 2
            ):
                recommended_candidate = top_alias
                score_warnings.append(
                    "主持人缺少至少两条可核验的新证据，未允许其偏离裁判第一名"
                )
            round_summary = [
                f"第{item.round_number}轮：{item.verdict}，平均决策分变化 {item.average_overall_delta:+.2f}"
                for item in effectiveness
            ]
            cumulative_deltas = {
                key: round(
                    sum(item.average_dimension_deltas.get(key, 0.0) for item in effectiveness),
                    2,
                )
                for key in JudgeDimensions.model_fields
            }
            result = FinalSynthesis(
                provider_name=actual_moderator,
                raw_content=self._bounded(call.raw, 20_000),
                retry_count=call.retry_count,
                elapsed_seconds=call.elapsed_seconds,
                recommendation=self._bounded(
                    data.get("recommendation", call.raw), 5000
                ),
                reasons=list_field("reasons"),
                execution_steps=list_field("execution_steps"),
                rejected_options=list_field("rejected_options"),
                unresolved_questions=list_field("unresolved_questions"),
                user_confirmations=list_field("user_confirmations"),
                contributions=contributions,
                consensus=list_field("consensus"),
                disagreements=list_field("disagreements"),
                synergy_gains=list_field("synergy_gains"),
                decisive_tradeoffs=list_field("decisive_tradeoffs"),
                validation_plan=list_field("validation_plan"),
                risks=list_field("risks")
                + (["裁判评分不可用，最终结论未经过结构化评分"] if not scores else [])
                + score_warnings
                + ([fallback_notice] if fallback_notice else []),
                score_averages=self._score_averages(scores),
                decision_scores=decision_scores,
                recommended_candidate=recommended_candidate,
                candidate_ranking=candidate_ranking,
                selection_rationale=selection_rationale,
                minority_report=list_field("minority_report"),
                score_warnings=score_warnings,
                round_effectiveness_summary=round_summary,
                cumulative_dimension_deltas=cumulative_deltas,
                confidence=final_confidence,
            )
        except (ValueError, ValidationError, TypeError):
            plain = self._bounded(call.raw, 4000)
            result = FinalSynthesis(
                provider_name=actual_moderator,
                raw_content=self._bounded(call.raw, 20_000),
                recommendation=plain,
                reasons=["主持人返回了非结构化内容，已按原文保留"]
                + ([fallback_notice] if fallback_notice else []),
                unresolved_questions=["结构化字段缺失，需要用户复核"],
                risks=([fallback_notice] if fallback_notice else []),
                confidence=ConfidenceLevel.LOW,
                score_averages=self._score_averages(scores),
                decision_scores=self._decision_scores(scores),
                candidate_ranking=self._candidate_ranking(scores),
                recommended_candidate=(self._candidate_ranking(scores) or [""])[0],
                score_warnings=self._score_warnings(scores),
                round_effectiveness_summary=[
                    f"第{item.round_number}轮：{item.verdict}，平均决策分变化 {item.average_overall_delta:+.2f}"
                    for item in effectiveness
                ],
            )
        completion = (
            f"最终综合完成（{actual_moderator} 在线接力）"
            if fallback_notice
            else "最终综合完成"
        )
        self._emit("stage_completed", completion, stage)
        return result

    @staticmethod
    def _score_averages(scores: list[JudgeScore]) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for score in scores:
            grouped.setdefault(score.base_alias or score.candidate_alias, []).append(
                score.dimensions.average
            )
        return {alias: round(mean(values), 2) for alias, values in grouped.items()}

    @staticmethod
    def _decision_scores(scores: list[JudgeScore]) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for score in scores:
            grouped.setdefault(score.base_alias or score.candidate_alias, []).append(
                score.weighted_total or score.dimensions.weighted_total
            )
        return {alias: round(mean(values), 2) for alias, values in grouped.items()}

    @staticmethod
    def _candidate_ranking(scores: list[JudgeScore]) -> list[str]:
        decisions = RoundtableEngine._decision_scores(scores)
        rank_means: dict[str, list[int]] = {}
        for score in scores:
            rank_means.setdefault(score.base_alias or score.candidate_alias, []).append(
                score.rank or 999
            )
        return sorted(
            decisions,
            key=lambda alias: (
                mean(rank_means.get(alias, [999])),
                -decisions[alias],
                alias,
            ),
        )

    @staticmethod
    def _score_warnings(scores: list[JudgeScore]) -> list[str]:
        decisions = RoundtableEngine._decision_scores(scores)
        warnings: list[str] = []
        if len(decisions) > 1:
            spread = max(decisions.values()) - min(decisions.values())
            if spread < 0.75:
                warnings.append(f"裁判评分区分度偏低（最高与最低仅差 {spread:.2f} 分）")
        if any(score.score_confidence == ConfidenceLevel.LOW for score in scores):
            warnings.append("部分评分可信度较低，需要人工复核")
        return warnings

    @staticmethod
    def _fallback_synthesis(
        moderator_name: str,
        responses: list[AIResponse],
        revisions: list[RevisedResponse],
        scores: list[JudgeScore],
        effectiveness: list[RoundEffectiveness],
        error: ErrorRecord,
    ) -> FinalSynthesis:
        candidates = revisions or responses
        ranking = RoundtableEngine._candidate_ranking(scores)
        top_alias = ranking[0] if ranking else ""
        selected = next(
            (item for item in candidates if getattr(item, "alias", "") == top_alias),
            candidates[0] if candidates else None,
        )
        recommendation = selected.recommendation if selected else "没有足够材料生成综合答案。"
        return FinalSynthesis(
            provider_name="本地降级综合器",
            status=RunStatus.SUCCEEDED,
            recommendation=recommendation,
            reasons=["主持人适配器失败，本地降级选择裁判排名第一的完整方案，不再平均拼接"],
            unresolved_questions=[f"主持人 {moderator_name} 失败：{error.error_message}"],
            risks=["降级综合未进行语义去重或事实核验"],
            synergy_gains=["本地降级仅汇总已有建议，未验证是否形成真实协同增益"],
            validation_plan=["恢复在线主持后重新进行语义去重、冲突裁决和事实核验"],
            confidence=ConfidenceLevel.LOW,
            contributions={item.provider_name: item.recommendation for item in candidates},
            score_averages=RoundtableEngine._score_averages(scores),
            decision_scores=RoundtableEngine._decision_scores(scores),
            recommended_candidate=top_alias,
            candidate_ranking=ranking,
            selection_rationale=["按加权决策分、硬门槛和强制排名选择第一名"],
            score_warnings=RoundtableEngine._score_warnings(scores),
            round_effectiveness_summary=[
                f"第{item.round_number}轮：{item.verdict}，平均决策分变化 {item.average_overall_delta:+.2f}"
                for item in effectiveness
            ],
        )
