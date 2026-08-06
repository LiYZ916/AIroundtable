from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from app.core.enums import DiscussionStage, RunStatus
from app.models import DiscussionRecord


class MessageKind(StrEnum):
    USER = "user"
    AI = "ai"
    SYSTEM = "system"
    ERROR = "error"
    FINAL = "final"


@dataclass(slots=True)
class ChatMessageView:
    id: str
    provider_name: str
    stage: DiscussionStage
    content: str
    kind: MessageKind = MessageKind.AI
    status: RunStatus = RunStatus.SUCCEEDED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_seconds: float = 0.0
    retry_count: int = 0
    target_alias: str = ""


FIELD_LABELS = {
    "understanding": "问题理解",
    "conclusion": "核心结论",
    "reasoning": "理由",
    "actions": "行动建议",
    "distinctive_contribution": "独有贡献",
    "key_assumptions": "关键假设",
    "risks": "风险",
    "uncertainties": "不确定性",
    "recommendation": "最终建议",
    "strengths": "优点",
    "logical_gaps": "逻辑缺口",
    "unverified_assumptions": "待验证假设",
    "fact_conflicts": "事实冲突",
    "improvements": "改进建议",
    "unique_contributions": "值得保留的独有贡献",
    "integration_proposals": "跨方案组合建议",
    "decisive_tests": "冲突裁决测试",
    "kept_points": "保留内容",
    "changed_points": "修改内容",
    "change_reasons": "修改原因",
    "borrowed_ideas": "吸收的其他方案思想",
    "resolved_conflicts": "已解决冲突",
    "synergy_gains": "协同增益",
    "reasons": "综合理由",
    "execution_steps": "执行步骤",
    "rejected_options": "未采用方案",
    "unresolved_questions": "未解决问题",
    "user_confirmations": "待用户确认",
    "contributions": "方案贡献",
    "consensus": "共识",
    "disagreements": "分歧",
    "decisive_tradeoffs": "关键取舍",
    "validation_plan": "验证与止损计划",
    "confidence": "可信度",
    "recommended_candidate": "明确推荐方案",
    "candidate_ranking": "候选排名",
    "selection_rationale": "选择依据",
    "minority_report": "少数意见保留",
    "rank": "名次",
    "verdict": "裁判结论",
    "comparative_reason": "相对优劣",
    "fatal_flaw": "致命缺陷",
    "score_confidence": "评分可信度",
    "reason": "评分理由",
    "evidence": "依据",
}

DIMENSION_LABELS = {
    "correctness": "正确性",
    "logical_completeness": "逻辑完整性",
    "executability": "可执行性",
    "objectivity": "客观性",
    "risk_control": "风险控制",
    "constraint_alignment": "约束匹配",
    "evidence_grounding": "证据支撑",
    "uncertainty_expression": "不确定性表达",
}


