from pathlib import Path

from dummy_orchestrator.config import ProjectRegistry


def test_multiple_projects_are_independent(tmp_path: Path):
    config = tmp_path / "projects.yaml"
    config.write_text("""runtime: {data_root: '%LOCALAPPDATA%/test'}\nprojects:\n  a: {mode: dummy, local_workspace: a, auditor_chat_url: https://x/a, gmail_subject_prefix: '[A]', worker_email: a@example.test, max_rounds: 3}\n  b: {mode: dummy, local_workspace: b, auditor_chat_url: https://x/b, gmail_subject_prefix: '[B]', worker_email: b@example.test, max_rounds: 3}\n""")
    registry = ProjectRegistry(config)
    assert registry.get("a").auditor_chat_url != registry.get("b").auditor_chat_url
