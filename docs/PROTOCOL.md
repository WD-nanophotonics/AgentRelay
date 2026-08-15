# AgentRelay worker protocol

Each managed worker turn receives an injected `AGENTRELAY EXECUTION CONTRACT` before the task. The worker handles exactly one TASK or CORRECTIVE, validates/tests it, commits, pushes to the registered worker branch, runs `agent-relay deliver`, and ends the turn.

`deliver` is the final normal action. The worker must not poll Gmail, read ChatGPT, wait for the auditor, or invent a next phase. The supervisor independently verifies Git facts and owns all continuation. `HUMAN_REQUIRED` is a hard stop.

The durable autonomous identity is the managed Codex CLI session, not an indefinitely running OS process and not an arbitrary desktop ChatGPT thread. The old interactive chat remains available to the human but is not silently adopted as the autonomous identity.
