"""High-frequency, no-console Windows observer for final P0 qualification."""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from datetime import datetime, timezone

OUT = os.environ.get("P0_OBSERVER_OUT", os.path.join(os.environ.get("TEMP", "."), "agentrelay_p0_observer.jsonl"))
DURATION = float(os.environ.get("P0_OBSERVER_SECONDS", "20"))
POLL_SECONDS = float(os.environ.get("P0_OBSERVER_POLL_SECONDS", "0.05"))
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
        ("heap", ctypes.c_void_p), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD), ("base", wintypes.LONG), ("flags", wintypes.DWORD),
        ("name", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

TERMINAL_NAMES = {"cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe", "windowsterminal.exe", "wt.exe"}
TERMINAL_CLASSES = {"consolewindowclass", "cascadia_hosting_window_class"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def executable(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def processes() -> dict[int, dict[str, object]]:
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    result: dict[int, dict[str, object]] = {}
    if handle in (0, -1):
        return result
    entry = ProcessEntry()
    entry.size = ctypes.sizeof(ProcessEntry)
    try:
        if kernel32.Process32FirstW(handle, ctypes.byref(entry)):
            while True:
                pid = int(entry.pid)
                result[pid] = {"name": entry.name, "parent_pid": int(entry.parent), "executable": executable(pid)}
                if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(handle)
    for pid, info in result.items():
        chain: list[dict[str, object]] = []
        seen = {pid}
        parent = int(info["parent_pid"])
        while parent and parent not in seen and parent in result and len(chain) < 12:
            seen.add(parent)
            ancestor = result[parent]
            chain.append({"pid": parent, **ancestor})
            parent = int(ancestor["parent_pid"])
        info["ancestor_chain"] = chain
    return result


def windows() -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(512)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 512)
            user32.GetClassNameW(hwnd, cls, 256)
            result[int(hwnd)] = {"pid": int(pid.value), "title": title.value, "class": cls.value}
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return result


def enrich(window: dict[str, object], process_map: dict[int, dict[str, object]], foreground: bool = False) -> dict[str, object]:
    value = dict(window)
    pid = int(value["pid"])
    value["foreground"] = foreground
    value["process"] = process_map.get(pid)
    process = process_map.get(pid, {})
    name = str(process.get("name", "")).lower()
    cls = str(value.get("class", "")).lower()
    value["terminal_candidate"] = name in TERMINAL_NAMES or cls in TERMINAL_CLASSES
    return value


def emit(handle, event: str, **details: object) -> None:
    handle.write(json.dumps({"timestamp": now(), "event": event, **details}, ensure_ascii=False) + "\n")
    handle.flush()


with open(OUT, "w", encoding="utf-8") as stream:
    started = now()
    emit(stream, "observer_started", pid=os.getpid(), duration=DURATION, poll_seconds=POLL_SECONDS)
    previous_processes = processes()
    previous_windows = windows()
    previous_foreground = int(user32.GetForegroundWindow())
    emit(stream, "baseline_snapshot", windows={str(hwnd): enrich(info, previous_processes, hwnd == previous_foreground) for hwnd, info in previous_windows.items()}, processes=previous_processes, foreground_hwnd=previous_foreground)
    end = time.monotonic() + DURATION
    counts = {"window_appeared": 0, "foreground_changed": 0, "terminal_events": 0}
    while time.monotonic() < end:
        current_processes = processes()
        current_windows = windows()
        foreground = int(user32.GetForegroundWindow())
        for pid, info in current_processes.items():
            if pid not in previous_processes:
                emit(stream, "process_appeared", pid=pid, **info)
        for pid, info in previous_processes.items():
            if pid not in current_processes:
                emit(stream, "process_disappeared", pid=pid, **info)
        for hwnd, info in current_windows.items():
            current = enrich(info, current_processes, hwnd == foreground)
            if hwnd not in previous_windows:
                emit(stream, "window_appeared", hwnd=hwnd, **current)
                counts["window_appeared"] += 1
                if current["terminal_candidate"]:
                    counts["terminal_events"] += 1
            else:
                before = enrich(previous_windows[hwnd], previous_processes, hwnd == previous_foreground)
                if {k: before.get(k) for k in ("pid", "title", "class")} != {k: current.get(k) for k in ("pid", "title", "class")}:
                    emit(stream, "window_changed", hwnd=hwnd, before=before, after=current)
                    if current["terminal_candidate"]:
                        counts["terminal_events"] += 1
        for hwnd, info in previous_windows.items():
            if hwnd not in current_windows:
                emit(stream, "window_disappeared", hwnd=hwnd, **enrich(info, previous_processes, hwnd == previous_foreground))
        if foreground != previous_foreground:
            current = enrich(current_windows[foreground], current_processes, True) if foreground in current_windows else None
            emit(stream, "foreground_changed", hwnd=foreground, window=current, process=current_processes.get(int(current["pid"])) if current else None)
            counts["foreground_changed"] += 1
            if current and current["terminal_candidate"]:
                counts["terminal_events"] += 1
        previous_processes, previous_windows, previous_foreground = current_processes, current_windows, foreground
        time.sleep(POLL_SECONDS)
    emit(stream, "observer_stopped", pid=os.getpid(), started_at=started, counts=counts)
