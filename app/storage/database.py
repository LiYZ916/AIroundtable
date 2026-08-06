from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.models import DiscussionRecord, ProjectConfig
from app.orchestration.events import EngineEvent


class SQLiteStorage:
    """Small SQLite repository storing complete Pydantic snapshots."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS discussions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    title TEXT NOT NULL,
                    question TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_discussions_updated
                    ON discussions(updated_at DESC);

                CREATE TABLE IF NOT EXISTS discussion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discussion_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    call_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_discussion
                    ON discussion_events(discussion_id, id);

                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS manual_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discussion_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(discussion_events)").fetchall()
            }
            if "call_id" not in columns:
                connection.execute(
                    "ALTER TABLE discussion_events ADD COLUMN call_id TEXT NOT NULL DEFAULT ''"
                )

    def save_discussion(self, record: DiscussionRecord) -> None:
        payload = record.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO discussions(
                    id, created_at, updated_at, status, stage, title, question, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    stage=excluded.stage,
                    title=excluded.title,
                    question=excluded.question,
                    payload_json=excluded.payload_json
                """,
                (
                    record.id,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    str(record.status.value),
                    str(record.current_stage.value),
                    record.title,
                    record.question.question,
                    payload,
                ),
            )

    def load_discussion(self, discussion_id: str) -> DiscussionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM discussions WHERE id = ?", (discussion_id,)
            ).fetchone()
        return DiscussionRecord.model_validate_json(row["payload_json"]) if row else None

    def list_discussions(self, limit: int = 100) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, updated_at, status, stage, title, question
                FROM discussions ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_discussion(self, discussion_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM discussion_events WHERE discussion_id = ?", (discussion_id,)
            )
            connection.execute(
                "DELETE FROM manual_responses WHERE discussion_id = ?", (discussion_id,)
            )
            connection.execute("DELETE FROM discussions WHERE id = ?", (discussion_id,))

    def append_event(self, discussion_id: str, event: EngineEvent) -> None:
        if not discussion_id or event.transient:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO discussion_events(
                    discussion_id, created_at, event_type, stage,
                    provider_name, status, call_id, message, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    discussion_id,
                    event.created_at.isoformat(),
                    event.event_type,
                    event.stage.value,
                    event.provider_name,
                    event.status.value,
                    event.call_id,
                    event.message,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                ),
            )

    def list_events(self, discussion_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM discussion_events WHERE discussion_id = ? ORDER BY id",
                (discussion_id,),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return events

    def save_config(self, config: ProjectConfig) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_config(key, value_json) VALUES('project', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP
                """,
                (config.model_dump_json(),),
            )

    def load_config(self) -> ProjectConfig | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_config WHERE key = 'project'"
            ).fetchone()
        return ProjectConfig.model_validate_json(row["value_json"]) if row else None

    def save_manual_response(
        self, discussion_id: str, provider_name: str, stage: str, text: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_responses(
                    discussion_id, provider_name, stage, response_text
                ) VALUES (?, ?, ?, ?)
                """,
                (discussion_id, provider_name, stage, text),
            )

    def clear_runtime_data(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM discussion_events")
            connection.execute("DELETE FROM manual_responses")
            connection.execute("DELETE FROM discussions")
            connection.execute("DELETE FROM app_config")
