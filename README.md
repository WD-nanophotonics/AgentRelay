# Dummy Agent Orchestrator

Standalone Gmail → persistent Codex worker → ChatGPT auditor loop with a dummy delivery harness and independently verified production Git delivery path.

The same package now exposes the reusable user-scoped `agent-relay` tool for production-oriented projects. The dummy mode remains the certified regression path; production mode verifies Git delivery but never commits or pushes for the worker.

Current branch roles, source identity, explicit sandbox invocation, and the
production-installation boundary are defined in
[docs/VERSIONING.md](docs/VERSIONING.md). Do not use a bare `agent-relay`
command for certification.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\orchestrator init
# Sandbox-only explicit invocation:
.\.venv\Scripts\agent-relay.exe --version
# install the official standalone CLI locally (never use the desktop MSIX binary)
npm.cmd install --prefix .codex-cli --no-audit --no-fund @openai/codex@0.147.0
```

The `.venv` editable install is for this sandbox checkout only. Do not use
`pip install --user -e .` as a production installation. Production must later
come from the exact certified `master` commit or stable tag in a separate
non-editable environment.

Edit `config/projects.yaml` only if a separate recipient is intended; otherwise leave `worker_email` empty and the adapter will use Gmail `users.getProfile` after OAuth. Place the existing Google OAuth desktop-client JSON at `%LOCALAPPDATA%\CodexOrchestrator\secrets\oauth-client.json` (never in this repository), then run:

```powershell
orchestrator auth-gmail
orchestrator auth-chatgpt dummy_project
orchestrator browser-probe dummy_project
orchestrator preflight dummy_project
orchestrator run dummy_project
```

`auth-chatgpt` starts the normal installed Chrome with a dedicated user-data directory and loopback-only CDP (`127.0.0.1:9222`). The adapter never launches a Playwright browser, uses the default Chrome profile, copies cookies, or bypasses a challenge. If a human challenge is shown, complete it in the visible dedicated Chrome window and retry. `browser-probe` attaches over CDP, reuses the exact auditor URL, and submits only `ORCHESTRATOR_BROWSER_PROBE` (no Gmail action).

If a run is interrupted after a delivery exists, use `orchestrator resume-audit dummy_project`; it reloads the persisted delivery and never resends the TASK/DELIVERY or creates a new Codex session. `resume` is restart-safe and recognizes the auditor's terminal `R004` control message.

`preflight` is mandatory: it validates the actual installed Codex CLI before any work. If the executable is unavailable or its non-interactive help does not advertise resume support, the command exits with `HUMAN_REQUIRED` and does not continue.

## Reusable project workflow

From an unambiguous Git working tree, register once:

```powershell
agent-relay register --project-id generic_chess --auditor-url https://chatgpt.com/c/...
```

Then each bounded worker turn ends with:

```powershell
agent-relay deliver
```

Registration discovers repository root, origin remote, branch, repository HEAD, and stores the project-scoped routing record under `%LOCALAPPDATA%\CodexOrchestrator\registry\projects.json`. The production verifier rejects wrong branches, protected branches, dirty trees, missing remote refs, local/remote SHA mismatches, and unchanged baseline SHAs. It then emits a concise `[ORCH][project][phase][DELIVERED]` monitoring mail and submits the independently verified Git payload to the exact auditor URL.

See [docs/AGENT_USAGE.md](docs/AGENT_USAGE.md) for the copy/paste worker bootstrap and managed-worker handoff.

The public v0.3 contract is intentionally small:

```powershell
agent-relay --version
agent-relay adopt --project-id generic_chess --auditor-url https://chatgpt.com/c/...
agent-relay identify
agent-relay agent-guide
agent-relay deliver
agent-relay status
agent-relay pause
agent-relay resume
agent-relay doctor
agent-relay browser recover --project-id generic_chess
agent-relay service start|stop|status
```

`agent-relay deliver` is the final normal worker action. Optional `--note`/`--note-file` is metadata only; Git facts remain authoritative. `agent-relay adopt` writes a non-secret handoff package and can perform the one-time managed-worker bootstrap with `--bootstrap`.

The resolver rejects `C:\Program Files\WindowsApps\OpenAI.Codex_*\...\codex.exe` because that is a desktop-app-private MSIX binary. It selects the project-local official npm CLI, invokes it through Node, and records the selected path, version, and reason in SQLite. Run the Gmail-independent persistence proof with:

```powershell
orchestrator worker-test dummy_project
```

This creates only `worker_turn_a.txt` and `worker_turn_b.txt` under the configured dummy workspace and verifies both turns use one persisted session ID.

Use `orchestrator status dummy_project`, `history`, `stop`, `resume`, or `resume-audit` for operator control. A stop file at `%LOCALAPPDATA%\CodexOrchestrator\stops\dummy_project.STOP` also ends progression.

## Safety boundary

Every delivery message states **SIMULATION ONLY — NO REAL GIT PUSH OCCURRED**. Dummy task files are written only below the configured `%LOCALAPPDATA%\CodexOrchestrator\dummy-workspaces` location. OAuth tokens, browser profile, SQLite state, logs, and round artifacts live in `%LOCALAPPDATA%\CodexOrchestrator`; only templates and source are tracked here.
