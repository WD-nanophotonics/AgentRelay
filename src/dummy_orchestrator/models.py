from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProjectState(StrEnum):
    IDLE = "IDLE"
    WAITING_FOR_INSTRUCTION = "WAITING_FOR_INSTRUCTION"
    TASK_READY = "TASK_READY"
    CORRECTIVE_READY = "CORRECTIVE_READY"
    WORKER_RUNNING = "WORKER_RUNNING"
    WAITING_FOR_DELIVERY = "WAITING_FOR_DELIVERY"
    AUDIT_PENDING = "AUDIT_PENDING"
    AUDITOR_RUNNING = "AUDITOR_RUNNING"
    WAITING_FOR_AUDITOR_GMAIL = "WAITING_FOR_AUDITOR_GMAIL"
    WORKER_BLOCKED = "WORKER_BLOCKED"
    CORRECTIVE_PENDING = "CORRECTIVE_PENDING"
    COMPLETE = "COMPLETE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class EventType(StrEnum):
    TASK = "TASK"
    CORRECTIVE = "CORRECTIVE"
    DELIVERY = "DELIVERY"
    DONE = "DONE"
    TERMINAL_CONTROL = "TERMINAL_CONTROL"


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    mode: str
    local_workspace: str
    auditor_chat_url: str
    gmail_subject_prefix: str
    worker_email: str
    max_rounds: int
    repository_root: str = ""
    git_remote: str = "origin"
    worker_branch: str = ""
    protected_branches: tuple[str, ...] = ("main", "master", "develop")
    persistent_codex_session: str | None = None


@dataclass(frozen=True)
class OrchestratorEvent:
    project_id: str
    run_id: str
    round_id: str
    event_type: EventType
    payload: dict[str, Any]
    gmail_message_id: str | None = None

    def validate(self) -> None:
        if not all((self.project_id, self.run_id, self.round_id, self.event_type)):
            raise ValueError("project_id, run_id, round_id, and event_type are required")
