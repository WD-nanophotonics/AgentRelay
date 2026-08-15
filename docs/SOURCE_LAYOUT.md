# Source / install / runtime layout

Canonical editable source tree (machine-specific checkout path intentionally omitted from the public repository):

```text
<agentrelay-checkout>/
```

An incidental bootstrap workspace may exist locally, but its user name and absolute path are intentionally not recorded here.

Installed command wrappers:

```text
%LOCALAPPDATA%\CodexOrchestrator\bin\agent-relay.cmd
%LOCALAPPDATA%\CodexOrchestrator\bin\codex-orchestrator.cmd
```

Runtime state and secrets remain under `%LOCALAPPDATA%\CodexOrchestrator`; no source checkout path is required to invoke the command.
