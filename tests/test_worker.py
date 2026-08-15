from pathlib import Path
from types import SimpleNamespace

import pytest

from dummy_orchestrator.adapters import CodexWorkerAdapter, HumanRequired
from dummy_orchestrator.models import ProjectConfig


def test_unexecutable_candidate_is_rejected(monkeypatch, tmp_path: Path):
    worker = CodexWorkerAdapter(base_dir=tmp_path)
    monkeypatch.setattr(worker, "_candidates", lambda: [(tmp_path / "bad-codex", "test candidate")])
    monkeypatch.setattr(worker, "_validate_candidate", lambda *_: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(HumanRequired, match="codex_cli_execution"):
        worker.preflight()


def test_working_candidate_is_selected(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "codex.cmd"
    candidate.write_text("placeholder")
    worker = CodexWorkerAdapter(base_dir=tmp_path)
    worker.persist_path = tmp_path / "codex_cli.json"
    monkeypatch.setattr(worker, "_candidates", lambda: [(candidate, "test working candidate")])
    monkeypatch.setattr(worker, "_validate_candidate", lambda *_: (["node", "codex.js"], "codex-cli 0.147.0"))
    monkeypatch.setattr("dummy_orchestrator.adapters.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout="resume --json\n", stderr=""))
    result = worker.preflight()
    assert result["path"] == str(candidate)
    assert result["selection_reason"] == "test working candidate"


def test_session_id_is_returned_and_resume_reuses_it(monkeypatch, tmp_path: Path):
    project = ProjectConfig("dummy", "dummy", str(tmp_path), "https://chatgpt.com/c/dummy", "[DUMMY]", "worker@example.test", 3)
    worker = CodexWorkerAdapter(base_dir=tmp_path)
    worker.command = ["node", "codex.js"]
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "resume" in args:
            return SimpleNamespace(stdout='{"type":"turn.completed"}\n', stderr="")
        return SimpleNamespace(stdout='{"type":"thread.started","thread_id":"session-1"}\n', stderr="")

    monkeypatch.setattr("dummy_orchestrator.adapters.subprocess.run", fake_run)
    first = worker.run_instruction(project, "Turn A", None)
    second = worker.run_instruction(project, "Turn B", first)
    assert first == second == "session-1"
    assert calls[1][2:5] == ["exec", "resume", "session-1"]
