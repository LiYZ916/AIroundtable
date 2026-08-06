from __future__ import annotations

import json

from app.ui.view_models import format_ai_content


def test_structured_ai_output_is_rendered_as_natural_language() -> None:
    raw = json.dumps(
        {
            "understanding": "先确认目标和约束。",
            "conclusion": "应先做小规模试点。",
            "reasoning": ["风险可控", "便于验证"],
            "actions": ["确定指标", "运行两周"],
            "risks": ["样本可能不足"],
            "uncertainties": ["真实转化率未知"],
            "recommendation": "达到门槛后再扩大。",
            "distinctive_contribution": "把成功和退出条件绑定。",
            "synergy_gains": ["证据核验与自动止损形成闭环"],
            "validation_plan": ["未达阈值即回滚"],
        },
        ensure_ascii=False,
    )
    rendered = format_ai_content(raw)
    assert "问题理解" in rendered
    assert "核心结论" in rendered
    assert "• 风险可控" in rendered
    assert "最终建议" in rendered
    assert "独有贡献" in rendered
    assert "协同增益" in rendered
    assert "验证与止损计划" in rendered
    assert "{" not in rendered
    assert '"reasoning"' not in rendered


def test_partial_json_progress_never_leaks_json_syntax_to_chat() -> None:
    rendered = format_ai_content('{"understanding":"正在分析",', pending=True)
    assert rendered.startswith("正在生成完整回答")
    assert "{" not in rendered
    assert '"understanding"' not in rendered
