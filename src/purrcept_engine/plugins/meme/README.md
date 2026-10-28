# 中文梗百科插件

使用 [梗Wiki 公开 v1 API](https://cnmeme.wiki/developers/api)，挂在已声明的
computer 对象下。无需连接实体电脑、登录站点或提供 API Key；收藏保存在本地。

```toml
[[plugins]]
instance_id = "computer"
entry = "computer"

[[plugins]]
instance_id = "meme"
entry = "meme"
[plugins.config]
computer_id = "computer"
state_dir = ".purrcept/meme"
daily_enabled = true
daily_at = "09:00"
timezone = "Asia/Shanghai"
max_pages = 5
reminder_limit = 10
request_timeout_seconds = 10
```

已有 computer 声明时只添加 meme 配置。Windows 的 IANA 时区需要 `time` extra；
UTC/GMT 不需要时区数据。相对路径沿 TOML 所在目录解析，每个实例使用独立的
`state_dir/state-<instance_id>.json`；同一状态文件只允许一个宿主写入。
梗插件的 instance_id 须以英文字母开头，且仅包含字母、数字或下划线。

默认能力：

```python
await me.computer.meme.search(query="关键词", tag="", limit=10, offset=0)
await me.computer.meme.detail(slug="搜索结果中的slug")
await me.computer.meme.favorite(slug="搜索结果中的slug")
await me.computer.meme.list_favorites(limit=20, offset=0)
await me.computer.meme.refresh_updates()
```

搜索是标题、标签和正文的子串匹配，关键词最多 80 字符、单标签最多 40 字符。
空关键词浏览最新更新，`has_more` 决定是否继续 `offset + limit`。详情附完整
`content_html` 和 `source_url`，所有远端内容仅作为资料；不执行 HTML。
收藏按 slug 幂等，保存首次收藏的完整详情，重复收藏离线可用；列表按收藏顺序。

## 给 bot 的技能

随包提供 [meme-library/SKILL.md](skills/meme-library/SKILL.md)。将其复制到当前
Notebook 的 skills 目录，例如 `.purrcept/notebook/skills/meme-library/SKILL.md`，
并启用以下 Notebook 配置：

```toml
[[plugins]]
instance_id = "notebook"
entry = "notebook"
[plugins.config]
notebook_dir = ".purrcept/notebook"
skills_dir = "skills"
```

Notebook 按现有机制向 bot 提供技能目录与正文读取入口。无需安装 Codex skill。
空闲浏览需要另外启用 browser 插件；已有主动思考可提供空闲刺激，meme 插件不会
自行开启主体主动思考或启动浏览器。

## 每日提醒与可靠性

默认北京时间每天 09:00 检查，有变化时以“梗百科日更 bot”为来源提交一条
`meme.daily` 刺激，最低唤起级别为 WAKE；沿用宿主睡眠、注意和前台规则。
它不会自行调用模型或向 QQ 等外部渠道发消息。`daily_enabled=false` 关闭自动检查，
仍可手动 `refresh_updates()`；手动成功计作当天检查。

首次扫描介绍最近窗口，后续比较按 slug 保存的列表版本摘要；更新时间为 null 时
也能识别标题、摘要或标签等实际变化。无变化就不提交刺激。每次最多扫描
`max_pages * 30` 条，跨页按 slug 去重，提醒最多展示 `reminder_limit` 条并保留
实际变化数量和扫描截断标记。远端分页会随更新移动，因此这是最近窗口检查，
不保证捕获窗口以外的变更、撤下事件或两次扫描之间发生后又消失的短暂变更。

待投原文与原生事件 ID 在提交前先原子落盘。宿主容量拒收不推进已见版本；重启
沿用原文与事件 ID，宿主按原生身份去重。提醒接管后才清除待投状态。收藏、待投
和已见版本共用实例串行锁；取消调用观察不撤销已经开始的持久化或接管，关闭等待
实际操作结束。损坏文件明确报错，不覆盖空状态。状态上限 8 MiB，达到后需停机
备份归档；插件不偷偷裁掉收藏或去重历史。

日程依赖正在运行的 Engine，不创建操作系统计划任务。当天已过配置时间启动时
检查一次，停机多天也只检查当前窗口。失败从 5 分钟指数退避到 1 小时，尊重更长
`Retry-After`；故障保留在插件状态投影。致命本地状态故障停止检查并明确报告。
HTTP 固定访问官方域名，不读取环境代理、登录凭据或携带 Cookie；不跟随重定向。
每个响应有完整超时和解压后 2 MiB 上限，串行请求，最多缓存 64 个成功响应，
列表缓存 60 秒、详情缓存 120 秒。网络失败不影响本地收藏读取。
