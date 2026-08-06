from __future__ import annotations

import asyncio
import json
import re

from app.core.enums import DiscussionStage
from app.models import ProviderConfig
from app.providers.base import AIProvider


ROLE_PROFILES = {
    "analyst": {
        "label": "证据分析派",
        "focus": "先澄清目标与可验证事实，再按证据强弱分层决策",
        "risk": "现有信息可能不足以证明因果关系",
    },
    "skeptic": {
        "label": "审慎质疑派",
        "focus": "优先寻找反例、隐含假设与不可逆风险",
        "risk": "过度乐观会掩盖边界条件和失败成本",
    },
    "pragmatist": {
        "label": "务实执行派",
        "focus": "把方案拆为低成本、可测量、可回滚的小步骤",
        "risk": "执行速度不能替代事实核验",
    },
    "systems": {
        "label": "系统思考派",
        "focus": "同时考察短期效果、反馈回路和长期二阶影响",
        "risk": "局部优化可能把问题转移到系统其他位置",
    },
}


def _find_question(prompt: str) -> str:
    for marker in ("用户问题：", "原问题："):
        if marker in prompt:
            value = prompt.split(marker, 1)[1].splitlines()[0].strip()
            if value:
                return value
    return "当前问题"


def _find_alias(prompt: str) -> str:
    match = re.search(r"(?:待评方案别名|候选方案)：([^\n]+)", prompt)
    return match.group(1).strip() if match else "方案 A"