def _parse_json_object(raw: str) -> dict | None:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1]
        if clean.endswith("```"):
            clean = clean[:-3].rstrip()
    candidates = [clean]
    start, end = clean.find("{"), clean.rfind("}")
    if start >= 0 and end > start:
        candidates.append(clean[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _value_lines(value: object) -> list[str]:
    if value is None or value == "" or value == [] or value == {}:
        return []
    if isinstance(value, list):
        return [f"• {item}" for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [f"• {key}：{item}" for key, item in value.items() if str(item).strip()]
    return [str(value)]


def _section(title: str, value: object) -> str:
    lines = _value_lines(value)
    return f"{title}\n" + "\n".join(lines) if lines else ""


def _format_review_items(items: list[object]) -> str:
    sections = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = [str(item.get("target_alias", "候选方案"))]
        for key in (
            "strengths",
            "logical_gaps",
            "unverified_assumptions",
            "fact_conflicts",
            "risks",
            "improvements",
            "unique_contributions",
            "integration_proposals",
            "decisive_tests",
        ):
            rendered = _section(FIELD_LABELS[key], item.get(key))
            if rendered:
                parts.append(rendered)
        sections.append("\n".join(parts))
    return "\n\n".join(sections)


def _format_score_items(items: list[object]) -> str:
    sections = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dimensions = item.get("dimensions", {})
        numeric = (
            [float(score) for score in dimensions.values() if isinstance(score, (int, float))]
            if isinstance(dimensions, dict)
            else []
        )
        title = str(item.get("candidate_alias", "候选方案"))
        decision_score = item.get("weighted_total") or item.get("decision_score")
        headline = title
        if isinstance(item.get("rank"), int) and item["rank"] > 0:
            headline = f"#{item['rank']} {headline}"
        if isinstance(decision_score, (int, float)):
            headline += f"：决策分 {decision_score:.2f}/10"
        elif numeric:
            headline += f"：八维均值 {sum(numeric) / len(numeric):.1f}/10"
        parts = [headline]
        if isinstance(dimensions, dict):
            parts.extend(
                f"• {DIMENSION_LABELS.get(key, key)}：{score}/10"
                for key, score in dimensions.items()
            )
        for key in (
            "verdict",
            "reason",
            "comparative_reason",
            "fatal_flaw",
            "evidence",
            "score_confidence",
        ):
            rendered = _section(FIELD_LABELS[key], item.get(key))
            if rendered:
                parts.append(rendered)
        sections.append("\n".join(parts))
    return "\n\n".join(sections)


def format_ai_content(raw: str, *, pending: bool = False) -> str:
    value = _parse_json_object(raw)
    if value is None:
        clean = raw.strip()
        if pending and (clean.startswith("{") or clean.startswith("```json")):
            return f"正在生成完整回答… 已接收 {len(clean)} 个字符"
        if clean.startswith("{") or clean.startswith("```json"):
            readable = []
            pattern = r'"[^"\\]+"\s*:\s*"((?:\\.|[^"\\])*)"'
            for match in re.finditer(pattern, clean):
                try:
                    text = json.loads(f'"{match.group(1)}"')
                except json.JSONDecodeError:
                    continue
                if text and text not in readable:
                    readable.append(text)
            return "\n\n".join(readable) or "回答已完成，但内容格式不完整，无法生成可读摘要。"
        return clean

    if isinstance(value.get("reviews"), list):
        return _format_review_items(value["reviews"])
    if isinstance(value.get("scores"), list):
        return _format_score_items(value["scores"])

    ordered_keys = (
        "understanding",
        "conclusion",
        "reasoning",
        "actions",
        "distinctive_contribution",
        "key_assumptions",
        "kept_points",
        "changed_points",
        "change_reasons",
        "borrowed_ideas",
        "resolved_conflicts",
        "synergy_gains",
        "recommendation",
        "reasons",
        "execution_steps",
        "consensus",
        "disagreements",
        "decisive_tradeoffs",
        "validation_plan",
        "risks",
        "uncertainties",
        "unresolved_questions",
        "user_confirmations",
        "rejected_options",
        "contributions",
        "confidence",
        "recommended_candidate",
        "candidate_ranking",
        "selection_rationale",
        "minority_report",
    )
    parts = []
    for key in ordered_keys:
        if key in value:
            rendered = _section(FIELD_LABELS.get(key, key), value.get(key))
            if rendered:
                parts.append(rendered)
    for key, item in value.items():
        if key in ordered_keys or key in {"reviews", "scores"}:
            continue
        rendered = _section(FIELD_LABELS.get(key, key.replace("_", " ")), item)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts) or "回答内容为空。"


def record_to_chat_messages(record: DiscussionRecord) -> list[ChatMessageView]:
    messages = [
        ChatMessageView(
            id=record.question.id,
            provider_name="你",
            stage=DiscussionStage.PREPARING,
            content=record.question.question,
            kind=MessageKind.USER,
            created_at=record.question.created_at,
        )
    ]
    for round_item in record.rounds:
        for response in round_item.responses:
            messages.append(
                ChatMessageView(
                    id=response.id,
                    provider_name=response.provider_name,
                    stage=response.stage,
                    content=format_ai_content(response.raw_content or response.recommendation),
                    status=response.status,
                    created_at=response.created_at,
                    elapsed_seconds=response.elapsed_seconds,
                    retry_count=response.retry_count,
                )
            )
        reviews_by_provider: dict[str, list[object]] = {}
        for review in round_item.reviews:
            reviews_by_provider.setdefault(review.provider_name, []).append(review)
        for provider_name, provider_reviews in reviews_by_provider.items():
            first_review = provider_reviews[0]
            content = "\n\n".join(
                f"{review.target_alias}\n"
                f"合理：{'；'.join(review.strengths) or '—'}\n"
                f"独有贡献：{'；'.join(review.unique_contributions) or '—'}\n"
                f"组合建议：{'；'.join(review.integration_proposals) or '—'}\n"
                f"裁决测试：{'；'.join(review.decisive_tests) or '—'}\n"
                f"改进：{'；'.join(review.improvements) or '—'}"
                for review in provider_reviews
            )
            messages.append(
                ChatMessageView(
                    id=f"reviews-{round_item.id}-{provider_name}",
                    provider_name=provider_name,
                    stage=first_review.stage,
                    content=content,
                    status=first_review.status,
                    created_at=first_review.created_at,
                    elapsed_seconds=first_review.elapsed_seconds,
                    retry_count=first_review.retry_count,
                )
            )
        for revision in round_item.revisions:
            messages.append(
                ChatMessageView(
                    id=revision.id,
                    provider_name=revision.provider_name,
                    stage=revision.stage,
                    content=format_ai_content(revision.raw_content or revision.recommendation),
                    status=revision.status,
                    created_at=revision.created_at,
                    elapsed_seconds=revision.elapsed_seconds,
                    retry_count=revision.retry_count,
                )
            )
        scores_by_judge: dict[str, list[object]] = {}
        for score in round_item.scores:
            scores_by_judge.setdefault(score.judge_name, []).append(score)
        for judge_name, judge_scores in scores_by_judge.items():
            first_score = judge_scores[0]
            messages.append(
                ChatMessageView(
                    id=f"scores-{round_item.id}-{judge_name}",
                    provider_name=judge_name,
                    stage=first_score.stage,
                    content="\n\n".join(
                        f"#{score.rank} {score.base_alias or score.candidate_alias}：决策分 {score.weighted_total:.2f}/10 · {score.verdict}\n"
                        f"{score.comparative_reason or score.reason}"
                        + (f"\n致命缺陷：{score.fatal_flaw}" if score.fatal_flaw else "")
                        for score in judge_scores
                    ),
                    status=first_score.status,
                    created_at=first_score.created_at,
                    elapsed_seconds=first_score.elapsed_seconds,
                    retry_count=first_score.retry_count,
                )
            )
    for error in record.errors:
        messages.append(
            ChatMessageView(
                id=error.id,
                provider_name=error.provider_name,
                stage=error.stage,
                content=error.error_message,
                kind=MessageKind.ERROR,
                status=error.status,
                created_at=error.created_at,
                elapsed_seconds=error.elapsed_seconds,
                retry_count=error.retry_count,
            )
        )
    if record.final_synthesis:
        final = record.final_synthesis
        messages.append(
            ChatMessageView(
                id=final.id,
                provider_name=final.provider_name,
                stage=final.stage,
                content=format_ai_content(final.raw_content or final.recommendation),
                kind=MessageKind.FINAL,
                status=final.status,
                created_at=final.created_at,
                elapsed_seconds=final.elapsed_seconds,
                retry_count=final.retry_count,
            )
        )
    return sorted(messages, key=lambda item: item.created_at)
