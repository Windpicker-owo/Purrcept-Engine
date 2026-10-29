# 构建与分发

Engine 构建和安装不需要同级 Core、LiteLLM 或 Design 源码。目前验收平台是
Windows x64、Python 3.12。前端依赖、Cel 快照与 npm 锁文件都由 Engine 独立持有；
面板和调音室静态资源包含在 wheel 内，运行时无需 Node。

本轮适配库 0.2.1 尚未发布到 PyPI。开发锁文件通过 `[tool.uv.sources]` 指向仓内
`vendor/purrcept_litellm-0.2.1-py3-none-any.whl`；普通发行依赖仍为版本约束。
独立安装应同时交付该 wheel，并用 `--find-links vendor` 安装。其来源及校验值见
`vendor/README.md`。确认适配库公开发布并完成公开索引安装验收后才能移除该覆盖。

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

`uv.lock` 固定仓内适配库产物与公开依赖组合。Core 至少为 0.6.0，
Purrcept LiteLLM 至少为 0.2.1。官方 LiteLLM 精确固定为 1.105.0：
判断传输借用其编译、签名、提供商转换和错误映射接缝，由 Engine 的 HTTPX
客户端发送一次请求，不进入 SDK 默认 handler 的隐式连接重发或全局客户端缓存。
升级该版本须重跑 `test_sdk_boundary.py` 和 `test_model_requests.py`，核验真实
编码、环境隔离、尝试次数及资源归还。原本地补丁及生成脚本已移除。

## 构建发行包

```powershell
uv build --no-sources
```

构建产生 `dist/purrcept_engine-0.1.0.tar.gz` 和 wheel。普通用户可从克隆的仓库
执行 `python -m pip install --find-links vendor .`，或配合 vendor 安装分发的 wheel。
安装 extras 示例为 `python -m pip install '.[web,qq,time]'`。
根目录本地 `plugins/`、真实配置、运行数据和构建临时目录不进入发行包。
内置 `src/purrcept_engine/plugins`、配置模板和静态资源必须在 wheel 中。

## 全新环境验证

在 Engine 仓库运行以下命令。Engine 和适配库使用本轮构建产物，其余包从公开索引解析；不使用 editable 或同级源码。

```powershell
uv venv --python 3.12 build/install-public-minimal
uv pip install --python build/install-public-minimal/Scripts/python.exe --find-links vendor dist/purrcept_engine-0.1.0-py3-none-any.whl
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

发布 Engine 前，先按实际分发依赖完成最小/全部 extra 独立安装，再核对发行包中的
README、默认配置和静态资源。模型、账号与设备效果需要独立验收，不能由离线
测试或依赖安装成功推断。

## 独立前端检查

```powershell
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
node scripts/check_web.mjs
```

只有更新 Cel 快照时需要明确指定上游提交。Design 未提交修改不属于 Engine 依赖。
前端在没有同级仓库的临时目录中重复安装及构建，静态资源随 wheel 一起验收。
