# AgentRelay versioning and installation contract

This document is the current version-management contract. Historical
certification reports remain evidence, but they do not define the current
installed identity.

## Branch roles

- `sandbox` is the active development and certification-candidate branch. It
  may contain unreleased changes and must identify itself through runtime
  identity before a real project is operated.
- `master` is promotion-only. A future promotion must move the exact commit
  already validated on `sandbox`; it must not receive ordinary development.
- The current remote exposes `sandbox` only. No `master` branch, stable tag, or
  master worktree is currently present. This is an inventory fact, not a
  reason to create or promote one during routine development.

The canonical source checkout on this machine is:

```text
C:\Users\icywo\PycharmProjects\AgentRelay
```

It is the `sandbox` checkout. Do not create independent AgentRelay clones in a
managed target project. AgentRelay is user-scoped infrastructure.

## Single version source

The only manually maintained semantic version is:

```text
src/dummy_orchestrator/version.py::__version__
```

`pyproject.toml` derives package metadata from that value. The CLI, diagnostics
manifest, and runtime identity import the same value. A version change must
update only `version.py`, then run the focused version tests and the full test
suite.

## Runtime identity

`source_identity()` reports:

- semantic version and its source file;
- package path and install mode;
- source checkout and SHA-256 source fingerprint when running from a checkout;
- Git revision, branch, and dirty state;
- installed distribution metadata version when available, including whether it
  agrees with the authoritative version.

Before operating a real project, record this identity. A stale or ambiguous
identity is a stop condition.

## Explicit sandbox invocation

The sandbox environment is intentionally explicit and may be editable:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\agent-relay.exe --version
```

For the next Mechanics canary, invoke only this path from the canonical
checkout:

```powershell
& 'C:\Users\icywo\PycharmProjects\AgentRelay\.venv\Scripts\agent-relay.exe' <subcommand> ...
```

Do not use a bare `agent-relay` command for certification. The machine has
legacy wrappers in more than one user command directory.

## Production installation

There is no stable production AgentRelay installation established by this
cleanup. Production must eventually be installed non-editably from the exact
promoted `master` commit or stable tag, in a separate environment. It must
never use:

```powershell
pip install --user -e <mutable sandbox checkout>
```

Until a master promotion and master-specific certification complete, the
production role is **NOT INSTALLED / NOT READY**. Do not silently reuse the
sandbox editable environment as production.

## Required workflow

1. Develop and test on `sandbox`.
2. Run the approved sandbox Mechanics canary.
3. Promote the exact tested sandbox commit to `master`.
4. Freeze and certify `master` separately.
5. Only then use the stable installation for real production projects.

Never change `master` during normal development, never force-push, and never
tag an uncertified sandbox commit as a production release.
