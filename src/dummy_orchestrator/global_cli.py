from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import hashlib
import ctypes
from importlib import metadata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from pathlib import Path

from .adapters import ChatGPTWebAuditorAdapter, CodexWorkerAdapter, GmailInstructionBus, GitDeliveryAdapter, HumanRequired, subject
from .models import EventType, OrchestratorEvent, ProjectState
from .registry import ProjectRegistrationStore, discover_registration, normalize_project_id
from .state import StateStore
from .process_policy import run_hidden, spawn_background
from .version import __version__


TOOL_VERSION = __version__

def _source_root(source: Path) -> Path | None:
    for candidate in source.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
            return candidate
    return None


def _git_identity(root: Path | None) -> dict[str, object]:
    if root is None:
        return {"git_revision": None, "git_branch": None, "git_dirty": None}
    try:
        revision = run_hidden(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        branch = run_hidden(["git", "-C", str(root), "branch", "--show-current"], capture_output=True, text=True, timeout=5).stdout.strip()
        status = run_hidden(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, timeout=5).stdout
        return {"git_revision": revision or None, "git_branch": branch or None, "git_dirty": bool(status.strip())}
    except Exception:
        return {"git_revision": None, "git_branch": None, "git_dirty": None}


def source_identity() -> dict:
    source = Path(__file__).resolve()
    package_root = _source_root(source)
    package_files = sorted((package_root / "src/dummy_orchestrator").glob("*.py")) if package_root else sorted(source.parent.glob("*.py"))
    digest = hashlib.sha256()
    for path in package_files:
        relative = path.relative_to(package_root).as_posix() if package_root else path.name
        digest.update(relative.encode()); digest.update(path.read_bytes())
    try:
        metadata_version = metadata.version("dummy-agent-orchestrator")
    except metadata.PackageNotFoundError:
        metadata_version = None
    git = _git_identity(package_root)
    source_package = package_root / "src/dummy_orchestrator" if package_root else None
    return {
        "version": __version__,
        "version_source": str(source_package / "version.py") if source_package else str(source.parent / "version.py"),
        "install_source": str(package_root) if package_root else None,
        "package_path": str(source),
        "install_mode": "editable/source" if package_root else "installed",
        "installed_metadata_version": metadata_version,
        "version_consistent": metadata_version in (None, __version__),
        "source_fingerprint": digest.hexdigest()[:16],
        **git,
    }

AGENT_GUIDE = """You are an AgentRelay-managed bounded worker.

Work only on the current TASK or CORRECTIVE. When complete:
1. run the required validation/tests;
2. commit;
3. push to the registered worker branch;
4. run `agent-relay deliver`;
5. end this work turn.

Do not wait for ChatGPT. Do not poll Gmail or start the next phase yourself.
AgentRelay independently verifies Git delivery, contacts the exact auditor,
waits for Gmail, and resumes the managed persistent worker for the next task.
If AgentRelay reports HUMAN_REQUIRED, stop.
"""


def data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "CodexOrchestrator"


def _store() -> ProjectRegistrationStore:
    return ProjectRegistrationStore(data_root())


def _state() -> StateStore:
    return StateStore(data_root() / "state" / "orchestrator.sqlite")


def _bus() -> GmailInstructionBus:
    return GmailInstructionBus(data_root() / "secrets" / "oauth-client.json", data_root() / "secrets" / "oauth-token.json")


def _validate_auditor_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"chatgpt.com", "www.chatgpt.com"} or not parsed.path.startswith("/c/"):
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = auditor_chat_url\ndetail = expected https://chatgpt.com/c/<conversation>")
    return url


def _write_handoff(registration) -> Path:
    path = data_root() / "projects" / registration.project_id / "handoff.json"; path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"project_id": registration.project_id, "repository_root": registration.repository_root,
               "git_remote": registration.git_remote, "worker_branch": registration.worker_branch,
               "head_sha": registration.head_sha, "continuity_note": "Interactive desktop Codex context is not assumed resumable; managed worker is the autonomous identity."}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def register(args: argparse.Namespace) -> dict:
    if args.mode == "production" and not getattr(args, "worker_email", None):
        try: args.worker_email = _bus().resolve_recipient("")
        except Exception as exc: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = worker_email\ndetail = pass --worker-email or make Gmail OAuth identity available") from exc
    item = discover_registration(args.project_id, _validate_auditor_url(args.auditor_url), Path.cwd(), args.worker_branch, tuple(args.protected_branch))
    if args.mode != "production":
        item = type(item)(**{**item.__dict__, "mode": args.mode})
    if args.worker_email:
        item = type(item)(**{**item.__dict__, "worker_email": args.worker_email})
    saved = _store().register(item)
    _state().ensure_project(saved.project_id)
    _state().transition(saved.project_id, ProjectState.IDLE, {"registered": True}, repository_root=saved.repository_root, git_remote=saved.git_remote, worker_branch=saved.worker_branch, protected_branches=json.dumps(saved.protected_branches), persistent_codex_session=saved.persistent_codex_session, error=None)
    return saved.__dict__


