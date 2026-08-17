from __future__ import annotations

from app.models import (
    AIResponse,
    DiscussionRecord,
    FinalSynthesis,
    ProjectConfig,
    ReviewComment,
    RevisedResponse,
    UserQuestion,
)
from app.prompts.templates import DISCUSSION_STRATEGIES, PromptFactory
from app.services.handoff import build_handoff_context


def test_old_project_config_defaults_to_standard_collaboration() -> None:
    restored = ProjectConfig.model_validate({"name": "旧配置", "providers": []})
    assert restored.discussion_strategy == "标准共创"


def test_selected_strategy_is_applied_to_every_discussion_stage() -> None:
    question = UserQuestion(question="是否发布？", template_name="红队压力测试")
    response = AIResponse(provider_name="A", recommendation="先灰度发布")
    review = ReviewComment(provider_name="B", target_alias="方案 A")
    revision = RevisedResponse(
        provider_name="A",
        original_response_id=response.id,
        recommendation="先灰度并设置回滚阈值",
    )

    prompts = [
        PromptFactory.independent(question),
        PromptFactory.review_batch(question, [("方案 A", response)]),
        PromptFactory.revision(question, response, [review], 1),
        PromptFactory.judge_batch(question, [("方案 A", revision)]),
        PromptFactory.synthesis(question, [response], [review], [revision], []),
    ]

    assert list(DISCUSSION_STRATEGIES) == [
        "标准共创",
        "红队压力测试",
        "证据审计",
        "执行决策",
        "创新发散",
    ]
    assert all("本轮讨论策略：红队压力测试" in prompt for prompt in prompts)
    assert all("回滚" in prompt for prompt in prompts)


def test_handoff_uses_semantic_summary_and_never_raw_provider_output() -> None:
    record = DiscussionRecord(
        question=UserQuestion(
            question="如何安排试点？",
            template_name="执行决策",
        ),
        provider_names=["A", "B"],
        moderator_name="A",
        judge_names=["B"],
        final_synthesis=FinalSynthesis(
            provider_name="A",
            recommendation="先在一个团队运行两周。",
            recommended_candidate="方案 B",
            consensus=["先小范围验证"],
            disagreements=["是否需要第二个对照组"],
            unresolved_questions=["谁负责验收"],
            validation_plan=["转化率下降 5% 即回滚"],
            raw_content="SECRET_RAW_PROVIDER_OUTPUT",
        ),
    )

    handoff = build_handoff_context(record)

    assert "执行决策" in handoff
    assert "先在一个团队运行两周" in handoff
    assert "谁负责验收" in handoff
    assert "SECRET_RAW_PROVIDER_OUTPUT" not in handoff
