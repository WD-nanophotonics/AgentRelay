"""Run the sandbox full pytest suite with durable, host-independent evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dummy_orchestrator.global_cli import source_identity
from dummy_orchestrator.process_policy import spawn_background
from dummy_orchestrator.version import __version__


DEFAULT_TIMEOUT_SECONDS = 900


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run(session: Path, timeout_seconds: int) -> int:
    session.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    python = repo / ".venv" / "Scripts" / "python.exe"
    identity = source_identity()
    command = [str(python), "-m", "pytest", "-q"]
    child_env = dict(os.environ)
    child_env["PATH"] = str(python.parent) + os.pathsep + child_env.get("PATH", "")
    # A few legacy tests intentionally invoke the literal ``python`` command
    # from an unrelated cwd. Keep those helpers on this checkout even when
    # Windows resolves that bare command to a system interpreter.
    source_root = repo / "src"
    child_env["PYTHONPATH"] = str(source_root) + os.pathsep + child_env.get("PYTHONPATH", "")
    started = time.monotonic()
    write_json(session / "manifest.json", {
        "timestamp": now(),
        "cwd": str(repo),
        "python_executable": str(python),
        "sandbox_head": identity.get("git_revision"),
        "branch": identity.get("git_branch"),
        "source_fingerprint": identity.get("source_fingerprint"),
        "version": __version__,
        "pytest_command": command,
        "launcher_pid": os.getpid(),
        "timeout_seconds": timeout_seconds,
    })
    (session / "started.marker").write_text(now(), encoding="utf-8")
    stdout_path = session / "pytest.stdout.log"
    stderr_path = session / "pytest.stderr.log"
    exit_code: int | None = None
    timed_out = False
    error: str | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            # The runner is already an independent pythonw process. Keep the
            # test child short-lived and hidden so its exit code/handle remain
            # directly observable while no console can be created.
            child = spawn_background(command, cwd=str(repo), stdout=stdout, stderr=stderr,
                                     close_fds=True, detached=False, env=child_env)
            (session / "pytest.pid").write_text(str(child.pid), encoding="utf-8")
            deadline = time.monotonic() + timeout_seconds
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(1)
            if child.poll() is None:
                timed_out = True
                try:
                    child.terminate()
                    child.wait(timeout=10)
                except Exception:
                    try:
                        child.kill()
                    except Exception:
                        pass
                exit_code = None
            else:
                exit_code = child.returncode
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    summary_match = re.search(r"(?m)^.*\b\d+\s+(?:passed|failed|error|errors)\b.*$", stdout_text + "\n" + stderr_text)
    summary = summary_match.group(0).strip() if summary_match else None
    if timed_out:
        status = "TIMEOUT"
    elif error:
        status = "ERROR"
    elif exit_code == 0 and summary is None:
        status = "UNDETERMINED"
        error = "pytest exited 0 without a final summary line"
    elif exit_code == 0:
        status = "PASS"
    else:
        status = "FAIL"
    if exit_code is not None:
        (session / "pytest.exitcode").write_text(str(exit_code), encoding="utf-8")
    write_json(session / "result.json", {
        "exit_code": exit_code,
        "finished_at": now(),
        "duration_seconds": round(duration, 3),
        "status": status,
        "pytest_summary": summary,
        "error": error,
    })
    (session / "finished.marker").write_text(now(), encoding="utf-8")
    return 0 if status == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    return run(args.session_dir, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