class MockAIProvider(AIProvider):
    """Deterministic offline AI used to exercise every roundtable stage."""

    def __init__(self, config: ProviderConfig, role: str = "analyst", delay: float = 0.02):
        super().__init__(config)
        self.role = role if role in ROLE_PROFILES else "analyst"
        self.delay = delay

    async def open_login_page(self) -> None:
        self.session.is_logged_in = True

    async def check_login_status(self) -> bool:
        self.session.is_logged_in = True
        return True

    async def send_message(self, message: str) -> None:
        if self.config.metadata.get("fail_stage") == self._current_stage.value:
            raise RuntimeError(f"模拟故障：{self.name} 在 {self._current_stage.value} 失败")

    async def wait_for_response(self) -> str:
        await asyncio.sleep(self.delay)
        if self._stopped:
            raise asyncio.CancelledError
        profile = ROLE_PROFILES[self.role]
        question = _find_question(self._current_message)
        alias = _find_alias(self._current_message)

        if self._current_stage == DiscussionStage.INDEPENDENT:
            payload = {
                "understanding": f"问题要求对“{question}”形成可执行且可检验的判断。",
                "conclusion": f"{profile['label']}建议：{profile['focus']}。",
                "reasoning": [
                    "先区分已知事实、待验证假设与价值取舍",
                    "用小规模验证降低一次性决策风险",
                    "预先定义成功、停止和回滚条件",
                ],
                "actions": ["补齐关键约束", "列出备选方案", "进行最小可行验证", "依据结果复盘"],
                "risks": [profile["risk"], "模拟回答不包含外部实时事实核验"],
                "uncertainties": ["缺少用户的优先级、预算或时间边界"],
                "recommendation": f"以“{profile['focus']}”为主线，先做可逆试验再扩大投入。",
                "distinctive_contribution": f"从{profile['label']}视角补充其他方案容易遗漏的边界条件。",
                "key_assumptions": ["用户允许先进行小规模试验", "结果可以用预先约定的指标观察"],
            }
        elif self._current_stage == DiscussionStage.REVIEW:
            aliases = self._runtime_context.get("candidate_aliases") or [alias]
            reviews = [
                {
                    "target_alias": target_alias,
                    "strengths": [f"{target_alias}提供了清晰的决策主线", "强调了可执行步骤"],
                    "logical_gaps": ["成功指标和停止阈值仍不够量化"],
                    "unverified_assumptions": ["默认关键资源能够按时获得"],
                    "fact_conflicts": ["未发现可由当前材料确认的直接事实冲突"],
                    "risks": [profile["risk"]],
                    "improvements": ["补充反例测试", "让每个步骤对应负责人、截止时间和回滚条件"],
                    "unique_contributions": [f"{target_alias}提供了可作为组合方案骨架的决策主线"],
                    "integration_proposals": [f"把{target_alias}的主线与反例测试、量化阈值组合成分阶段试点"],
                    "decisive_tests": ["用预设成功指标和停止阈值比较方案在小规模试点中的表现"],
                }
                for target_alias in aliases
            ]
            payload = {"reviews": reviews} if self._runtime_context.get("batch") else reviews[0]
        elif self._current_stage == DiscussionStage.REVISION:
            payload = {
                "kept_points": [profile["focus"], "采用可逆的小步验证"],
                "changed_points": ["将笼统的验证改成带阈值的阶段检查"],
                "change_reasons": ["匿名评审指出指标不够量化，可能导致事后解释"],
                "borrowed_ideas": ["吸收其他方案的反例测试和负责人机制"],
                "resolved_conflicts": ["以小规模试点结果裁决速度与核验深度的冲突"],
                "synergy_gains": ["把证据核验、风险反例和执行阈值连成一个可自动停止的闭环"],
                "uncertainties": ["具体阈值仍需用户结合场景确认"],
                "recommendation": "先确认约束与成功阈值，再执行低成本试点；达到阈值才进入下一阶段。",
            }
        elif self._current_stage == DiscussionStage.JUDGE:
            base = {"analyst": 8.4, "skeptic": 8.1, "pragmatist": 8.6, "systems": 8.2}[self.role]
            aliases = self._runtime_context.get("candidate_aliases") or [alias]

            def score(target_alias: str, index: int) -> dict[str, object]:
                round_match = re.search(r"第(\d+)轮", target_alias)
                discussion_gain = (
                    0.0
                    if "讨论前" in target_alias
                    else 0.35 + 0.15 * int(round_match.group(1))
                    if round_match
                    else 0.35
                )
                candidate_offset = max(0.0, (len(aliases) - index - 1) * 0.08)
                calibrated = min(9.3, base + discussion_gain + candidate_offset)
                return {
                    "candidate_alias": target_alias,
                    "rank": index + 1,
                    "verdict": "推荐" if index == 0 else "淘汰" if index == len(aliases) - 1 else "备选",
                    "dimensions": {
                        "correctness": calibrated,
                        "logical_completeness": min(10, calibrated + 0.2),
                        "executability": min(10, calibrated + 0.4),
                        "objectivity": calibrated,
                        "risk_control": min(10, calibrated + 0.3),
                        "constraint_alignment": calibrated - 0.1,
                        "evidence_grounding": calibrated - 0.4,
                        "uncertainty_expression": min(10, calibrated + 0.2),
                    },
                    "reason": f"{target_alias}具备结构化步骤和风险意识，但外部事实仍需验证。",
                    "comparative_reason": f"相较下一名，{target_alias}的约束匹配和执行闭环更完整。",
                    "fatal_flaw": "" if index < len(aliases) - 1 else "关键证据仍不足",
                    "evidence": ["方案提出分阶段试点", "方案明确保留不确定项"],
                    "score_confidence": "medium",
                }
            score_items = [score(target_alias, index) for index, target_alias in enumerate(aliases)]
            payload = (
                {
                    "winner_alias": aliases[0],
                    "rejected_alias": aliases[-1],
                    "scores": score_items,
                }
                if self._runtime_context.get("batch")
                else score_items[0]
            )
        elif self._current_stage == DiscussionStage.SYNTHESIS:
            payload = {
                "recommendation": "采用分阶段、可验证、可回滚的组合方案：先澄清约束，再进行最小试点，依据预设指标决定扩展或停止。",
                "reasons": ["兼顾证据质量与执行效率", "将主要风险前置", "允许根据新信息修正"],
                "execution_steps": ["确认目标、预算、期限和不可接受结果", "定义指标及停止阈值", "运行小规模试点", "独立核验结果", "决定扩展、修订或回滚"],
                "rejected_options": ["一次性全面投入：不可逆成本和未经验证假设过多", "只按多数意见决策：共识不能代替证据"],
                "unresolved_questions": ["用户的优先级与资源边界尚未给出", "涉及实时事实的部分尚未外部核验"],
                "user_confirmations": ["可接受的最大成本", "决策期限", "成功和停止阈值"],
                "contributions": {"证据分析派": "区分事实与假设", "审慎质疑派": "补充反例和风险", "务实执行派": "提出可回滚步骤", "系统思考派": "提示二阶影响"},
                "consensus": ["先澄清约束", "使用可验证试点", "保留回滚路径"],
                "disagreements": ["事实核验深度与行动速度的取舍"],
                "recommended_candidate": "方案 A",
                "candidate_ranking": ["方案 A", "方案 B", "方案 C", "方案 D"],
                "selection_rationale": ["方案 A 的约束匹配和风险闭环最佳", "其关键假设最容易通过低成本试点验证"],
                "minority_report": ["更审慎的方案仍可能在高风险场景中优于当前主方案"],
                "synergy_gains": ["证据核验、反例检查和执行阈值共同构成可验证且可自动止损的决策闭环"],
                "decisive_tradeoffs": ["先用低成本试点换取信息，再决定是否牺牲速度进行更深核验"],
                "validation_plan": ["启动前记录基线", "达到成功阈值才扩展", "触发停止阈值立即回滚并复盘"],
                "risks": ["模拟 AI 不能核验实时外部事实", "指标设置不当会产生误导"],
                "confidence": "medium",
            }
        else:
            payload = {"recommendation": "当前阶段无模拟内容"}
        return json.dumps(payload, ensure_ascii=False)
