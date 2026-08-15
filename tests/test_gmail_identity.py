from dummy_orchestrator.adapters import GmailInstructionBus


def test_configured_worker_email_is_preserved_without_lookup():
    bus = GmailInstructionBus.__new__(GmailInstructionBus)
    assert bus.resolve_recipient("worker@example.test") == "worker@example.test"
