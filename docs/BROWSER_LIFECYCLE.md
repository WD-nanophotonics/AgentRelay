# Browser lifecycle

AgentRelay uses one dedicated Chrome user-data directory and one loopback CDP endpoint. Startup is singleton-protected:

1. probe the expected endpoint;
2. acquire an atomic launch lock only if unavailable;
3. recheck the endpoint;
4. launch exactly one Chrome instance with `--user-data-dir`, loopback CDP, `--no-first-run`, `--no-default-browser-check`, `--disable-sync`, and `--start-minimized`;
5. attach and reuse the exact auditor URL.

The apparent extra Google/Chrome sign-in-style page was a browser first-run/account UI from the same dedicated profile, not a second ChatGPT authentication requirement. The auditor page was in the CDP-owned controlled instance; the sign-in-style UI was not required. First-run/default-browser/sync prompts are now suppressed where Chrome safely permits, and normal headful launches start minimized so they do not intentionally steal foreground focus.

The existing authenticated profile was cold-tested with visible Chrome: close all dedicated-profile processes, relaunch, attach, detect the composer, submit a harmless probe, and reattach. Headless qualification on this machine returned `HEADLESS_CHATGPT_AUTH_OR_CHALLENGE_FAILURE` (`Just a moment...`, no composer), so the default remains `headful_background`. Use `agent-relay browser recover --project-id <id>` for legitimate visible authentication/challenge handling; never bypass a challenge.
