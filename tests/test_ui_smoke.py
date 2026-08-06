from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.providers import build_default_registry
from app.storage import SQLiteStorage


def test_ui_starts_and_closes_headlessly(tmp_path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, "main.py", "--smoke-test"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_group_chat_ui_has_five_online_image_avatars_and_visual_panels(tmp_path) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    from app.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    registry = build_default_registry()
    window = MainWindow(tmp_path, registry, SQLiteStorage(tmp_path / "data/db.sqlite3"))
    online_names = {"GPT", "Kimi", "元宝", "豆包", "DeepSeek"}
    assert {name for name in window.provider_rows if name in online_names} == {
        "GPT", "Kimi", "元宝", "豆包", "DeepSeek"
    }
    assert window.moderator_combo.currentText() == "GPT"
    assert window.judge_combo.currentText() == "Kimi"
    assert all(window.provider_rows[name].enabled_check.isChecked() for name in online_names)
    assert all(window.provider_rows[name].avatar_label.image_loaded for name in online_names)
    assert all(
        not window.provider_rows[name].enabled_check.isChecked()
        for name in {"模拟分析师", "模拟质疑者", "模拟执行顾问"}
    )
    assert window.chat_stream is not None
    assert window.score_radar is not None
    assert window.effectiveness_view is not None
    assert window.synergy_view is not None
    assert window.open_logs_button.text() == "打开运行日志"
    assert len(window.stage_progress.labels) == 5

    window._select_three_ai_mode()
    selected = {
        name
        for name, row in window.provider_rows.items()
        if row.enabled_check.isChecked()
    }
    assert selected == {"Kimi", "元宝", "豆包", "DeepSeek"}
    assert window.moderator_combo.currentText() == "DeepSeek"
    assert window.judge_combo.currentText() == "Kimi"

    window.provider_rows["模拟分析师"].enabled_check.setChecked(True)
    assert window._sync_provider_config() == ["Kimi", "元宝", "豆包", "DeepSeek"]
    assert not window.provider_rows["模拟分析师"].enabled_check.isChecked()
    window.close()
