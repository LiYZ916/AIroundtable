from __future__ import annotations

import json

from app.models import JudgeScore
from app.models.schemas import JudgeDimensions
from app.orchestration import RoundtableEngine


def _dimensions(**overrides: float) -> JudgeDimensions:
    values = {key: 6.0 for key in JudgeDimensions.model_fields}
    values.update(overrides)
    return JudgeDimensions(**values)


def test_weighted_decision_score_does_not_use_simple_average() -> None:
    evidence_first = _dimensions(correctness=9, objectivity=3)
    style_first = _dimensions(correctness=3, objectivity=9)
    assert evidence_first.average == style_first.average
    assert evidence_first.weighted_total > style_first.weighted_total


def test_malformed_batch_scores_are_recovered_per_candidate() -> None:
    first = {
        "candidate_alias": "方案 A",
        "rank": 1,
        "verdict": "推荐",
        "dimensions": _dimensions(correctness=8).model_dump(mode="json"),
        "reason": "证据更强",
        "comparative_reason": "优于方案 B",
        "fatal_flaw": "",
        "evidence": ["证据 A"],
        "score_confidence": "high",
    }
    second = {
        "candidate_alias": "方案 B",
        "rank": 2,
        "verdict": "淘汰",
        "dimensions": _dimensions(correctness=4).model_dump(mode="json"),
        "reason": "证据不足",
        "comparative_reason": "弱于方案 A",
        "fatal_flaw": "关键事实未验证",
        "evidence": ["证据 B"],
        "score_confidence": "medium",
    }
    # 模拟真实运行中每个 score 对象后多出一个右花括号的情况。
    raw = (
        '{"scores":['
        + json.dumps(first, ensure_ascii=False)
        + "},"
        + json.dumps(second, ensure_ascii=False)
        + "]}"
    )
    recovered = RoundtableEngine._score_items(raw)
    assert [item["candidate_alias"] for item in recovered] == ["方案 A", "方案 B"]
    assert recovered[0]["dimensions"]["correctness"] == 8
    assert recovered[1]["fatal_flaw"] == "关键事实未验证"


def test_round_effectiveness_reports_dimension_improvements_and_regressions() -> None:
    before = JudgeScore(
        provider_name="裁判",
        judge_name="裁判",
        candidate_alias="方案 A·讨论前",
        base_alias="方案 A",
        snapshot="independent",
        candidate_response_id="before",
        dimensions=_dimensions(correctness=5, executability=6),
        weighted_total=_dimensions(correctness=5, executability=6).weighted_total,
        reason="基线",
    )
    after_dimensions = _dimensions(correctness=8, executability=5)
    after = JudgeScore(
        provider_name="裁判",
        judge_name="裁判",
        candidate_alias="方案 A·第1轮",
        base_alias="方案 A",
        snapshot="round_1",
        candidate_response_id="after",
        dimensions=after_dimensions,
        weighted_total=after_dimensions.weighted_total,
        reason="修订后",
    )
    effect = RoundtableEngine._measure_effectiveness(
        1, [before], [after], {"方案 A": "测试 AI"}
    )
    provider = effect.provider_results[0]
    assert provider.dimension_deltas["correctness"] == 3
    assert provider.dimension_deltas["executability"] == -1
    assert "correctness" in provider.improved_dimensions
    assert "executability" in provider.regressed_dimensions
