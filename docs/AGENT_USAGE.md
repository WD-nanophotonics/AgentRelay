# Agent usage

This repository provides the user-scoped `agent-relay` command. It is a bounded delivery contact tool; the worker must not wait for ChatGPT or poll Gmail.

One-time adoption from the project Git worktree:

```powershell
agent-relay adopt --project-id generic_chess --auditor-url https://chatgpt.com/c/...
```

Use `agent-relay agent-guide` for the authoritative concise protocol and `agent-relay identify` to check an existing repository. Add `--bootstrap` to `adopt` when the one-time managed worker handoff is approved.

For each bounded work unit:

```text
1. Work only on the current instruction.
2. Run the required tests.
3. Commit the completed work.
4. Push to the registered worker branch.
5. Run `agent-relay deliver` from the repository.
6. End this worker turn; do not wait for the auditor.
```

Every managed worker turn receives an injected AgentRelay execution contract. The orchestrator independently verifies repository identity, branch, local HEAD, remote branch SHA, push equality, baseline SHA when supplied, and clean working-tree status. Git is the audit evidence; Gmail is transport and human-visible progress history.

If the current interactive Codex environment exposes `CODEX_THREAD_ID`, the optional command `agent-relay bind-current-worker --project-id generic_chess` records that identifier for diagnostics. Reliable CLI resume compatibility is not assumed. The supported handoff is:

```powershell
agent-relay bootstrap-worker --project-id generic_chess
```

This creates one dedicated managed Codex worker session. Future TASK/CORRECTIVE rounds reuse that session while each operating-system worker process remains bounded.

Useful controls:

```powershell
agent-relay status --project-id generic_chess
agent-relay projects
agent-relay pause --project-id generic_chess
agent-relay service start
agent-relay service status
agent-relay service stop
```

The tool never touches the default Chrome profile, stores secrets in a repository, or performs a real Git push on behalf of the worker.
