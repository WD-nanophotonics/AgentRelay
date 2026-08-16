from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

import dummy_orchestrator.global_cli as cli


def service_args(action: str) -> argparse.Namespace:
    return argparse.Namespace(action=action)


def service_files(runtime: Path) -> dict[str, Path]:
    root = runtime / "CodexOrchestrator" / "service"
    return {name: root / filename for name, filename in {
        "pid": "supervisor.pid",
        "ready": "supervisor.ready.json",
        "heartbeat": "supervisor.heartbeat.json",
        "stop": "supervisor.stop.json",
    }.items()}


def test_h5_heartbeat_writer_stops_after_generation_ownership_loss(tmp_path: Path, monkeypatch):
    files = service_files(tmp_path)
    files["pid"].parent.mkdir(parents=True)
    record = {"schema": 1, "owner": cli.SERVICE_OWNER, "pid": os.getpid(), "generation": "generation-a"}
    cli._atomic_json_write(files["pid"], record)
    stop_event = threading.Event()
    monkeypatch.setattr(cli, "SERVICE_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    thread = threading.Thread(target=cli._heartbeat_loop, args=(files, record, stop_event), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while not files["heartbeat"].exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert files["heartbeat"].exists()
    before = json.loads(files["heartbeat"].read_text(encoding="utf-8"))["heartbeat_epoch"]

    cli._atomic_json_write(files["pid"], record | {"generation": "generation-b"})
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    time.sleep(0.15)
    after = json.loads(files["heartbeat"].read_text(encoding="utf-8"))["heartbeat_epoch"]
    assert after == before
    stop_event.set()


def test_h1_h2_h3_h4_heartbeat_survives_blocking_supervisor_and_reentrant_ensure(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    # This exceeds the production freshness TTL and is intentionally isolated
    # from Gmail, Mechanics, and registered worker projects.
    monkeypatch.setenv("AGENTRELAY_TEST_BLOCK_SUPERVISOR_SECONDS", "31")
    files = service_files(runtime)

    started = cli.service(service_args("ensure"))
    assert started["running"] is True
    samples: list[float] = []
    for _ in range(4):
        time.sleep(8)
        observed = cli.service(service_args("status"))
        assert observed["running"] is True
        assert observed["pid"] == started["pid"]
        assert observed["generation"] == started["generation"]
        samples.append(json.loads(files["heartbeat"].read_text(encoding="utf-8"))["heartbeat_epoch"])

    ensured = cli.service(service_args("ensure"))
    assert ensured["running"] is True
    assert ensured["pid"] == started["pid"]
    assert ensured["generation"] == started["generation"]
    assert max(samples) - min(samples) >= 16.0

    stopped = cli.service(service_args("stop"))
    assert stopped["running"] is False
    assert all(not path.exists() for path in files.values())


def test_h6_stop_during_short_block_eventually_cleans_all_artifacts(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(runtime))
    monkeypatch.setenv("AGENTRELAY_TEST_BLOCK_SUPERVISOR_SECONDS", "1.5")
    files = service_files(runtime)

    started = cli.service(service_args("ensure"))
    assert started["running"] is True
    stopped = cli.service(service_args("stop"))
    assert stopped["running"] is False
    assert cli.service(service_args("status"))["running"] is False
    assert all(not path.exists() for path in files.values())
