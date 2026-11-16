# Purrcept-Engine 架构导读与代码质量审查指南

这份文档专门写给项目作者与日常维护者：用**直白的人话**把整个仓库拆清楚——每个目录到底管什么、日常改功能该打开哪几个文件、三轮工程化清理解决掉了哪些历史包袱，以及目前保留的大文件为什么这样划分。

---

## 一、三分钟看懂整个系统怎么转起来

不要被仓库里 260 多个 Python 文件吓到。整个 `Purrcept-Engine` 其实只做一件事：**让一个持续在线的 AI 主体接收外界“刺激”，先用轻量注意判断要不要醒来，醒来后在连续会话里通过 Python 行动代码（`me.*`）操作世界，并把一切事实记进同一个 SQLite 数据库。**

一条消息从进入到产生回复，只会依次路过下面 **6 站**：

```text
[外部世界：QQ / 麦克风 / 控制台 / Web 后台聊天]
   │
   ▼  1. 接管入库（app/ingress.py -> infrastructure/sqlite/store_inputs.py）
      先把原文、图片/音频附件、待办状态原子写进 engine.sqlite3，拿到 input_id
   │
   ▼  2. 刺激协调（domain/subject/coordinator.py）
      把收件箱里的消息合批。如果是私聊/@/后台聊天（保底唤起），直接唤醒主脑；
      如果是普通群聊等背景噪音，交给 Attention 模型打分（observe 留存 / wake 唤醒 / urgent 抢占）
   │
   ▼  3. 主脑思考（domain/subject/foreground_service.py -> session.py）
      唤醒时把整个收件箱一次性交给主脑（Core 连续会话）。
      注意：主脑输出的普通文本只是“内心独白”，不会自动发给任何人！
   │
   ▼  4. 显式行动（infrastructure/execution/service.py -> worker.py）
      主脑调用 play_action 执行受限 Python 代码（例如 me.phone.qq.send(...) 或 me.microphone.speak(...)）。
      代码在独立 Windows Job 子进程里跑，通过本地管道把每次调用请求发回宿主
   │
   ▼  5. 插件执行与回执入账（plugins/* -> infrastructure/sqlite/store_execution.py）
      插件真正发消息/放声音/截屏，每次调用的成功/失败/未知回执逐条写回 SQLite
   │
   ▼  6. 后台反思与手账整理（domain/subject/background_service.py & plugins/notebook.py）
      后台对刚才的行动做行为自检；主体记下的 Markdown 笔记由 Notebook 插件异步建立 FTS5/向量索引并整理成专题记忆
```

---

## 二、五个顶层目录各自只管什么？

`tests/test_architecture.py` 用自动测试守住了单向依赖边界（内层绝不反向 import 外层），所以你在读代码时可以按下面的职责直接定位：

| 目录 | 一句话职责 | 日常什么时候需要改它？ |
| --- | --- | --- |
| `app/` | **总装车间 + 命令行 + Web 后台** | 改 CLI 命令（`cli.py` / `onboarding.py`）、改 `purrcept.toml` 配置字段（`config.py`）、改启动顺序（`bootstrap.py`）、改 Web 后台接口（`admin_api.py` / `admin_config.py` / `admin_notebooks.py`） |
| `domain/` | **纯业务规则（不碰 SQL、不碰网络、不碰子进程）** | 改主脑/注意提示词（`domain/subject/prompts.py`）、改刺激合批与睡眠唤醒逻辑（`coordinator.py`）、改上下文压缩与 Token 估算（`domain/context/`） |
| `infrastructure/` | **脏活累活实现层（SQLite、模型请求、沙箱子进程）** | 改数据库表或查询（`infrastructure/sqlite/`）、改 OpenAI/LiteLLM 请求适配（`infrastructure/models/`）、改 Windows Worker 进程隔离（`infrastructure/execution/` & `processes/`） |
| `plugin_system/` | **给插件作者看的公共接口清单** | 几乎不用改，除非你要给所有插件新增一种通用宿主端口 |
| `plugins/` | **11 个互不耦合的具体设备/平台插件** | 改 QQ 消息格式去 `plugins/qq/`，改 TTS 与 FFmpeg 音效去 `plugins/audio/`，改记忆手账去 `plugins/notebook.py`，改 VTube Studio 口型表情去 `plugins/live2d/` |

---

## 三、本轮代码瘦身、解耦与工程化去魅成果汇总

针对早期由 GPT/Codex 维护遗留的“过度防御性编程、上帝类堆积、法条式黑话注释、日志反人类、配置无引导、WebUI 只看不控”六大痛点，已完成系统性重构：

### 1. 拆解核心上帝类与巨型模块
- **`infrastructure/sqlite/store.py`（从 2,361 行拆解为 ~165 行清晰门面 + 4 个单一职责子模块）**：
  - `store.py`：仅保留 `Store.open` / `close`、子账本属性借出、面板投影与过期清理入口，对外 `from purrcept_engine.infrastructure.sqlite.store import Store, input_logical_bytes` 保持 100% 兼容。
  - `store_inputs.py`：专职输入准入去重、睡眠积压分流与收件箱阶段流转。
  - `store_sessions.py`：专职连续会话版本、轮次事务、摘要占位、容量锚点与 `subject_work` 未决工作投影。
  - `store_execution.py`：专职行动源码、能力调用逐次回执、模型请求证据与长时活动状态持久化。
  - `rows.py`：提取统一的 SQLite 字段校验与时间转换函数（`as_text`、`as_opt_text`、`as_int`、`as_bool`、`as_datetime`），消除了原先在 **12 个 SQLite 模块**里反复复制粘贴的 `_text` / `_optional_text` / `_integer` 样板代码。
