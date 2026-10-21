# 构建与分发

Engine 的 Python 运行依赖全部从 PyPI 解析，安装不需要同级 Core、Purrcept
LiteLLM、Design 源码或本地补丁 wheel。目前验收平台是 Windows x64、Python 3.12。
Design 仅用于开发阶段重建前端；面板和调音室静态资源已包含在 Engine 包内。

## 开发环境

```powershell
uv sync --locked --all-groups
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
uv run python -m pytest -q
uv run ruff check src tests examples scripts
uv run ruff format --check src tests examples scripts
uv run pyright
uv run python -m compileall -q src tests examples scripts
```

`uv.lock` 固定公开依赖组合，`pyproject.toml` 不配置本地 source 覆盖。Core 至少
为 0.6.0，Purrcept LiteLLM 至少为 0.2.0。官方 LiteLLM 精确固定为 1.105.0：
判断传输借用其编译、签名、提供商转换和错误映射接缝，由 Engine 的 HTTPX
客户端发送一次请求，不进入 SDK 默认 handler 的隐式连接重发或全局客户端缓存。
升级该版本须重跑 `test_sdk_boundary.py` 和 `test_model_requests.py`，核验真实
编码、环境隔离、尝试次数及资源归还。原本地补丁及生成脚本已移除。

## 构建发行包

```powershell
uv build --no-sources
```

构建产生 `dist/purrcept_engine-0.1.0.tar.gz` 和 wheel。普通用户可从克隆的仓库
执行 `python -m pip install .`，或直接安装分发的 wheel；其依赖由 PyPI 提供。
安装 extras 示例为 `python -m pip install '.[web,qq,time]'`。
根目录本地 `plugins/`、真实配置、运行数据和构建临时目录不进入发行包。
内置 `src/purrcept_engine/plugins`、配置模板和静态资源必须在 wheel 中。

## 全新环境验证

在 Engine 仓库运行以下命令。只有 Engine 使用本地构建产物，其余包全部从 PyPI
解析，不使用 editable、同级 SDK wheel 或额外本地索引。

```powershell
uv venv --python 3.12 build/install-public-minimal
uv pip install --python build/install-public-minimal/Scripts/python.exe dist/purrcept_engine-0.1.0-py3-none-any.whl
uv pip check --python build/install-public-minimal/Scripts/python.exe
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
build/install-public-minimal/Scripts/python.exe -I -X utf8 scripts/verify_installation.py
```

另建 `build/install-public-all`，将安装参数替换为
`'dist/purrcept_engine-0.1.0-py3-none-any.whl[web,qq,time,browser,voice-input,live2d]'`
以验证全部可选依赖。安装不连接模型或开启设备；浏览器程序、FFmpeg、VAD 模型
及设备校准仍需使用者明确准备，宿主不会自动下载。

冒烟脚本要求非 editable 安装，检查 SDK 版本、八份静态资源、真实 Windows
Worker、临时 SQLite 图片保存和重开、目录锁收回。模型后端禁止调用，图片和
输出均为测试样本。`-I` 隔离当前目录和 PYTHONPATH，避免误读开发源码。
`LITELLM_LOCAL_MODEL_COST_MAP=True` 只选择包内价格表，不会把真实模型请求
变成离线请求，也不替代容量或媒体声明。

## 依赖发布顺序

Core 的 GitHub Release 触发其 PyPI 发布工作流；确认对应版本在 PyPI 可安装后，
再发布 Purrcept LiteLLM。两者的 `RELEASING.md` 记录 Trusted Publisher 配置。
只有推送 Git 提交或标签不会触发这两个仓库的 PyPI 发布。

发布 Engine 前，先以公开依赖完成最小/全部 extra 独立安装，再核对发行包中的
README、默认配置和静态资源。模型、账号与设备效果需要独立验收，不能由离线
测试或依赖安装成功推断。
