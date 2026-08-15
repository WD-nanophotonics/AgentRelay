import subprocess
from pathlib import Path

import pytest

from dummy_orchestrator.adapters import GitDeliveryAdapter, HumanRequired
from dummy_orchestrator.models import ProjectConfig
from dummy_orchestrator.registry import ProjectRegistrationStore, discover_registration


def git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path: Path):
    root, remote = tmp_path / "repo", tmp_path / "remote.git"
    root.mkdir(); subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    git(root, "config", "user.email", "test@example.test"); git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("one", encoding="utf-8"); git(root, "add", "."); git(root, "commit", "-m", "initial")
    git(root, "branch", "-M", "worker"); subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(root, "remote", "add", "origin", str(remote)); return root, remote


def project(root: Path) -> ProjectConfig:
    return ProjectConfig("generic_chess", "production", str(root), "https://chatgpt.com/c/test", "[ORCH]", "worker@example.test", 99, str(root), "origin", "worker", ("main", "master", "develop"))


def test_git_delivery_rejects_unpushed_commit(tmp_path: Path):
    root, _ = make_repo(tmp_path); (root / "file.txt").write_text("two", encoding="utf-8"); git(root, "commit", "-am", "second")
    with pytest.raises(HumanRequired, match="git_delivery_verification|remote_branch"):
        GitDeliveryAdapter().verify(project(root))


def test_git_delivery_accepts_verified_remote_sha(tmp_path: Path):
    root, _ = make_repo(tmp_path); (root / "file.txt").write_text("two", encoding="utf-8"); git(root, "commit", "-am", "second")
    try:
        git(root, "push", "-u", "origin", "worker")
    except subprocess.CalledProcessError as exc:
        if "signal pipe" in exc.stderr:
            pytest.skip("host Git cannot launch local transport in this sandbox")
        raise
    result = GitDeliveryAdapter().verify(project(root))
    assert result["push_verified"] is True and result["local_sha"] if "local_sha" in result else result["delivery_sha"] == result["remote_sha"]


def test_git_delivery_accepts_registered_remote_url(tmp_path: Path):
    root, remote = make_repo(tmp_path); (root / "file.txt").write_text("two", encoding="utf-8"); git(root, "commit", "-am", "second")
    try:
        git(root, "push", "-u", "origin", "worker")
    except subprocess.CalledProcessError as exc:
        if "signal pipe" in exc.stderr:
            pytest.skip("host Git cannot launch local transport in this sandbox")
        raise
    discovered = discover_registration("scratch", "https://chatgpt.com/c/test", root, "worker")
    result = GitDeliveryAdapter().verify(discovered.to_project_config())
    assert result["repository_identity"] == str(remote)
    assert result["push_verified"] is True


def test_git_delivery_rejects_protected_or_wrong_branch(tmp_path: Path):
    root, _ = make_repo(tmp_path)
    wrong = project(root)
    wrong = ProjectConfig(**{**wrong.__dict__, "worker_branch": "other"})
    with pytest.raises(HumanRequired, match="worker_branch"):
        GitDeliveryAdapter().verify(wrong)
    protected = ProjectConfig(**{**project(root).__dict__, "worker_branch": "worker", "protected_branches": ("worker",)})
    with pytest.raises(HumanRequired, match="protected_branch"):
        GitDeliveryAdapter().verify(protected)


def test_registration_discovers_and_rejects_duplicate_repository(tmp_path: Path):
    root, _ = make_repo(tmp_path); store = ProjectRegistrationStore(tmp_path / "data")
    first = discover_registration("generic_chess", "https://chatgpt.com/c/a", root)
    store.register(first)
    with pytest.raises(HumanRequired, match="duplicate_repository"):
        store.register(discover_registration("alpha_sho", "https://chatgpt.com/c/b", root))
