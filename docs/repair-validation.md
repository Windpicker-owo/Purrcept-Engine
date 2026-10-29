# 全仓库修复实施与验收记录

本轮基于 Engine `24c9d8a` 实施五部分修复。代码、测试、依赖快照和文档保留在工作区，
未创建提交、推送或发布。Core 无改动；Design 的既有未提交修改未纳入打包，也未被修改。
配置和公共契约见 [运行治理与维护](governance.md)、[配置说明](configuration.md)
及 [分发说明](distribution.md)。

## 实施对应关系

| 批次 | 交付内容 | 主要验证入口 |
| --- | --- | --- |
| 一 | 固定 Design 包与 npm 锁、独立前端 CI、组件健康、启动健康接口、分段计时、中文终端与脱敏结构化文件日志。 | `tests/test_logging.py`、`tests/test_host.py`、`tests/test_cli.py`、`tests/test_structured_calls.py`、`tests/test_web.py`；前端与浏览器检查。 |
| 二 | 按用途／来源版本／覆盖范围领取、真实前缀续页、有限退避、人工评价优先及普通来源名额、完整请求容量预检、持久化共享传输额度。 | `tests/test_analysis_runtime.py`、`tests/test_governance.py`、`tests/test_reflection.py`、`tests/test_model_requests.py`。 |
| 三 | SDK 公共纯编译与身份接口、Engine 公共调用、启动完整检查迁出、浏览器延迟初始化、WebUI 提前监听及业务就绪门禁。 | SDK `tests/test_compilation.py`；Engine `tests/test_browser.py`、`tests/test_host.py`、`tests/test_storage_upgrade.py` 及独立安装。 |
| 四 | 格式16与原子15→16迁移、诊断正文淘汰与状态查询、媒体回收、显式离线维护、轻量面板、查询索引、数据库线程外精确向量计算。 | `tests/test_governance.py`、`tests/test_storage_upgrade.py`、`tests/test_maintenance_storage.py`、`tests/test_media_history.py`、`tests/test_storage.py`。 |
| 五 | 独立行为评审与记忆整理、每批空历史、角色工具隔离、证据资格、建议去重及可靠投递、接受与改善分开记录、真实行动／轮次／调用评价。 | `tests/test_analysis_runtime.py`、`tests/test_governance.py`、`tests/test_background.py`、`tests/test_supervision_context.py`、`tests/test_review_evaluation.py`。 |

现有“第33条来源必须等待新事实”的测试契约已替换。来源溢出、长材料分页、持续人工
评价、预算拒绝、准备阶段失败、重启恢复和无证据结案均有合成回归。故障测试覆盖正常
取消、重复关闭、嵌套原因链及异常组；存储可读时仍允许查询故障与历史。

## 本地质量与安装验收

- Engine：全量 pytest **1365 项通过**；Ruff、格式检查、strict Pyright、脚本类型检查和
  `compileall` 通过。最后新增的用量锚点回归确认：指纹与 token 计数保留，引用身份不
  永久保护请求正文。仅有第三方 Pydantic 对 `ReadOnly` 的一项提示。
- LiteLLM 适配库：**250 项通过、2 项真实模型集成测试未运行**；Ruff、格式检查、
  strict Pyright 和 `compileall` 通过。新进程拦截传输模块加载及 HTTP，生成／判断请求
  编译与身份查询通过；同一断言已在非 editable wheel 安装中复验。
- 前端：独立临时目录没有兄弟仓库，`npm ci --offline`、类型检查、**40 项测试**、
  Design 样式校验和构建通过。四个静态产物与仓内资源逐字节相同。
- 浏览器检查：**28 个快速切换帧、35 个页面／视口组合**，包含搜索、键盘操作、
  部分失败详情、断线控制、开关、人工评价、窄屏关闭、减少动效与空状态，全部通过。
- Engine wheel：分别安装于最小依赖与完整插件依赖环境，`uv pip check` 通过。
  使用隔离模式运行安装冒烟，确认真实 Windows Worker、媒体像素与引用重开、目录锁
  及8个静态资源；两次运行均未调用模型。源码包包含仓内 SDK wheel，不包含运行配置、
  `.venv`、`node_modules` 或本地验证目录。

CI 已配置独立 npm 安装／类型检查／测试／构建及 Python、wheel、Windows Worker 检查。
以上为本机实测；尚未把工作区推送至远端执行 CI。

## 冻结依赖与构建产物

Design 包只读取提交 `0f66aef9efd254752c634ba1e24d299d0b038116`，重复导出得到相同
SHA-256 `4ea64290a45f7587b55f22455be06e65552f65a0cf0efad96705500b36fb8b0b`。
组件、Token、类型和样式校验器一同进入仓内依赖；上游该提交未声明许可证，此事实及
来源元数据随包保留，没有另行授予许可证。前端已适配这一已提交版本的 Token。

SDK `0.2.1` wheel 来自 `ec8decf` 加本轮公共接口修改，保留 MIT 许可证，SHA-256 为
`30d46254d3835bcc3d80aaea79877162e85c6e99e3bdf780bffe28877ca4a413`。
Engine 最低依赖与 uv 锁已更新；仓内 `vendor` 固定该 wheel，无私有接口回退。
该版本尚未发布到 PyPI，独立安装按分发文档使用 `--find-links vendor`。

Engine 构建产物位于 `dist/purrcept_engine-0.1.0-py3-none-any.whl` 和同版本源码包。
安装后的面板直接读取随 wheel 分发的资源，无须安装 Node 或获取 Design 源码。

## 合成性能测量

`scripts/benchmark_store.py` 仅创建临时库。基线使用 `24c9d8a` 的独立源码快照；两侧
使用相同 Python 3.12.11、依赖和 Windows 11 环境。每侧10,000条行动、4张1024×1024
PNG，重复5次，下表为毫秒中位数。

| Store 路径 | 基线 | 本轮实现 |
| --- | ---: | ---: |
| 打开数据库 | 17.63 | 4.86 |
| 面板查询 | 99.83 | 1.10 |
| 行动历史 | 2.62 | 1.05 |
| 完整会话恢复 | 95.90 | 94.12 |

行动历史计划从全表扫描加临时排序变为覆盖索引 `actions_recent`。面板不加载检查点
或媒体正文。完整会话恢复仍承担实际恢复工作，不以面板优化代替恢复校验。
这是 Store 局部测量，不代表真实宿主启动比例、设备连接耗时或模型效果。新增元数据与
索引使本组合成库文件略大；在线淘汰释放可复用页，不承诺文件自动缩小。

## 效果与真实数据边界

固定评审样本包括正常行为、差评、重复表达、边界冲突和证据不足；
`scripts/evaluate_reviews.py` 提供连续反思与局部评审输出的对比入口和指标校验。
本轮没有调用真实模型生成对比输出，缺少的质量标签、token 用量和后续观察保持缺失，
不宣称漏检率、误干预率、记忆泛化错误率或实际 token 成本已经改善。

建议接受与实际改善分开记录；改善必须来自后续带证据的行为观察。真实运行库清理、
离线压缩、真实模型、设备连接和实际纠偏效果均未执行。所有迁移、清理、回滚和媒体
回收验证使用临时库及合成材料，后续可按同一契约另行验收。
