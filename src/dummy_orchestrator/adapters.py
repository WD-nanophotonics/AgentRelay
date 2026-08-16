from __future__ import annotations

import json
import base64
import os
import shutil
import time
import subprocess
import sys
import uuid
import urllib.request
import re
from urllib.error import URLError
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .models import EventType, OrchestratorEvent, ProjectConfig
from .process_policy import run_hidden, spawn_background


class HumanRequired(RuntimeError):
    pass


def _process_is_alive(pid: int) -> bool:
    """Check a Windows process without sending it a signal."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259)
    finally:
        kernel32.CloseHandle(handle)


AGENTRELAY_EXECUTION_CONTRACT = """AGENTRELAY EXECUTION CONTRACT
- Work only on the supplied TASK or CORRECTIVE instruction.
- Do not advance to another phase autonomously.
- Run required validation/tests, then commit and push only to the registered worker branch.
- When complete, run `agent-relay deliver` and end this work turn.
- Do not poll Gmail or ChatGPT and do not wait for the next task.
- If HUMAN_REQUIRED or unsafe ambiguity occurs, stop and report it.
"""

AUDITOR_RETURN_PROTOCOL = """AUDITOR_RETURN_PROTOCOL (schema_version=1)
Send exactly one Gmail to the target worker mailbox. Subject: [ORCH][project_id][round_id][TASK|CORRECTIVE|TERMINAL_CONTROL] short title.
Body MUST contain exactly one canonical JSON object (a fenced JSON object is allowed): {\"ORCHESTRATOR_EVENT\":{\"schema_version\":1,\"project_id\":\"...\",\"run_id\":\"...\",\"round_id\":\"...\",\"event_type\":\"TASK|CORRECTIVE|TERMINAL_CONTROL\",\"payload\":{\"instruction\":\"...\"}}}. No prose-only, multiple, or conflicting envelopes.
"""


class InstructionBus(Protocol):
    def send(self, event: OrchestratorEvent, recipient: str) -> str: ...
    def poll(self, project: ProjectConfig, run_id: str) -> list[OrchestratorEvent]: ...


def subject(project_or_prefix: ProjectConfig | str, event: OrchestratorEvent) -> str:
    """Format routing subject from canonical event identity; payload never supplies project identity."""
    prefix = project_or_prefix.gmail_subject_prefix if isinstance(project_or_prefix, ProjectConfig) else project_or_prefix
    label = event.payload.get("gmail_event_type", str(event.event_type))
    return f"{prefix}[{event.project_id}][{event.round_id}][{label}]"


class GmailInstructionBus:
    """Gmail API adapter; OAuth material is always outside the repository."""
    scopes = ["https://www.googleapis.com/auth/gmail.modify"]

    def __init__(self, credential_path: Path, token_path: Path):
        self.credential_path, self.token_path = credential_path, token_path
        self.diagnostics: list[dict] = []

    def _service(self):
        if not self.credential_path.exists():
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = gmail_oauth_client\nexpected = {self.credential_path}")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = gmail_dependencies\naction = pip install -e .") from exc
        creds = Credentials.from_authorized_user_file(self.token_path, self.scopes) if self.token_path.exists() else None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(self.credential_path, self.scopes)
            creds = flow.run_local_server(port=0)
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def resolve_recipient(self, configured: str) -> str:
        if configured and configured != "REPLACE_WITH_CONNECTED_GMAIL_ADDRESS": return configured
        try:
            return self._service().users().getProfile(userId="me").execute()["emailAddress"]
        except KeyError as exc:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = gmail_identity\ndetail = Gmail profile did not return emailAddress") from exc

    def send(self, event: OrchestratorEvent, recipient: str) -> str:
        from email.message import EmailMessage
        import base64
        event.validate()
        mail = EmailMessage()
        configured_prefix = event.payload.get("gmail_subject_prefix")
        legacy_project = event.payload.get("project")
        if not configured_prefix and isinstance(legacy_project, ProjectConfig): configured_prefix = legacy_project.gmail_subject_prefix
        mail["To"], mail["Subject"] = recipient, subject(configured_prefix or "[ORCH]", event)
        envelope = {"schema_version": 1, "project_id": event.project_id, "run_id": event.run_id,
                    "round_id": event.round_id, "event_type": str(event.event_type), "payload": event.payload}
        mail.set_content(json.dumps({"ORCHESTRATOR_EVENT": envelope}, default=str, indent=2))
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        return self._service().users().messages().send(userId="me", body={"raw": raw}).execute()["id"]

    def poll(self, project: ProjectConfig, run_id: str) -> list[OrchestratorEvent]:
        self.diagnostics = []
        service = self._service()
        query = f'subject:"{project.gmail_subject_prefix}[{project.project_id}]"'
        result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        events: list[OrchestratorEvent] = []
        for item in result.get("messages", []):
            message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
            payload = message["payload"]
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            data = self._text_data(payload)
            if not data: continue
            actual_subject = headers.get("subject", "")
            namespace_match = actual_subject.startswith(f"{project.gmail_subject_prefix}[{project.project_id}]")
            try:
                decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8")
                document = self._extract_document(decoded)
                envelope = document["ORCHESTRATOR_EVENT"]
                if not isinstance(envelope, dict): raise ValueError("ORCHESTRATOR_EVENT is not an object")
                if any(k not in envelope for k in ("project_id", "run_id", "round_id", "event_type", "payload")):
                    raise ValueError("missing canonical envelope field")
                if envelope.get("schema_version", 1) != 1: raise ValueError("unsupported schema_version")
                if not isinstance(envelope["payload"], dict): raise ValueError("payload is not an object")
                event = OrchestratorEvent(envelope["project_id"], envelope["run_id"], envelope["round_id"], EventType(envelope["event_type"]), envelope["payload"], item["id"])
                event.validate()
            except (KeyError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                if namespace_match:
                    self.diagnostics.append({"code": "MATCHING_SUBJECT_INVALID_ENVELOPE", "message_id": item["id"], "project_id": project.project_id, "reason": str(exc)[:240]})
                continue
            expected_prefix = f"{project.gmail_subject_prefix}[{project.project_id}][{event.round_id}]"
            allowed_labels = {str(event.event_type), "DELIVERED" if event.event_type == EventType.DELIVERY else str(event.event_type)}
            subject_match = actual_subject.startswith(expected_prefix) and any(f"[{label}]" in actual_subject for label in allowed_labels)
            if event.project_id == project.project_id and event.run_id == run_id and subject_match:
                events.append(event)
        return events

    @staticmethod
    def _extract_document(text: str) -> dict:
        decoder = json.JSONDecoder(); candidates: list[dict] = []
        for candidate in [text, *re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S)]:
            try:
                value, _ = decoder.raw_decode(candidate.lstrip())
                if isinstance(value, dict) and "ORCHESTRATOR_EVENT" in value: candidates.append(value)
            except json.JSONDecodeError: pass
        for match in re.finditer(r"\{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                if isinstance(value, dict) and "ORCHESTRATOR_EVENT" in value: candidates.append(value)
            except json.JSONDecodeError: pass
        unique = {json.dumps(item, sort_keys=True) for item in candidates}
        if len(unique) != 1: raise ValueError("expected exactly one unique ORCHESTRATOR_EVENT envelope")
        return json.loads(next(iter(unique)))

    @staticmethod
    def _text_data(part: dict) -> str | None:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return part["body"]["data"]
        for child in part.get("parts", []):
            if data := GmailInstructionBus._text_data(child):
                return data
        if part.get("body", {}).get("data"):
            return part["body"]["data"]
        return None


class DummyGmailDeliveryAdapter:
    def __init__(self, bus: InstructionBus): self.bus = bus

    def deliver(self, project: ProjectConfig, run_id: str, round_id: str, corrected: bool = False) -> tuple[str, OrchestratorEvent]:
        simulated_commit = f"SIM-{uuid.uuid4().hex[:12]}"
        payload = {"project": project, "delivery_type": "DUMMY_GMAIL_DELIVERY", "simulated_commit_id": simulated_commit,
                   "delivery_summary": "Corrected deterministic dummy result" if corrected else "Deterministic dummy result",
                   "simulation_notice": "SIMULATION ONLY — NO REAL GIT PUSH OCCURRED", "intentionally_incomplete": round_id == "R002" and not corrected}
        event = OrchestratorEvent(project.project_id, run_id, round_id, EventType.DELIVERY, payload)
        return self.bus.send(event, project.worker_email), event


class GitDeliveryAdapter:
    """Production delivery verifier. It never performs git commit or push."""
    def __init__(self, bus: InstructionBus | None = None): self.bus = bus

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = run_hidden(["git", "-C", str(root), *args], capture_output=True, text=True)
        if result.returncode:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = git_delivery_verification\ndetail = git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    def verify(self, project: ProjectConfig, baseline_sha: str | None = None) -> dict:
        root = Path(project.repository_root or project.local_workspace).resolve()
        actual_root = Path(self._git(root, "rev-parse", "--show-toplevel")).resolve()
        if actual_root != root:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = repository_identity\ndetail = Git root does not match registered repository root")
        branch = self._git(root, "symbolic-ref", "--short", "HEAD")
        expected_branch = project.worker_branch or branch
        if branch != expected_branch:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = worker_branch\ndetail = expected {expected_branch}, found {branch}")
        if branch in set(project.protected_branches):
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = protected_branch\ndetail = delivery branch {branch} is protected")
        local_sha = self._git(root, "rev-parse", "HEAD")
        remote_name = project.git_remote or "origin"
        configured_url = remote_name
        remotes = self._git(root, "remote").splitlines()
        if remote_name not in remotes:
            matches = [name for name in remotes if self._git(root, "remote", "get-url", name) == remote_name]
            if matches: remote_name = matches[0]
        remote_url = self._git(root, "remote", "get-url", remote_name)
        remote_line = self._git(root, "ls-remote", remote_name, f"refs/heads/{branch}")
        remote_sha = remote_line.split()[0] if remote_line else ""
        if not remote_sha:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = remote_branch\ndetail = remote branch SHA was not returned")
        if local_sha != remote_sha:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = remote_sha_mismatch\ndetail = local {local_sha} != remote {remote_sha}")
        status = self._git(root, "status", "--porcelain")
        if status:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = working_tree\ndetail = working tree is not clean after delivery")
        if baseline_sha and local_sha == baseline_sha:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = delivery_sha\ndetail = delivery SHA did not advance beyond baseline")
        return {"repository_root": str(root), "repository_identity": remote_url, "branch": branch,
                "baseline_sha": baseline_sha, "local_sha": local_sha, "delivery_sha": local_sha, "remote_sha": remote_sha,
                "push_verified": True, "working_tree_clean": True, "timestamp": datetime.now(UTC).isoformat()}

    def deliver(self, project: ProjectConfig, run_id: str, round_id: str, baseline_sha: str | None = None, task_id: str | None = None, phase_id: str | None = None) -> tuple[str | None, OrchestratorEvent]:
        payload = self.verify(project, baseline_sha)
        payload.update({"project_id": project.project_id, "run_id": run_id, "task_id": task_id, "phase_id": phase_id or round_id,
                        "attempt": 0, "delivery_type": "GIT_DELIVERY", "evidence_authority": "Git",
                        "gmail_event_type": "DELIVERED",
                        "instruction": "The worker reports completion. Git delivery has been independently verified. Inspect the repository/delivery directly; do not rely on a worker receipt."})
        event = OrchestratorEvent(project.project_id, run_id, round_id, EventType.DELIVERY, payload)
        if self.bus is None:
            return None, event
        return self.bus.send(event, project.worker_email), event


class ChromeResolver:
    """Resolve and validate the normal installed Chrome executable without launching it."""
    def __init__(self, configured_path: str | None = None):
        self.configured_path = configured_path

    def candidates(self) -> list[tuple[Path, str]]:
        values: list[tuple[Path, str]] = []
        if self.configured_path:
            return [(Path(self.configured_path), "explicit configured path")]
        if found := shutil.which("chrome.exe"):
            values.append((Path(found), "PATH resolution"))
        for env, suffix in (("PROGRAMFILES", "Google\\Chrome\\Application\\chrome.exe"),
                            ("PROGRAMFILES(X86)", "Google\\Chrome\\Application\\chrome.exe"),
                            ("LOCALAPPDATA", "Google\\Chrome\\Application\\chrome.exe")):
            base = os.environ.get(env)
            if base: values.append((Path(base) / suffix, f"{env} standard install"))
        unique: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for path, reason in values:
            key = str(path).lower()
            if key not in seen:
                seen.add(key); unique.append((path, reason))
        return unique

    def preflight(self) -> dict:
        errors: list[str] = []
        for path, reason in self.candidates():
            try:
                if not path.exists(): raise FileNotFoundError(str(path))
                result = run_hidden([str(path), "--version"], capture_output=True, text=True, timeout=20, check=True)
                output = (result.stdout or result.stderr).strip().splitlines()
                version = output[0] if output and "chrome" in output[0].lower() else self._file_version(path)
                if not version: raise RuntimeError("could not determine Chrome version")
                return {"path": str(path), "version": version, "selection_reason": reason}
            except Exception as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = chrome_executable\ndetail = no safe normal Chrome executable passed preflight\n" + "\n".join(errors))

    @staticmethod
    def _file_version(path: Path) -> str:
        if os.name != "nt": return "Chrome (version output unavailable)"
        try:
            version = ctypes.windll.version
            size = version.GetFileVersionInfoSizeW(str(path), None)
            if not size: return ""
            buffer = ctypes.create_string_buffer(size)
            if not version.GetFileVersionInfoW(str(path), 0, size, buffer): return ""
            class FixedInfo(ctypes.Structure):
                _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD), ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD)]
            pointer = ctypes.POINTER(FixedInfo)(); length = wintypes.UINT()
            if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)): return ""
            info = pointer.contents
            return f"Chrome {info.dwFileVersionMS >> 16}.{info.dwFileVersionMS & 0xffff}.{info.dwFileVersionLS >> 16}.{info.dwFileVersionLS & 0xffff}"
        except Exception:
            return ""


class ChatGPTWebAuditorAdapter:
    def __init__(self, profile_dir: Path, timeout_seconds: int, chrome_path: str | None = None, debug_port: int = 9222, browser_mode: str = "headful_background"):
        self.profile_dir, self.timeout_ms = profile_dir, timeout_seconds * 1000
        self.chrome_path, self.debug_port = chrome_path, int(debug_port)
        self.browser_mode = browser_mode
        self.last_diagnostics: dict = {}

    def _acquire_launch_lock(self):
        lock = self.profile_dir.parent / "browser-launch.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            return lock, fd
        except FileExistsError as exc:
            try:
                owner = int(lock.read_text(encoding="utf-8"))
                if not _process_is_alive(owner):
                    raise OSError(owner)
            except (OSError, ValueError):
                lock.unlink(missing_ok=True)
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                return lock, fd
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = browser_launch_lock\ndetail = another AgentRelay browser launcher is active") from exc

    @staticmethod
    def _release_launch_lock(lock: Path, fd: int) -> None:
        os.close(fd); lock.unlink(missing_ok=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"

    def _cdp_json(self, path: str = "/json/version") -> dict:
        try:
            with urllib.request.urlopen(self.endpoint + path, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = chrome_cdp\ndetail = CDP endpoint unavailable at {self.endpoint}") from exc

    def chrome_preflight(self) -> dict:
        result = ChromeResolver(self.chrome_path).preflight()
        result["cdp_endpoint"] = self.endpoint
        self.last_diagnostics = result
        return result

    def _launch(self, url: str) -> dict:
        info = self.chrome_preflight()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._cdp_json()
            return info | {"launched": False, "attached_existing": True}
        except HumanRequired:
            pass
        lock, fd = self._acquire_launch_lock()
        try:
            try:
                self._cdp_json()
                return info | {"launched": False, "attached_existing": True}
            except HumanRequired:
                pass
            args = [info["path"], f"--user-data-dir={self.profile_dir}", "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={self.debug_port}", "--no-startup-window", "--no-first-run", "--no-default-browser-check",
                    "--disable-sync", "--disable-features=SigninInterceptFirstRunExperience"]
            if self.browser_mode == "headless":
                args.insert(1, "--headless=new")
            else:
                args.insert(1, "--start-minimized")
            args.append(url)
            try:
                process = spawn_background(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = chrome_launch\ndetail = failed to launch normal Chrome: {exc}") from exc
            self.last_diagnostics.update({"chrome_pid": process.pid, "command_line": args, "profile_dir": str(self.profile_dir), "browser_mode": self.browser_mode})
            deadline = time.monotonic() + min(30, self.timeout_ms / 1000)
            while time.monotonic() < deadline:
                try:
                    self._cdp_json(); return info | {"launched": True, "attached_existing": False, "chrome_pid": process.pid, "browser_mode": self.browser_mode}
                except HumanRequired:
                    time.sleep(0.5)
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = chrome_cdp\ndetail = Chrome did not expose loopback CDP at {self.endpoint}")
        finally:
            self._release_launch_lock(lock, fd)

    @staticmethod
    def _is_challenge(page) -> bool:
        try:
            title = (page.title() or "").lower()
            if any(x in title for x in ("just a moment", "verify you are human", "checking your browser", "enable javascript")):
                return True
            for label in ("Verify you are human", "Checking your browser"):
                locator = page.get_by_text(label, exact=True)
                if locator.count() and any(locator.nth(i).is_visible() for i in range(locator.count())):
                    return True
            return False
        except Exception:
            return False

    def _attach_page(self, url: str, open_if_missing: bool = True):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = playwright\naction = pip install -e .") from exc
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(self.endpoint)
        except Exception as exc:
            pw.stop()
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = chrome_cdp_attach\ndetail = Playwright could not attach to {self.endpoint}") from exc
        contexts = browser.contexts
        if not contexts:
            pw.stop()
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = chrome_context\ndetail = CDP reported no browser context")
        context = contexts[0]
        canonical = url.rstrip("/")
        page = next((p for p in context.pages if p.url.rstrip("/") == canonical), None)
        if page is None:
            if not open_if_missing:
                pw.stop()
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = auditor_page\ndetail = exact auditor URL is not open")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        for extra in list(context.pages):
            if extra is not page and extra.url.lower() in {"about:blank", "chrome://newtab/", "chrome://newtab"}:
                try: extra.close()
                except Exception: pass
        return pw, browser, context, page

    def _composer(self, page, deadline: float):
        while time.monotonic() < deadline:
            if self._is_challenge(page):
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = chatgpt_challenge\ndetail = human authentication or challenge is visible; Chrome remains open")
            try:
                boxes = page.get_by_role("textbox")
                for idx in range(boxes.count() - 1, -1, -1):
                    box = boxes.nth(idx)
                    if box.is_visible() and box.is_enabled(): return box
            except Exception:
                pass
            page.wait_for_timeout(500)
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = chatgpt_composer\ndetail = exact auditor page did not expose an enabled composer")

    def auth(self, url: str) -> None:
        self._launch(url)
        pw, browser, _, page = self._attach_page(url)
        try:
            self._composer(page, time.monotonic() + 300)
            self.last_diagnostics.update({"ready": True, "url": page.url})
        finally:
            pw.stop()

    def send_probe(self, url: str) -> None:
        self._launch(url)
        pw, browser, _, page = self._attach_page(url)
        try:
            box = self._composer(page, time.monotonic() + self.timeout_ms / 1000)
            box.fill("ORCHESTRATOR_BROWSER_PROBE")
            box.press("Enter")
            self.last_diagnostics.update({"probe_submitted": True, "url": page.url})
        finally:
            pw.stop()

    def send_audit_request(self, project: ProjectConfig, event: OrchestratorEvent, delivery_message_id: str) -> None:
        payload = {"ORCHESTRATOR_AUDIT_REQUEST": True, "project_id": event.project_id, "run_id": event.run_id, "round_id": event.round_id,
                   "delivery_message_id": delivery_message_id, "target_worker_email": project.worker_email,
                   **event.payload,
                   "instruction": ("The worker reports completion. Git delivery has been independently verified. "
                                   "Inspect the repository/delivery directly; do not rely on a worker receipt. "
                                   "If accepted, send the next TASK Gmail. If rejected, send a CORRECTIVE Gmail. "
                                   "If human judgment is required, stop progression."),
                   "gmail_subject_prefix": project.gmail_subject_prefix,
                   "AUDITOR_RETURN_PROTOCOL": AUDITOR_RETURN_PROTOCOL}
        self._launch(project.auditor_chat_url)
        pw, browser, _, page = self._attach_page(project.auditor_chat_url)
        try:
            box = self._composer(page, time.monotonic() + self.timeout_ms / 1000)
            box.fill(json.dumps(payload, indent=2)); box.press("Enter")
            self.last_diagnostics.update({"audit_submitted": True, "url": page.url})
        finally:
            pw.stop()


class CodexWorkerAdapter:
    def __init__(self, configured_path: str | None = None, base_dir: Path | None = None):
        self.configured_path, self.base_dir = configured_path, base_dir or Path.cwd()
        self.command: list[str] | None = None
        self.selected_path: str | None = None
        self.selected_version: str | None = None
        self.selection_reason: str | None = None
        self.persist_path = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "CodexOrchestrator" / "config" / "codex_cli.json"

    def _candidates(self) -> list[tuple[Path, str]]:
        paths: list[tuple[Path, str]] = []
        if self.persist_path.exists():
            try:
                saved = json.loads(self.persist_path.read_text(encoding="utf-8")); saved_path = saved.get("path")
                if saved_path: paths.append((Path(saved_path), "persisted validated CLI"))
            except (OSError, json.JSONDecodeError): pass
        if self.configured_path:
            p = Path(self.configured_path)
            paths.append((p if p.is_absolute() else self.base_dir / p, "explicit configured path"))
        if found := shutil.which("codex"):
            paths.append((Path(found), "PATH resolution"))
        appdata = os.environ.get("APPDATA")
        if appdata: paths.append((Path(appdata) / "npm" / "codex.cmd", "user-level npm CLI"))
        paths.append((self.base_dir / ".codex-cli/node_modules/.bin/codex.cmd", "project-local official npm CLI"))
        return paths

    @staticmethod
    def _command_for(path: Path) -> list[str]:
        lower = str(path).lower()
        if os.name == "nt" and lower.endswith(os.sep + "codex"):
            raise PermissionError("extensionless npm shim is not a standalone Windows executable")
        if lower.endswith(".cmd"):
            global_script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            script = global_script if global_script.exists() else path.parent.parent / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node") or os.environ.get("NODE", "node")
            if not script.exists(): raise FileNotFoundError(f"CLI wrapper target missing: {script}")
            return [node, str(script)]
        return [str(path)]

    def _validate_candidate(self, path: Path) -> tuple[list[str], str]:
        if not path.exists(): raise FileNotFoundError(str(path))
        if "windowsapps" in str(path).lower() or "openai.codex" in str(path).lower():
            raise PermissionError("MSIX desktop-app private executable is not a standalone CLI")
        command = self._command_for(path)
        result = run_hidden([*command, "--version"], capture_output=True, text=True, timeout=20, check=True)
        version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        if not version.lower().startswith("codex-cli"):
            raise RuntimeError(f"unexpected CLI version output: {version[:100]}")
        return command, version

    def preflight(self) -> dict:
        try:
            errors = []
            for candidate, reason in self._candidates():
                try:
                    command, version = self._validate_candidate(candidate)
                    help_text = run_hidden([*command, "exec", "--help"], capture_output=True, text=True, timeout=20, check=True)
                    resume_text = run_hidden([*command, "exec", "resume", "--help"], capture_output=True, text=True, timeout=20, check=True)
                    if "resume" not in help_text.stdout.lower() or "--json" not in help_text.stdout.lower():
                        raise RuntimeError("exec help lacks resume or JSON support")
                    self.command, self.selected_path, self.selected_version, self.selection_reason = command, str(candidate), version, reason
                    if "persisted" not in reason:
                        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
                        self.persist_path.write_text(json.dumps({"path": str(candidate), "invocation_kind": "WINDOWS_CMD_WRAPPER" if str(candidate).lower().endswith(".cmd") else "NATIVE_EXECUTABLE", "version": version, "json_output_supported": True, "resume_supported": True, "validated_at": datetime.now(UTC).isoformat()}, indent=2), encoding="utf-8")
                    return {"path": str(candidate), "version": version, "selection_reason": reason, "resume_supported": True, "json_output_supported": True}
                except (OSError, subprocess.SubprocessError, RuntimeError, PermissionError) as exc:
                    errors.append(f"{candidate}: {exc}")
        except Exception as exc:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = codex_cli_execution\ndetail = {exc}") from exc
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = codex_cli_execution\ndetail = no validated standalone CLI candidate; " + " | ".join(errors))

    def run_instruction(self, project: ProjectConfig, instruction: str, prior_session: str | None) -> str:
        workspace = Path(project.local_workspace); workspace.mkdir(parents=True, exist_ok=True)
        if not self.command: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = codex_preflight\ndetail = worker was not preflighted")
        command = [*self.command, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json"]
        if prior_session:
            command = [*self.command, "exec", "resume", prior_session, "--skip-git-repo-check", "--json"]
        command += [f"{AGENTRELAY_EXECUTION_CONTRACT}\nTASK FROM AGENTRELAY\n{instruction}"]
        try:
            result = run_hidden(command, cwd=workspace, capture_output=True, text=True, timeout=600, check=True)
        except subprocess.CalledProcessError as exc:
            stdout = (exc.stdout or "")[-4000:]
            stderr = (exc.stderr or "")[-4000:]
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = codex_worker_execution\nexit_code = {exc.returncode}\nargv = {command!r}\ncwd = {workspace}\nCODEX_HOME = {os.environ.get('CODEX_HOME', '<default>')}\nstdout = {stdout}\nstderr = {stderr}") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = codex_worker_execution\nargv = {command!r}\ncwd = {workspace}\ndetail = {exc}") from exc
        session_id = None
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") in {"thread.started", "session.started"}:
                session_id = item.get("thread_id") or item.get("session_id")
        if prior_session:
            return prior_session
        if not session_id:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = codex_session_identifier\ndetail = CLI completed without a recognizable session identifier")
        return session_id

    def run_dummy_task(self, project: ProjectConfig, run_id: str, round_id: str, prior_session: str | None) -> str:
        workspace = Path(project.local_workspace); workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "dummy_round.txt").write_text(f"run_id={run_id}\nround_id={round_id}\n", encoding="utf-8")
        return self.run_instruction(project, ("Dummy orchestration task only. Do not access Git remotes or real projects. "
            f"Record harmless successful completion for run {run_id}, round {round_id} in the current dummy workspace."), prior_session)
