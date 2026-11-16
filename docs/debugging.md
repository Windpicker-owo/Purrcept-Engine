# 日志排查与 VS Code 断点调试

## 一、命令行日志排查

Engine 默认在终端输出本地时区的人类可读生命周期日志（`[刺激入站]` -> `[注意评估]` -> `[主脑思考]` -> `[能力调用]` -> `[行动完成]`），并在报错时直接打印脱敏后的错误原因与异常堆栈。

可通过以下参数随时切换日志详细度：

```powershell
# 开启 DEBUG 级别日志并在顶层异常时输出完整堆栈
uv run purrcept-engine --debug run

# 或显式指定日志级别
uv run purrcept-engine --log-level DEBUG run
```

运行期间的结构化 JSON Lines 滚动日志保存在 `<data_dir>/logs/engine-YYYYMMDD-NNNNNN.log` 中。

## 二、VS Code 断点调试

1. 在 VS Code 中打开 `Purrcept-Engine` 仓库根目录，确认已安装 `ms-python.python` 与 `ms-python.debugpy` 扩展，且 `.venv/Scripts/python.exe` 已就绪。
2. 按 `Ctrl+Shift+D` 打开“运行和调试”，选择 `.vscode/launch.json` 中的预设配置并按 `F5` 启动：
   - **`Engine：启动宿主`**：读取本地 `purrcept.toml` 启动真实模型与插件；当 `console = true` 时可在集成终端直接输入文字发送刺激。
   - **`Engine：离线调试 decision 到前台`**：使用离线替身与临时 SQLite 跟踪注意判断与前台循环，不请求外部网络。

### 核心断点位置速查

| 阶段 | 文件与方法 | 可观察状态 |
| --- | --- | --- |
| 1. 刺激入站 | `app/ingress.py` -> `InputAdmissionService.admit_input` | 原始 `submission`、来源通道与分配的 `input_id` |
| 2. 注意调度 | `domain/subject/coordinator.py` -> `StimulusCoordinator.launch` / `adopt` | 合批记录 `records`、注意评估结果 `observe`/`wake`/`urgent` |
| 3. 注意请求 | `domain/subject/attention_service.py` -> `AttentionService.assess` | 发送给注意模型的完整请求与返回的理由 |
| 4. 主脑轮次 | `domain/subject/foreground_service.py` -> `ForegroundService.run_foreground` | 被唤醒的输入列表、角色指令与 `ContinuousSession.run` |
| 5. 能力调用 | `infrastructure/execution/call_dispatcher.py` -> `CallDispatcher.enter_provider` | 具体 `me.*` 能力路径、参数与执行回执 `CallOutcome` |

> **提示**：断点暂停期间真实时钟仍在走动。若需长时间单步停留，可在 `purrcept.toml` 中临时调大 `[subject.attention_retry]` 或 `[subject.foreground_retry]` 的 `attempt_seconds` 与 `total_seconds`。
