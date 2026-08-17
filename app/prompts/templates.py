from __future__ import annotations

import json
from typing import Any

from app.models import (
    AIResponse,
    JudgeScore,
    ReviewComment,
    RevisedResponse,
    RoundEffectiveness,
    UserQuestion,
)


DISCUSSION_RULES = """
圆桌规则：
1. 目标是共同解决问题，不是赢得辩论；不得进行人身化评价。
2. 不得为形成共识而忽略事实；多数意见不等于正确答案。
3. 不得编造数据、来源、实验结果或用户信息；未知事实必须标注。
4. 重要结论须说明依据；信息不足时明确需要补充什么。
5. 优先考虑安全性、可逆性、用户约束和可验证性。
6. 只传递有用信息，避免复述题目、其他方案或讨论过程。
""".strip()


PERSPECTIVES = (
    "证据与正确性：核对关键事实、因果链和证据强弱",
    "反例与失败模式：寻找边界条件、隐含假设和不可逆风险",
    "执行与资源：把建议变成有负责人、指标和退出条件的行动",
    "系统与二阶影响：分析反馈回路、长期影响和利益相关方",
    "用户价值与简化：删除低价值复杂度，突出最小可行方案",
)


def _text(value: object, limit: int = 1200) -> str:
    clean = " ".join(str(value or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _items(value: object, *, count: int = 6, item_limit: int = 420) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        clean = _text(item, item_limit)
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= count:
            break
    return result


def _compact(value: object, limit: int = 24_000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[:limit] + "…[已截断]"


def _candidate(candidate: AIResponse | RevisedResponse) -> dict[str, Any]:
    """Return only semantic content; never feed raw webpage output back to an AI."""
    if isinstance(candidate, RevisedResponse):
        return {
            "recommendation": _text(candidate.recommendation, 2600),
            "kept_points": _items(candidate.kept_points),
            "changed_points": _items(candidate.changed_points),
            "borrowed_ideas": _items(candidate.borrowed_ideas),
            "resolved_conflicts": _items(candidate.resolved_conflicts),
            "synergy_gains": _items(candidate.synergy_gains),
            "uncertainties": _items(candidate.uncertainties, count=4),
        }
    return {
        "understanding": _text(candidate.understanding, 700),
        "conclusion": _text(candidate.conclusion, 1000),
        "reasoning": _items(candidate.reasoning),
        "actions": _items(candidate.actions),
        "risks": _items(candidate.risks, count=5),
        "uncertainties": _items(candidate.uncertainties, count=4),
        "recommendation": _text(candidate.recommendation, 2600),
        "distinctive_contribution": _text(candidate.distinctive_contribution, 700),
        "key_assumptions": _items(candidate.key_assumptions, count=5),
    }


def _review(review: ReviewComment) -> dict[str, Any]:
    return {
        "target_alias": review.target_alias,
        "strengths": _items(review.strengths, count=4),
        "logical_gaps": _items(review.logical_gaps, count=4),
        "unverified_assumptions": _items(review.unverified_assumptions, count=4),
        "fact_conflicts": _items(review.fact_conflicts, count=4),
        "risks": _items(review.risks, count=4),
        "improvements": _items(review.improvements, count=5),
        "unique_contributions": _items(review.unique_contributions, count=4),
        "integration_proposals": _items(review.integration_proposals, count=5),
        "decisive_tests": _items(review.decisive_tests, count=4),
    }


def _score(score: JudgeScore) -> dict[str, Any]:
    return {
        "candidate_alias": score.candidate_alias,
        "base_alias": score.base_alias or score.candidate_alias,
        "snapshot": score.snapshot,
        "decision_score": score.weighted_total or score.dimensions.weighted_total,
        "rank": score.rank,
        "verdict": score.verdict,
        "dimensions": score.dimensions.model_dump(mode="json"),
        "comparative_reason": _text(score.comparative_reason or score.reason, 500),
        "fatal_flaw": _text(score.fatal_flaw, 300),
        "evidence": _items(score.evidence, count=2, item_limit=260),
    }


def _judge_candidate(candidate: AIResponse | RevisedResponse) -> dict[str, Any]:
    semantic = _candidate(candidate)
    return {
        "recommendation": _text(semantic.get("recommendation", ""), 1700),
        "reasoning": _items(semantic.get("reasoning", []), count=3, item_limit=260),
        "actions": _items(semantic.get("actions", []), count=3, item_limit=260),
        "risks": _items(semantic.get("risks", []), count=3, item_limit=260),
        "uncertainties": _items(
            semantic.get("uncertainties", []), count=3, item_limit=260
        ),
        "synergy_gains": _items(
            semantic.get("synergy_gains", []), count=3, item_limit=260
        ),
    }


class PromptFactory:
    PERSPECTIVES = PERSPECTIVES

    @staticmethod
    def independent(question: UserQuestion, perspective: str = "") -> str:
        lens = perspective or PERSPECTIVES[0]
        return f"""{DISCUSSION_RULES}

阶段：独立回答。你看不到其他参与者的答案。
你的专属视角：{lens}
用户问题：{question.question}
背景：{question.background or '未提供'}
约束：{question.constraints or '未提供'}

请直接解决用户问题，并给出只有你的专属视角最可能发现的一项贡献。
仅返回一个 JSON 对象，不使用 Markdown 代码围栏：
{{"understanding":"...","conclusion":"...","reasoning":["..."],"actions":["..."],"risks":["..."],"uncertainties":["..."],"recommendation":"可直接交付给用户的完整建议","distinctive_contribution":"本方案不可替代的独有贡献","key_assumptions":["..."]}}
"""

    @staticmethod
    def review(question: UserQuestion, target_alias: str, target: AIResponse) -> str:
        return PromptFactory.review_batch(question, [(target_alias, target)])

    @staticmethod
    def review_batch(
        question: UserQuestion,
        candidates: list[tuple[str, AIResponse | RevisedResponse]],
    ) -> str:
        material = [
            {"target_alias": alias, "content": _candidate(candidate)}
            for alias, candidate in candidates
        ]
        return f"""{DISCUSSION_RULES}

阶段：匿名批量交叉评审。不要猜测回答者身份，也不要按文风评分。
原问题：{question.question}
待评方案：{_compact(material, 20_000)}

你的任务不是重复方案，而是为下一轮协作建立“融合接口”：
- 指出每个方案至少一个真正独有、值得保留的贡献；
- 提出如何把它与其他方案组合，组合后必须产生单一方案没有的新价值；
- 对互相冲突的主张提出可观察、可执行的裁决测试。
仅返回 JSON：
{{"reviews":[{{"target_alias":"方案 A","strengths":["..."],"logical_gaps":["..."],"unverified_assumptions":["..."],"fact_conflicts":["..."],"risks":["..."],"improvements":["..."],"unique_contributions":["..."],"integration_proposals":["..."],"decisive_tests":["..."]}}]}}
必须为每个待评方案各返回一项。
"""

    @staticmethod
    def revision(
        question: UserQuestion,
        own: AIResponse | RevisedResponse,
        reviews: list[ReviewComment],
        round_number: int,
        peers: list[tuple[str, AIResponse | RevisedResponse]] | None = None,
        integration_brief: dict[str, list[str]] | None = None,
    ) -> str:
        missions = {
            1: "纠正错误，并组合不同方案的互补优势",
            2: "用可验证的裁决条件解决剩余冲突",
            3: "删去重复和低价值复杂度，收敛为可执行最终方案",
        }
        peer_material = [
            {"alias": alias, "content": _candidate(candidate)}
            for alias, candidate in (peers or [])
        ]
        return f"""{DISCUSSION_RULES}

阶段：第 {round_number} 轮协同修订。
本轮使命：{missions.get(round_number, missions[3])}
原问题：{question.question}
你的当前方案：{_compact(_candidate(own), 8_000)}
针对你的匿名评审：{_compact([_review(item) for item in reviews], 9_000)}
其他匿名方案的可借鉴内容：{_compact(peer_material, 12_000)}
跨方案融合简报：{_compact(integration_brief or {}, 6_000)}

请吸收其他方案中的有效内容，但不要机械拼接。最终 recommendation 必须直接回答原问题，不能写成“我做了哪些修订”的过程说明。
仅返回 JSON：
{{"kept_points":["..."],"changed_points":["..."],"change_reasons":["..."],"borrowed_ideas":["来自方案 B 的具体思想及用途"],"resolved_conflicts":["冲突及裁决依据"],"synergy_gains":["组合后新增、任一单独方案都不具备的价值"],"uncertainties":["..."],"recommendation":"可直接交付给用户的完整最终建议"}}
"""

    @staticmethod
    def judge(
        question: UserQuestion,
        candidate_alias: str,
        candidate: RevisedResponse | AIResponse,
    ) -> str:
        return PromptFactory.judge_batch(question, [(candidate_alias, candidate)])

    @staticmethod
    def judge_batch(
        question: UserQuestion,
        candidates: list[tuple[str, RevisedResponse | AIResponse]],
    ) -> str:
        material = [
            {"candidate_alias": alias, "content": _judge_candidate(candidate)}
            for alias, candidate in candidates
        ]
        comparison_rule = (
            "先做两两比较再评分。禁止‘大家都不错’、并列第一、所有方案集中在同一分数段或每个维度都给相同分数。"
            "必须明确一个最佳方案和一个最弱方案；同一快照内 rank 不得重复。"
            if len(candidates) > 1
            else "当前输入仅有一个可评候选：只评价该候选并令 rank=1，不得虚构其他方案进行比较。"
        )
        return f"""{DISCUSSION_RULES}

阶段：批量裁判评分。原问题：{question.question}
候选方案：{_compact(material, 28_000)}

利益冲突规则：系统已经从候选列表中硬性移除可能由你创作的方案。只能评价当前明确列出的 candidate_alias；即使你从记忆、文风或上下文猜到被移除的自有方案，也不得补写、打分、排名或暗示其优劣。不要猜测任何方案作者身份。
{comparison_rule}
评分锚点：0–3=存在致命缺陷，4–5=明显不足，6–7=可用但需改进，8–9=有充分证据的强方案，10=极少使用且近乎完备。正确性、约束匹配或风险控制任一低于 4 时必须判为淘汰。
决策分由系统按权重计算：正确性22%、约束匹配16%、可执行性16%、风险控制14%、证据12%、逻辑10%、客观性5%、不确定性表达5%。不要自行平均。
reason、comparative_reason 各不超过 80 字，evidence 最多两项，避免冗长输出。
仅返回严格 JSON：
{{"winner_alias":"方案 A","rejected_alias":"方案 D","scores":[{{"candidate_alias":"方案 A","rank":1,"verdict":"推荐|备选|保留观察|淘汰","dimensions":{{"correctness":0,"logical_completeness":0,"executability":0,"objectivity":0,"risk_control":0,"constraint_alignment":0,"evidence_grounding":0,"uncertainty_expression":0}},"reason":"绝对质量判断","comparative_reason":"相对其他方案为何更优或更差","fatal_flaw":"无则为空字符串","evidence":["证据1","证据2"],"score_confidence":"low|medium|high"}}]}}
必须覆盖每个候选方案，且每个方案只出现一次。
"""

    @staticmethod
    def synthesis(
        question: UserQuestion,
        responses: list[AIResponse],
        reviews: list[ReviewComment],
        revisions: list[RevisedResponse],
        scores: list[JudgeScore],
        effectiveness: list[RoundEffectiveness] | None = None,
    ) -> str:
        latest: dict[str, AIResponse | RevisedResponse] = {
            item.provider_name: item for item in responses
        }
        latest.update({item.provider_name: item for item in revisions})

        unique: list[str] = []
        integrations: list[str] = []
        tests: list[str] = []
        for review in reviews:
            for source, target in (
                (review.unique_contributions, unique),
                (review.integration_proposals, integrations),
                (review.decisive_tests, tests),
            ):
                for item in _items(source, count=4):
                    if item not in target:
                        target.append(item)

        materials = {
            "latest_candidates": [
                {"provider": name, "content": _candidate(candidate)}
                for name, candidate in latest.items()
            ],
            "distinctive_contributions": unique[:12],
            "integration_proposals": integrations[:12],
            "decisive_tests": tests[:10],
            "scores": [_score(item) for item in scores],
            "round_effectiveness": [
                item.model_dump(mode="json") for item in (effectiveness or [])
            ],
        }
        return f"""{DISCUSSION_RULES}

阶段：主持人综合。不要简单选择最高分方案，也不要把各方案逐段拼接。
原问题：{question.question}
背景：{question.background or '未提供'}
约束：{question.constraints or '未提供'}
精简协作材料：{_compact(materials, 30_000)}

你必须明确选择一个主方案，不得并列推荐、轮流表扬或把互相冲突的观点折中平均。默认选择裁判排名第一的方案；若要改选，selection_rationale 必须列出至少两条可核验的新证据。低排名方案的局部内容只有在明确改善主方案且不引入其致命缺陷时才可吸收。
逐轮效果数据是约束而非装饰：评分下降的维度不得被描述为“取得提升”。先识别真正互补的贡献，再解决冲突，最后输出一份可独立阅读、直接回答原问题的方案。“协同增益”必须是组合后出现、任何单一输入方案都没有的新能力或新价值；若不存在则诚实写明。
仅返回 JSON：
{{"recommended_candidate":"方案 A","candidate_ranking":["方案 A","方案 B","方案 C","方案 D"],"selection_rationale":["为什么选择主方案而不是第二名"],"minority_report":["未被采纳但仍可能正确的少数意见"],"recommendation":"最终答案","reasons":["..."],"execution_steps":["..."],"rejected_options":["明确淘汰项及原因"],"unresolved_questions":["..."],"user_confirmations":["..."],"contributions":{{"AI 名称":"不可替代的贡献"}},"consensus":["..."],"disagreements":["..."],"synergy_gains":["组合产生的新价值"],"decisive_tradeoffs":["取舍及依据"],"validation_plan":["验证指标、停止条件或回滚条件"],"risks":["..."],"confidence":"low|medium|high"}}
"""
