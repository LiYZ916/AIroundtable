from __future__ import annotations

import json
import re
from pathlib import Path

from app.models import DiscussionRecord


PRIVACY_NOTICE = "导出内容可能包含用户问题和 AI 回答；分享前请检查并删除个人或敏感信息。"


def _list(items: list[str], empty: str = "（无）") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


class DiscussionExporter:
    def __init__(self, export_directory: str | Path) -> None:
        self.export_directory = Path(export_directory)
        self.export_directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def to_markdown(record: DiscussionRecord) -> str:
        lines = [
            f"# AI Roundtable：{record.title}",
            "",
            f"> 隐私提示：{PRIVACY_NOTICE}",
            "",
            f"- 讨论 ID：`{record.id}`",
            f"- 状态：{record.status.value}",
            f"- 创建时间：{record.created_at.isoformat()}",
            f"- 参与者：{', '.join(record.provider_names)}",
            f"- 主持人：{record.moderator_name}",
            f"- 裁判：{', '.join(record.judge_names)}",
            "",
            "## 用户问题",
            "",
            record.question.question,
            "",
            "### 背景",
            "",
            record.question.background or "（未提供）",
            "",
            "### 约束",
            "",
            record.question.constraints or "（未提供）",
        ]

        for discussion_round in record.rounds:
            lines.extend(["", f"## 第 {discussion_round.round_number} 轮"])
            for response in discussion_round.responses:
                lines.extend(
                    [
                        "",
                        f"### 独立回答：{response.alias}（{response.provider_name}）",
                        "",
                        response.raw_content or response.recommendation,
                    ]
                )
            for review in discussion_round.reviews:
                lines.extend(
                    [
                        "",
                        f"### 评审：{review.reviewer_alias} → {review.target_alias}",
                        "",
                        review.raw_content,
                    ]
                )
            for revision in discussion_round.revisions:
                lines.extend(
                    [
                        "",
                        f"### 修订：{revision.alias}（{revision.provider_name}）",
                        "",
                        revision.raw_content or revision.recommendation,
                    ]
                )
            for score in discussion_round.scores:
                lines.extend(
                    [
                        "",
                        f"### 评分：{score.judge_name} → {score.candidate_alias}",
                        "",
                        f"- 排名：#{score.rank}",
                        f"- 裁判结论：{score.verdict or '未提供'}",
                        f"- 加权决策分：{score.weighted_total or score.dimensions.weighted_total}/10",
                        f"- 八维简单均值（仅供参考）：{score.dimensions.average}/10",
                        f"- 理由：{score.reason}",
                        f"- 相对优劣：{score.comparative_reason or '未提供'}",
                        f"- 致命缺陷：{score.fatal_flaw or '无'}",
                        f"- 证据：{'; '.join(score.evidence) or '未提供'}",
                    ]
                )
            if discussion_round.effectiveness:
                effect = discussion_round.effectiveness
                lines.extend(
                    [
                        "",
                        f"### 第 {effect.round_number} 轮讨论效果：{effect.verdict}",
                        "",
                        f"- 对比基准：{effect.comparison_basis}",
                        f"- 平均决策分变化：{effect.average_overall_delta:+.2f}",
                        f"- 提升方案数：{effect.improved_provider_count}",
                        f"- 退步方案数：{effect.regressed_provider_count}",
                        "- 八维平均变化："
                        + "；".join(
                            f"{name} {value:+.2f}"
                            for name, value in effect.average_dimension_deltas.items()
                        ),
                    ]
                )

        if record.final_synthesis:
            final = record.final_synthesis
            lines.extend(
                [
                    "",
                    "## 最终综合",
                    "",
                    final.recommendation,
                    "",
                    f"**明确推荐方案：{final.recommended_candidate or '未确定'}**",
                    "",
                    "### 候选排名",
                    "",
                    "\n".join(
                        f"{index}. {alias}（决策分 {final.decision_scores.get(alias, 0):.2f}）"
                        for index, alias in enumerate(final.candidate_ranking, 1)
                    ) or "（无）",
                    "",
                    "### 选择依据",
                    "",
                    _list(final.selection_rationale),
                    "",
                    "### 少数意见保留",
                    "",
                    _list(final.minority_report),
                    "",
                    "### 推荐理由",
                    "",
                    _list(final.reasons),
                    "",
                    "### 执行步骤",
                    "",
                    "\n".join(f"{index}. {item}" for index, item in enumerate(final.execution_steps, 1)) or "（无）",
                    "",
                    "### 被否决方案",
                    "",
                    _list(final.rejected_options),
                    "",
                    "### 共识",
                    "",
                    _list(final.consensus),
                    "",
                    "### 分歧",
                    "",
                    _list(final.disagreements),
                    "",
                    "### 协同增益",
                    "",
                    _list(final.synergy_gains),
                    "",
                    "### 关键取舍",
                    "",
                    _list(final.decisive_tradeoffs),
                    "",
                    "### 验证与止损计划",
                    "",
                    _list(final.validation_plan),
                    "",
                    "### 风险",
                    "",
                    _list(final.risks),
                    "",
                    "### 未解决问题",
                    "",
                    _list(final.unresolved_questions),
                    "",
                    "### 需用户确认",
                    "",
                    _list(final.user_confirmations),
                    "",
                    "### 各 AI 贡献",
                    "",
                    "\n".join(f"- {name}：{contribution}" for name, contribution in final.contributions.items()) or "（无）",
                    "",
                    "### 八维简单均值（仅供参考）",
                    "",
                    "\n".join(f"- {alias}：{average}/10" for alias, average in final.score_averages.items()) or "（无）",
                    "",
                    "### 每轮讨论效果",
                    "",
                    _list(final.round_effectiveness_summary),
                    "",
                    f"**可信度：{final.confidence.value}**",
                ]
            )
        if record.errors:
            lines.extend(["", "## 错误与重试", ""])
            lines.extend(
                f"- {error.provider_name} / {error.stage.value}：{error.error_message}（重试 {error.retry_count} 次）"
                for error in record.errors
            )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def to_json(record: DiscussionRecord) -> str:
        payload = record.model_dump(mode="json")
        payload["privacy_notice"] = PRIVACY_NOTICE
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _safe_stem(self, record: DiscussionRecord) -> str:
        title = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", record.title).strip("_")[:40]
        return f"{record.created_at:%Y%m%d_%H%M%S}_{title or record.id[:8]}"

    def export_markdown(self, record: DiscussionRecord, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.export_directory / f"{self._safe_stem(record)}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_markdown(record), encoding="utf-8")
        return target

    def export_json(self, record: DiscussionRecord, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.export_directory / f"{self._safe_stem(record)}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(record), encoding="utf-8")
        return target
