from __future__ import annotations

from collections.abc import Iterable

from app.models import DiscussionRecord


def _clean(text: object, limit: int = 1200) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _section(title: str, values: Iterable[object], *, limit: int = 5) -> str:
    items = [_clean(value, 500) for value in values]
    items = [value for value in items if value][:limit]
    if not items:
        return ""
    return f"{title}：\n" + "\n".join(f"- {value}" for value in items)


def build_handoff_context(record: DiscussionRecord, *, max_length: int = 6000) -> str:
    """Build a compact semantic handoff without replaying raw provider output."""

    final = record.final_synthesis
    if final is None:
        return ""

    sections = [
        f"上轮主题：{_clean(record.question.question, 900)}",
        f"上轮讨论策略：{_clean(record.question.template_name, 80)}",
        f"推荐方案：{_clean(final.recommended_candidate or '未确定', 120)}",
        f"已形成结论：{_clean(final.recommendation, 1800)}",
        _section("选择依据", final.selection_rationale),
        _section("已达成共识", final.consensus),
        _section("仍有分歧", final.disagreements),
        _section("未解决问题", final.unresolved_questions),
        _section("主要风险", final.risks),
        _section("验证与止损计划", final.validation_plan),
        _section("保留的少数意见", final.minority_report),
    ]
    compact = "\n\n".join(section for section in sections if section)
    if len(compact) <= max_length:
        return compact
    return compact[:max_length].rstrip() + "\n…[交接摘要已截断]"
