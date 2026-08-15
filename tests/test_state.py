from pathlib import Path

from dummy_orchestrator.models import ProjectState
from dummy_orchestrator.state import StateStore
from dummy_orchestrator.runtime import ProjectLock


def test_state_survives_restart_and_deduplicates(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    store = StateStore(db); store.transition("a", ProjectState.WAITING_FOR_INSTRUCTION, {"test": True}, run_id="run-1")
    assert store.mark_processed("gmail-1", "a") is True
    assert store.mark_processed("gmail-1", "a") is False
    restarted = StateStore(db)
    assert restarted.project("a")["run_id"] == "run-1"
    restarted.transition("a", ProjectState.IDLE, codex_path="x", codex_version="codex-cli 1", codex_selection_reason="test")
    assert restarted.project("a")["codex_path"] == "x"


def test_delivery_is_idempotent(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite")
    assert store.mark_delivery_once("delivery-1", "a", "run", "R001") is True
    assert store.mark_delivery_once("delivery-1", "a", "run", "R001") is False


def test_project_lock_is_scoped_and_exclusive(tmp_path: Path):
    with ProjectLock(tmp_path, "a"):
        try:
            ProjectLock(tmp_path, "a").__enter__()
        except RuntimeError:
            pass
        else:
            raise AssertionError("same project lock was not exclusive")
        with ProjectLock(tmp_path, "b"):
            pass
