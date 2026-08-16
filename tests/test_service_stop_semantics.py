from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import dummy_orchestrator.global_cli as cli


def args(action: str) -> argparse.Namespace:
    return argparse.Namespace(action=action)


def files(runtime: Path) -> dict[str, Path]:
    root = runtime / "CodexOrchestrator" / "service"
    return {name: root / filename for name, filename in {
        "pid": "supervisor.pid",
        "ready": "supervisor.ready.json",
        "heartbeat": "supervisor.heartbeat.json",
        "stop": "supervisor.stop.json",
        "lock": "supervisor.start.lock",
    }.items()}


def test_bounded_stop_is_idempotent_and_keeps_fresh_heartbeat(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    monkeypatch.setenv("AGENTRELAY_TEST_BLOCK_SUPERVISOR_SECONDS", "2")
    monkeypatch.setattr(cli, "SERVICE_STOP_TIMEOUT_SECONDS", 0.2)

    started = cli.service(args("ensure"))
    assert started["running"] is True
    stopping = cli.service(args("stop"))
    assert stopping["running"] is True
    assert stopping["stopping"] is True
    assert stopping["stop_requested"] is True
    assert stopping["pid"] == started["pid"]
    assert stopping["generation"] == started["generation"]

    time.sleep(0.35)
    observed = cli.service(args("status"))
    assert observed["running"] is True
    assert observed["stopping"] is True
    assert observed["heartbeat_age_seconds"] < cli.SERVICE_HEARTBEAT_MAX_AGE_SECONDS

    repeated = cli.service(args("stop"))
    assert repeated["running"] is True
    assert repeated["stopping"] is True
    assert repeated["pid"] == started["pid"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and cli.service(args("status"))["running"]:
        time.sleep(0.1)
    assert cli.service(args("status"))["running"] is False
    assert all(not path.exists() for path in files(runtime).values())
