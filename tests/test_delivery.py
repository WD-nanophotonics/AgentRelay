from dummy_orchestrator.adapters import DummyGmailDeliveryAdapter
from dummy_orchestrator.models import ProjectConfig


class CapturingBus:
    def __init__(self): self.event = None
    def send(self, event, recipient): self.event, self.recipient = event, recipient; return "gmail-delivery-1"


def test_dummy_delivery_is_explicitly_simulated():
    bus = CapturingBus()
    project = ProjectConfig("dummy_project", "dummy", "x", "https://chatgpt.com/c/x", "[ORCH-DUMMY]", "worker@example.test", 3)
    message_id, event = DummyGmailDeliveryAdapter(bus).deliver(project, "run-1", "R002")
    assert message_id == "gmail-delivery-1"
    assert event.payload["simulation_notice"] == "SIMULATION ONLY — NO REAL GIT PUSH OCCURRED"
    assert event.payload["intentionally_incomplete"] is True
