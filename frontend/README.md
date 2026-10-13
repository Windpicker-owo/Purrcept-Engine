# 本机面板源码

产品入口是 `src/main.tsx`，外壳是 `src/App.tsx`（导航轨 + 主舞台 + Inspector，窄屏
为 Sheet）。`contracts.ts` 在真实 HTTP 与浏览器恢复入口校验数据，`client.ts` 持有观察
与稳定请求身份；React 只呈现这些事实，不拥有设备或业务状态。快照断线后仍可阅读，
但控制关闭并注明过期。刷新只查询状态，不连接或启动任何设备。

## 结构

| 位置 | 职责 |
| --- | --- |
| `src/model/agent.ts` | 全站唯一的主体状态推导：首页吉祥物、顶栏与标签页标题共用 |
| `src/model/attention.ts` | 待处理事项（发生了什么 → 影响 → 下一步），首页、徽标与待处理页共用 |
| `src/model/devices.ts` | 设备展示注册表：人话摘要、行为说明、允许的快捷控制与完整技术事实 |
| `src/model/timeline.ts` | 行动与本页控制合并的时间线与过滤 |
| `src/model/routes.ts` | hash 深链；浏览器后退即返回上一层级 |
| `src/model/names.ts` / `controls.ts` | 能力与插件的用户命名；控制资格与同操作合并 |
| `src/semantics.ts` | 原始状态值到 Cel 状态与用户状态词的唯一映射 |
| `src/pages/*` | 首页、待处理、时间线、设备、活动、系统 |
| `src/ui/*` | 状态标记（每屏一处持续运动）、技术细节折叠、Inspector 详情 |

主视图只使用用户语言；实例 ID、代次、回执版本等实现事实原样保留在 Inspector 的
“技术细节”折叠区。导航图标来自 Design 的导航图标组，不借用状态图标。

## 构建与检查

开发期从兄弟 `purrcept_design/react/src` 引用真实 Cel 参考组件和 Token，使用该仓库
已经安装的冻结 Node 依赖。不会修改 Design 的 node_modules。重新构建要求两仓库相邻、
Design 的 `dist`/`react/src/generated` 已生成、Node 与 Design 依赖已经安装。

在本目录运行 `npm run typecheck`、`npm test`、`npm run build`。样式检查运行
`python ../../purrcept_design/scripts/lint_styles.py src index.html`。构建脚本是
`../scripts/build_web.mjs`，输出到 `src/purrcept_engine/app/static` 的 `index.html`、
`panel.js`、`panel.css`、`purrcept.css`。产物包含 React、Cel 和所有依赖，不使用 CDN、
网络字体或开发服务器；安装 Engine 后无需 Node 和 Design 目录。

浏览器验收在 Engine 根目录运行 `node scripts/check_web.mjs`（需要 Node 24 和系统
Edge）。它只启动回环静态服务并提供内存合成数据，不加载 Engine 配置，不接入模型或设备，
任何控制写入与外网请求都使检查失败。覆盖首次连接等待、普通动效下快速切页的 28 帧、
六页的 320/390/768/1024/1440 宽度、时间线搜索与键盘筛选、部分失败详情、窄屏 Sheet、
断线禁用和当前行动路径；另行检查减少动效偏好。截图保存在
`.validation/webui/`。截图中的设备、活动和行动均为测试样本。

## 设计参考与取舍

- [Linear：A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh)：
  借鉴退后的导航、稳定的顶栏位置、依靠留白建立层级。落实为墨色猫头导航轨、位置栏、
  平面章节与对齐的时间线；本轮改为墨色导航轨与反白猫头，颜色与尺寸仍完全来自 Cel。
- [LangGraph Studio](https://www.langchain.com/blog/langgraph-studio-the-first-agent-ide)：
  借鉴运行过程与所选对象详情联动。落实为可筛选的记录、固定 Inspector 与手机 Sheet，
  不引入编辑执行状态或重放行动的功能。

首页保持吉祥物为唯一视觉中心及持续运动源；白色主画布上，状态舞台与行动路径组成完整
首屏区域。下方左侧按连续列表组织近期记录与持续目标，右侧以明确分界组织待处理与设备，
窄屏沿阅读顺序堆叠。普通设置组不增加卡片，独立设备与活动保留对象表面。时间线搜索只匹配当前已取得记录的
内容标题与结果文案，清除筛选不会改变已选择的回执或发起业务控制。

同级页面与桌面 Inspector 内容直接替换，不保留退场页或使用透明入场；导航反白文字与
背景同帧切换，避免颜色过渡造成短暂不可读。窄屏 Sheet 使用 Design 的 snap 弹簧保留
进出层级方向。首次连接等待不展示断线警告，取得失败结果后才显示恢复入口。

## 控制与结果

控制接管与真实结果分开。HTTP 202/控制 finished 仅说明取得业务返回；逐调用
confirmed/failed/unknown/not_started、活动 command 的 status/actual_state/applied
决定结果文案。网络未确认可以查询或以原 ID 原参数重传；业务失败不提供自动重做。
刷新保存最近 16 条页面控制身份到该标签页 sessionStorage，令牌只保存在内存中；
宿主实例变化后保留旧身份供核对，不向新宿主重传。保存失败会明确提示，当前页面
内的请求去重仍然有效。宿主侧控制历史接入前，“我的操作”只属于当前标签页。

收音卡片显示实际选择的输入设备，以及宿主从权威账本补充的最近发言处理状态；
开启收音、收到输入、处理成功和语音播放分别呈现。语音输出只有 `started` 为真时
才显示播放器开始，合成中的活动对象不冒充已经发声。原声模式同时受模型能力和
媒体计量准入约束；CLI 已提供 LiteLLM token 规则与 Gemini 3 内联媒体规则，
配置后可选择原声输入。计量失败仍显示真实失败阶段，不冒充已回应或已播放。

设备控制还受产品范围约束：QQ 只恢复连接，收音只枚举/开始/停止，音频只停止输出，
电脑只连接/断开/开始或暂停观察，Live2D 只读。收音设备由明确枚举操作填充缓存，
每次快照只读取缓存；未提供设备、模式或对应 readiness 时不会启用开始。停止与
活动结束不受其它在途操作锁住，相同操作才合并等待。

Design 的 PartialFailure 预设“从失败步骤重试”，当前 Engine 没有这样的业务端口。
本页使用同一 Feedback、StatusMark 与 EFFECT_OUTCOMES 语义按“已经发生 / 结果未知 /
失败 / 进行中 / 没有发生”呈现真实调用回执，不给逐调用 confirmed 额外声称独立效果
观察，也不提供会重复副作用的假重试。
