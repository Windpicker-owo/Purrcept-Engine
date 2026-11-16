# Purrcept Engine 贡献指南

欢迎参与 Purrcept Engine 的开发与维护！为了让这个项目对开源使用者和协作者都足够友好、好读、好改，请在提交代码前阅读以下准则。

## 一、核心工程哲学：克制、直观、说人话

1. **拒绝防御性过度设计**：
   - **不要预设不存在的敌人**：本地配置、内部数据流转与标准 JSON 序列化直接复用标准库与 Pydantic，不要手写冗余的深度扫描器或层层复制校验。
   - **不要制造传声筒抽象**：除非真的存在多种需要运行时切换的实现（例如不同模型后端或测试替身边界），否则不要为单一类额外套一层 `Protocol` 或 `*Port` 文件。
   - **报错必须提供可行动线索**：配置错误要指出具体字段、行列号与修复建议；运行异常要保留原始异常消息与堆栈，严禁静默 `pass` 或把错误吞成一句“请核对组件诊断”。
2. **代码可读性优先**：
   - 让主业务链路（`刺激入站 -> 注意评估 -> 主脑思考 -> 能力调用 -> 记忆沉淀`）顺着读就能看懂。
   - 优先保持适度内聚的文件结构，不要把百来行的连贯逻辑拆成七八个小文件。

## 二、注释与 Docstring 规范

1. **面向调用者，而非免责声明**：
   - 模块、类和公开方法使用清晰的中文说明，公开 API 采用 Google 风格 Docstring（按需包含 `Args:`、`Returns:`、`Raises:`）。
   - **第一句话直接讲清业务作用**（例如：`读取并校验 TOML 配置文件，支持 ${ENV} 环境变量展开。`）。
   - **删掉八股文套话**：不要在每个方法底下机械粘贴“实例字段构造后不可重新赋值”、“等待取消不撤销已受理工作”、“不借用外部资源”等防御性口水话；仅在确有并发/生命周期陷阱时用简短的 `Note:` 说明原因。
2. **简洁示例**：

```python
async def submit(self, submission: InputSubmission) -> Admission:
    """向引擎提交外部输入刺激（文本、图片或语音），返回受理凭证。

    提交成功后输入即持久写入收件箱并异步唤醒注意评估；本方法返回仅代表已入账，
    后续处理结果可通过 ``query_input`` 查询。

    Args:
        submission: 待提交的输入内容、来源标识与附件媒体。

    Returns:
        包含权威 ``input_id`` 与去重标记的受理凭证。

    Raises:
        HostStateError: 引擎尚未启动或正在关闭。
        StorageError: 待处理收件箱已达容量上限或写入失败。
    """
    return await self._inputs.submit(submission)
```

3. **行内注释与 TODO**：
   - 行内 `#` 注释只解释 **“为什么这么做（Why）”**，不复述代码字面意思。
   - 待办事项注明上下文：`# TODO(模块/议题): 具体待改进点`。

## 三、类型检查与代码风格

- **Strict Pyright**：保持 `typeCheckingMode = "strict"` 通过，不用 `Any` 或无理由的 `# type: ignore` 糊弄类型边界，但也不要为了炫技堆砌晦涩的泛型体操。
- **Ruff 检查与格式化**：行宽上限 100 字符，中文多行 Docstring 首行摘要后空一行，结尾 `"""` 独占一行（PEP 257）。
- **Git 提交规范**：提交标题与正文使用简洁自然的中文，讲清“改了什么”以及“为什么改”。

## 四、本地验证流程

提交前请运行以下命令确认测试、代码风格与类型检查通过：

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
uv run python -m pytest -q
uv run ruff check src tests examples scripts
uv run ruff format --check src tests examples scripts
uv run pyright
uv run python -m compileall -q src tests examples scripts
```
