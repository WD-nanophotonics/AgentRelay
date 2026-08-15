from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from datetime import datetime, timezone

OUT = os.environ.get("P0_OBSERVER_OUT", os.path.join(os.environ.get("TEMP", "."), "agentrelay_p0_observer.jsonl"))
DURATION = float(os.environ.get("P0_OBSERVER_SECONDS", "20"))
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
TH32CS_SNAPPROCESS = 0x2


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
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                result[int(entry.pid)] = {"name": entry.name, "parent": int(entry.parent)}
                if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(handle)
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


def emit(handle, event: str, **details: object) -> None:
    handle.write(json.dumps({"timestamp": now(), "event": event, **details}, ensure_ascii=False) + "\n")
    handle.flush()


with open(OUT, "w", encoding="utf-8") as stream:
    emit(stream, "observer_started", pid=os.getpid(), duration=DURATION)
    previous_processes = processes()
    previous_windows = windows()
    previous_foreground = int(user32.GetForegroundWindow())
    end = time.monotonic() + DURATION
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
            if hwnd not in previous_windows:
                emit(stream, "window_appeared", hwnd=hwnd, **info)
        for hwnd, info in previous_windows.items():
            if hwnd not in current_windows:
                emit(stream, "window_disappeared", hwnd=hwnd, **info)
        if foreground != previous_foreground:
            info = current_windows.get(foreground)
            emit(stream, "foreground_changed", hwnd=foreground, window=info,
                 process=current_processes.get(info.get("pid")) if info else None)
        previous_processes, previous_windows, previous_foreground = current_processes, current_windows, foreground
        time.sleep(0.05)
    emit(stream, "observer_stopped", pid=os.getpid())
