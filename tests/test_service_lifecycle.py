from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import dummy_orchestrator.global_cli as cli
from dummy_orchestrator.adapters import HumanRequired


def service_args(action: str) -> argparse.Namespace:
    return argparse.Namespace(action=action)


def service_files(runtime: Path) -> dict[str, Path]:
    root = runtime / "CodexOrchestrator" / "service"
    return {name: root / filename for name, filename in {
        "pid": "supervisor.pid",
        "ready": "supervisor.ready.json",
        "heartbeat": "supervisor.heartbeat.json",
        "stop": "supervisor.stop.json",
        "lock": "supervisor.start.lock",
    }.items()}


def owned_record(pid: int, generation: str = "test-generation") -> dict[str, object]:
    return {"schema": 1, "owner": cli.SERVICE_OWNER, "pid": pid, "generation": generation}


def test_t1_stale_dead_pid_is_cleaned_then_real_supervisor_starts(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    files = service_files(runtime)
    files["pid"].parent.mkdir(parents=True)
    record = owned_record(4_000_000_000)
    files["pid"].write_text(json.dumps(record), encoding="utf-8")
    for key in ("ready", "heartbeat"):
        files[key].write_text(json.dumps(record | {"heartbeat_epoch": time.time()}), encoding="utf-8")

    result = cli.service(service_args("status"))

    assert result == {"running": False, "pid": None}
    assert not files["pid"].exists()
    assert not files["ready"].exists()
    assert not files["heartbeat"].exists()

    started = cli.service(service_args("ensure"))
    assert started["running"] is True
    assert started["pid"] != record["pid"]
    assert cli.service(service_args("status"))["running"] is True
    cli.service(service_args("stop"))


def test_ownership_live_foreign_pid_is_never_overwritten_or_stopped(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    files = service_files(runtime)
    files["pid"].parent.mkdir(parents=True)
    foreign = {"pid": os.getpid(), "owner": "unrelated-process", "generation": "foreign"}
    files["pid"].write_text(json.dumps(foreign), encoding="utf-8")

    with pytest.raises(HumanRequired, match="supervisor_ownership"):
        cli.service(service_args("start"))
    with pytest.raises(HumanRequired, match="supervisor_ownership"):
        cli.service(service_args("stop"))
    assert json.loads(files["pid"].read_text(encoding="utf-8")) == foreign


def test_t2_immediate_death_does_not_report_running(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    monkeypatch.setattr(cli, "SERVICE_START_TIMEOUT_SECONDS", 0.01)

    class ExitedChild:
        pid = 45678

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(cli, "spawn_background", lambda *args, **kwargs: ExitedChild())
    with pytest.raises(HumanRequired, match="supervisor_readiness"):
        cli.service(service_args("start"))
    assert not (runtime / "CodexOrchestrator" / "service" / "supervisor.pid").exists()


def test_t3_normal_clean_start(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    started = cli.service(service_args("start"))
    assert started["running"] is True
    assert cli.service(service_args("status"))["running"] is True
    cli.service(service_args("stop"))


def test_concurrent_start_lock_has_bounded_failure(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    files = service_files(runtime)
    files["lock"].parent.mkdir(parents=True)
    files["lock"].write_text(json.dumps({"pid": os.getpid(), "created_epoch": time.time()}), encoding="utf-8")
    monkeypatch.setattr(cli, "SERVICE_START_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(HumanRequired, match="supervisor_start_lock"):
        cli.service(service_args("start"))
    assert files["lock"].exists()


def test_t7_service_start_preserves_detached_hidden_policy(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    monkeypatch.setattr(cli, "SERVICE_START_TIMEOUT_SECONDS", 0.01)
    seen: dict[str, object] = {}

    class ExitedChild:
        pid = 56789

        @staticmethod
        def poll():
            return 1

    def fake_spawn(*args, **kwargs):
        seen.update(kwargs)
        return ExitedChild()

    monkeypatch.setattr(cli, "spawn_background", fake_spawn)
    with pytest.raises(HumanRequired, match="supervisor_readiness"):
        cli.service(service_args("start"))
    assert seen["detached"] is True
    assert seen["close_fds"] is True
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL


def test_t5_status_false_and_cleans_artifacts_after_crash(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    files = service_files(runtime)
    files["pid"].parent.mkdir(parents=True)
    record = owned_record(4_000_000_001)
    files["pid"].write_text(json.dumps(record), encoding="utf-8")
    files["ready"].write_text(json.dumps(record | {"ready_epoch": time.time()}), encoding="utf-8")
    files["heartbeat"].write_text(json.dumps(record | {"heartbeat_epoch": time.time()}), encoding="utf-8")

    assert cli.service(service_args("status"))["running"] is False
    assert all(not path.exists() for path in (files["pid"], files["ready"], files["heartbeat"]))


def test_t6_real_detached_subprocess_is_ready_idempotent_and_stoppable(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    source = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", source + os.pathsep + os.environ.get("PYTHONPATH", ""))

    first = cli.service(service_args("ensure"))
    second = cli.service(service_args("ensure"))
    assert first["running"] is True
    assert second["running"] is True
    assert first["pid"] == second["pid"]
    assert first["generation"] == second["generation"]
    time.sleep(2)
    observed = cli.service(service_args("status"))
    assert observed["running"] is True
    assert observed["pid"] == first["pid"]
    assert observed["heartbeat_age_seconds"] < cli.SERVICE_HEARTBEAT_MAX_AGE_SECONDS

    stopped = cli.service(service_args("stop"))
    assert stopped["running"] is False
    assert cli.service(service_args("status"))["running"] is False
    files = service_files(runtime)
    assert all(not path.exists() for path in (files["pid"], files["ready"], files["heartbeat"], files["stop"]))


def test_t7_concurrent_cli_ensure_converges_on_one_supervisor(tmp_path: Path):
    runtime = tmp_path / "runtime"
    env = dict(os.environ, LOCALAPPDATA=str(runtime), PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    command = [sys.executable, "-m", "dummy_orchestrator.global_cli", "service", "ensure"]
    first = subprocess.Popen(command, env=env, cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, env=env, cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    first_data = json.loads(first_out)
    second_data = json.loads(second_out)
    assert first_data["running"] is True
    assert second_data["running"] is True
    assert first_data["pid"] == second_data["pid"]
    assert first_data["generation"] == second_data["generation"]

    stop = subprocess.run([*command[:-1], "stop"], env=env,
                          cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert json.loads(stop.stdout)["running"] is False
