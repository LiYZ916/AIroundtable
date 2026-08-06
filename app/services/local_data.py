from __future__ import annotations

import shutil
from pathlib import Path

from app.storage import SQLiteStorage


class LocalDataManager:
    """Deletes only known runtime directories inside the project root."""

    RUNTIME_DIRECTORIES = ("browser_profiles", "exports", "logs")

    def __init__(self, project_root: str | Path, storage: SQLiteStorage) -> None:
        self.project_root = Path(project_root).resolve()
        self.storage = storage

    def clear_all(self) -> list[Path]:
        removed: list[Path] = []
        self.storage.clear_runtime_data()
        for relative in self.RUNTIME_DIRECTORIES:
            target = (self.project_root / relative).resolve()
            if target.parent != self.project_root:
                raise RuntimeError(f"拒绝清理项目外路径：{target}")
            if target.exists():
                for child in target.iterdir():
                    if child.name == ".gitkeep":
                        continue
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    removed.append(child)
            target.mkdir(parents=True, exist_ok=True)
        return removed

