from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .adapters import HumanRequired
from .config import ProjectRegistry
from .models import ProjectState
from .orchestrator import Orchestrator


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("preflight", "worker-test", "auth-chatgpt", "browser-probe", "resume-audit", "run", "resume", "stop", "status", "history"):
        q = sub.add_parser(name); q.add_argument("project_id", nargs="?", default="dummy_project")
    sub.add_parser("init"); sub.add_parser("auth-gmail")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        args.config.parent.mkdir(parents=True, exist_ok=True)
        template = Path("config/projects.example.yaml")
        if not args.config.exists(): shutil.copyfile(template, args.config)
        print(f"Created {args.config}; set worker_email and place Gmail OAuth client outside the repository.")
        return 0
    app = Orchestrator(ProjectRegistry(args.config))
    try:
        if args.command == "preflight": print(json.dumps(app.preflight(args.project_id), indent=2))
        elif args.command == "worker-test": print(json.dumps(app.worker_test(args.project_id), indent=2))
        elif args.command == "auth-gmail": app.auth_gmail()
        elif args.command == "auth-chatgpt": app.auth_chatgpt(args.project_id)
        elif args.command == "browser-probe": print(json.dumps(app.browser_probe(args.project_id), indent=2))
        elif args.command == "resume-audit": app.resume_audit(args.project_id)
        elif args.command == "run": app.run(args.project_id)
        elif args.command == "resume": app.run(args.project_id, resume=True)
        elif args.command == "stop": app.stop(args.project_id)
        elif args.command == "status": print(json.dumps(app.status(args.project_id), indent=2))
        elif args.command == "history": print(json.dumps(app.history(args.project_id), indent=2))
        return 0
    except HumanRequired as exc:
        if hasattr(args, "project_id"):
            app.store.transition(args.project_id, ProjectState.HUMAN_REQUIRED, {"human_required": str(exc)}, error=str(exc))
        print(exc, file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
