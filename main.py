from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from app.models import UserQuestion
from app.core.enums import ProviderKind
from app.orchestration import RoundtableEngine
from app.providers import build_default_registry
from app.services import DiscussionExporter
from app.storage import SQLiteStorage
from app.utils.logging import configure_logging


PROJECT_ROOT = Path(__file__).resolve().parent


def ensure_directories() -> None:
    for relative in ("data", "exports", "logs", "browser_profiles"):
        (PROJECT_ROOT / relative).mkdir(parents=True, exist_ok=True)


async def run_demo(question: str) -> int:
    ensure_directories()
    registry = build_default_registry()
    storage = SQLiteStorage(PROJECT_ROOT / "data/roundtable.sqlite3")
    engine = RoundtableEngine(registry, storage)
    names = [
        provider.name
        for provider in registry.all()
        if provider.config.kind == ProviderKind.MOCK
    ]
    record = await engine.run(
        UserQuestion(question=question),
        names,
        moderator_name=names[0],
        judge_names=[names[1]],
    )
    exporter = DiscussionExporter(PROJECT_ROOT / "exports")
    markdown = exporter.export_markdown(record)
    json_path = exporter.export_json(record)
    print(f"状态：{record.status.value}")
    print(f"Markdown：{markdown}")
    print(f"JSON：{json_path}")
    if record.final_synthesis:
        print(f"结论：{record.final_synthesis.recommendation}")
    await registry.close_all()
    return 0 if record.status.value == "succeeded" else 1


def run_gui(smoke_test: bool = False) -> int:
    ensure_directories()
    if smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from app.ui import MainWindow
    except ImportError as exc:
        print("缺少 PySide6。请运行：python -m pip install -r requirements.txt", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI Roundtable")
    registry = build_default_registry()
    storage = SQLiteStorage(PROJECT_ROOT / "data/roundtable.sqlite3")
    window = MainWindow(PROJECT_ROOT, registry, storage)
    window.show()
    if smoke_test:
        QTimer.singleShot(250, window.close)
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Roundtable 本地多 AI 圆桌")
    parser.add_argument("--demo", metavar="QUESTION", help="无界面运行完整离线圆桌并导出结果")
    parser.add_argument("--smoke-test", action="store_true", help="无头启动并关闭 UI，用于安装验证")
    args = parser.parse_args()
    ensure_directories()
    logger = configure_logging(PROJECT_ROOT / "logs")

    def log_unhandled_exception(exc_type, exc_value, traceback) -> None:
        logger.critical(
            "unhandled_exception type=%s message=%s",
            getattr(exc_type, "__name__", str(exc_type)),
            exc_value,
            exc_info=(exc_type, exc_value, traceback),
        )
        sys.__excepthook__(exc_type, exc_value, traceback)

    sys.excepthook = log_unhandled_exception
    logger.info("application_start mode=%s", "demo" if args.demo else "smoke" if args.smoke_test else "gui")
    if args.demo:
        return asyncio.run(run_demo(args.demo))
    return run_gui(args.smoke_test)


if __name__ == "__main__":
    raise SystemExit(main())
