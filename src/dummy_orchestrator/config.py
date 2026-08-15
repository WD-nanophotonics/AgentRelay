from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ProjectConfig


def expand(value: str) -> str:
    return os.path.expandvars(value.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", "")))


@dataclass(frozen=True)
class RuntimeConfig:
    data_root: Path
    codex_cli_path: str | None = None
    gmail_oauth_client_filename: str = "oauth-client.json"
    gmail_oauth_token_filename: str = "oauth-token.json"
    poll_interval_seconds: int = 15
    gmail_timeout_seconds: int = 180
    browser_timeout_seconds: int = 60
    chrome_path: str | None = None
    chrome_profile_dir: str = "%LOCALAPPDATA%/CodexOrchestrator/profiles/chatgpt"
    chrome_debug_port: int = 9222
    browser_mode: str = "headful_background"
    worker_timeout_seconds: int = 600
    audit_watchdog_interval_seconds: int = 60
    audit_watchdog_max_retries: int = 1


class ProjectRegistry:
    def __init__(self, config_path: Path):
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        runtime = raw.get("runtime", {})
        self.runtime = RuntimeConfig(
            data_root=Path(expand(runtime.get("data_root", "%LOCALAPPDATA%/DummyOrchestrator"))),
            codex_cli_path=expand(runtime["codex_cli_path"]) if runtime.get("codex_cli_path") else None,
            gmail_oauth_client_filename=runtime.get("gmail_oauth_client_filename", "oauth-client.json"),
            gmail_oauth_token_filename=runtime.get("gmail_oauth_token_filename", "oauth-token.json"),
            chrome_path=expand(runtime["chrome_path"]) if runtime.get("chrome_path") else None,
            chrome_profile_dir=expand(runtime.get("chrome_profile_dir", "%LOCALAPPDATA%/CodexOrchestrator/profiles/chatgpt")),
            chrome_debug_port=int(runtime.get("chrome_debug_port", 9222)),
            browser_mode=runtime.get("browser_mode", "headful_background"),
            **{k: v for k, v in runtime.items() if k not in {"data_root", "codex_cli_path", "gmail_oauth_client_filename", "gmail_oauth_token_filename", "chrome_path", "chrome_profile_dir", "chrome_debug_port", "browser_mode"}},
        )
        self.config_dir = config_path.resolve().parent
        self._projects = {
            project_id: ProjectConfig(
                project_id=project_id,
                mode=value["mode"],
                local_workspace=expand(value["local_workspace"]),
                auditor_chat_url=value["auditor_chat_url"],
                gmail_subject_prefix=value["gmail_subject_prefix"],
                worker_email=value.get("worker_email", ""),
                max_rounds=int(value["max_rounds"]),
                repository_root=expand(value.get("repository_root", value.get("local_workspace", ""))),
                git_remote=value.get("git_remote", "origin"),
                worker_branch=value.get("worker_branch", ""),
                protected_branches=tuple(value.get("protected_branches", ["main", "master", "develop"])),
                persistent_codex_session=value.get("persistent_codex_session"),
            )
            for project_id, value in raw.get("projects", {}).items()
        }

    def get(self, project_id: str) -> ProjectConfig:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise KeyError(f"Unknown project_id: {project_id}") from exc

    def all(self) -> list[ProjectConfig]:
        return list(self._projects.values())
