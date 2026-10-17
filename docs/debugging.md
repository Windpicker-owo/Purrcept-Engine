# VS Code 断点调试

Engine 没有单独的 `--debug` 参数。调试器通过 Python 模块入口启动同一个宿主，
不会切换主体编排或模型协议。本文配置面向仓库当前的 Windows 开发环境。

## 启动

1. 在 VS Code 中打开 `purrcept_engine` 仓库目录，而不是相邻的 Runtime 或父目录。
2. 安装 Microsoft 的 Python（`ms-python.python`）与 Python Debugger
   （`ms-python.debugpy`）扩展。
3. 确认开发依赖已安装，仓库中存在 `.venv/Scripts/python.exe`。首次准备环境的
   补丁依赖步骤见根目录 README；启动配置直接使用该环境，不自动安装依赖。
4. 打开“运行和调试”（`Ctrl+Shift+D`），选择下面的配置，按 `F5`。

配置保存在 [launch.json](../.vscode/launch.json)：

仓库的 settings.json 为 Python 扩展指定同一 `.venv`，Pyright 也在 pyproject.toml
指定该环境。若此前已缓存其他解释器，运行 `Python: Select Interpreter` 选择
`.venv/Scripts/python.exe`，再重启语言服务；无需放宽 strict 检查。

| 配置 | 用途 |
| --- | --- |
| `Engine：启动宿主` | 读取本地 `purrcept.toml`，按其中的声明启动实际模型和插件。 |
| `Engine：离线调试 decision 到前台` | 使用现有 pytest 场景，以模型替身和临时 SQLite 验证注意判断、前台 Core 工具目录与内部最终结果；不读取本地运行配置，不调用真实模型或设备。 |

宿主配置等价于在仓库目录执行：

```powershell
.\.venv\Scripts\python.exe -m purrcept_engine --config purrcept.toml run
```

本地配置的 `console = true` 时，宿主就绪后可在本次调试的**集成终端**输入一行
文字并回车。输入会经正式接管入口入账。Debug Console 用于检查变量，不是程序的
标准输入。已有相同数据目录的实例须先退出，宿主不会共享该目录锁。

`justMyCode = false` 允许继续进入 Core、模型适配及依赖源码。`subProcess = false`
限定当前配置调试宿主；隔离行动 Worker 的内部代码需要另外设计调试接入。
`LITELLM_LOCAL_MODEL_COST_MAP=True` 只让本次进程使用 SDK 包内元数据，不会把宿主
模型请求变成离线请求。运行配置仍决定实际端点。

## 从输入跟到 decision 和前台

先在可执行语句左侧设置断点（`F9`），再启动。下面按调用顺序给出定位锚点，
行号变化时可以直接搜索函数或语句。

| 位置 | 断点锚点与观察内容 |
| --- | --- |
| `app/ingress.py` 的 `InputAdmissionService.submit` | 查看外部 `submission`；正式宿主输入从这里进入。离线测试直接在 Store 接管，不经过此入口。 |
| `domain/subject/coordinator.py` 的 `StimulusCoordinator.run` / `launch` | 查看缓存批次、提交版本 `expected` 和最低唤起升级资格；协调者拥有合批及前台唤醒。 |
| `domain/subject/attention.py` 的 `attention_request` | 在 `DecisionRequest(...)` 构造处查看 `decision_blocks`、绑定角色卡的 `question.instructions` 和三个注意选项；实际模型请求尚未发出。 |
| `domain/subject/attention_service.py` 的 `assess` | 在 `response = await backend.decide(...)` 前停住；越过后查看 `response`、`answer.choice` 和 `level`。 |
| `domain/subject/coordinator.py` 的 `adopt` | 判断证据已入账；查看原版本 `expected`、返回的 `result.level` 和采用后的状态。过期判断可能不被采用。 |
| `domain/subject/foreground_service.py` 的 `run_foreground` | 查看被唤起的 `records`；继续进入 `domain/subject/session.py` 的 `ContinuousSession.run`，跟踪前台 Core 会话。 |

在暂停处使用“变量”“监视”和“调用堆栈”。`F10` 越过当前调用，`F11` 进入调用，
`F5` 继续到下一断点。异步工作器之间的交接应在各自入口设置断点，调用堆栈不会
把后台任务还原成一条同步调用链。

注意判断的 `observe` 将输入提交到主脑收件箱但不立即唤醒，`wake` 请求下一轮
思考；`urgent` 在处置成功提交后才尝试抢占观察到的旧生成。`defer` 已删除。
主脑每次唤醒时原子取尽已提交输入，运行中不再拉取新刺激。高价值输入直接提交，
仅在忙碌时评估是否进一步升级为抢占，因此连续输入不一定每次命中 decision。

## 暂停时间与超时

断点不会停止真实时钟、远端服务或独立进程的计时。默认注意请求单次预算为
5 秒、总预算为 8 秒，长时间停在请求预算内部可能导致继续后出现超时。
需要慢速单步时，可仅在本机 `purrcept.toml` 的已有表中调整：

```toml
[subject.attention_retry]
attempt_seconds = 300.0
total_seconds = 600.0
```

不要重复声明同名 TOML 表。前台和行动仍有各自预算，调大注意预算不会同时改变
它们。调试结束后恢复本机预算；项目默认值无需修改。

离线测试自身也有 5 秒等待上限，且不读取上述 TOML 预算。离线配置适合快速断点
或日志点；慢速单步可能使测试因等待超时失败，这时应区分调试暂停和业务故障。
只需观察值时，可以右键断点改用日志点，避免暂停影响流程计时。

调试器配置项及断点行为见 [VS Code 官方 Python 调试说明](https://code.visualstudio.com/docs/python/debugging)。
