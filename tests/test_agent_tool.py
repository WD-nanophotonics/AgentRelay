import json
import os
import subprocess
from pathlib import Path

from dummy_orchestrator.adapters import AGENTRELAY_EXECUTION_CONTRACT


def git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def test_agent_guide_contract_and_json():
    text = subprocess.run(["python", "-m", "dummy_orchestrator.global_cli", "agent-guide"], capture_output=True, text=True, check=True).stdout
    assert "agent-relay deliver" in text and "Do not poll Gmail" in text
    data = json.loads(subprocess.run(["python", "-m", "dummy_orchestrator.global_cli", "agent-guide", "--format", "json"], capture_output=True, text=True, check=True).stdout)
    assert data["completion_command"] == "agent-relay deliver"
    assert "HUMAN_REQUIRED" in data["contract"] or data["stop_condition"] == "HUMAN_REQUIRED"
    assert "AGENTRELAY EXECUTION CONTRACT" in AGENTRELAY_EXECUTION_CONTRACT


def test_global_invocation_and_adopt_identify_from_unrelated_repo(tmp_path: Path):
    root = tmp_path / "project"; root.mkdir(); git(root, "init"); git(root, "config", "user.email", "test@example.test"); git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("scratch", encoding="utf-8"); git(root, "add", "."); git(root, "commit", "-m", "init"); git(root, "branch", "-M", "worker")
    runtime = tmp_path / "runtime"; env = dict(os.environ, LOCALAPPDATA=str(runtime))
    command = ["python", "-m", "dummy_orchestrator.global_cli"]
    adopted = subprocess.run(command + ["adopt", "--project-id", "scratch_project", "--auditor-url", "https://chatgpt.com/c/scratch", "--worker-branch", "worker"], cwd=root, env=env, capture_output=True, text=True, check=True)
    assert "scratch_project" in adopted.stdout and "handoff_package" in adopted.stdout
    identified = subprocess.run(command + ["identify"], cwd=root, env=env, capture_output=True, text=True, check=True)
    assert json.loads(identified.stdout)["managed"] is True
    outside = subprocess.run(command + ["identify"], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert json.loads(outside.stdout)["managed"] is False


def test_supervisor_wake_up_is_empty_without_registered_projects(tmp_path: Path, monkeypatch):
    import dummy_orchestrator.global_cli as cli
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "runtime"))
    assert cli.supervise_once() == []


def test_doctor_reports_transient_gmail_failure_without_traceback(tmp_path: Path, monkeypatch):
    import dummy_orchestrator.global_cli as cli

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "runtime"))

    class BrokenBus:
        def resolve_recipient(self, _worker_email):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(cli, "_bus", lambda: BrokenBus())
    result = cli.doctor(None)
    assert result["gmail"] == "UNAVAILABLE: RuntimeError"