- **`app/admin_api.py`（从 1,034 行拆解为 3 个高内聚模块）**：
  - `admin_api.py`（~230 行）：轻量 Starlette 路由装配层。
  - `admin_config.py`：专职 `purrcept.toml` 可视化/源码双模读写、`tomlkit` 注释保留、密钥掩码保护与滚动备份。
  - `admin_notebooks.py`：专职 Notebook 记忆与技能 Markdown 目录发现、路径越界防护与乐观锁读写。
- **`app/bootstrap.py` 与 `app/queries.py` 收敛**：
  - 将 `EngineAssembly.start()` 原先 240 行一镜到底的嵌套装配流程拆解为 `_open_store` -> `_open_models` -> `_build_plugins` -> `_assemble_execution_and_subject` -> `_activate_plugins_and_subject` 五个清晰阶段。
  - 合并 `HostQueries` 中重复 10 次的存储空指针守卫代码，清理 `app/config.py` 中围绕 `decode(..., errors="ignore")` 的多余宽泛 `except Exception`。

### 2. 清理机械防御样板与死板测试锁
- **移除 ~400+ 行机械复制的“AI 法条式注释噪音”**：清理了遍布 38 个内部模块、重复高达 84 次的 `@dataclass(frozen=True)` 废话附注（`实例字段在构造后不可重新赋值...`）、空洞的占位 `Raises` 与晦涩路径套话，同时**严格保留**了所有参与模型 `FunctionTool` 描述与能力参数 Schema 的协议文档字符串。
- **松绑架构测试**：移除了原先 `tests/test_architecture.py` 中用 `ast.dump` 死锁方法形参字面量的脆弱断言，只守护真正有价值的单向分层依赖与核心公开方法契约。
- **修复间歇性脆弱测试**：修复了 `tests/test_windows_worker.py` 在 Windows 15.6ms 计时器粒度下用 `time.sleep(0.1)` 测 CPU 句柄导致偶发 0 tick 失败的问题。

---

## 四、全库代码质量审查报告（现状评估与后续演进建议）

经过三轮改造，目前代码库在**类型安全（Pyright Strict 零妥协）、并发与进程边界安全、测试覆盖密度（1,540+ 用例）以及用户体验闭环**上均已达到可直接公开开源的水准。

以下是作为项目负责人你应当心中有数的 **5 个大体积业务模块**及其维护建议：

| 模块路径 | 当前规模 | 为什么目前保持单文件？ | 后续如果继续扩写怎么拆？ |
| --- | --- | --- | --- |
| `plugins/notebook.py` | ~2,080 行 | 聚合了笔记扫描、局部行编辑能力、FTS5/向量混合检索与后台专题整理循环，内部共享同一目录锁与版本核验逻辑 | 若后续新增更多记忆工具（如知识图谱或外部导入），可按 `plugins/notebook/tools.py`、`indexer.py`、`compiler.py` 拆成子包 |
| `infrastructure/sqlite/evidence.py` | ~1,480 行 | 承载共享原文快照、精确字符切片、行为评审与监管计划的同事务 SQL 校验 | 表间存在强外键与同一事务原子读写，不建议强行拆散 SQL；保持现状即可 |
| `domain/activities/service.py` | ~1,470 行 | 唯一的长时活动（如持续看屏幕/挂机任务）状态机、预算计时器与两阶段崩溃恢复 | 状态转移已在同目录拆分了 `control.py` / `state.py` / `validation.py`，主服务仅在新增活动类型时按需阅读 |
| `infrastructure/processes/windows.py` | ~1,120 行 | Win32 Job Object、句柄继承白名单与进程树强制回收的底层 `ctypes` 封装 | 属于纯底层基础设施“黑盒”，有完整的真实进程句柄泄漏测试保护，平时无需改动 |
| `plugins/qq/service.py` & `plugin.py` | 各 ~1,000 行 | OneBot v11 WebSocket 重连、消息/合并转发/群文件解析与发送白名单校验 | 如果以后接入 QQ 官方 Bot API 或更多社交平台，只改 `plugins/qq/` 内部即可，对内核零影响 |

### 日常审查代码的三条实用准则

1. **改提示词或工具说明前注意“协议文档”**：
   - `domain/subject/foreground_tools.py`、`reflection_tools.py` 以及各插件 `plugin.py` / `parameters.py` 里的 docstring 和 Pydantic `Field(description=...)` 会直接发给大模型看。改它们等于改 Prompt，改完跑一遍 `uv run purrcept-engine catalog` 就能直观看到模型眼里的能力说明书。
2. **异步函数里不要直接做同步磁盘读写**：
   - 项目开启了 Ruff `ASYNC` 规则。在 `async def` 里读写本地文件请统一用 `await asyncio.to_thread(...)`，写数据库统一走 `Store` 方法（内部自动派发到专属 SQLite 线程）。
3. **发版或合入 PR 前跑这四行命令即可兜底**：
   ```powershell
   $env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
   uv run ruff check src tests examples scripts
   uv run pyright
   uv run python -m pytest -q
   ```
   只要这三项全绿，分层依赖、严格类型和核心状态机就不会出暗病。
