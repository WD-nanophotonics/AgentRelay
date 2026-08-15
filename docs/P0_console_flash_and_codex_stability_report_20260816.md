# P0：控制台闪烁与 Codex 稳定性现场报告

日期：2026-08-16（Asia/Tokyo）  
当前状态：**已停止所有 AgentRelay 工作；未完成认证；不得宣称 `AGENTRELAY_SILENT_BACKGROUND_READY`。**

## 结论摘要

可见黑色控制台的生产来源已经被客观捕获：AgentRelay diagnostics recorder 每约 1 秒调用一次未隐藏的 `tasklist.exe`。该调用产生 `conhost.exe` / `OpenConsole.exe` / `WindowsTerminal.exe`，并出现 `CASCADIA_HOSTING_WINDOW_CLASS` 窗口；观察到该窗口成为前台，然后在下一次采样后消失。

因此，闪烁窗口的分类是：

```text
AGENTRELAY_PRODUCT_PROCESS
```

Codex 的崩溃/工具中止是另一条尚未证实的链路。本轮全套 pytest 在工具层被中止，没有 Python traceback、退出码或 Codex crash-forensics 报告，不能安全声称隐藏策略已经导致 Codex 崩溃，也不能声称修复完成。

## 现场证据

隔离条件：supervisor 停止、monitor 停止，只启动 diagnostics recorder；观察器使用 `pythonw.exe` + Windows ctypes API，不创建可见控制台。

诊断会话：

```text
<runtime-root>\diagnostics\20260815T233631_p0-diagnostics-only
```

关键 recorder：

```text
launcher PID       = 28100
launcher parent    = 28664 (cmd.exe，AgentRelay CLI 启动链)
recorder PID       = 19132
recorder parent    = 28100
```

会话 `commands.jsonl` 记录的 recorder 启动命令：

```text
<python312>\python.exe
-m dummy_orchestrator.diagnostics _recorder
<runtime-root>\diagnostics\20260815T233631_p0-diagnostics-only
```

观察到的代表性事件：

```text
tasklist.exe       PID=24256  parent=19132
conhost.exe        PID=20444  parent=24256
WindowsTerminal    PID=31560  parent=2144
window class       CASCADIA_HOSTING_WINDOW_CLASS
window title       C:\Windows\SYSTEM32\tasklist.exe
```

随后约每 1.0–1.1 秒重复新的 `tasklist.exe` PID。观察器多次记录：

```text
window_appeared      -> Terminal / tasklist title
foreground_changed   -> WindowsTerminal window
window_disappeared   -> tasklist title
foreground_changed   -> Word 或 ChatGPT
```

这解释了用户看到的：

```text
控制台出现 -> 抢前台 -> 消失 -> 下一次采样再次出现
```

停止 diagnostics 后，recorder 及其 tasklist/conhost 子进程均退出，AgentRelay 服务保持停止。

## 根因

旧实现位于 `src/dummy_orchestrator/diagnostics.py`：

```python
subprocess.run(["tasklist", "/FO", "CSV", "/NH"], ...)
```

`_run()` 没有传入 Windows 的 `CREATE_NO_WINDOW`、`STARTUPINFO`/`SW_HIDE` 或等价策略。recorder 自身虽然使用了 detached flags，但其周期性子进程没有继承“无可见控制台”的政策，因此每次 tasklist 采样都可能创建控制台宿主并激活前台。

同一旧实现还在会话启动时调用一次未隐藏的 `wevtutil`；它不是每秒循环，但也属于后台 helper 的控制台风险。

## 已实施但尚未认证的修复

工作区和 canonical package 已同步以下未认证改动：

1. 新增 `src/dummy_orchestrator/process_policy.py`：
   - 短命 helper：`CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW/SW_HIDE`；
   - 长命 recorder/supervisor：`DETACHED_PROCESS` + `CREATE_NEW_PROCESS_GROUP` + hidden startup info；
   - 明确不把 `CREATE_NO_WINDOW` 与 detached 长命模型盲目合并。