def adopt(args: argparse.Namespace) -> dict:
    if args.mode == "production" and not args.worker_email:
        try: args.worker_email = _bus().resolve_recipient("")
        except Exception as exc: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = worker_email\ndetail = pass --worker-email or make Gmail OAuth identity available") from exc
    result = register(args)
    registration = _store().get(result["project_id"])
    handoff = _write_handoff(registration)
    result["handoff_package"] = str(handoff)
    result["managed_worker"] = "existing" if registration.persistent_codex_session else "bootstrap-required"
    if args.bootstrap and not registration.persistent_codex_session:
        boot = bootstrap_worker(argparse.Namespace(project_id=registration.project_id))
        result.update(boot); result["managed_worker"] = "ready"
    return result


def agent_guide(args: argparse.Namespace):
    if args.format == "json":
        return {"version": TOOL_VERSION, "contract": AGENT_GUIDE, "completion_command": "agent-relay deliver", "stop_condition": "HUMAN_REQUIRED"}
    return AGENT_GUIDE


def identify(args: argparse.Namespace) -> dict:
    try:
        registration = _resolve(args)
    except HumanRequired:
        return {"managed": False, "cwd": str(Path.cwd()), "next_action": "agent-relay adopt --project-id <id> --auditor-url <url>"}
    state = _state().project(registration.project_id)
    return {"managed": True, "project_id": registration.project_id, "auditor_registered": True,
            "worker": "managed persistent worker" if registration.persistent_codex_session else "bootstrap required",
            "state": state["state"], "next_worker_action": "run `agent-relay deliver` after the assigned work is complete",
            "guide": "agent-relay agent-guide"}


def doctor(_args) -> dict:
    registry = _store(); root = data_root(); result = source_identity() | {"global_command": True,
        "registry_readable": True, "registered_projects": len(registry.all()), "runtime_root": str(root),
        "browser_mode": "headful_background", "managed_worker_capability": True,
        "gmail_worker_routing": {item.project_id: ("READY" if item.worker_email else "HUMAN_REQUIRED: worker_email") for item in registry.all()}}
    try:
        _bus().resolve_recipient(""); result["gmail"] = "GMAIL_UNATTENDED_PASS"
    except HumanRequired as exc:
        result["gmail"] = "HUMAN_REQUIRED: " + (str(exc).splitlines()[1] if len(str(exc).splitlines()) > 1 else str(exc))
    except Exception as exc:
        result["gmail"] = f"UNAVAILABLE: {type(exc).__name__}"
    auditor = ChatGPTWebAuditorAdapter(Path(os.environ.get("LOCALAPPDATA", ".")) / "CodexOrchestrator" / "profiles" / "chatgpt", 10)
    try:
        auditor._cdp_json(); result["cdp"] = "reachable"; result["chatgpt"] = "profile/cdp reachable"
    except HumanRequired:
        result["cdp"] = "not running"; result["chatgpt"] = "not probed (no browser launched by doctor)"
    result["service"] = service(argparse.Namespace(action="status"))
    try:
        worker = CodexWorkerAdapter(None, Path(__file__).resolve().parents[2]); probe = worker.preflight()
        result["codex_cli"] = probe | {"ready": True, "invocation_kind": "WINDOWS_CMD_WRAPPER" if probe["path"].lower().endswith(".cmd") else "NATIVE_EXECUTABLE", "persisted": "persisted" in probe["selection_reason"]}
    except HumanRequired as exc:
        result["codex_cli"] = {"ready": False, "error": str(exc)}
    return result

def send_event(args) -> dict:
    registration = _store().get(args.project_id)
    if registration is None: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = registered_project\ndetail = project is not registered")
    if not registration.worker_email or "@" not in registration.worker_email:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = worker_email\ndetail = registered project has no valid worker mailbox")
    record = _state().project(args.project_id)
    if record.get("run_id") != args.run_id: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = active_run\ndetail = supplied run_id does not match active project run")
    try: event_type = EventType(args.type)
    except ValueError as exc: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = event_type\ndetail = expected TASK, CORRECTIVE, DELIVERY, DONE, or TERMINAL_CONTROL") from exc
    instruction = Path(args.instruction_file).read_text(encoding="utf-8").strip()
    if not instruction: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = instruction_file\ndetail = instruction is empty")
    if _state().outbound_exists(args.project_id, args.run_id, args.round_id, event_type):
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = duplicate_recovery_event\ndetail = canonical recovery event already sent for this project/run/round/type")
    bus = _bus(); project = registration.to_project_config(); payload = {"instruction": instruction, "operator_recovery": True}
    event = OrchestratorEvent(args.project_id, args.run_id, args.round_id, event_type, payload)
    message_id = bus.send(event, project.worker_email)
    _state().record_outbound_event(args.project_id, args.run_id, args.round_id, event_type, message_id, payload)
    return {"operator_recovery": True, "message_id": message_id, "subject": subject(project.gmail_subject_prefix, event), "event": {"ORCHESTRATOR_EVENT": {"schema_version": 1, "project_id": args.project_id, "run_id": args.run_id, "round_id": args.round_id, "event_type": str(event_type), "payload": payload}}}

