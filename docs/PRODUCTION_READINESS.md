# Production-readiness certification

> **HISTORICAL** — This report describes the earlier 0.3.0 certification
> state. It is retained as evidence and is not the current AgentRelay runtime
> or version-management contract. See [VERSIONING.md](VERSIONING.md).

Date: 2026-08-14

## Result

`SEMI_AUTOMATIC_READY`

The normal post-bootstrap path is unattended on this machine. Gmail refresh-token access and the dedicated ChatGPT profile both survived a cold application restart. Headless ChatGPT qualification failed with a `Just a moment...` challenge and no composer, so the default is one minimized/background headful Chrome instance. The classification remains semi-automatic because initial OAuth/ChatGPT login is an operator bootstrap boundary, and the code intentionally stops at `HUMAN_REQUIRED` for any later authentication challenge, ambiguous routing, protected-branch violation, or unverified Git delivery.

## Certified facts

- Tool: `agent-relay` (aliases: `codex-orchestrator`, `orchestrator` for the legacy dummy CLI).
- Version: `0.3.0`.
- User-level command directory: `%LOCALAPPDATA%\CodexOrchestrator\bin`.
- Registry: `%LOCALAPPDATA%\CodexOrchestrator\registry\projects.json`.
- State/logs/artifacts: `%LOCALAPPDATA%\CodexOrchestrator\state`, `logs`, and `projects`.
- Secrets: `%LOCALAPPDATA%\CodexOrchestrator\secrets`; no secret is stored in a repository.
- Browser profile: `%LOCALAPPDATA%\CodexOrchestrator\profiles\chatgpt`; CDP is loopback-only.
- Current interactive Codex environment exposed `CODEX_THREAD_ID`, but no supported proof was found that a desktop interactive thread can be resumed by the standalone CLI. The safe outcome is the managed-worker handoff: `agent-relay bootstrap-worker --project-id <id>`.
- The certified dummy run resumed one persistent Codex CLI session across R001, R002, R002-C1, and R003, then stopped cleanly at terminal R004.
- Gmail cold restart: `GMAIL_UNATTENDED_PASS`.
- Chrome/CDP cold restart: `CHATGPT_UNATTENDED_PASS`.
- No human action was required after the existing initial OAuth and ChatGPT profile bootstrap during cold certification.
- Browser foreground check: the controlled Chrome window was not the foreground window after hardened launch.
- Browser process diagnosis: one CDP-owning Chrome root with its normal child processes and one auditor page; the earlier apparent second sign-in window was Chrome first-run/account UI from the same dedicated profile, not a second ChatGPT authentication requirement.

## Safety boundaries

Production delivery is verification-only: repository identity, worker branch, protected branches, local HEAD, remote branch SHA, baseline SHA where supplied, push equality, and clean working tree are checked. The worker must commit and push; the orchestrator does not push. Ambiguous Gmail routing, duplicate messages, duplicate audit submissions, watchdog expiry, authentication/challenge UI, and unexpected branch state fail closed.

## Tests

The final suite passed `18 tests` in the approved scratch environment. It covers dummy transport/state recovery, project locks/isolation, CDP challenge handling, registration/routing, production Git rejection/acceptance, duplicate delivery behavior, and persistent worker continuity. No real project was registered or modified.

## Recommended next phase

Run one explicitly approved, bounded real-project canary only after registering that project with `agent-relay register`, selecting a non-protected worker branch, and confirming the human has reviewed the Git verification policy. Do not start autonomous multi-round development automatically.
