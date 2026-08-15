from __future__ import annotations

import json
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox


class Monitor:
    """Small observation/control plane; closing it never stops the supervisor."""
    def __init__(self, cli):
        self.cli = cli; self.root = tk.Tk(); self.root.title("AgentRelay Monitor"); self.root.geometry("900x650")
        self.text = tk.Text(self.root, state="disabled", font=("Consolas", 10)); self.text.pack(fill="both", expand=True)
        bar = tk.Frame(self.root); bar.pack(fill="x")
        for label, action in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart"), ("Refresh", "refresh"), ("Open logs", "logs")):
            tk.Button(bar, text=label, command=lambda a=action: self.action(a)).pack(side="left", padx=3, pady=3)
        self.refresh()

    def action(self, action: str):
        try:
            self.cli._diag("monitor_action", action=action)
            if action == "refresh": return self.refresh()
            if action == "logs":
                path = self.cli.data_root() / "logs"
                subprocess.Popen(["explorer.exe", str(path)])
                return
            if action == "restart": self.cli.service(type("A", (), {"action": "stop"})())
            self.cli.service(type("A", (), {"action": "ensure" if action in ("start", "restart") else "stop"})())
            # Re-read the canonical service state after the control action.
            # The control callback must not leave the previous rendered frame
            # as the apparent source of truth.
            self.refresh()
        except Exception as exc: messagebox.showerror("AgentRelay", str(exc)); self.refresh()

    def snapshot(self) -> str:
        try: service = self.cli.service(type("A", (), {"action": "status"})())
        except Exception as exc: service = {"running": False, "pid": None, "error": f"{type(exc).__name__}: {exc}"}
        lines = [f"AgentRelay {self.cli.TOOL_VERSION}", f"SOURCE {self.cli.source_identity().get('source_fingerprint')}", "", f"SERVICE  {'RUNNING' if service.get('running') else 'STOPPED'}  PID={service.get('pid')}"]
        browser = "UNKNOWN"
        try:
            from urllib.request import urlopen
            with urlopen("http://127.0.0.1:9222/json", timeout=1) as r: browser = f"READY pages={len(json.loads(r.read()))}"
        except Exception: browser = "STOPPED/UNAVAILABLE"
        lines.append(f"BROWSER  {browser}")
        lines.append("GMAIL    see `agent-relay doctor` for OAuth health")
        lines.append("\nPROJECTS")
        try:
            registrations = self.cli._store().all()
            for reg in registrations:
                try:
                    row = self.cli._state().project(reg.project_id)
                    lines.extend([f"{reg.project_id}", f"  run={row.get('run_id')} round={row.get('round_id')} state={row.get('state')}", f"  worker={row.get('worker_session_id') or reg.persistent_codex_session}", f"  active={row.get('active_event_type')} gmail={row.get('active_event_gmail_id')}", f"  last_activity={row.get('last_transition_at')} error={row.get('error') or 'none'}"])
                except Exception as exc: lines.append(f"{reg.project_id}\n  STATUS ERROR: {type(exc).__name__}: {exc}")
        except Exception as exc: lines.append(f"PROJECTS ERROR: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def refresh(self):
        try: self.cli._diag("monitor_refresh_begin")
        except Exception: pass
        try:
            rendered = self.snapshot()
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", rendered)
            self.text.see("1.0")
            self.text.configure(state="disabled")
            # Force Tk to flush the new frame before the callback returns.
            # This keeps a successful Start/Restart from visually retaining
            # the previous STOPPED frame until the window is reopened.
            self.root.update_idletasks()
            first_line = rendered.splitlines()[3] if len(rendered.splitlines()) > 3 else ""
            try: self.cli._diag("monitor_render", service_line=first_line)
            except Exception: pass
        except Exception as exc:
            try: self.cli._diag("monitor_refresh_error", error=f"{type(exc).__name__}: {exc}")
            except Exception: pass
            try:
                self.text.configure(state="normal"); self.text.insert("end", f"MONITOR REFRESH ERROR: {type(exc).__name__}: {exc}\n"); self.text.configure(state="disabled")
            except Exception: pass
        finally:
            try: self.cli._diag("monitor_refresh_end")
            except Exception: pass
            try: self.root.after(2000, self.refresh)
            except Exception: pass

    def run(self): self.root.mainloop()
