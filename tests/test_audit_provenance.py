from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import dummy_orchestrator.global_cli as cli
from dummy_orchestrator.audit_provenance import (
    AUDIT_DECISION_CONTRACT,
    STALE_CHAT_CONTEXT_POLICY,
    build_audit_replay_request,
    build_audit_request,
)
from dummy_orchestrator.adapters import HumanRequired
from dummy_orchestrator.models import EventType, ProjectConfig, ProjectState
from dummy_orchestrator.state import StateStore


def identity(version: str = "0.3.10", revision: str = "rev-delivery") -> dict[str, object]:
    return {
        "version": version,
        "git_revision": revision,
        "git_branch": "sandbox",
        "git_dirty": False,
        "source_fingerprint": "fingerprint-0310",
        "package_path": "C:/AgentRelay/src/dummy_orchestrator/global_cli.py",
        "install_source": "C:/AgentRelay",
        "install_mode": "editable/source",
        "version_consistent": True,
        "python_executable": "C:/Python/python.exe",
        "oauth_token": "MUST-NOT-ESCAPE",
    }


def test_audit_request_is_self_contained_and_safe():
    request = build_audit_request(
        project_id="mechanics",
        run_id="RUN-F4",
        round_id="F4",
        active_event_type="TASK",
        active_event_gmail_id="task-gmail-id",
        active_event_payload={"task_id": "F4", "instruction": "inspect and implement"},
        delivery_message_id="delivery-gmail-id",
        delivery_payload={"delivery_sha": "sha", "push_verified": True, "git_revision": "rev-delivery"},
        agentrelay_delivery_identity=identity(),
        agentrelay_audit_transport_identity=identity(revision="rev-transport"),
    )

    provenance = request["AUDIT_PROVENANCE"]
    assert provenance["project"] == {"project_id": "mechanics", "run_id": "RUN-F4", "round_id": "F4"}
    assert provenance["active_instruction_event"]["gmail_message_id"] == "task-gmail-id"
    assert provenance["active_instruction_event"]["payload"]["task_id"] == "F4"
    assert provenance["delivery"]["delivery_message_id"] == "delivery-gmail-id"
    assert provenance["agentrelay_delivery_identity"] == {k: v for k, v in identity().items() if k != "oauth_token"}
    assert request["stale_chat_context_policy"] == STALE_CHAT_CONTEXT_POLICY
    assert request["audit_decision_contract"] == AUDIT_DECISION_CONTRACT
    serialized = json.dumps(request)
    assert "MUST-NOT-ESCAPE" not in serialized
    assert "oauth_token" not in serialized


def test_replay_preserves_delivery_identity_but_records_new_transport():
    original = build_audit_request(
        project_id="mechanics",
        run_id="RUN-F4",
        round_id="F4",
        active_event_type="TASK",
        active_event_gmail_id="task-gmail-id",
        active_event_payload={"task_id": "F4"},
        delivery_message_id="delivery-gmail-id",
        delivery_payload={"delivery_sha": "sha"},
        agentrelay_delivery_identity=identity(revision="rev-original"),
    )["AUDIT_PROVENANCE"]
    replay = build_audit_replay_request(
        original_provenance=original,
        delivery_payload={"delivery_sha": "sha"},
        delivery_message_id="delivery-gmail-id",
        agentrelay_audit_transport_identity=identity(revision="rev-replay"),
    )["AUDIT_PROVENANCE"]

    assert replay["agentrelay_delivery_identity"]["git_revision"] == "rev-original"
    assert replay["agentrelay_audit_transport_identity"]["git_revision"] == "rev-replay"
    assert replay["active_instruction_event"] == original["active_instruction_event"]


def test_state_migrates_active_payload_and_delivery_provenance(tmp_path: Path):
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE projects (
              project_id TEXT PRIMARY KEY, run_id TEXT, round_id TEXT, state TEXT NOT NULL,
              worker_session_id TEXT, last_gmail_id TEXT, last_transition_at TEXT, error TEXT,
              codex_path TEXT, codex_version TEXT, codex_selection_reason TEXT,
              chrome_path TEXT, chrome_version TEXT, chrome_selection_reason TEXT, cdp_endpoint TEXT,
              repository_root TEXT, git_remote TEXT, worker_branch TEXT, protected_branches TEXT,
              persistent_codex_session TEXT, current_thread_id TEXT,
              active_event_round_id TEXT, active_event_type TEXT, active_event_gmail_id TEXT
            );
            CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
              run_id TEXT NOT NULL, round_id TEXT NOT NULL, audited_at TEXT);
            """
        )
    store = StateStore(db)
    payload = {"task_id": "F4", "instruction": "canonical"}
    store.transition("mechanics", ProjectState.TASK_READY, active_event_round_id="F4",
                     active_event_type="TASK", active_event_gmail_id="task-gmail-id",
                     active_event_payload_json=json.dumps(payload))
    provenance = {"schema_version": 1, "project": {"project_id": "mechanics"}}
    assert store.mark_delivery_once("delivery-gmail-id", "mechanics", "RUN-F4", "F4", provenance=provenance)
    assert json.loads(store.project("mechanics")["active_event_payload_json"]) == payload
    assert store.delivery_provenance("delivery-gmail-id") == provenance


def test_global_deliver_fails_closed_on_round_mismatch(tmp_path: Path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite")
    store.transition("mechanics", ProjectState.WAITING_FOR_DELIVERY, run_id="RUN-F4", round_id="F4",
                     active_event_round_id="F4", active_event_type=str(EventType.TASK),
                     active_event_gmail_id="task-gmail-id", active_event_payload_json="{}")
    project = ProjectConfig("mechanics", "production", "C:/workspace", "https://chatgpt.com/c/test",
                            "AgentRelay", "worker@example.com", 10)
    registration = SimpleNamespace(mode="production", project_id="mechanics", to_project_config=lambda: project)
    monkeypatch.setattr(cli, "_resolve", lambda _args: registration)
    monkeypatch.setattr(cli, "_state", lambda: store)
    monkeypatch.setattr(cli, "_bus", lambda: SimpleNamespace(resolve_recipient=lambda email: email))
    args = SimpleNamespace(project_id="mechanics", phase_id="F5", round_id=None, task_id=None,
                           baseline_sha=None, note=None, note_file=None)
    with pytest.raises(HumanRequired, match="active_event_round_id"):
        cli.deliver(args)
