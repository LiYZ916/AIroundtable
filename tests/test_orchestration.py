from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.core.enums import DiscussionStage, ProviderKind, ProviderMode, RunStatus
from app.models import ProviderConfig, UserQuestion
from app.orchestration import RoundtableEngine
from app.providers import MockAIProvider, ProviderRegistry, build_default_registry


class CountingMockProvider(MockAIProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    async def ask(self, message, stage, **runtime_context):
        self.calls.append(stage)
        return await super().ask(message, stage, **runtime_context)


class ProgressMockProvider(MockAIProvider):
    async def wait_for_response(self):
        callback = self._runtime_context.get("_progress_callback")
        if callable(callback):
            callback("partial response")
        return await super().wait_for_response()


class SelfPromotingMockProvider(CountingMockProvider):
    """Simulate a judge that tries to smuggle its excluded solution into its ballot."""

    async def wait_for_response(self):
        raw = await super().wait_for_response()
        if self._current_stage != DiscussionStage.JUDGE:
            return raw
        own_alias = str(self.config.metadata.get("own_alias", ""))
        data = json.loads(raw)
        scores = data.get("scores", [])
        if not own_alias or not scores:
            return raw
        template = scores[0]
        suffixes = {
            candidate_alias.split("·", 1)[1]
            for candidate_alias in self._runtime_context.get("candidate_aliases", [])
            if "·" in candidate_alias
        }
        for suffix in suffixes:
            spoofed = dict(template)
            spoofed["candidate_alias"] = f"{own_alias}·{suffix}"
            spoofed["rank"] = 1
            spoofed["dimensions"] = {
                key: 10 for key in template.get("dimensions", {})
            }
            spoofed["reason"] = "试图给自己的方案满分"
            scores.append(spoofed)
        return json.dumps(data, ensure_ascii=False)


class FailOnceMockProvider(MockAIProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_once = False

    async def send_message(self, message):
        if self._current_stage == DiscussionStage.INDEPENDENT and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("first call failed")
        await super().send_message(message)


class AlwaysFailMockProvider(MockAIProvider):
    async def send_message(self, message):
        raise RuntimeError("automatic web failure")


class AlwaysTimeoutMockProvider(MockAIProvider):
    async def ask(self, message, stage, **runtime_context):
        raise asyncio.TimeoutError


def test_complete_roundtable_flow() -> None:
    async def scenario():
        registry = build_default_registry()
        names = [provider.name for provider in registry.enabled()]
        record = await RoundtableEngine(registry).run(
            UserQuestion(question="如何设计一个低风险试点？"),
            names,
            moderator_name=names[0],
            judge_names=[names[1]],
        )
        return record

    record = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    assert len(record.rounds) == 1
    assert len(record.rounds[0].responses) == 3
    assert len(record.rounds[0].reviews) == 6
    for provider_name in record.provider_names:
        targets = {
            review.target_alias
            for review in record.rounds[0].reviews
            if review.provider_name == provider_name
        }
        assert len(targets) == 2
    assert len(record.rounds[0].revisions) == 3
    assert len(record.rounds[0].scores) == 6
    assert len(record.rounds[0].baseline_scores) == 6
    assert record.rounds[0].effectiveness is not None
    assert record.rounds[0].effectiveness.average_overall_delta > 0
    assert record.final_synthesis is not None
    assert record.final_synthesis.execution_steps
    assert record.final_synthesis.synergy_gains
    assert record.final_synthesis.validation_plan
    assert all(item.borrowed_ideas for item in record.rounds[0].revisions)
    assert all(item.synergy_gains for item in record.rounds[0].revisions)
    assert record.final_synthesis.recommended_candidate
    assert record.final_synthesis.candidate_ranking[0] == record.final_synthesis.recommended_candidate
    assert len(set(record.final_synthesis.decision_scores.values())) > 1


def test_four_provider_conflict_free_protocol_uses_seventeen_calls() -> None:
    providers = []
    for name, role in zip(
        ("GPT", "Kimi", "元宝", "豆包"),
        ("analyst", "skeptic", "pragmatist", "systems"),
    ):
        provider_class = SelfPromotingMockProvider if name == "Kimi" else CountingMockProvider
        providers.append(
            provider_class(
                ProviderConfig(
                    name=name,
                    kind=ProviderKind.MOCK,
                    mode=ProviderMode.MOCK,
                    metadata={"own_alias": "方案 B"} if name == "Kimi" else {},
                ),
                role=role,
                delay=0,
            )
        )
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(registry).run(
            UserQuestion(question="四 AI 标准协议测试"),
            ["GPT", "Kimi", "元宝", "豆包"],
            "GPT",
            ["Kimi"],
        )

    record = asyncio.run(scenario())
    assert sum(len(provider.calls) for provider in providers) == 17
    assert {provider.name: len(provider.calls) for provider in providers} == {
        "GPT": 5,
        "Kimi": 4,
        "元宝": 4,
        "豆包": 4,
    }
    assert len(record.rounds[0].reviews) == 12
    assert len(record.rounds[0].scores) == 12
    assert len(record.rounds[0].baseline_scores) == 12
    author_by_alias = {
        f"方案 {chr(65 + index)}": name
        for index, name in enumerate(record.provider_names)
    }
    for score in [
        *record.rounds[0].baseline_scores,
        *record.rounds[0].scores,
    ]:
        assert score.judge_name != author_by_alias[score.base_alias]
    for score_group in (
        record.rounds[0].baseline_scores,
        record.rounds[0].scores,
    ):
        for candidate_alias, author in author_by_alias.items():
            actual_judges = {
                score.judge_name
                for score in score_group
                if score.base_alias == candidate_alias
            }
            assert actual_judges == set(record.provider_names) - {author}
    assert record.settings["judge_conflict_policy"] == "leave_one_out_panel"
    assert record.settings["effective_judge_names"] == ["Kimi", "GPT", "元宝", "豆包"]


def test_review_starts_only_after_every_independent_call_finishes() -> None:
    events = []
    registry = build_default_registry()
    names = [provider.name for provider in registry.enabled()]

    async def scenario():
        return await RoundtableEngine(registry, event_handler=events.append).run(
            UserQuestion(question="阶段屏障测试"), names, names[0], [names[1]]
        )

    asyncio.run(scenario())
    review_start = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "stage_started" and event.stage == DiscussionStage.REVIEW
    )
    independent_finishes = [
        index
        for index, event in enumerate(events)
        if event.stage == DiscussionStage.INDEPENDENT
        and event.event_type in {"provider_completed", "provider_failed"}
    ]
    barrier = [
        index
        for index, event in enumerate(events)
        if event.event_type == "stage_barrier"
        and event.stage == DiscussionStage.INDEPENDENT
    ]
    assert len(independent_finishes) == len(names)
    assert barrier and max(independent_finishes) < barrier[0] < review_start


def test_failed_participating_judge_is_covered_by_conflict_free_panel() -> None:
    providers = []
    for name, role in (("GPT", "analyst"), ("Kimi", "skeptic"), ("元宝", "pragmatist")):
        metadata = {"fail_stage": "judge_scoring"} if name == "Kimi" else {}
        providers.append(
            MockAIProvider(
                ProviderConfig(
                    name=name,
                    kind=ProviderKind.MOCK,
                    mode=ProviderMode.MOCK,
                    max_retries=0,
                    metadata=metadata,
                ),
                role=role,
                delay=0,
            )
        )
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(registry).run(
            UserQuestion(question="裁判回退测试"),
            ["GPT", "Kimi", "元宝"],
            "GPT",
            ["Kimi"],
        )

    record = asyncio.run(scenario())
    author_by_alias = {"方案 A": "GPT", "方案 B": "Kimi", "方案 C": "元宝"}
    assert {score.base_alias for score in record.rounds[0].scores} == set(author_by_alias)
    assert {score.judge_name for score in record.rounds[0].scores} == {"GPT", "元宝"}
    assert len(record.rounds[0].scores) == 3
    assert record.settings["judge_ballots_per_candidate_by_round"]["1"] == 1
    assert all(
        score.judge_name != author_by_alias[score.base_alias]
        for score in record.rounds[0].scores
    )


def test_failed_moderator_uses_local_fallback_synthesis() -> None:
    providers = []
    for name, role in (("GPT", "analyst"), ("Kimi", "skeptic"), ("元宝", "pragmatist")):
        metadata = {"fail_stage": "final_synthesis"} if name == "GPT" else {}
        providers.append(
            MockAIProvider(
                ProviderConfig(
                    name=name,
                    kind=ProviderKind.MOCK,
                    mode=ProviderMode.MOCK,
                    max_retries=0,
                    metadata=metadata,
                ),
                role=role,
                delay=0,
            )
        )
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(registry).run(
            UserQuestion(question="主持回退测试"),
            ["GPT", "Kimi", "元宝"],
            "GPT",
            ["Kimi"],
        )

    record = asyncio.run(scenario())
    assert record.final_synthesis is not None
    assert record.final_synthesis.provider_name == "本地降级综合器"
    assert record.final_synthesis.confidence.value == "low"


def test_missing_web_moderator_uses_fast_healthy_online_backup() -> None:
    events = []
    providers = [
        CountingMockProvider(
            ProviderConfig(
                name="元宝",
                kind=ProviderKind.WEB,
                mode=ProviderMode.AUTOMATIC,
                max_retries=0,
                allow_manual_fallback=False,
                metadata={"fail_stage": "independent_answer"},
            ),
            role="pragmatist",
            delay=0,
        ),
        CountingMockProvider(
            ProviderConfig(
                name="Kimi",
                kind=ProviderKind.WEB,
                mode=ProviderMode.AUTOMATIC,
                max_retries=0,
                allow_manual_fallback=False,
            ),
            role="skeptic",
            delay=0.01,
        ),
        CountingMockProvider(
            ProviderConfig(
                name="豆包",
                kind=ProviderKind.WEB,
                mode=ProviderMode.AUTOMATIC,
                max_retries=0,
                allow_manual_fallback=False,
            ),
            role="analyst",
            delay=0,
        ),
    ]

    async def scenario():
        return await RoundtableEngine(
            ProviderRegistry(providers), event_handler=events.append
        ).run(
            UserQuestion(question="在线主持接力测试"),
            ["Kimi", "元宝", "豆包"],
            "元宝",
            ["Kimi"],
        )

    record = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    assert record.final_synthesis is not None
    assert record.final_synthesis.provider_name == "豆包"
    assert record.final_synthesis.confidence.value == "low"
    assert any("元宝" in risk and "豆包" in risk for risk in record.final_synthesis.risks)
    assert DiscussionStage.SYNTHESIS not in providers[0].calls
    assert DiscussionStage.SYNTHESIS in providers[2].calls
    fallbacks = [event for event in events if event.event_type == "moderator_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0].payload["reason"] == "missing_valid_candidate"


def test_progress_events_are_transient_and_share_logical_call_id() -> None:
    events = []
    providers = [
        ProgressMockProvider(
            ProviderConfig(name=f"p{i}", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role=role,
            delay=0,
        )
        for i, role in enumerate(("analyst", "skeptic"))
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(registry, event_handler=events.append).run(
            UserQuestion(question="进度事件测试"), ["p0", "p1"], "p0", ["p1"]
        )

    asyncio.run(scenario())
    progress = [event for event in events if event.event_type == "provider_progress"]
    assert progress
    assert all(event.transient and event.call_id for event in progress)
    started_ids = {event.call_id for event in events if event.event_type == "provider_started"}
    assert {event.call_id for event in progress}.issubset(started_ids)


def test_interactive_retry_recovers_same_logical_call() -> None:
    events = []
    providers = [
        FailOnceMockProvider(
            ProviderConfig(name="p0", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK, max_retries=0),
            role="analyst",
            delay=0,
        ),
        MockAIProvider(
            ProviderConfig(name="p1", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="skeptic",
            delay=0,
        ),
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        engine = None

        def handler(event):
            events.append(event)
            if event.event_type == "provider_action_required":
                asyncio.get_running_loop().create_task(
                    engine.resolve_provider_action(event.call_id, "retry")
                )

        engine = RoundtableEngine(
            registry, event_handler=handler, interactive_recovery=True
        )
        return await engine.run(
            UserQuestion(question="交互重试测试"), ["p0", "p1"], "p0", ["p1"]
        )

    record = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    actions = [event for event in events if event.event_type == "provider_action_required"]
    assert len(actions) == 1
    call_id = actions[0].call_id
    assert len([event for event in events if event.call_id == call_id and event.event_type == "provider_started"]) == 2


def test_strict_automatic_provider_retries_then_skips_without_manual_prompt() -> None:
    events = []
    providers = [
        AlwaysFailMockProvider(
            ProviderConfig(
                name="web",
                kind=ProviderKind.WEB,
                mode=ProviderMode.AUTOMATIC,
                allow_manual_fallback=False,
                max_retries=1,
            ),
            role="analyst",
            delay=0,
        ),
        MockAIProvider(
            ProviderConfig(name="p1", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="skeptic",
            delay=0,
        ),
        MockAIProvider(
            ProviderConfig(name="p2", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="pragmatist",
            delay=0,
        ),
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(
            registry,
            event_handler=events.append,
            interactive_recovery=True,
        ).run(
            UserQuestion(question="严格自动模式测试"),
            ["web", "p1", "p2"],
            "p1",
            ["p2"],
        )

    record = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    assert not [event for event in events if event.event_type == "manual_input_required"]
    assert not [event for event in events if event.event_type == "provider_action_required"]
    failed_starts = [
        event for event in events
        if event.provider_name == "web" and event.event_type == "provider_started"
    ]
    assert len(failed_starts) == 2


def test_timeout_error_is_written_as_readable_message() -> None:
    events = []
    providers = [
        AlwaysTimeoutMockProvider(
            ProviderConfig(
                name="slow-web",
                kind=ProviderKind.WEB,
                mode=ProviderMode.AUTOMATIC,
                allow_manual_fallback=False,
                timeout_seconds=12,
                max_retries=0,
            ),
            role="analyst",
            delay=0,
        ),
        MockAIProvider(
            ProviderConfig(name="p1", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="skeptic",
            delay=0,
        ),
        MockAIProvider(
            ProviderConfig(name="p2", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="pragmatist",
            delay=0,
        ),
    ]

    async def scenario():
        return await RoundtableEngine(
            ProviderRegistry(providers), event_handler=events.append
        ).run(
            UserQuestion(question="超时日志测试"),
            ["slow-web", "p1", "p2"],
            "p1",
            ["p2"],
        )

    asyncio.run(scenario())
    failures = [event for event in events if event.event_type == "provider_failed"]
    assert failures
    assert "超过 12 秒仍未完成" in failures[0].message


def test_multiple_review_rounds_are_persisted_in_record() -> None:
    async def scenario():
        registry = build_default_registry()
        names = [provider.name for provider in registry.enabled()]
        return await RoundtableEngine(registry).run(
            UserQuestion(question="两轮评审测试"),
            names,
            names[0],
            [names[1]],
            rounds=2,
        )

    record = asyncio.run(scenario())
    assert len(record.rounds) == 2
    assert all(len(round_item.reviews) == 6 for round_item in record.rounds)
    assert len(record.rounds[1].revisions) == 3
    assert len(record.rounds[1].scores) == 6


def test_single_provider_failure_does_not_abort_others() -> None:
    providers = []
    for index, role in enumerate(("analyst", "skeptic", "pragmatist")):
        config = ProviderConfig(
            name=f"p{index}",
            kind=ProviderKind.MOCK,
            mode=ProviderMode.MOCK,
            max_retries=0,
            metadata={"fail_stage": "independent_answer"} if index == 0 else {},
        )
        providers.append(MockAIProvider(config, role=role, delay=0))
    registry = ProviderRegistry(providers)

    async def scenario():
        return await RoundtableEngine(registry).run(
            UserQuestion(question="容错测试"),
            ["p0", "p1", "p2"],
            "p1",
            ["p2"],
        )

    record = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    assert len(record.rounds[0].responses) == 2
    assert any(error.provider_name == "p0" for error in record.errors)


def test_all_independent_failures_mark_discussion_failed() -> None:
    providers = [
        MockAIProvider(
            ProviderConfig(
                name=f"p{index}",
                kind=ProviderKind.MOCK,
                mode=ProviderMode.MOCK,
                max_retries=0,
                metadata={"fail_stage": "independent_answer"},
            ),
            delay=0,
        )
        for index in range(2)
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        engine = RoundtableEngine(registry)
        with pytest.raises(RuntimeError):
            await engine.run(UserQuestion(question="全部失败"), ["p0", "p1"], "p0", ["p1"])
        return engine.current_record

    record = asyncio.run(scenario())
    assert record is not None
    assert record.status == RunStatus.FAILED


def test_only_one_independent_success_refuses_fake_roundtable() -> None:
    providers = [
        MockAIProvider(
            ProviderConfig(
                name=f"p{index}",
                kind=ProviderKind.MOCK,
                mode=ProviderMode.MOCK,
                max_retries=0,
                metadata={"fail_stage": "independent_answer"} if index < 2 else {},
            ),
            delay=0,
        )
        for index in range(3)
    ]

    async def scenario():
        engine = RoundtableEngine(ProviderRegistry(providers))
        with pytest.raises(RuntimeError, match="至少需要 2 份"):
            await engine.run(
                UserQuestion(question="单回答不得伪装圆桌"),
                ["p0", "p1", "p2"],
                "p2",
                ["p2"],
            )
        return engine.current_record

    record = asyncio.run(scenario())
    assert record is not None
    assert record.status == RunStatus.FAILED
    assert record.current_stage == DiscussionStage.INDEPENDENT


def test_mock_tasks_execute_concurrently() -> None:
    providers = [
        MockAIProvider(
            ProviderConfig(name=f"p{i}", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role=role,
            delay=0.12,
        )
        for i, role in enumerate(("analyst", "skeptic", "pragmatist"))
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        engine = RoundtableEngine(registry)
        started = time.perf_counter()
        record = await engine.run(
            UserQuestion(question="并发测试"),
            ["p0", "p1", "p2"],
            "p0",
            ["p1"],
            concurrency=16,
        )
        return record, time.perf_counter() - started

    record, elapsed = asyncio.run(scenario())
    assert record.status == RunStatus.SUCCEEDED
    # 14 provider calls at 0.12s would take ~1.68s sequentially; stages still run in order.
    assert elapsed < 1.25


def test_cancellation_returns_cancelled_record() -> None:
    providers = [
        MockAIProvider(
            ProviderConfig(name=f"p{i}", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            delay=0.4,
        )
        for i in range(2)
    ]
    registry = ProviderRegistry(providers)

    async def scenario():
        engine = RoundtableEngine(registry)
        task = asyncio.create_task(
            engine.run(UserQuestion(question="取消测试"), ["p0", "p1"], "p0", ["p1"])
        )
        await asyncio.sleep(0.05)
        await engine.cancel()
        return await task

    record = asyncio.run(scenario())
    assert record.status == RunStatus.CANCELLED
