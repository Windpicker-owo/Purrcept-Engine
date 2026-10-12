# Engine 维护约定

Git 提交标题与正文使用中文。设计与验收边界见 `docs/refactor/contract.md`，
进度只维护 `docs/refactor/plan.md`。旧 Runtime 是只读参考，不复制其兼容层。

源码使用中文模块说明、公开 API 契约和必要的私有流程说明。说明职责、状态、
顺序、资源所有权、取消和失败机制；主流程保持可追踪，不机械拆小方法。

类型检查使用 strict Pyright；不得用 Any、ignore、宽泛异常转换或静默 fallback
掩盖故障。模型、持久化、配置、插件及协议属于真实校验边界。

先运行影响范围内 pytest，再检查 Ruff、格式与 Pyright。跨层变更补真实临时库、
Worker 或传输替身验证。真实模型、消息和设备验证需要另行授权。