2. diagnostics 的 `tasklist`/`wevtutil` 改为 `run_hidden()`。
3. diagnostics recorder 和 supervisor 改为 `spawn_background(..., detached=True)`。
4. registry、Git、Codex preflight/run、Chrome launch 等内部非交互调用统一进入策略层。
5. 新增进程策略和 diagnostics 路由测试；聚焦测试结果：

```text
10 passed
```

6. 版本已按生产修复递增为 `0.3.7`；完整套件尚未完成。

显式用户交互的 Explorer/Open logs 路径仍然是有意可见的，不属于后台采样路径。

## 旧 subprocess 审计摘要

| call site | 用途/频率 | 旧行为 | 处理状态 |
|---|---|---|---|
| `diagnostics._run` → `tasklist` | recorder 每秒 | 未传隐藏 flags，已实测产生 Terminal/前台切换 | 已改 `run_hidden()`，未做桌面资格认证 |
| `diagnostics._run` → `wevtutil` | session 创建时一次 | 未传隐藏 flags | 已改 `run_hidden()` |
| `diagnostics.start` → recorder `Popen` | 长命 detached | 旧 flags 同时含 detached/no-window，策略分散 | 已改集中 `spawn_background(detached=True)` |
| `global_cli.service start` → supervisor `Popen` | 服务启动一次 | 旧 flags 同时含 detached/no-window | 已改集中 `spawn_background(detached=True)` |
| `global_cli`/`registry`/`adapters` → `git` | CLI/状态/交付期间一次或少量 | `capture_output` 但无 Windows 隐藏策略 | 已改 `run_hidden()` |
| `adapters` → Codex/Chrome preflight/run | 一次性 helper/worker/browser 启动 | 无统一策略 | 已改内部非交互调用；浏览器仍需单独认证 |
| `monitor` → `explorer.exe` | 用户点击 Open logs | 有意的人机交互 | 保持显式可见，不属于后台路径 |

## Codex 崩溃/中止状态

在应用当前不稳定的情况下运行全套 pytest 时，工具调用被中止，输出没有返回：

```text
Python traceback = none
pytest exit code = unavailable
Codex crash report = not captured
```

因此目前只能得出：

```text
可见窗口根因       = 已证实为 AgentRelay diagnostics tasklist 路径
静默模式 Codex 崩溃 = 尚未证实，不能归因
```

静默策略修复尚未通过 60 秒桌面资格测试，也没有重新启动 diagnostics/supervisor 做验证。继续测试前必须先建立独立 crash-forensics 证据，避免再次丢失 Codex 崩溃现场。

## 当前安全状态

```text
AgentRelay service     = stopped
diagnostics recorder   = stopped
monitor                = stopped
pytest                 = no residual process observed
mechanics project      = not touched
Gmail                  = no send
worker                 = not resumed
browser certification  = not performed
external workflow      = not started
```

历史 mechanics 状态仍应保持：

```text
mechanics_sim_workflow
RUN-eacec3b4a5
F2
COMPLETE
01a000a9-56ce-70e2-ab04-3adbbe45a9dd
```

## Git 上传阻塞

当前 canonical AgentRelay checkout：

```text
branch = master
commits = 0
remotes = none
```

所以现在不能安全执行“上传到 Git”：没有远程 URL，也没有可供 Chat 访问的提交。修复文件保留在工作区和 canonical package 中，等待用户提供目标远程仓库/允许创建远程后再做明确的 commit/push。不能把“本地未提交文件”描述成已经上传。

## 建议给 Chat 审阅的问题

1. 是否接受 diagnostics `tasklist` 证据作为 P0 生产缺陷根因？
2. 静默策略下 Codex 崩溃应如何采集独立 crash report，避免测试工具自身吞掉证据？
3. canonical package 与工作区双路径是否应合并，避免 `agent-relay` 运行的源码和当前编辑源码漂移？
4. 请提供目标 Git remote；在此之前不做 push。

最终门禁目前不是：

```text
AGENTRELAY_SILENT_BACKGROUND_READY
```

而是：

```text
P0_ROOT_CAUSE_IDENTIFIED_FIX_UNQUALIFIED
```
