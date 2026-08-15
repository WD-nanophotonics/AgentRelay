# Human onboarding contract

For an existing project, provide the worker only:

```text
Project ID: generic_chess
Auditor Chat: https://chatgpt.com/c/...

This project now uses AgentRelay. Run:
agent-relay adopt --project-id generic_chess --auditor-url https://chatgpt.com/c/...
Then follow `agent-relay agent-guide`.
```

If already registered:

```text
This project is registered with AgentRelay as `generic_chess`.
Use `agent-relay identify` and `agent-relay agent-guide`.
```

`adopt` writes only non-secret registration and handoff metadata to the user runtime. It does not store credentials in the repository and does not start a real production canary automatically. Add `--bootstrap` only when the one-time managed worker handoff is approved.
