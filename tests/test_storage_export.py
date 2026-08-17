from __future__ import annotations

import asyncio
import json
import sqlite3

from app.core.enums import DiscussionStage, RunStatus
from app.models import DiscussionRecord, ProjectConfig, UserQuestion
from app.orchestration import EngineEvent, RoundtableEngine
from app.providers import build_default_registry
from app.services import DiscussionExporter
from app.storage import SQLiteStorage


def _completed_record():
    async def scenario():
        registry = build_default_registry()
        names = [provider.name for provider in registry.enabled()]
        return await RoundtableEngine(registry).run(
            UserQuestion(question="持久化与导出测试"), names, names[0], [names[1]]
        )

    return asyncio.run(scenario())


def test_sqlite_save_load_list_and_delete(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "db.sqlite3")
    record = _completed_record()
    storage.save_discussion(record)
    loaded = storage.load_discussion(record.id)
    assert loaded == record
    assert storage.list_discussions()[0]["id"] == record.id
    storage.delete_discussion(record.id)
    assert storage.load_discussion(record.id) is None


def test_discussion_history_can_be_searched_without_changing_records(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "db.sqlite3")
    alpha = DiscussionRecord(
        question=UserQuestion(question="比较两个低风险试点"),
        provider_names=["A", "B"],
        moderator_name="A",
        judge_names=["B"],
    )
    beta = DiscussionRecord(
        question=UserQuestion(question="制定证据审计清单"),
        provider_names=["A", "B"],
        moderator_name="A",
        judge_names=["B"],
    )
    storage.save_discussion(alpha)
    storage.save_discussion(beta)

    matches = storage.list_discussions(query="证据审计")
    assert [item["id"] for item in matches] == [beta.id]
    assert storage.load_discussion(alpha.id) == alpha
    assert storage.load_discussion(beta.id) == beta


def test_sqlite_event_and_config_round_trip(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "db.sqlite3")
    config = ProjectConfig(rounds=2)
    storage.save_config(config)
    assert storage.load_config() == config
    storage.append_event(
        "discussion-1",
        EngineEvent(
            event_type="test",
            message="ok",
            stage=DiscussionStage.INDEPENDENT,
            status=RunStatus.SUCCEEDED,
            payload={"n": 1},
            call_id="call-1",
        ),
    )
    events = storage.list_events("discussion-1")
    assert events[0]["payload"] == {"n": 1}
    assert events[0]["call_id"] == "call-1"


def test_transient_progress_event_is_not_persisted(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "db.sqlite3")
    storage.append_event(
        "discussion-1",
        EngineEvent(
            event_type="provider_progress",
            message="partial",
            stage=DiscussionStage.INDEPENDENT,
            transient=True,
            call_id="call-1",
        ),
    )
    assert storage.list_events("discussion-1") == []


def test_existing_event_table_is_migrated_with_call_id(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE discussion_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discussion_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                stage TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
    SQLiteStorage(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(discussion_events)")}
    assert "call_id" in columns


def test_engine_persists_intermediate_and_final_state(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "db.sqlite3")

    async def scenario():
        registry = build_default_registry()
        names = [provider.name for provider in registry.enabled()]
        return await RoundtableEngine(registry, storage).run(
            UserQuestion(question="引擎持久化测试"), names, names[0], [names[1]]
        )

    record = asyncio.run(scenario())
    restored = storage.load_discussion(record.id)
    assert restored is not None
    assert restored.status == RunStatus.SUCCEEDED
    assert restored.final_synthesis is not None
    assert len(storage.list_events(record.id)) > 5


def test_engine_writes_privacy_safe_per_run_jsonl_log(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "data" / "db.sqlite3")

    async def scenario():
        registry = build_default_registry()
        names = [provider.name for provider in registry.enabled()]
        return await RoundtableEngine(registry, storage).run(
            UserQuestion(question="不应进入日志的完整问题正文"),
            names,
            names[0],
            [names[1]],
        )

    record = asyncio.run(scenario())
    log_path = tmp_path / "logs" / f"run_{record.id}.jsonl"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["event_type"] == "discussion_started"
    assert any(row["event_type"] == "stage_barrier" for row in rows)
    assert rows[-1]["event_type"] == "discussion_completed"
    assert "不应进入日志的完整问题正文" not in text
    assert '"raw"' not in text
    assert '"prompt"' not in text


def test_markdown_and_json_exports_are_complete(tmp_path) -> None:
    record = _completed_record()
    exporter = DiscussionExporter(tmp_path)
    markdown_path = exporter.export_markdown(record)
    json_path = exporter.export_json(record)
    markdown = markdown_path.read_text(encoding="utf-8")
    json_text = json_path.read_text(encoding="utf-8")
    assert "匿名交叉评审" not in markdown or "评审" in markdown
    assert "最终综合" in markdown
    assert "隐私提示" in markdown
    assert record.id in markdown
    assert '"privacy_notice"' in json_text
    assert record.id in json_text
