# AgentRelay P0 审计交接（给 Chat）

## 从哪里开始

请直接审计这个公开仓库的 sandbox 分支：

```text
Repository: https://github.com/WD-nanophotonics/AgentRelay
Branch:     sandbox
HEAD:       0daf60bab8b1f1bcdd0519735cd34cf0514eefa4
Visibility: public
```

这是当前唯一持续开发分支。`master` 没有冻结版本，不应作为本次审计基线，也不应据此宣称生产认证通过。

建议审计入口：

```text
src/dummy_orchestrator/process_policy.py
src/dummy_orchestrator/diagnostics.py
src/dummy_orchestrator/global_cli.py
src/dummy_orchestrator/adapters.py
src/dummy_orchestrator/monitor.py
tests/test_process_policy.py
docs/P0_console_flash_and_codex_stability_report_20260816.md
```

## 用户报告的问题

Windows 桌面出现大约每秒一次的黑色 CMD/Terminal 窗口：

```text
窗口出现 → 抢前台 → 消失 → 重复
```

这使正常 GUI 输入困难。另一个独立现象是：在静默后台策略下，Codex App/工具调用曾中途崩溃或被工具层中止。

## 已证实的窗口根因

通过不创建可见窗口的 Python/ctypes 观察器，在“仅 diagnostics recorder、supervisor/monitor 停止”的隔离条件下捕获到：

```text
AgentRelay diagnostics recorder
  → 每约 1 秒 subprocess.run(["tasklist", ...])
  → tasklist.exe
  → conhost.exe / OpenConsole.exe / WindowsTerminal.exe
  → CASCADIA_HOSTING_WINDOW_CLASS
  → 标题包含 tasklist.exe
  → 前台在 Terminal 与用户窗口之间切换
```

代表性进程关系（PID 是一次性现场证据，不是稳定配置）：

```text
recorder PID       = 19132
tasklist PID       = 24256
tasklist parent    = 19132
window class       = CASCADIA_HOSTING_WINDOW_CLASS
window title       = C:\Windows\SYSTEM32\tasklist.exe
```

重复采样产生了不同的 tasklist PID，间隔约 1.0–1.1 秒；同时观察到窗口出现、前台切换、窗口消失的完整序列。

因此这个 P0 窗口问题的分类是：

```text
AGENTRELAY_PRODUCT_PROCESS
```

不是临时截图脚本造成的假象。

## 旧代码根因

旧 diagnostics helper 对 `tasklist` 和启动时的 `wevtutil` 使用普通 `subprocess.run`，没有统一传递 Windows 隐藏进程策略。recorder 自己虽然 detached，但周期性 helper 没有 `CREATE_NO_WINDOW` / hidden `STARTUPINFO`，导致 Windows 创建控制台宿主并激活前台。

## sandbox 中已提交的修复

`process_policy.py` 提供一个集中策略：

```text
短命后台 helper:
  CREATE_NO_WINDOW
  STARTF_USESHOWWINDOW + SW_HIDE

长命 detached child:
  DETACHED_PROCESS
  CREATE_NEW_PROCESS_GROUP
  STARTF_USESHOWWINDOW + SW_HIDE
```

已迁移到策略层的路径包括：

- diagnostics 的 `tasklist` / `wevtutil`；
- diagnostics recorder 启动；
- supervisor 启动；
- registry/Git 状态调用；
- Codex preflight/run；
- Chrome preflight/launch。

显式用户动作 `Open logs → explorer.exe` 保持可见，这是有意的人机交互，不属于后台采样。

当前 sandbox 提交历史：

```text
9f67c92  sandbox: add AgentRelay and silent background process policy
0daf60b  sandbox: ignore generated package metadata
```

## 已完成验证

进程策略和 diagnostics 路由的聚焦测试：

```text
10 passed
```

公开内容已脱敏：

- 没有本机用户名或绝对 checkout 路径；
- 运行时路径使用环境变量或占位符；
- OAuth、token、SQLite、日志、浏览器 profile、项目运行时状态未上传；
- `config/projects.yaml` 未上传，仅保留 example 配置。

## 尚未完成、不能假设通过的项目

以下项目尚未完成：

1. 60 秒 diagnostics + supervisor + monitor 桌面资格测试；
2. 30 秒 diagnostics + supervisor 无 monitor 测试；
3. 完整 pytest suite；
4. Codex 静默模式崩溃的独立 crash report；
5. `AGENTRELAY_SILENT_BACKGROUND_READY` 门禁。

完整 pytest 曾被 Codex 工具层中止，但没有返回 Python traceback、pytest exit code 或 Codex crash report。因此不能把 Codex 崩溃归因于新隐藏策略，也不能声称静默修复已经证明安全。

## 当前运行状态与安全边界

```text
AgentRelay supervisor = stopped
diagnostics recorder  = stopped
monitor               = stopped
mechanics workflow    = untouched
Gmail                 = no send
worker                = not resumed
browser certification = not performed
external workflow     = not started
```

历史 mechanics 状态应保持：

```text
project_id       = mechanics_sim_workflow
run_id           = RUN-eacec3b4a5
round_id         = F2
state            = COMPLETE
worker_session   = 01a000a9-56ce-70e2-ab04-3adbbe45a9dd
```

## 请 Chat 优先审计的问题

1. `process_policy.py` 的 Windows flags 是否适用于 Python 3.12 的短命 helper、detached recorder 和 supervisor？
2. `DETACHED_PROCESS`、`CREATE_NEW_PROCESS_GROUP`、`STARTUPINFO` 的组合是否会造成 Codex/launcher 生命周期或崩溃副作用？
3. 是否还有遗漏的生产 `subprocess.run/Popen` 没有经过统一策略？
4. 为什么完整 pytest 在静默改动后被工具层中止？需要怎样的独立 crash-forensics 才能区分 Codex App 崩溃与测试进程问题？
5. 在不启动 Gmail、worker、browser certification 或 mechanics workflow 的前提下，如何安全完成桌面资格测试？

## 当前结论

```text
P0 console root cause = IDENTIFIED
silent-background fix  = COMMITTED TO PUBLIC SANDBOX
Codex crash cause      = UNKNOWN / NEEDS INDEPENDENT EVIDENCE
release certification  = NOT GRANTED
```