def configure_project(args) -> dict:
    store = _store()
    current = store.get(args.project_id)
    auditor_url = getattr(args, "auditor_url", None)
    worker_email = getattr(args, "worker_email", None)
    if auditor_url is not None and worker_email is not None:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = configuration_change\ndetail = configure one of auditor_url or worker_email at a time")
    if auditor_url is not None:
        validated_url = _validate_auditor_url(auditor_url)
        updated = store.configure_auditor_url(current.project_id, validated_url)
        changed = updated.auditor_chat_url != current.auditor_chat_url
        return {"project_id": updated.project_id, "old_auditor_url": current.auditor_chat_url,
                "new_auditor_url": updated.auditor_chat_url, "repository_root": updated.repository_root,
                "worker_branch": updated.worker_branch, "changed": changed, "unchanged": not changed}
    if worker_email is not None:
        updated = store.configure_worker_email(current.project_id, worker_email)
        return {"project_id": updated.project_id, "worker_email": updated.worker_email, "gmail_worker_routing": "READY"}
    raise HumanRequired("HUMAN_REQUIRED\nmissing_field = configuration_change\ndetail = pass --auditor-url or --worker-email")

def resend_audit(args) -> dict:
    registration = _store().get(args.project_id); record = _state().project(args.project_id)
    if record.get("run_id") != args.run_id: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = active_run\ndetail = supplied run_id does not match active project run")
    project = registration.to_project_config(); bus = _bus(); matches = []
    for item in bus.poll(project, args.run_id):
        if item.event_type == EventType.DELIVERY and (item.payload.get("delivery_sha") or item.payload.get("local_sha")) == args.delivery_sha:
            matches.append(item)
    if len(matches) != 1: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = delivery_sha\ndetail = expected exactly one matching canonical delivery Gmail")
    if not _state().mark_audit_replay_once(args.delivery_sha, args.project_id, args.run_id, args.round_id):
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = duplicate_audit_replay\ndetail = audit replay already submitted for this delivery SHA")
    source = matches[0]; event = OrchestratorEvent(args.project_id, args.run_id, args.round_id, EventType.DELIVERY, source.payload, source.gmail_message_id)
    auditor = ChatGPTWebAuditorAdapter(Path(os.environ.get("LOCALAPPDATA", ".")) / "CodexOrchestrator" / "profiles" / "chatgpt", 60)
    try:
        auditor.send_audit_request(project, event, source.gmail_message_id or args.delivery_sha)
    except Exception:
        # A failed transport must remain retryable; remove the reservation.
        with _state().connect() as con: con.execute("DELETE FROM audit_replays WHERE delivery_sha=?", (args.delivery_sha,))
        raise
    return {"delivery_sha": args.delivery_sha, "delivery_message_id": source.gmail_message_id, "project_id": args.project_id, "run_id": args.run_id, "round_id": args.round_id, "audit_replay": True}


def browser_recover(args) -> dict:
    registration = _resolve(args)
    auditor = ChatGPTWebAuditorAdapter(Path(os.environ.get("LOCALAPPDATA", ".")) / "CodexOrchestrator" / "profiles" / "chatgpt", 60, browser_mode="headful")
    auditor.auth(registration.auditor_chat_url)
    return {"browser": "headful recovery ready", "profile": str(auditor.profile_dir), "cdp": auditor.endpoint}


def _resolve(args: argparse.Namespace):
    return _store().resolve(args.project_id, Path.cwd())


