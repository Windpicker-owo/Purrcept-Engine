# Engine 维护约定

Git 提交标题与正文使用中文。设计与验收边界见 `docs/refactor/contract.md`，
进度只维护 `docs/refactor/plan.md`。旧 Runtime 是只读参考，不复制其兼容层。

源码使用中文模块说明、公开 API 契约和必要的私有流程说明，具体格式与编写
规范详见根目录 `CONTRIBUTING.md`。公开 API 采用 Google 风格结构化契约
（包含 Args/Returns/Raises/Note），平衡调用者用法指导与架构防御边界；
说明职责、状态、顺序、资源所有权、取消和失败机制；主流程保持可追踪，不机械拆小方法。

类型检查使用 strict Pyright；不得用 Any、ignore、宽泛异常转换或静默 fallback
掩盖故障。模型、持久化、配置、插件及协议属于真实校验边界。

先运行影响范围内 pytest，再检查 Ruff、格式与 Pyright。跨层变更补真实临时库、
Worker 或传输替身验证。真实模型、消息和设备验证需要另行授权。

## 架构与阅读入口

开始修改前阅读 README 与 `docs/architecture.md`，再读目标领域协议和实现。
`domain` 不导入 app、infrastructure、具体插件或模型 SDK；基础设施不反向
依赖应用配置类。插件作者契约由 `plugin_system` 导出，插件不得借用 SQL 实现
或其他具体插件的内部资源。技术驱动可以复用明确的基础设施工具。

应用是唯一装配点。EngineState 只在应用内部保存资源事实，不能成为通用
服务定位器或传入插件/Worker。阶段、事务、生成资格和资源归还由明确所有者
维护，不用事件订阅、全局单例或旧路径转发层替代业务主流程。

离线验收使用命令级 `LITELLM_LOCAL_MODEL_COST_MAP=True`，只选择 SDK 本地
元数据；不要写入运行配置。后端用 `uv run python -m pytest -q`，随后运行
Ruff、格式、strict Pyright 和 compileall。跨层改动还须验证真实临时库与
Windows Worker；包路径变化须构建 wheel 并完成非 editable 离线冒烟。
