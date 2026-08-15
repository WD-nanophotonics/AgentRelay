"""Central Windows process-creation policy for AgentRelay.

Background AgentRelay work must never create or activate a console window.  All
short-lived helpers therefore use ``CREATE_NO_WINDOW`` plus hidden startup
metadata, while long-lived detached children use ``DETACHED_PROCESS`` and the
same hidden startup metadata.  Human-requested UI processes (for example
Explorer or a browser) are intentionally outside this policy.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any


def _hidden_startupinfo() -> Any | None:
    if os.name != "nt":
        return None
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return startup


def _short_lived_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": _hidden_startupinfo(),
    }


def _detached_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    # DETACHED_PROCESS is the correct lifetime model for a service/recorder;
    # CREATE_NO_WINDOW is intentionally not combined with it because Windows
    # documents that CREATE_NO_WINDOW is ignored for detached processes.
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": flags, "startupinfo": _hidden_startupinfo()}


def run_hidden(command: Sequence[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a short-lived non-interactive helper without a Windows console."""
    options = _short_lived_kwargs()
    options.update(kwargs)
    return subprocess.run(list(command), *args, **options)


def spawn_background(command: Sequence[str], *args: Any, detached: bool = False,
                     **kwargs: Any) -> subprocess.Popen[Any]:
    """Spawn a non-interactive child under the central Windows policy."""
    options = _detached_kwargs() if detached else _short_lived_kwargs()
    options.update(kwargs)
    return subprocess.Popen(list(command), *args, **options)


def hidden_creation_flags(*, detached: bool = False) -> int:
    """Expose the selected flags for diagnostics/tests without spawning."""
    options = _detached_kwargs() if detached else _short_lived_kwargs()
    return int(options.get("creationflags", 0))