def deliver(args: argparse.Namespace) -> dict:
    registration = _resolve(args)
    if registration.mode != "production":
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = production_mode\ndetail = global deliver only accepts registered production projects")
    project = registration.to_project_config()
    bus = _bus()
    project = type(project)(**{**project.__dict__, "worker_email": bus.resolve_recipient(project.worker_email)})
    state = _state(); state.ensure_project(project.project_id); record = state.project(project.project_id)
    run_id = record["run_id"] or f"RUN-{uuid.uuid4().hex[:10]}"
    phase_id = args.phase_id or args.round_id or record.get("active_event_round_id") or record.get("round_id")
    if not phase_id:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = active_event_round_id\ndetail = delivery requires a persisted active event identity")
    delivery_id, event = GitDeliveryAdapter(bus).deliver(project, run_id, phase_id, args.baseline_sha, args.task_id, phase_id)
    note = args.note
    if args.note_file:
        note = Path(args.note_file).read_text(encoding="utf-8")
    event = OrchestratorEvent(event.project_id, event.run_id, event.round_id, event.event_type, event.payload | {"gmail_event_type": "DELIVERED", "worker_note": note} if note else event.payload | {"gmail_event_type": "DELIVERED"}, delivery_id)
    if delivery_id and not state.mark_delivery_once(delivery_id, project.project_id, run_id, phase_id):
        return {"duplicate": True, "delivery_message_id": delivery_id}
    state.transition(project.project_id, ProjectState.AUDIT_PENDING, {"production_delivery_verified": True}, run_id=run_id, round_id=phase_id, last_gmail_id=delivery_id, repository_root=project.repository_root, git_remote=project.git_remote, worker_branch=project.worker_branch, error=None)
    if delivery_id and not state.mark_audit_once(delivery_id, project.project_id, run_id, phase_id):
        state.transition(project.project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_duplicate_suppressed": True})
        return {"duplicate": True, "delivery_message_id": delivery_id, "state": ProjectState.WAITING_FOR_AUDITOR_GMAIL}
    auditor = ChatGPTWebAuditorAdapter(Path(os.environ.get("LOCALAPPDATA", ".")) / "CodexOrchestrator" / "profiles" / "chatgpt", 60)
    auditor.send_audit_request(project, event, delivery_id or "local-verified")
    state.transition(project.project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_sent": True})
    ensured = service(argparse.Namespace(action="ensure"))
    if not ensured.get("running"):
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_service\ndetail = service could not be maintained after delivery")
    return {"project_id": project.project_id, "run_id": run_id, "phase_id": phase_id, "delivery": event.payload, "delivery_message_id": delivery_id, "state": ProjectState.WAITING_FOR_AUDITOR_GMAIL}


def status(args: argparse.Namespace) -> dict:
    registration = _resolve(args)
    result = _state().project(registration.project_id)
    result["registration"] = registration.__dict__
    result["gmail_worker_routing"] = "READY" if registration.worker_email else "HUMAN_REQUIRED: worker_email"
    result["diagnostics"] = _state().diagnostics(registration.project_id)
    return result


def bind_current_worker(args: argparse.Namespace) -> dict:
    registration = _resolve(args)
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = CODEX_THREAD_ID\ndetail = current Codex environment did not expose a supported thread identifier")
    updated = type(registration)(**{**registration.__dict__, "persistent_codex_session": thread_id})
    _store().register(updated)
    _state().transition(registration.project_id, _state().project(registration.project_id)["state"], {"current_worker_binding": "identified_via_CODEX_THREAD_ID"}, persistent_codex_session=thread_id, current_thread_id=thread_id)
    return {"project_id": registration.project_id, "thread_id": thread_id, "binding": "identified; CLI resume compatibility requires managed-worker bootstrap verification"}


def bootstrap_worker(args: argparse.Namespace) -> dict:
    registration = _resolve(args)
    project = registration.to_project_config()
    package_root = Path(__file__).resolve().parents[2]
    worker = CodexWorkerAdapter(None, package_root)
    worker.preflight()
    session = worker.run_instruction(project, "Bootstrap only: confirm this dedicated persistent worker context is ready for bounded instructions. Do not modify files, use Git, or access remotes.", None)
    updated = type(registration)(**{**registration.__dict__, "persistent_codex_session": session})
    _store().register(updated)
    _state().transition(registration.project_id, ProjectState.IDLE, {"managed_worker_bootstrap": True}, worker_session_id=session, persistent_codex_session=session)
    return {"project_id": registration.project_id, "persistent_codex_session": session, "mode": "managed-worker"}


def pause(args: argparse.Namespace) -> dict:
    registration = _resolve(args)
    stop = data_root() / "stops" / f"{registration.project_id}.STOP"; stop.parent.mkdir(parents=True, exist_ok=True); stop.write_text("operator pause\n", encoding="utf-8")
    _state().transition(registration.project_id, ProjectState.STOPPED, {"reason": "operator pause"})
    return {"project_id": registration.project_id, "state": ProjectState.STOPPED}


def projects(_args: argparse.Namespace) -> list[dict]:
    return [item.__dict__ for item in _store().all()]


def supervise_once() -> list[dict]:
    """Wake bounded managed workers for newly accepted TASK/CORRECTIVE mail."""
    state = _state(); bus = _bus(); package_root = Path(__file__).resolve().parents[2]; results = []
    for registration in _store().all():
        record = state.project(registration.project_id)
        if not record["run_id"] or not registration.persistent_codex_session:
            continue
        project = registration.to_project_config()
        try:
            events = bus.poll(project, record["run_id"])
            for diagnostic in bus.diagnostics:
                state.record_diagnostic(project.project_id, diagnostic["code"], diagnostic.get("message_id"), diagnostic)
            for event in events:
                if not event.gmail_message_id:
                    continue
                if event.event_type == EventType.TERMINAL_CONTROL:
                    if state.is_processed(event.gmail_message_id, project.project_id): continue
                    state.mark_processed(event.gmail_message_id, project.project_id)
                    state.transition(project.project_id, ProjectState.COMPLETE, {"terminal_control": True, "instruction": event.payload.get("instruction"), "gmail_message_id": event.gmail_message_id}, run_id=event.run_id, round_id=event.round_id, last_gmail_id=event.gmail_message_id, active_event_round_id=event.round_id, active_event_type=str(event.event_type), active_event_gmail_id=event.gmail_message_id, error=None)
                    results.append({"project_id": project.project_id, "round_id": event.round_id, "event_type": str(event.event_type), "terminal": True})
                    continue
                if event.event_type not in (EventType.TASK, EventType.CORRECTIVE):
                    continue
                if state.is_processed(event.gmail_message_id, project.project_id):
                    continue
                ready = ProjectState.CORRECTIVE_READY if event.event_type == EventType.CORRECTIVE else ProjectState.TASK_READY
                state.transition(project.project_id, ready, {"gmail_instruction": True}, run_id=event.run_id, round_id=event.round_id, last_gmail_id=event.gmail_message_id, active_event_round_id=event.round_id, active_event_type=str(event.event_type), active_event_gmail_id=event.gmail_message_id)
                state.transition(project.project_id, ProjectState.WORKER_RUNNING, {"bounded_resume": True})
                worker = CodexWorkerAdapter(None, package_root); codex_probe = worker.preflight()
                session = worker.run_instruction(project, json.dumps(event.payload, sort_keys=True), registration.persistent_codex_session)
                if session != registration.persistent_codex_session:
                    raise HumanRequired("HUMAN_REQUIRED\nmissing_field = persistent_worker_continuity\ndetail = managed worker resume returned a different session")
                state.mark_processed(event.gmail_message_id, project.project_id)
                state.transition(project.project_id, ProjectState.WAITING_FOR_DELIVERY, {"worker_turn_ended": True, "selected_codex_command": codex_probe.get("path"), "invocation_kind": "WINDOWS_CMD_WRAPPER" if codex_probe.get("path", "").lower().endswith(".cmd") else "NATIVE_EXECUTABLE", "codex_version": codex_probe.get("version")}, worker_session_id=session)
                results.append({"project_id": project.project_id, "round_id": event.round_id, "event_type": str(event.event_type), "worker_session_id": session})
        except HumanRequired as exc:
            state.transition(registration.project_id, ProjectState.HUMAN_REQUIRED, {"supervisor_error": str(exc)}, error=str(exc))
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            state.transition(registration.project_id, ProjectState.ERROR, {"supervisor_error": detail}, error=detail)
    return results


def resume_project(args) -> dict:
    if args.project_id:
        registration = _resolve(args); current = _state().project(registration.project_id)
        if current["state"] == ProjectState.HUMAN_REQUIRED and current.get("last_gmail_id"):
            _state().requeue_processed(current["last_gmail_id"], registration.project_id)
            _state().transition(registration.project_id, ProjectState.WAITING_FOR_INSTRUCTION, {"operator_resume_requeued": True})
    events = supervise_once()
    if args.project_id:
        try:
            result = status(args)
            result["woken_events"] = events
            return result
        except HumanRequired:
            pass
    return {"woken_events": events}


SERVICE_START_TIMEOUT_SECONDS = 12.0
SERVICE_STOP_TIMEOUT_SECONDS = 8.0
SERVICE_POLL_SECONDS = 0.1
SERVICE_HEARTBEAT_MAX_AGE_SECONDS = 30.0
SERVICE_OWNER = "agentrelay-supervisor"


def _service_paths() -> dict[str, Path]:
    path = data_root() / "service"
    return {
        "root": path,
        "pid": path / "supervisor.pid",
        "ready": path / "supervisor.ready.json",
        "heartbeat": path / "supervisor.heartbeat.json",
        "stop": path / "supervisor.stop.json",
        "lock": path / "supervisor.start.lock",
    }


def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _read_pid_record(path: Path) -> dict[str, object] | None:
    """Read current metadata, accepting the pre-0.3.8 integer format safely."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        try:
            value = int(raw)
        except ValueError:
            return {"invalid": True}
    if isinstance(value, int):
        return {"pid": value, "legacy": True}
    if isinstance(value, dict):
        return value
    return {"invalid": True}


def _record_pid(record: dict[str, object] | None) -> int | None:
    if not record or record.get("invalid"):
        return None
    try:
        return int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _owned_record(record: dict[str, object] | None) -> bool:
    return bool(record and record.get("owner") == SERVICE_OWNER and record.get("generation"))


def _same_identity(record: dict[str, object] | None, other: dict[str, object] | None) -> bool:
    return bool(record and other and record.get("pid") == other.get("pid")
                and record.get("generation") == other.get("generation"))


def _cleanup_owned_artifacts(paths: dict[str, Path], record: dict[str, object] | None) -> None:
    """Remove only artifacts proven to belong to this AgentRelay generation."""
    if not _owned_record(record):
        return
    current = _read_pid_record(paths["pid"])
    if _same_identity(current, record):
        paths["pid"].unlink(missing_ok=True)
    for key in ("ready", "heartbeat", "stop"):
        value = _read_json(paths[key])
        if _same_identity(value, record):
            paths[key].unlink(missing_ok=True)


def _service_status(paths: dict[str, Path]) -> dict[str, object]:
    record = _read_pid_record(paths["pid"])
    pid = _record_pid(record)
    alive = bool(pid and _pid_alive(pid))
    ready = _read_json(paths["ready"])
    heartbeat = _read_json(paths["heartbeat"])
    identity_ready = _same_identity(record, ready)
    identity_heartbeat = _same_identity(record, heartbeat)
    heartbeat_age = None
    if identity_heartbeat:
        try:
            heartbeat_age = max(0.0, time.time() - float(heartbeat["heartbeat_epoch"]))
        except (KeyError, TypeError, ValueError):
            heartbeat_age = None
    certified = bool(alive and _owned_record(record) and identity_ready and identity_heartbeat
                     and heartbeat_age is not None and heartbeat_age <= SERVICE_HEARTBEAT_MAX_AGE_SECONDS)
    if record and not alive and (_owned_record(record) or record.get("legacy")):
        _cleanup_owned_artifacts(paths, record)
        if record.get("legacy"):
            paths["pid"].unlink(missing_ok=True)
        record = None
        pid = None
    result: dict[str, object] = {"running": certified, "pid": pid if certified else None}
    if certified:
        result.update({"generation": record["generation"], "heartbeat_age_seconds": round(heartbeat_age or 0.0, 3)})
    elif record and pid and alive:
        # A live but uncertified PID is never overwritten or killed blindly.
        result["detail"] = "live supervisor metadata is not ready or is not AgentRelay-owned"
        result["observed_pid"] = pid
    return result


def _acquire_service_lock(paths: dict[str, Path]) -> str:
    token = json.dumps({"pid": os.getpid(), "created_epoch": time.time()})
    deadline = time.monotonic() + SERVICE_START_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(str(paths["lock"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
            return token
        except FileExistsError:
            lock = _read_json(paths["lock"])
            owner_pid = _record_pid(lock)
            if not owner_pid or not _pid_alive(owner_pid):
                paths["lock"].unlink(missing_ok=True)
                continue
            current = _service_status(paths)
            if current["running"]:
                return "already-running"
            if time.monotonic() >= deadline:
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_start_lock\ndetail = another AgentRelay supervisor start did not become ready")
            time.sleep(SERVICE_POLL_SECONDS)


def _release_service_lock(paths: dict[str, Path], token: str) -> None:
    if token == "already-running":
        return
    try:
        if paths["lock"].read_text(encoding="utf-8") == token:
            paths["lock"].unlink(missing_ok=True)
    except (FileNotFoundError, OSError):
        return


def _wait_for_service_ready(paths: dict[str, Path], expected_pid: int, generation: str,
                            child: subprocess.Popen[Any] | None = None) -> dict[str, object]:
    deadline = time.monotonic() + SERVICE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = _service_status(paths)
        if current.get("running") and current.get("generation") == generation:
            # On some Windows Python installations the detached launcher PID
            # differs from the final interpreter PID.  The generation-specific
            # publication is the authoritative child identity; retain the
            # launcher PID only as diagnostic evidence.
            return current | {"launch_pid": expected_pid}
        if child is not None and child.poll() is not None:
            break
        time.sleep(SERVICE_POLL_SECONDS)
    current = _service_status(paths)
    raise HumanRequired(
        "HUMAN_REQUIRED\nmissing_field = supervisor_readiness\n"
        f"detail = supervisor PID {expected_pid} did not publish matching ready/heartbeat evidence "
        f"within {SERVICE_START_TIMEOUT_SECONDS:.0f}s; observed={json.dumps(current, sort_keys=True)}"
    )


def service(args: argparse.Namespace) -> dict:
    _diag("service_command", action=args.action)
    paths = _service_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    if args.action == "status":
        result = _service_status(paths)
        _diag("service_status", **result)
        return result
    if args.action == "ensure":
        current = service(argparse.Namespace(action="status"))
        if current["running"]: return current | {"ensured": True}
        started = service(argparse.Namespace(action="start"))
        if not started.get("running"):
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_service\ndetail = failed to start supervisor")
        return started | {"ensured": True}
    if args.action == "stop":
        record = _read_pid_record(paths["pid"])
        pid = _record_pid(record)
        if not pid or not _owned_record(record):
            if pid and _pid_alive(pid):
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_ownership\ndetail = refusing to stop a live non-AgentRelay PID")
            if record and record.get("legacy"):
                paths["pid"].unlink(missing_ok=True)
            return {"running": False, "pid": None, "stop_requested": False}
        stop = {"owner": SERVICE_OWNER, "pid": pid, "generation": record["generation"], "requested_epoch": time.time()}
        _atomic_json_write(paths["stop"], stop)
        _diag("service_stop_requested", pid=pid, generation=record["generation"])
        deadline = time.monotonic() + SERVICE_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _service_status(paths)["running"]:
                return {"running": False, "pid": None, "stop_requested": True}
            time.sleep(SERVICE_POLL_SECONDS)
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_stop\ndetail = supervisor did not acknowledge the stop request")
    if args.action == "start":
        token = _acquire_service_lock(paths)
        try:
            current = service(argparse.Namespace(action="status"))
            if current["running"]: return current
            existing = _read_pid_record(paths["pid"])
            existing_pid = _record_pid(existing)
            if existing_pid and _pid_alive(existing_pid):
                if _owned_record(existing):
                    return _wait_for_service_ready(paths, existing_pid, str(existing["generation"]))
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_ownership\ndetail = refusing to overwrite a live non-AgentRelay PID record")
            if existing and existing.get("invalid"):
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = supervisor_metadata\ndetail = refusing to overwrite malformed supervisor metadata")
            if existing and _owned_record(existing):
                _cleanup_owned_artifacts(paths, existing)
            generation = uuid.uuid4().hex
            child_env = dict(os.environ)
            child_env.update({
                "AGENTRELAY_SUPERVISOR_GENERATION": generation,
                "AGENTRELAY_SUPERVISOR_PID_PATH": str(paths["pid"]),
                "AGENTRELAY_SUPERVISOR_READY_PATH": str(paths["ready"]),
                "AGENTRELAY_SUPERVISOR_HEARTBEAT_PATH": str(paths["heartbeat"]),
                "AGENTRELAY_SUPERVISOR_STOP_PATH": str(paths["stop"]),
            })
            command = [sys.executable, "-m", "dummy_orchestrator.global_cli", "_supervisor"]
            proc = spawn_background(command, detached=True, close_fds=True, cwd=str(data_root()),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env)
            _diag("supervisor_spawned", pid=proc.pid, generation=generation, argv=command,
                  creation_flags="detached_hidden_policy", cwd=str(data_root()))
            return _wait_for_service_ready(paths, proc.pid, generation, proc)
        finally:
            _release_service_lock(paths, token)
    raise ValueError(args.action)


def _diag(event: str, **details) -> None:
    """Best-effort event bridge; diagnostics must never affect orchestration."""
    try:
        from . import diagnostics
        session, _pid = diagnostics.current()
        if session:
            diagnostics.append(session / "timeline.jsonl", "agentrelay", event, **details)
    except Exception:
        return

def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            kernel = ctypes.windll.kernel32; handle = kernel.OpenProcess(0x1000, False, int(pid))
            if handle: kernel.CloseHandle(handle); return True
            return False
        except Exception: return False
    try: os.kill(pid, 0); return True
    except (OSError, ProcessLookupError, PermissionError): return False

def monitor(_args) -> int:
    from .monitor import Monitor
    Monitor(sys.modules[__name__]).run()
    return 0

def diagnostics_command(args):
    from . import diagnostics
    if args.action == "start": return diagnostics.start(args.tag)
    if args.action == "status": return diagnostics.status()
    if args.action == "stop": return diagnostics.stop()
    if args.action == "report": return diagnostics.report()
    raise ValueError(args.action)


def supervisor(_args: argparse.Namespace) -> int:
    paths = _service_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    generation = os.environ.get("AGENTRELAY_SUPERVISOR_GENERATION") or uuid.uuid4().hex
    env_paths = {
        "pid": os.environ.get("AGENTRELAY_SUPERVISOR_PID_PATH"),
        "ready": os.environ.get("AGENTRELAY_SUPERVISOR_READY_PATH"),
        "heartbeat": os.environ.get("AGENTRELAY_SUPERVISOR_HEARTBEAT_PATH"),
        "stop": os.environ.get("AGENTRELAY_SUPERVISOR_STOP_PATH"),
    }
    for key, value in env_paths.items():
        if value:
            paths[key] = Path(value)
    record = {"schema": 1, "owner": SERVICE_OWNER, "pid": os.getpid(), "generation": generation,
              "started_epoch": time.time(), "started_at": datetime.now(UTC).isoformat()}
    existing = _read_pid_record(paths["pid"])
    existing_pid = _record_pid(existing)
    if existing_pid and existing_pid != os.getpid() and _pid_alive(existing_pid):
        _diag("supervisor_refused_foreign_pid", pid=existing_pid, generation=generation)
        return 1
    if existing and _owned_record(existing):
        _cleanup_owned_artifacts(paths, existing)
    _atomic_json_write(paths["pid"], record)
    _atomic_json_write(paths["ready"], record | {"ready_epoch": time.time(), "ready_at": datetime.now(UTC).isoformat()})
    try:
        while True:
            try:
                current = _read_pid_record(paths["pid"])
                if not _same_identity(current, record):
                    return 0
                stop = _read_json(paths["stop"])
                if _same_identity(stop, record):
                    return 0
                heartbeat = record | {"heartbeat_epoch": time.time(), "heartbeat_at": datetime.now(UTC).isoformat()}
                _atomic_json_write(paths["heartbeat"], heartbeat)
                _diag("supervisor_heartbeat", pid=os.getpid())
                supervise_once()
                time.sleep(1)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                _diag("supervisor_exception", traceback=__import__("traceback").format_exc())
                crash = data_root() / "service" / "supervisor-crash.log"
                crash.parent.mkdir(parents=True, exist_ok=True)
                crash.write_text(f"{datetime.now(UTC).isoformat()} {type(exc).__name__}: {exc}\n", encoding="utf-8")
                time.sleep(1)
    finally:
        _cleanup_owned_artifacts(paths, record)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-relay")
    p.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("register"); q.add_argument("--project-id", required=True); q.add_argument("--auditor-url", required=True); q.add_argument("--worker-branch"); q.add_argument("--worker-email"); q.add_argument("--protected-branch", action="append", default=["main", "master", "develop"]); q.add_argument("--mode", choices=["production", "dummy"], default="production")
    q = sub.add_parser("adopt"); q.add_argument("--project-id", required=True); q.add_argument("--auditor-url", required=True); q.add_argument("--worker-branch"); q.add_argument("--worker-email"); q.add_argument("--protected-branch", action="append", default=["main", "master", "develop"]); q.add_argument("--mode", choices=["production", "dummy"], default="production"); q.add_argument("--bootstrap", action="store_true")
    q = sub.add_parser("project"); project_sub = q.add_subparsers(dest="project_action", required=True); q = project_sub.add_parser("configure"); q.add_argument("--project-id", required=True); q.add_argument("--worker-email"); q.add_argument("--auditor-url")
    q = sub.add_parser("audit"); audit_sub = q.add_subparsers(dest="audit_action", required=True); q = audit_sub.add_parser("resend"); q.add_argument("--project-id", required=True); q.add_argument("--run-id", required=True); q.add_argument("--round-id", required=True); q.add_argument("--delivery-sha", required=True)
    q = sub.add_parser("agent-guide"); q.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_parser("identify").add_argument("--project-id", default=None)
    sub.add_parser("doctor")
    for name, func in (("deliver", deliver), ("status", status), ("pause", pause), ("bind-current-worker", bind_current_worker), ("bootstrap-worker", bootstrap_worker)):
        q = sub.add_parser(name); q.add_argument("--project-id");
        if name == "deliver":
            q.add_argument("--phase-id"); q.add_argument("--round-id"); q.add_argument("--task-id"); q.add_argument("--baseline-sha"); q.add_argument("--note"); q.add_argument("--note-file")
    sub.add_parser("projects")
    sub.add_parser("monitor")
    q = sub.add_parser("diagnostics"); q.add_argument("action", choices=["start", "status", "stop", "report"]); q.add_argument("--tag", default="agentrelay")
    q = sub.add_parser("service"); q.add_argument("action", choices=["start", "stop", "status", "ensure"])
    q = sub.add_parser("browser"); q.add_argument("action", choices=["recover"]); q.add_argument("--project-id")
    sub.add_parser("resume").add_argument("--project-id", default=None)
    q = sub.add_parser("event"); event_sub = q.add_subparsers(dest="event_action", required=True)
    q = event_sub.add_parser("send"); q.add_argument("--project-id", required=True); q.add_argument("--run-id", required=True); q.add_argument("--round-id", required=True); q.add_argument("--type", required=True); q.add_argument("--instruction-file", required=True)
    sub.add_parser("_supervisor")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "register": result = register(args)
        elif args.command == "project" and args.project_action == "configure": result = configure_project(args)
        elif args.command == "audit" and args.audit_action == "resend": result = resend_audit(args)
        elif args.command == "adopt": result = adopt(args)
        elif args.command == "agent-guide": result = agent_guide(args)
        elif args.command == "identify": result = identify(args)
        elif args.command == "doctor": result = doctor(args)
        elif args.command == "deliver": result = deliver(args)
        elif args.command == "status": result = status(args)
        elif args.command == "pause": result = pause(args)
        elif args.command == "bind-current-worker": result = bind_current_worker(args)
        elif args.command == "bootstrap-worker": result = bootstrap_worker(args)
        elif args.command == "projects": result = projects(args)
        elif args.command == "service": result = service(args)
        elif args.command == "browser" and args.action == "recover": result = browser_recover(args)
        elif args.command == "resume": result = resume_project(args)
        elif args.command == "event" and args.event_action == "send": result = send_event(args)
        elif args.command == "_supervisor": return supervisor(args)
        elif args.command == "monitor": return monitor(args)
        elif args.command == "diagnostics": result = diagnostics_command(args)
        else: raise ValueError(args.command)
        if args.command == "agent-guide" and args.format == "text":
            print(result)
        else:
            print(json.dumps(result, indent=2, default=str))
        return 0
    except HumanRequired as exc:
        print(exc, file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
