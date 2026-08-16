from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .adapters import ChatGPTWebAuditorAdapter, CodexWorkerAdapter, DummyGmailDeliveryAdapter, GitDeliveryAdapter, GmailInstructionBus, HumanRequired
from .audit_provenance import build_audit_request, build_audit_replay_request
from .config import ProjectRegistry
from .models import EventType, OrchestratorEvent, ProjectState
from .state import StateStore
from .runtime import ProjectLock, StructuredLog


class Orchestrator:
    def __init__(self, registry: ProjectRegistry):
        self.registry = registry
        root = registry.runtime.data_root
        self.store = StateStore(root / "state" / "orchestrator.sqlite")
        self.bus = GmailInstructionBus(root / "secrets" / registry.runtime.gmail_oauth_client_filename, root / "secrets" / registry.runtime.gmail_oauth_token_filename)
        self.delivery = DummyGmailDeliveryAdapter(self.bus)
        self.git_delivery = GitDeliveryAdapter(self.bus)
        self.auditor = ChatGPTWebAuditorAdapter(Path(registry.runtime.chrome_profile_dir), registry.runtime.browser_timeout_seconds, registry.runtime.chrome_path, registry.runtime.chrome_debug_port, registry.runtime.browser_mode)
        self.worker = CodexWorkerAdapter(registry.runtime.codex_cli_path, registry.config_dir.parent)
        self.log = StructuredLog(root / "logs" / "orchestrator.jsonl")

    def _stop_file(self, project_id: str) -> Path:
        return self.registry.runtime.data_root / "stops" / f"{project_id}.STOP"

    def _artifact(self, project_id: str, round_id: str, name: str, value: dict) -> None:
        path = self.registry.runtime.data_root / "projects" / project_id / "rounds" / round_id
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")

    def initialize(self) -> None:
        root = self.registry.runtime.data_root
        for name in ("state", "logs", "projects", "stops", "secrets", "profiles", "dummy-workspaces", "locks"):
            (root / name).mkdir(parents=True, exist_ok=True)
        for project in self.registry.all(): self.store.ensure_project(project.project_id)

    def preflight(self, project_id: str) -> dict:
        self.initialize()
        result = self.worker.preflight()
        self.store.transition(project_id, ProjectState.IDLE, {"codex_preflight": result}, codex_path=result["path"], codex_version=result["version"], codex_selection_reason=result["selection_reason"], error=None)
        return result

    def worker_test(self, project_id: str) -> dict:
        self.initialize(); project = self.registry.get(project_id); preflight = self.preflight(project_id)
        session = self.worker.run_instruction(project, "Worker Turn A: create worker_turn_a.txt containing the nonce ORCH-TURN-A-7F31. Do not modify any other file or access Git/network.", None)
        self.store.transition(project_id, ProjectState.WORKER_RUNNING, {"worker_test": "turn_a"}, run_id="WORKER-TEST", round_id="A", worker_session_id=session)
        resumed = self.worker.run_instruction(project, "Worker Turn B: inspect worker_turn_a.txt, verify the nonce is ORCH-TURN-A-7F31, and create worker_turn_b.txt stating that Turn A was resumed. Do not modify any other file or access Git/network.", session)
        if resumed != session: raise HumanRequired("HUMAN_REQUIRED\nmissing_field = persistent_worker_continuity\ndetail = resume returned a different session identifier")
        self.store.transition(project_id, ProjectState.COMPLETE, {"worker_test": "turn_a_then_turn_b", "continuity": True}, worker_session_id=session)
        evidence = {"preflight": preflight, "session_id": session, "turn_a": "worker_turn_a.txt", "turn_b": "worker_turn_b.txt", "same_session": True}
        self._artifact(project_id, "WORKER-TEST", "evidence.json", evidence)
        return evidence

    def auth_gmail(self) -> None:
        self.initialize(); self.bus._service(); self.bus.resolve_recipient(""); print("Gmail OAuth bootstrap complete; connected identity resolved without printing it.")

    def auth_chatgpt(self, project_id: str) -> None:
        self.initialize(); project = self.registry.get(project_id)
        diagnostics = self.auditor.chrome_preflight()
        self.auditor.auth(project.auditor_chat_url)
        self.store.transition(project_id, self.store.project(project_id)["state"], {"chrome_auth": self.auditor.last_diagnostics}, chrome_path=diagnostics["path"], chrome_version=diagnostics["version"], chrome_selection_reason=diagnostics["selection_reason"], cdp_endpoint=diagnostics["cdp_endpoint"], error=None)

    def browser_probe(self, project_id: str) -> dict:
        self.initialize(); project = self.registry.get(project_id)
        diagnostics = self.auditor.chrome_preflight()
        self.auditor.send_probe(project.auditor_chat_url)
        record = self.store.project(project_id)
        self.store.transition(project_id, record["state"], {"browser_probe": self.auditor.last_diagnostics}, chrome_path=diagnostics["path"], chrome_version=diagnostics["version"], chrome_selection_reason=diagnostics["selection_reason"], cdp_endpoint=diagnostics["cdp_endpoint"], error=None)
        return diagnostics | self.auditor.last_diagnostics

    def resume_audit(self, project_id: str) -> None:
        self.initialize(); project = self.registry.get(project_id); record = self.store.project(project_id)
        if record["state"] == ProjectState.WAITING_FOR_AUDITOR_GMAIL: return
        if record["state"] not in (ProjectState.HUMAN_REQUIRED, ProjectState.AUDIT_PENDING):
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = resumable_audit_state\ndetail = current state is {record['state']}")
        if not record["run_id"] or not record["round_id"]:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = persisted_r001_identity\ndetail = run or round is missing")
        artifact = self.registry.runtime.data_root / "projects" / project_id / "rounds" / record["round_id"] / "delivery.json"
        if not artifact.exists():
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = persisted_delivery_artifact\ndetail = expected {artifact}")
        raw = json.loads(artifact.read_text(encoding="utf-8")); event_raw = raw["event"]
        delivery_id = raw["gmail_message_id"]
        event = OrchestratorEvent(event_raw["project_id"], event_raw["run_id"], event_raw["round_id"], EventType(event_raw["event_type"]), event_raw["payload"], delivery_id)
        self.store.transition(project_id, ProjectState.AUDIT_PENDING, {"resume_audit": True, "delivery_message_id": delivery_id}, error=None)
        if not self.store.mark_audit_once(delivery_id, project_id, record["run_id"], record["round_id"]):
            self.store.transition(project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_duplicate_suppressed": True})
            return
        stored_provenance = raw.get("audit_provenance")
        if stored_provenance:
            from .global_cli import source_identity
            audit_request = build_audit_replay_request(
                original_provenance=stored_provenance,
                delivery_payload=event.payload,
                delivery_message_id=delivery_id,
                agentrelay_audit_transport_identity=source_identity(),
            )
        else:
            audit_request = build_audit_request(
                project_id=project_id,
                run_id=record["run_id"],
                round_id=record["round_id"],
                active_event_type=record.get("active_event_type") or "UNKNOWN",
                active_event_gmail_id=record.get("active_event_gmail_id"),
                active_event_payload=json.loads(record.get("active_event_payload_json") or "{}"),
                delivery_message_id=delivery_id,
                delivery_payload=event.payload,
                agentrelay_delivery_identity=None,
                legacy_provenance=True,
            )
        try:
            self.auditor.send_audit_request(project, event, delivery_id, audit_request=audit_request)
        except HumanRequired as exc:
            self.store.transition(project_id, ProjectState.HUMAN_REQUIRED, {"auditor_error": str(exc)}, error=str(exc)); raise
        self.store.transition(project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_sent": True, "resumed": True})

    def status(self, project_id: str) -> dict:
        return self.store.project(project_id)

    def history(self, project_id: str) -> list[dict]:
        return self.store.history(project_id)

    def stop(self, project_id: str) -> None:
        stop = self._stop_file(project_id); stop.parent.mkdir(parents=True, exist_ok=True); stop.write_text("operator stop\n")
        self.store.transition(project_id, ProjectState.STOPPED, {"reason": "operator stop"})

    def _seed(self, project_id: str, project) -> None:
        record = self.store.project(project_id)
        if record["run_id"]: return
        run_id = f"RUN-{uuid.uuid4().hex[:10]}"
        event = OrchestratorEvent(project_id, run_id, "R001", EventType.TASK, {"project": project, "task": "bootstrap deterministic dummy task"})
        gmail_id = self.bus.send(event, project.worker_email)
        self.store.transition(project_id, ProjectState.WAITING_FOR_INSTRUCTION, {"seed": True}, run_id=run_id, round_id="R001", last_gmail_id=gmail_id)
        self._artifact(project_id, "R001", "seed-task.json", {"gmail_message_id": gmail_id, "event": event.__dict__})
        self.log.write("bootstrap_task_sent", project_id=project_id, run_id=run_id, round_id="R001", gmail_message_id=gmail_id)

    def _handle_instruction(self, project_id: str, event: OrchestratorEvent, project) -> None:
        record = self.store.project(project_id)
        if event.gmail_message_id and not self.store.mark_processed(event.gmail_message_id, project_id): return
        self.log.write("instruction_received", project_id=project_id, run_id=event.run_id, round_id=event.round_id, event_type=event.event_type, gmail_message_id=event.gmail_message_id)
        if event.event_type == EventType.DONE:
            self.store.transition(project_id, ProjectState.COMPLETE, {"auditor_done": True}, round_id=event.round_id, last_gmail_id=event.gmail_message_id)
            return
        if event.event_type not in (EventType.TASK, EventType.CORRECTIVE): return
        terminal_payload = event.payload.get("payload", event.payload)
        if event.event_type == EventType.TASK and terminal_payload.get("action") == "COMPLETE":
            self.store.transition(project_id, ProjectState.COMPLETE, {"terminal_control": True, "do_not_invoke_worker": True}, run_id=event.run_id, round_id=event.round_id, last_gmail_id=event.gmail_message_id, active_event_round_id=event.round_id, active_event_type=str(event.event_type), active_event_gmail_id=event.gmail_message_id, active_event_payload_json=json.dumps(event.payload, sort_keys=True, default=str), error=None)
            return
        numeric_round = int(event.round_id[1:4]) if event.round_id.startswith("R") and event.round_id[1:4].isdigit() else 0
        if numeric_round > project.max_rounds:
            self.store.transition(project_id, ProjectState.ERROR, {"max_rounds": project.max_rounds}, error="Maximum dummy rounds exceeded")
            return
        next_state = ProjectState.CORRECTIVE_PENDING if event.event_type == EventType.CORRECTIVE else ProjectState.WORKER_RUNNING
        self.store.transition(project_id, next_state, {"event_type": event.event_type}, round_id=event.round_id, last_gmail_id=event.gmail_message_id, active_event_round_id=event.round_id, active_event_type=str(event.event_type), active_event_gmail_id=event.gmail_message_id, active_event_payload_json=json.dumps(event.payload, sort_keys=True, default=str))
        session = self.worker.run_dummy_task(project, event.run_id, event.round_id, record["worker_session_id"])
        self.store.transition(project_id, ProjectState.WAITING_FOR_DELIVERY, {"worker_complete": True}, worker_session_id=session)
        corrected = event.event_type == EventType.CORRECTIVE
        if project.mode == "production":
            gmail_id, delivery = self.git_delivery.deliver(project, event.run_id, event.round_id, task_id=event.payload.get("task_id"), phase_id=event.payload.get("phase_id"))
        else:
            gmail_id, delivery = self.delivery.deliver(project, event.run_id, event.round_id, corrected)
        if "agentrelay_delivery_identity" not in delivery.payload:
            from .global_cli import source_identity
            delivery = OrchestratorEvent(delivery.project_id, delivery.run_id, delivery.round_id, delivery.event_type,
                                         delivery.payload | {"agentrelay_delivery_identity": source_identity()}, delivery.gmail_message_id)
        delivery = OrchestratorEvent(delivery.project_id, delivery.run_id, delivery.round_id, delivery.event_type, delivery.payload, gmail_id)
        audit_request = build_audit_request(
            project_id=project_id,
            run_id=event.run_id,
            round_id=event.round_id,
            active_event_type=str(event.event_type),
            active_event_gmail_id=event.gmail_message_id,
            active_event_payload=event.payload,
            delivery_message_id=gmail_id,
            delivery_payload=delivery.payload,
            agentrelay_delivery_identity=delivery.payload.get("agentrelay_delivery_identity"),
            agentrelay_audit_transport_identity=delivery.payload.get("agentrelay_delivery_identity"),
        )
        if not self.store.mark_delivery_once(gmail_id, project_id, event.run_id, event.round_id, provenance=audit_request["AUDIT_PROVENANCE"]): return
        self._artifact(project_id, event.round_id, "delivery.json", {"gmail_message_id": gmail_id, "event": delivery.__dict__, "audit_provenance": audit_request["AUDIT_PROVENANCE"]})
        self.store.transition(project_id, ProjectState.AUDIT_PENDING, {"delivery_message_id": gmail_id})
        if not self.store.mark_audit_once(gmail_id, project_id, event.run_id, event.round_id):
            self.store.transition(project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_duplicate_suppressed": True})
            return
        try:
            self.auditor.send_audit_request(project, delivery, gmail_id, audit_request=audit_request)
        except HumanRequired as exc:
            self.store.transition(project_id, ProjectState.HUMAN_REQUIRED, {"auditor_error": str(exc)}, error=str(exc))
            raise
        except Exception as exc:
            self.store.transition(project_id, ProjectState.ERROR, {"auditor_error": type(exc).__name__}, error=str(exc))
            raise
        self.store.transition(project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"audit_request_sent": True})
        self.log.write("audit_request_sent", project_id=project_id, run_id=event.run_id, round_id=event.round_id, delivery_message_id=gmail_id)

    def run(self, project_id: str, resume: bool = False) -> None:
        self.initialize()
        with ProjectLock(self.registry.runtime.data_root, project_id):
            self.worker.preflight()
            project = self.registry.get(project_id)
            project = replace(project, worker_email=self.bus.resolve_recipient(project.worker_email))
            if not resume: self._seed(project_id, project)
            if resume:
                recovered = self.store.project(project_id)
                if recovered["state"] == ProjectState.ERROR and recovered.get("error", "").startswith("Timed out waiting for matching Gmail instruction"):
                    self.store.transition(project_id, ProjectState.WAITING_FOR_AUDITOR_GMAIL, {"restart_recovery": True}, error=None)
            deadline = time.monotonic() + self.registry.runtime.gmail_timeout_seconds
            while time.monotonic() < deadline:
                if self._stop_file(project_id).exists():
                    self.store.transition(project_id, ProjectState.STOPPED, {"reason": "STOP file"}); return
                record = self.store.project(project_id)
                if record["state"] in (ProjectState.COMPLETE, ProjectState.HUMAN_REQUIRED, ProjectState.ERROR, ProjectState.STOPPED): return
                for event in self.bus.poll(project, record["run_id"]): self._handle_instruction(project_id, event, project)
                time.sleep(self.registry.runtime.poll_interval_seconds)
            final_record = self.store.project(project_id)
            if final_record["state"] == ProjectState.WAITING_FOR_AUDITOR_GMAIL:
                self.store.transition(project_id, ProjectState.HUMAN_REQUIRED, {"audit_watchdog": True, "max_retries": self.registry.runtime.audit_watchdog_max_retries}, error="Auditor Gmail did not arrive within the bounded watchdog interval")
            else:
                self.store.transition(project_id, ProjectState.ERROR, {"timeout": "gmail arrival"}, error="Timed out waiting for matching Gmail instruction")
