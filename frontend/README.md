# 本机观察台源码

产品入口是 `src/main.tsx`。`contracts.ts` 在真实 HTTP 与浏览器恢复入口校验数据，
`client.ts` 持有观察与稳定请求身份；React 只呈现这些事实，不拥有设备或业务状态。
快照断线后仍可阅读，但控制关闭并注明过期。刷新只查询状态，不连接或启动任何设备。

开发期从兄弟 `purrcept_design/react/src` 引用真实 Cel 参考组件和 Token，使用该仓库
已经安装的冻结 Node 依赖。不会修改 Design 源码或 node_modules。重新构建要求两仓库
相邻、Design 的 `dist`/`react/src/generated` 已生成、Node 与 Design 依赖已经安装。

在本目录运行 `npm run typecheck`、`npm test`、`npm run build`。样式检查运行
`python ../../purrcept_design/scripts/lint_styles.py src index.html`。构建脚本是
`../scripts/build_web.mjs`，输出到 `src/purrcept_engine/app/static` 的 `index.html`、
`panel.js`、`panel.css`、`purrcept.css`。产物包含 React、Cel 和所有依赖，不使用 CDN、
网络字体或开发服务器；安装 Engine 后无需 Node 和 Design 目录。

控制接管与真实结果分开。HTTP 202/控制 finished 仅说明取得业务返回；逐调用
confirmed/failed/unknown/not_started、活动 command 的 status/actual_state/applied
决定结果文案。网络未确认可以查询或以原 ID 原参数重传；业务失败不提供自动重做。
刷新保存最近 16 条页面控制身份到该标签页 sessionStorage，令牌只保存在内存中；
宿主实例变化后保留旧身份供核对，不向新宿主重传。保存失败会明确提示，当前页面
内的请求去重仍然有效。

设备控制还受产品范围约束：QQ 只恢复连接，收音只枚举/开始/停止，音频只停止输出，
电脑只连接/断开/开始或暂停观察，Live2D 只读。收音设备由明确枚举操作填充缓存，
每次快照只读取缓存；未提供设备、模式或对应 readiness 时不会启用开始。停止与
活动结束不受其它在途操作锁住，相同操作才合并等待。

Design 的 PartialFailure 预设“从失败步骤重试”，当前 Engine 没有这样的业务端口。
本页使用同一 Feedback、StatusMark 与 EFFECT_OUTCOMES 语义呈现真实调用回执，
不给逐调用 confirmed 额外声称独立效果观察，也不提供会重复副作用的假重试。
