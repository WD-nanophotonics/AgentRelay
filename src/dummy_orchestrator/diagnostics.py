"""Independent, detached crash-forensics recorder for AgentRelay."""
from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .process_policy import run_hidden, spawn_background

VERSION = "0.3.7"


def root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "CodexOrchestrator" / "diagnostics"


def now() -> str:
    return datetime.now(UTC).isoformat()


def append(path: Path, category: str, event: str, **details: Any) -> None:
    """Append one flushed, fsynced JSONL event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": now(), "monotonic_ms": int(time.monotonic() * 1000),
           "category": category, "event": event, "details": details}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        handle.flush(); os.fsync(handle.fileno())


def _run(command: list[str], timeout: float = 8) -> tuple[int, str, str]:
    try:
        completed = run_hidden(command, capture_output=True, text=True,
                               timeout=timeout, errors="replace")
        return completed.returncode, completed.stdout, completed.stderr
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def process_snapshot() -> list[dict[str, Any]]:
    code, raw, err = _run(["tasklist", "/FO", "CSV", "/NH"], timeout=5)
    if code != 0:
        return [{"error": err or f"tasklist exit {code}"}]
    out: list[dict[str, Any]] = []
    for row in csv.reader(raw.splitlines()):
        if len(row) < 2:
            continue
        try:
            out.append({"name": row[0], "pid": int(row[1])})
        except (TypeError, ValueError):
            continue
    return out


def discover_codex_logs() -> list[str]:
    candidates = [Path.home() / ".codex",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Codex"]
    return [str(path) for path in candidates if path.exists()]


def _log_inventory(paths: Iterable[str]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        try:
            files = list(path.glob("*.log"))[:100] if path.is_dir() else [path]
            for file in files:
                stat = file.stat()
                inventory.append({"path": str(file), "size": stat.st_size,
                                  "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()})
        except OSError as exc:
            inventory.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return inventory


def _collect_windows_events(session: Path) -> None:
    """Capture a bounded, best-effort Application event-log window."""
    code, stdout, stderr = _run(["wevtutil", "qe", "Application", "/c:40", "/f:xml"], timeout=12)
    target = session / "windows-events.jsonl"
    if code != 0:
        append(session / "timeline.jsonl", "windows_event", "collection_unavailable",
               exit_code=code, error=stderr[-1000:])
        return
    blocks = stdout.split("</Event>"); kept = 0
    for block in blocks:
        if not block.strip():
            continue
        text = block[-6000:]; lowered = text.lower()
        if any(term in lowered for term in ("application error", "windows error reporting", "codex", "chatgpt")):
            append(target, "windows_event", "application_event", xml=text + "</Event>"); kept += 1
    append(session / "timeline.jsonl", "windows_event", "collection_complete",
           exit_code=code, candidates=len(blocks), retained=kept)


def create_session(tag: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in tag)[:50] or "agentrelay"
    root().mkdir(parents=True, exist_ok=True)
    session = root() / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_{safe}"
    session.mkdir(parents=True, exist_ok=False)
    logs = discover_codex_logs()
    manifest = {"started_at": now(), "tag": tag, "launcher_pid": os.getpid(),
                "launcher_parent_pid": os.getppid(),
                "python": sys.version, "platform": platform.platform(),
                "agentrelay_version": VERSION, "diagnostics_session": str(session),
                "codex_logs": logs, "recorder_contract": "detached-observation-v1"}
    (session / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (session / "codex-log-inventory.json").write_text(json.dumps(_log_inventory(logs), indent=2), encoding="utf-8")
    append(session / "timeline.jsonl", "diagnostics", "session_created", pid=os.getpid(), tag=tag)
    _collect_windows_events(session)
    return session


def log_command(session: Path, argv: list[str], *, cwd: str | None = None,
                pid: int | None = None, returncode: int | None = None,
                stdout: str = "", stderr: str = "", phase: str = "observed") -> None:
    append(session / "commands.jsonl", "command", phase, argv=argv, cwd=cwd,
           pid=pid, returncode=returncode, stdout_tail=stdout[-4000:], stderr_tail=stderr[-4000:])


def recorder(session: Path) -> None:
    timeline = session / "timeline.jsonl"; processes_file = session / "processes.jsonl"; last: set[int] = set()
    append(timeline, "diagnostics", "recorder_started", pid=os.getpid(), parent_pid=os.getppid())
    try:
        while (session / "RUNNING").exists():
            current = process_snapshot(); seen = {int(item["pid"]) for item in current if item.get("pid")}
            with processes_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": now(), "pid": os.getpid(), "processes": current}, ensure_ascii=False) + "\n")
                handle.flush()
            for pid in sorted(seen - last): append(timeline, "process", "appeared", pid=pid)
            for pid in sorted(last - seen): append(timeline, "process", "disappeared", pid=pid)
            last = seen
            append(timeline, "diagnostics", "heartbeat", pid=os.getpid(), process_count=len(current))
            time.sleep(1)
    except Exception:
        trace = traceback.format_exc(); (session / "traceback.txt").write_text(trace, encoding="utf-8")
        append(timeline, "diagnostics", "recorder_exception", traceback=trace)
    finally:
        append(timeline, "diagnostics", "recorder_stopped", pid=os.getpid(), parent_pid=os.getppid())


def current() -> tuple[Path | None, int | None]:
    path = root() / "current.json"
    if not path.exists(): return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8")); return Path(data["session"]), int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError): return None, None


def status() -> dict[str, Any]:
    session, pid = current()
    if not session or not pid or not (session / "RUNNING").exists():
        return {"running": False, "pid": pid, "session": str(session) if session else None}
    alive = any(item.get("pid") == pid for item in process_snapshot())
    if not alive: append(session / "timeline.jsonl", "diagnostics", "recorder_missing", pid=pid)
    return {"running": alive, "pid": pid, "session": str(session)}


def start(tag: str = "agentrelay") -> dict[str, Any]:
    existing = status()
    if existing.get("running"): return existing | {"already_running": True}
    session = create_session(tag); (session / "RUNNING").write_text("1", encoding="utf-8")
    command = [sys.executable, "-m", "dummy_orchestrator.diagnostics", "_recorder", str(session)]
    try:
        proc = spawn_background(command, detached=True, close_fds=True, cwd=str(root()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        append(session / "timeline.jsonl", "diagnostics", "recorder_spawn_failed", traceback=traceback.format_exc(), argv=command); raise
    (session / "recorder.pid").write_text(str(proc.pid), encoding="utf-8")
    log_command(session, command, cwd=str(root()), pid=proc.pid, phase="spawned")
    append(session / "timeline.jsonl", "diagnostics", "recorder_spawned", pid=proc.pid, argv=command, creation_flags="detached_hidden_policy", detached=True)
    (root() / "current.json").write_text(json.dumps({"session": str(session), "pid": proc.pid, "started_at": now()}), encoding="utf-8")
    return {"running": True, "pid": proc.pid, "session": str(session), "detached": True}


def stop() -> dict[str, Any]:
    session, pid = current()
    if session:
        (session / "RUNNING").unlink(missing_ok=True); append(session / "timeline.jsonl", "diagnostics", "stop_requested", pid=pid)
    return {"running": False, "pid": pid, "session": str(session) if session else None}


def report() -> str:
    session, pid = current()
    if not session: return "No diagnostic session found."
    lines = [f"Diagnostic session: {session}", f"Recorder PID: {pid}", f"Status: {json.dumps(status(), ensure_ascii=False)}"]
    for name in ("manifest.json", "codex-log-inventory.json", "windows-events.jsonl", "traceback.txt"):
        path = session / name
        if path.exists(): lines.append(f"Artifact {name}: {path.stat().st_size} bytes")
    timeline = session / "timeline.jsonl"
    if timeline.exists(): lines += ["Recent timeline:"] + timeline.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "_recorder":
        try: recorder(Path(sys.argv[2]))
        except Exception:
            session = Path(sys.argv[2]); trace = traceback.format_exc(); (session / "traceback.txt").write_text(trace, encoding="utf-8")
            append(session / "timeline.jsonl", "diagnostics", "recorder_fatal", traceback=trace)
