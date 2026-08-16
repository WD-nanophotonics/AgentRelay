# AgentRelay development contract

- Work on `sandbox`; `master` is promotion-only and must remain untouched during normal development.
- Before using `agent-relay`, identify the exact executable, package path, version, source fingerprint, Git revision, branch, and dirty state.
- Use the explicit sandbox environment under `.venv`; do not rely on PATH precedence.
- Never install a mutable sandbox checkout as production with `pip install --user -e`.
- Do not clone or vendor AgentRelay into a managed target project.
- Do not start Mechanics, send Gmail, resume a worker, launch browser/auditor workflows, or begin autonomous work unless the task explicitly authorizes that next phase.
- Promote only the exact sandbox commit that passed its canary, then certify `master` separately.
- Preserve the existing process-policy, diagnostics, browser, Gmail, Git-delivery, and worker safety contracts unless a task explicitly requires a reviewed change.

See [docs/VERSIONING.md](docs/VERSIONING.md) for the complete version and installation policy.
