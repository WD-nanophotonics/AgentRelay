from __future__ import annotations

from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

import pytest

from dummy_orchestrator import global_cli
from dummy_orchestrator.adapters import HumanRequired
from dummy_orchestrator.registry import ProjectRegistration, ProjectRegistrationStore


OLD_URL = "https://chatgpt.com/c/old-conversation"
NEW_URL = "https://chatgpt.com/c/6a814597-5c2c-83e8-99b4-25d4701e224e"


def registration() -> ProjectRegistration:
    return ProjectRegistration(
        project_id="mechanics_test",
        auditor_chat_url=OLD_URL,
        repository_root="C:\\isolated\\mechanics",
        git_remote="https://github.com/example/mechanics.git",
        worker_branch="worker",
        repository_identity="https://github.com/example/mechanics.git",
        head_sha="abc123",
        protected_branches=("main", "master", "develop"),
        persistent_codex_session="worker-session",
        mode="production",
        gmail_subject_prefix="[ORCH]",
        worker_email="worker@example.test",
        max_rounds=7,
    )


def cli_args(**overrides: object) -> Namespace:
    values = {"project_id": "mechanics_test", "worker_email": None, "auditor_url": NEW_URL}
    values.update(overrides)
    return Namespace(**values)


def test_registered_project_auditor_url_can_be_rebound_atomically(tmp_path: Path, monkeypatch):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    original = registration(); store.register(original)
    monkeypatch.setattr(global_cli, "_store", lambda: store)

    result = global_cli.configure_project(cli_args())
    updated = store.get(original.project_id)

    assert result == {
        "project_id": original.project_id,
        "old_auditor_url": OLD_URL,
        "new_auditor_url": NEW_URL,
        "repository_root": original.repository_root,
        "worker_branch": original.worker_branch,
        "changed": True,
        "unchanged": False,
    }
    assert updated.auditor_chat_url == NEW_URL
    preserved = asdict(original)
    preserved["auditor_chat_url"] = NEW_URL
    assert asdict(updated) == preserved


def test_same_auditor_url_is_idempotent_without_extra_write(tmp_path: Path, monkeypatch):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    original = registration(); store.register(original)
    monkeypatch.setattr(global_cli, "_store", lambda: store)
    before = store.path.read_bytes()

    result = global_cli.configure_project(cli_args(auditor_url=OLD_URL))

    assert result["changed"] is False
    assert result["unchanged"] is True
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("url", [
    "http://chatgpt.com/c/not-https",
    "https://evil.example/c/conversation",
    "https://chatgpt.com/not-a-conversation",
])
def test_invalid_auditor_url_is_rejected(tmp_path: Path, monkeypatch, url: str):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    store.register(registration())
    monkeypatch.setattr(global_cli, "_store", lambda: store)

    with pytest.raises(HumanRequired, match="auditor_chat_url"):
        global_cli.configure_project(cli_args(auditor_url=url))


def test_unknown_project_is_rejected_before_rebind(tmp_path: Path, monkeypatch):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    monkeypatch.setattr(global_cli, "_store", lambda: store)

    with pytest.raises(HumanRequired, match="registered_project"):
        global_cli.configure_project(cli_args(project_id="unknown_project"))


def test_rebind_does_not_touch_runtime_state(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    store = ProjectRegistrationStore(runtime)
    original = registration(); store.register(original)
    state_marker = runtime / "state" / "marker.txt"
    state_marker.parent.mkdir(parents=True)
    state_marker.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(global_cli, "_store", lambda: store)

    global_cli.configure_project(cli_args())

    assert state_marker.read_text(encoding="utf-8") == "unchanged"
    assert not (runtime / "secrets").exists()


def test_register_still_rejects_conflicting_auditor_mapping(tmp_path: Path):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    original = registration(); store.register(original)
    conflicting = ProjectRegistration(**{**asdict(original), "auditor_chat_url": NEW_URL})

    with pytest.raises(HumanRequired, match="duplicate_project_id"):
        store.register(conflicting)


def test_adopt_still_rejects_conflicting_auditor_mapping(tmp_path: Path, monkeypatch):
    store = ProjectRegistrationStore(tmp_path / "runtime")
    original = registration(); store.register(original)
    conflicting = ProjectRegistration(**{**asdict(original), "auditor_chat_url": NEW_URL})
    monkeypatch.setattr(global_cli, "_store", lambda: store)
    monkeypatch.setattr(global_cli, "discover_registration", lambda *args, **kwargs: conflicting)
    args = Namespace(
        project_id=original.project_id,
        auditor_url=NEW_URL,
        worker_branch=None,
        protected_branch=["main", "master", "develop"],
        mode="dummy",
        worker_email="",
        bootstrap=False,
    )

    with pytest.raises(HumanRequired, match="duplicate_project_id"):
        global_cli.adopt(args)
