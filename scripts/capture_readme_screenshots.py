from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase

from app.models import DiscussionRecord, ProjectConfig, UserQuestion
from app.orchestration import RoundtableEngine
from app.providers import build_default_registry
from app.ui import MainWindow


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIRECTORY = PROJECT_ROOT / "docs" / "images"


class ScreenshotStorage:
    """Small in-memory store so documentation capture never touches user data."""

    path = OUTPUT_DIRECTORY / ".screenshot-storage.sqlite3"

    def __init__(self) -> None:
        self.config: ProjectConfig | None = None
        self.records: dict[str, DiscussionRecord] = {}

    def load_config(self) -> ProjectConfig | None:
        return self.config

    def save_config(self, config: ProjectConfig) -> None:
        self.config = config

    def save_discussion(self, record: DiscussionRecord) -> None:
        self.records[record.id] = record

    def load_discussion(self, discussion_id: str) -> DiscussionRecord | None:
        return self.records.get(discussion_id)

    def list_discussions(self, limit: int = 100, *, query: str = "") -> list[dict[str, Any]]:
        needle = " ".join(query.lower().split())
        records = sorted(self.records.values(), key=lambda item: item.updated_at, reverse=True)
        result = []
        for record in records:
            searchable = f"{record.title} {record.question.question} {record.status.value}".lower()
            if needle and needle not in searchable:
                continue
            result.append(
                {
                    "id": record.id,
                    "updated_at": record.updated_at.isoformat(),
                    "title": record.title,
                }
            )
        return result[:limit]

    def delete_discussion(self, discussion_id: str) -> None:
        self.records.pop(discussion_id, None)

    def append_event(self, *_args: object, **_kwargs: object) -> None:
        return None


def _save_window(window: MainWindow, path: Path) -> None:
    window.resize(1600, 1080)
    window.show()
    application = QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication has not been created")
    for _ in range(5):
        application.processEvents()
    screenshot = window.grab()
    if screenshot.isNull() or not screenshot.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save screenshot: {path}")
    window.close()
    application.processEvents()


async def _build_demo_record():
    registry = build_default_registry()
    mock_names = [provider.name for provider in registry.mocks()]
    record = await RoundtableEngine(registry).run(
        UserQuestion(
            question="如何用两周设计一个低风险、可回滚的产品试点？",
            background="团队 4 人，已有一百名种子用户。",
            constraints="预算有限；必须给出量化指标、停止条件与回滚方案。",
            template_name="执行决策",
        ),
        mock_names,
        "模拟分析师",
        ["模拟质疑者"],
        rounds=1,
        anonymous_review=True,
        enable_revision=True,
    )
    fixed_time = datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc)
    record.created_at = fixed_time
    record.updated_at = fixed_time
    record.question.created_at = fixed_time
    return record


def main() -> int:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    font_families = QFontDatabase.applicationFontFamilies(font_id)
    if font_families:
        application.setFont(QFont(font_families[0], 9))

    ready_window = MainWindow(
        PROJECT_ROOT,
        build_default_registry(),
        ScreenshotStorage(),
    )
    ready_window.question_edit.setPlainText(
        "如何让多个 AI 形成互补结论，而不是简单重复彼此？"
    )
    ready_window.strategy_combo.setCurrentText("红队压力测试")
    _save_window(ready_window, OUTPUT_DIRECTORY / "workbench.png")

    record = asyncio.run(_build_demo_record())
    result_storage = ScreenshotStorage()
    result_storage.save_discussion(record)
    result_window = MainWindow(
        PROJECT_ROOT,
        build_default_registry(),
        result_storage,
    )
    result_window._select_offline_mode()
    result_window.history_list.setCurrentRow(0)
    result_window._load_selected_history()
    result_window.insight_tabs.setCurrentIndex(0)
    _save_window(result_window, OUTPUT_DIRECTORY / "offline-result.png")

    application.quit()
    print(f"Saved README screenshots to {OUTPUT_DIRECTORY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
