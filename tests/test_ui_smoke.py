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
    assert window._sync_provider_config() == ["模拟分析师"]
    assert all(
        not window.provider_rows[name].enabled_check.isChecked()
        for name in online_names
    )
    window.close()


def test_interactive_controls_click_immediately_and_persist(tmp_path) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    storage = SQLiteStorage(tmp_path / "data/db.sqlite3")
    window = MainWindow(tmp_path, build_default_registry(), storage)
    window.show()
    app.processEvents()

    assert not window.history_load_button.isEnabled()
    assert not window.history_delete_button.isEnabled()
    assert not window.copy_result_button.isEnabled()
    assert not window.export_markdown_button.isEnabled()
    assert not window.export_json_button.isEnabled()

    QTest.mouseClick(window.offline_mode_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    mock_names = {"模拟分析师", "模拟质疑者", "模拟执行顾问"}
    selected = {
        name
        for name, row in window.provider_rows.items()
        if row.enabled_check.isChecked()
    }
    assert selected == mock_names
    assert window.moderator_combo.currentText() == "模拟分析师"
    assert window.judge_combo.currentText() == "模拟质疑者"

    participant_check = window.provider_rows["模拟执行顾问"].enabled_check
    QTest.mouseClick(participant_check, Qt.MouseButton.LeftButton)
    assert not participant_check.isChecked()
    QTest.mouseClick(participant_check, Qt.MouseButton.LeftButton)
    assert participant_check.isChecked()

    QTest.mouseClick(window.anonymous_check, Qt.MouseButton.LeftButton)
    QTest.mouseClick(window.revision_check, Qt.MouseButton.LeftButton)
    QTest.mouseClick(window.context_toggle, Qt.MouseButton.LeftButton)
    window.rounds_spin.setValue(2)
    app.processEvents()
    assert not window.anonymous_check.isChecked()
    assert not window.revision_check.isChecked()
    assert window.context_panel.isVisible()
    assert window.preference_status.text() == "设置已自动保存"

    saved = storage.load_config()
    assert saved is not None
    assert saved.rounds == 2
    assert not saved.anonymous_review
    assert not saved.enable_revision
    assert saved.moderator_name == "模拟分析师"
    assert saved.judge_name == "模拟质疑者"
    assert {item.name for item in saved.providers if item.enabled} == mock_names

    window._set_configuration_enabled(False)
    assert not window.anonymous_check.isEnabled()
    assert not window.provider_rows["模拟分析师"].enabled_check.isEnabled()
    assert not window.offline_mode_button.isEnabled()
    assert not window.history_list.isEnabled()
    window._set_configuration_enabled(True)
    assert window.anonymous_check.isEnabled()
    assert window.provider_rows["模拟分析师"].enabled_check.isEnabled()
    window.close()

    restored = MainWindow(tmp_path, build_default_registry(), storage)
    assert restored.rounds_spin.value() == 2
    assert not restored.anonymous_check.isChecked()
    assert not restored.revision_check.isChecked()
    assert restored.moderator_combo.currentText() == "模拟分析师"
    assert restored.judge_combo.currentText() == "模拟质疑者"
    assert {
        name
        for name, row in restored.provider_rows.items()
        if row.enabled_check.isChecked()
    } == mock_names
    restored.close()
