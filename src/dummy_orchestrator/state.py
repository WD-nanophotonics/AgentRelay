from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import ProjectState


def now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._initialize()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _initialize(self) -> None:
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
              project_id TEXT PRIMARY KEY, run_id TEXT, round_id TEXT, state TEXT NOT NULL,
              worker_session_id TEXT, last_gmail_id TEXT, last_transition_at TEXT, error TEXT,
              codex_path TEXT, codex_version TEXT, codex_selection_reason TEXT,
              chrome_path TEXT, chrome_version TEXT, chrome_selection_reason TEXT, cdp_endpoint TEXT,
              repository_root TEXT, git_remote TEXT, worker_branch TEXT, protected_branches TEXT,
              persistent_codex_session TEXT, current_thread_id TEXT,
              active_event_round_id TEXT, active_event_type TEXT, active_event_gmail_id TEXT,
              active_event_payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS processed_messages (
              gmail_message_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
              delivery_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
              round_id TEXT NOT NULL, audited_at TEXT, provenance_json TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_requests (
              delivery_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
              round_id TEXT NOT NULL, submitted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transitions (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, at TEXT NOT NULL, previous_state TEXT,
              next_state TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostics (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, at TEXT NOT NULL,
              code TEXT NOT NULL, gmail_message_id TEXT, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbound_events (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
              round_id TEXT NOT NULL, event_type TEXT NOT NULL, gmail_message_id TEXT,
              payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_replays (
              delivery_sha TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
              round_id TEXT NOT NULL, submitted_at TEXT NOT NULL
            );
            """)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(projects)")}
            for name in ("codex_path", "codex_version", "codex_selection_reason", "chrome_path", "chrome_version", "chrome_selection_reason", "cdp_endpoint", "repository_root", "git_remote", "worker_branch", "protected_branches", "persistent_codex_session", "current_thread_id", "active_event_round_id", "active_event_type", "active_event_gmail_id", "active_event_payload_json"):
                if name not in columns: con.execute(f"ALTER TABLE projects ADD COLUMN {name} TEXT")
            delivery_columns = {row["name"] for row in con.execute("PRAGMA table_info(deliveries)")}
            if "provenance_json" not in delivery_columns:
                con.execute("ALTER TABLE deliveries ADD COLUMN provenance_json TEXT")

    def ensure_project(self, project_id: str) -> None:
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO projects(project_id, state) VALUES (?, ?)", (project_id, ProjectState.IDLE))

    def project(self, project_id: str) -> dict:
        self.ensure_project(project_id)
        with self.connect() as con:
            return dict(con.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone())

    def transition(self, project_id: str, state: ProjectState, detail: dict | None = None, **updates: str | None) -> None:
        current = self.project(project_id)
        at = now()
        allowed = {"run_id", "round_id", "worker_session_id", "last_gmail_id", "error", "codex_path", "codex_version", "codex_selection_reason", "chrome_path", "chrome_version", "chrome_selection_reason", "cdp_endpoint", "repository_root", "git_remote", "worker_branch", "protected_branches", "persistent_codex_session", "current_thread_id", "active_event_round_id", "active_event_type", "active_event_gmail_id", "active_event_payload_json"}
        if unknown := set(updates) - allowed:
            raise ValueError(f"Unsupported project fields: {unknown}")
        fields = {"state": state, "last_transition_at": at, **updates}
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as con:
            con.execute(f"UPDATE projects SET {assignments} WHERE project_id=?", (*fields.values(), project_id))
            con.execute(
                "INSERT INTO transitions(project_id, at, previous_state, next_state, detail) VALUES (?, ?, ?, ?, ?)",
                (project_id, at, current["state"], state, json.dumps(detail or {}, sort_keys=True)),
            )

    def mark_processed(self, gmail_message_id: str, project_id: str) -> bool:
        with self.connect() as con:
            cursor = con.execute(
                "INSERT OR IGNORE INTO processed_messages(gmail_message_id, project_id, processed_at) VALUES (?, ?, ?)",
                (gmail_message_id, project_id, now()),
            )
            return cursor.rowcount == 1

    def requeue_processed(self, gmail_message_id: str, project_id: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM processed_messages WHERE gmail_message_id=? AND project_id=?", (gmail_message_id, project_id))

    def is_processed(self, gmail_message_id: str, project_id: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM processed_messages WHERE gmail_message_id=? AND project_id=?", (gmail_message_id, project_id)).fetchone() is not None

    def mark_delivery_once(self, delivery_id: str, project_id: str, run_id: str, round_id: str, provenance: dict | None = None) -> bool:
        with self.connect() as con:
            cursor = con.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id, project_id, run_id, round_id, provenance_json) VALUES (?, ?, ?, ?, ?)",
                (delivery_id, project_id, run_id, round_id, json.dumps(provenance, sort_keys=True) if provenance is not None else None),
            )
            return cursor.rowcount == 1

    def delivery_provenance(self, delivery_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT provenance_json FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if not row or not row["provenance_json"]:
            return None
        try:
            value = json.loads(row["provenance_json"])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def latest_delivery(self, project_id: str, run_id: str, round_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM deliveries WHERE project_id=? AND run_id=? AND round_id=? ORDER BY rowid DESC LIMIT 1",
                (project_id, run_id, round_id),
            ).fetchone()
            return dict(row) if row else None

    def mark_audit_once(self, delivery_id: str, project_id: str, run_id: str, round_id: str) -> bool:
        with self.connect() as con:
            cursor = con.execute(
                "INSERT OR IGNORE INTO audit_requests(delivery_id, project_id, run_id, round_id, submitted_at) VALUES (?, ?, ?, ?, ?)",
                (delivery_id, project_id, run_id, round_id, now()),
            )
            return cursor.rowcount == 1

    def history(self, project_id: str) -> list[dict]:
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM transitions WHERE project_id=? ORDER BY id", (project_id,))]

    def record_diagnostic(self, project_id: str, code: str, gmail_message_id: str | None, detail: dict | str) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO diagnostics(project_id, at, code, gmail_message_id, detail) VALUES (?, ?, ?, ?, ?)",
                        (project_id, now(), code, gmail_message_id, json.dumps(detail, sort_keys=True) if isinstance(detail, dict) else str(detail)))

    def diagnostics(self, project_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM diagnostics WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))]

    def record_outbound_event(self, project_id: str, run_id: str, round_id: str, event_type, message_id: str | None, payload: dict) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO outbound_events(project_id, run_id, round_id, event_type, gmail_message_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (project_id, run_id, round_id, str(event_type), message_id, json.dumps(payload, sort_keys=True), now()))

    def outbound_exists(self, project_id: str, run_id: str, round_id: str, event_type) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM outbound_events WHERE project_id=? AND run_id=? AND round_id=? AND event_type=? LIMIT 1",
                               (project_id, run_id, round_id, str(event_type))).fetchone() is not None

    def mark_audit_replay_once(self, delivery_sha: str, project_id: str, run_id: str, round_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("INSERT OR IGNORE INTO audit_replays(delivery_sha, project_id, run_id, round_id, submitted_at) VALUES (?, ?, ?, ?, ?)", (delivery_sha, project_id, run_id, round_id, now()))
            return cur.rowcount == 1
