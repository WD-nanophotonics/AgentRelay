from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path


class StructuredLog:
    def __init__(self, path: Path):
        self.path = path; path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: object) -> None:
        record = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


class ProjectLock(AbstractContextManager):
    """Host-local, project-scoped single-run lock with no global project state."""
    def __init__(self, root: Path, project_id: str):
        self.path = root / "locks" / f"{project_id}.lock"; self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"Project {self.path.stem} is already running") from exc
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd)
        self.path.unlink(missing_ok=True)

