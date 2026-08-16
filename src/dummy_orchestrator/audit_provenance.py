"""Self-contained, replayable provenance for auditor requests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


IDENTITY_FIELDS = (
    "version",
    "git_revision",
    "git_branch",
    "git_dirty",
    "source_fingerprint",
    "package_path",
    "install_source",
    "install_mode",
    "version_consistent",
    "python_executable",
)

STALE_CHAT_CONTEXT_POLICY = (
    "The AUDIT_PROVENANCE block in THIS audit request is authoritative for the current delivery. "
    "Do not infer the current AgentRelay version, revision, source identity, run, round, or active "
    "instruction from earlier ChatGPT conversation messages. If earlier conversation context conflicts "
    "with this provenance block, treat that earlier information as historical/stale."
)

AUDIT_DECISION_CONTRACT = (
    "Evaluate the supplied canonical active instruction. If it explicitly defines another phase/task "
    "on successful acceptance, emit that TASK. If it explicitly declares the phase terminal on "
    "acceptance, emit TERMINAL_CONTROL. If rejected, emit CORRECTIVE. If the supplied authoritative "
    "request genuinely lacks enough information for a safe decision, use HUMAN_REQUIRED and stop."
)


def safe_identity(identity: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep runtime identity explicit and exclude unrelated process/environment data."""
    if identity is None:
        return None
    return {key: identity.get(key) for key in IDENTITY_FIELDS if key in identity}


def _delivery_evidence(delivery_payload: dict[str, Any], delivery_message_id: str) -> dict[str, Any]:
    evidence = deepcopy(delivery_payload)
    evidence["delivery_message_id"] = delivery_message_id
    return evidence


def build_audit_request(
    *,
    project_id: str,
    run_id: str,
    round_id: str,
    active_event_type: str,
    active_event_gmail_id: str | None,
    active_event_payload: dict[str, Any],
    delivery_message_id: str,
    delivery_payload: dict[str, Any],
    agentrelay_delivery_identity: dict[str, Any] | None,
    agentrelay_audit_transport_identity: dict[str, Any] | None = None,
    legacy_provenance: bool = False,
) -> dict[str, Any]:
    """Build the exact object submitted to ChatGPT auditor transport."""
    provenance: dict[str, Any] = {
        "schema_version": 1,
        "legacy_provenance": legacy_provenance,
        "agentrelay_delivery_identity": safe_identity(agentrelay_delivery_identity),
        "project": {"project_id": project_id, "run_id": run_id, "round_id": round_id},
        "active_instruction_event": {
            "event_type": active_event_type,
            "gmail_message_id": active_event_gmail_id,
            "payload": deepcopy(active_event_payload),
        },
        "delivery": _delivery_evidence(delivery_payload, delivery_message_id),
    }
    if agentrelay_audit_transport_identity is not None:
        provenance["agentrelay_audit_transport_identity"] = safe_identity(agentrelay_audit_transport_identity)
    return {
        "ORCHESTRATOR_AUDIT_REQUEST": True,
        "AUDIT_PROVENANCE": provenance,
        "project_id": project_id,
        "run_id": run_id,
        "round_id": round_id,
        "delivery_message_id": delivery_message_id,
        "delivery_evidence": _delivery_evidence(delivery_payload, delivery_message_id),
        "instruction": (
            "The worker reports completion. Git delivery has been independently verified. Inspect the "
            "repository/delivery directly; do not rely on a worker receipt. "
            + AUDIT_DECISION_CONTRACT
        ),
        "stale_chat_context_policy": STALE_CHAT_CONTEXT_POLICY,
        "audit_decision_contract": AUDIT_DECISION_CONTRACT,
    }


def build_audit_replay_request(
    *,
    original_provenance: dict[str, Any],
    delivery_payload: dict[str, Any],
    delivery_message_id: str,
    agentrelay_audit_transport_identity: dict[str, Any],
) -> dict[str, Any]:
    """Replay original delivery provenance without rewriting its identity."""
    provenance = deepcopy(original_provenance)
    provenance["delivery"] = _delivery_evidence(delivery_payload, delivery_message_id)
    provenance["agentrelay_audit_transport_identity"] = safe_identity(agentrelay_audit_transport_identity)
    project = provenance["project"]
    active = provenance["active_instruction_event"]
    return build_audit_request(
        project_id=project["project_id"],
        run_id=project["run_id"],
        round_id=project["round_id"],
        active_event_type=active["event_type"],
        active_event_gmail_id=active.get("gmail_message_id"),
        active_event_payload=active.get("payload", {}),
        delivery_message_id=delivery_message_id,
        delivery_payload=delivery_payload,
        agentrelay_delivery_identity=provenance.get("agentrelay_delivery_identity"),
        agentrelay_audit_transport_identity=agentrelay_audit_transport_identity,
        legacy_provenance=bool(provenance.get("legacy_provenance")),
    )
