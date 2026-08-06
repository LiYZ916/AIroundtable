from __future__ import annotations

import json

from app.models import AIResponse, ReviewComment, RevisedResponse, UserQuestion
from app.orchestration import RoundtableEngine
from app.prompts import PromptFactory


def test_prompts_use_semantic_projection_and_never_reinject_raw_webpage_text() -> None:
    marker = "RAW_WEBPAGE_SHOULD_NEVER_RETURN_" * 1000
    question = UserQuestion(question="怎样得到优于单一方案的组合结果？")
    own = AIResponse(
        provider_name="甲",
        recommendation="先建立事实基线。",
        distinctive_contribution="证据基线",
        raw_content=marker,
    )
    peer = AIResponse(
        provider_name="乙",
        recommendation="设置停止阈值。",
        distinctive_contribution="自动止损",
        raw_content=marker,
    )
    review = ReviewComment(
        provider_name="乙",
        target_alias="方案 A",
        integration_proposals=["将事实基线与停止阈值绑定"],
        decisive_tests=["用试点结果判断是否扩展"],
        raw_content=marker,
    )

    revision_prompt = PromptFactory.revision(
        question,
        own,
        [review],
        1,
        peers=[("方案 B", peer)],
        integration_brief={"integration_proposals": review.integration_proposals},
    )
    assert marker not in revision_prompt
    assert "设置停止阈值" in revision_prompt
    assert "将事实基线与停止阈值绑定" in revision_prompt
    assert len(revision_prompt) < 25_000

    revision = RevisedResponse(
        provider_name="甲",
        original_response_id=own.id,
        recommendation="以基线触发停止阈值。",
        synergy_gains=["形成可自动止损的验证闭环"],
        raw_content=marker,
    )
    synthesis_prompt = PromptFactory.synthesis(
        question, [own, peer], [review], [revision], []
    )
    assert marker not in synthesis_prompt
    assert "可自动止损的验证闭环" in synthesis_prompt
    assert len(synthesis_prompt) < 35_000


def test_json_parser_accepts_prose_fences_and_nested_json_strings() -> None:
    payload = {"recommendation": "自然语言答案", "risks": ["待验证"]}
    raw = "以下是结果：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n谢谢"
    assert RoundtableEngine._json(raw) == payload

    nested = json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)
    assert RoundtableEngine._json(nested) == payload
