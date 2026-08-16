from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .adapters import HumanRequired
from .models import ProjectConfig
from .process_policy import run_hidden


PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
PROJECT_MARKER_RE = re.compile(r"\[(?:ORCH|ORCH-DUMMY)\]\[([^\]]+)\]", re.IGNORECASE)


def normalize_project_id(value: str) -> str:
    project_id = value.strip().lower()
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = project_id\ndetail = use 2-64 lowercase letters, digits, _ or -")
    return project_id


def safe_match_project_subject(subject: str, project_ids: list[str]) -> str:
    """Return one deterministic project match or fail closed on ambiguity."""
    normalized = {normalize_project_id(item): item for item in project_ids}
    markers = {normalize_project_id(match) for match in PROJECT_MARKER_RE.findall(subject)}
    structured = markers & set(normalized)
    if len(structured) == 1:
        return normalized[next(iter(structured))]
    if len(structured) > 1:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = ambiguous_gmail_project\ndetail = structured subject contains multiple registered project markers")
    fuzzy = [pid for pid in normalized if re.search(rf"(?<![a-z0-9_-]){re.escape(pid)}(?![a-z0-9_-])", subject.lower())]
    if len(fuzzy) == 1:
        return normalized[fuzzy[0]]
    if len(fuzzy) > 1:
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = ambiguous_gmail_project\ndetail = fuzzy subject match could belong to multiple registered projects")
    raise HumanRequired("HUMAN_REQUIRED\nmissing_field = gmail_project_marker\ndetail = subject does not contain a safe project marker")


def _git(root: Path, *args: str, allow_error: bool = False) -> str:
    result = run_hidden(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode and not allow_error:
        raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = git_repository\ndetail = git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


@dataclass(frozen=True)
class ProjectRegistration:
    project_id: str
    auditor_chat_url: str
    repository_root: str
    git_remote: str
    worker_branch: str
    repository_identity: str
    head_sha: str
    protected_branches: tuple[str, ...] = ("main", "master", "develop")
    persistent_codex_session: str | None = None
    mode: str = "production"
    gmail_subject_prefix: str = "[ORCH]"
    worker_email: str = ""
    max_rounds: int = 99

    def to_project_config(self) -> ProjectConfig:
        return ProjectConfig(
            self.project_id, self.mode, self.repository_root, self.auditor_chat_url,
            self.gmail_subject_prefix, self.worker_email, self.max_rounds,
            self.repository_root, self.git_remote, self.worker_branch,
            tuple(self.protected_branches), self.persistent_codex_session,
        )


class ProjectRegistrationStore:
    """User-scoped registry; no repository-local secrets or tokens are stored."""
    def __init__(self, data_root: Path):
        self.path = data_root / "registry" / "projects.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = project_registry\ndetail = invalid JSON at {self.path}") from exc

    def all(self) -> list[ProjectRegistration]:
        return [ProjectRegistration(**dict(item, protected_branches=tuple(item.get("protected_branches", ("main", "master", "develop"))))) for item in self._read().values()]

    def get(self, project_id: str) -> ProjectRegistration:
        normalized = normalize_project_id(project_id)
        try:
            raw = self._read()[normalized]
        except KeyError as exc:
            raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = registered_project\ndetail = project_id {normalized} is not registered") from exc
        return ProjectRegistration(**dict(raw, protected_branches=tuple(raw.get("protected_branches", ("main", "master", "develop")))))

    def resolve(self, project_id: str | None = None, cwd: Path | None = None) -> ProjectRegistration:
        if project_id:
            return self.get(project_id)
        here = (cwd or Path.cwd()).resolve()
        matches = [item for item in self.all() if here == Path(item.repository_root).resolve() or item.repository_root and here.is_relative_to(Path(item.repository_root).resolve())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = project_id\ndetail = current directory is not a registered project")
        raise HumanRequired("HUMAN_REQUIRED\nmissing_field = unambiguous_project\ndetail = current directory maps to multiple registered projects")

    def register(self, registration: ProjectRegistration) -> ProjectRegistration:
        project_id = normalize_project_id(registration.project_id)
        registration = ProjectRegistration(**{**asdict(registration), "project_id": project_id, "protected_branches": tuple(registration.protected_branches)})
        data = self._read()
        existing = data.get(project_id)
        if existing and (Path(existing["repository_root"]).resolve() != Path(registration.repository_root).resolve() or existing["auditor_chat_url"] != registration.auditor_chat_url):
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = duplicate_project_id\ndetail = project_id already maps to a different repository or auditor URL")
        for other_id, other in data.items():
            if other_id != project_id and Path(other["repository_root"]).resolve() == Path(registration.repository_root).resolve():
                raise HumanRequired("HUMAN_REQUIRED\nmissing_field = duplicate_repository_registration\ndetail = repository is already registered under another project_id")
        data[project_id] = asdict(registration)
        fd, temp_name = tempfile.mkstemp(prefix="projects-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(temp_name, self.path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return registration

    def _write(self, data: dict[str, dict]) -> None:
        """Atomically replace the registry JSON with already-validated data."""
        fd, temp_name = tempfile.mkstemp(prefix="projects-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(temp_name, self.path)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def configure_auditor_url(self, project_id: str, auditor_url: str) -> ProjectRegistration:
        """Explicitly update only the auditor URL of an existing registration."""
        registration = self.get(project_id)
        if registration.auditor_chat_url == auditor_url:
            return registration
        data = self._read()
        raw = dict(data[registration.project_id])
        raw["auditor_chat_url"] = auditor_url
        data[registration.project_id] = raw
        self._write(data)
        return ProjectRegistration(**dict(raw, protected_branches=tuple(raw.get("protected_branches", ("main", "master", "develop")))))

    def configure_worker_email(self, project_id: str, worker_email: str) -> ProjectRegistration:
        registration = self.get(project_id)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", worker_email.strip()):
            raise HumanRequired("HUMAN_REQUIRED\nmissing_field = worker_email\ndetail = expected a syntactically valid email address")
        return self.register(ProjectRegistration(**{**asdict(registration), "worker_email": worker_email.strip()}))


def discover_registration(project_id: str, auditor_url: str, cwd: Path | None = None, worker_branch: str | None = None, protected_branches: tuple[str, ...] = ("main", "master", "develop")) -> ProjectRegistration:
    root = Path(_git(cwd or Path.cwd(), "rev-parse", "--show-toplevel")).resolve()
    remote = _git(root, "remote", "get-url", "origin", allow_error=True) or "origin"
    branch = worker_branch or _git(root, "symbolic-ref", "--short", "HEAD")
    if branch in set(protected_branches):
        raise HumanRequired(f"HUMAN_REQUIRED\nmissing_field = worker_branch\ndetail = current branch {branch} is protected; pass --worker-branch explicitly after switching to a worker branch")
    head = _git(root, "rev-parse", "HEAD")
    identity = remote if remote != "origin" else str(root)
    return ProjectRegistration(normalize_project_id(project_id), auditor_url, str(root), remote, branch, identity, head, protected_branches)
