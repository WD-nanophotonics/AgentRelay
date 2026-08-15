from __future__ import annotations

import subprocess

from dummy_orchestrator import diagnostics
from dummy_orchestrator import process_policy


def test_run_hidden_applies_no_console_policy(monkeypatch):
    seen = {}

    def fake_run(command, *args, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process_policy.subprocess, "run", fake_run)
    process_policy.run_hidden(["tasklist", "/FO", "CSV"])
    assert seen["kwargs"]["creationflags"] & getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startup = seen["kwargs"]["startupinfo"]
    assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == subprocess.SW_HIDE


def test_detached_spawn_uses_independent_hidden_policy(monkeypatch):
    seen = {}

    class FakeProcess:
        pid = 1234

    def fake_popen(command, *args, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(process_policy.subprocess, "Popen", fake_popen)
    process_policy.spawn_background(["python", "-m", "dummy_orchestrator.diagnostics"], detached=True)
    flags = seen["kwargs"]["creationflags"]
    assert flags & getattr(subprocess, "DETACHED_PROCESS", 0)
    assert flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert not flags & getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert seen["kwargs"]["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_diagnostics_helper_routes_through_hidden_runner(monkeypatch):
    seen = {}

    def fake_run(command, *args, timeout=8, **kwargs):
        seen["command"] = command
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(diagnostics, "run_hidden", fake_run)
    assert diagnostics._run(["tasklist"], timeout=3) == (0, "ok", "")
    assert seen == {"command": ["tasklist"], "timeout": 3}
