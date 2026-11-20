# B 站只读插件

插件提供 `me.phone.bilibili.*`。Engine 在手机应用目录中装配它，连接实体手机是独立能力。内置 `web` 适配器读取 B 站网页接口，`manifest` 适配器读取本地快照。

## 启用

把下面的插件配置追加到 `purrcept.toml`，并在现有前台模型配置中设置 `supports_contents = ["text", "image"]`。确认模型端点能接收工具回执中的图片。

```toml
[[plugins]]
instance_id = "bilibili"
entry = "local/bilibili:factory"

[plugins.config]
adapter = "web"

[plugins.config.adapter_config]
cookie = "${PURRCEPT_BILIBILI_COOKIE:-}"
```

插件位于配置目录的 `plugins/bilibili/`；运行 `uv run purrcept-engine catalog` 核对能力路径。

## 实时网页数据

`cookie` 可选。通过环境变量注入操作者有权使用的会话 Cookie。适配器只向 B 站 API 发送该凭据，字幕与视频 CDN 请求独立发送。可选 `proxy` 指定已有的 HTTP(S) 网络代理。HTTP 412/429 会返回站点拒绝状态，适配器不会重试。

评论排序使用 B 站返回的热门或最新评论顺序。楼中楼使用站点返回的线程顺序。字幕使用对应 CID 的网页字幕轨，优先中文。视频读取优先选取完整的低码率 AVC 轨，单次读取最多 64 MiB。播放器主地址不在 HTTPS CDN 白名单内时，优先选用同一轨道的合规备用地址，再考虑同一分 P 的其他完整视频轨。

在 `[plugins.config.adapter_config]` 设置 `asr_model` 可为缺少可见字幕的分 P 启用本机转写。路径相对主配置目录，也接受绝对路径。需要系统的 `ffmpeg` 命令包含 `whisper` 滤镜，以及本机已有的 [Whisper.cpp 模型](https://huggingface.co/ggerganov/whisper.cpp)。转写只覆盖请求的时间段，返回原分 P 时间轴，并标记 `source = "asr"`。缺少字幕或转写时仍可取得同窗口分镜。

## 本地快照

使用本地数据时，把 `[plugins.config]` 的 `adapter` 改成 `"manifest"`，把 `[plugins.config.adapter_config]` 替换为 `path = "bilibili/manifest.json"` 所在的表。清单路径相对主配置目录，媒体路径相对清单目录。宿主启动时载入快照，更新后重启。

下面的清单演示两分 P、带时间戳的转写、首页推荐和楼中楼。把示例中的 BV 号、CID、标题、时间和文件名换成自己的材料。放入对应媒体后，就能用 `watch` 或 `inspect` 看它的真实画面。

```json
{
  "videos": [
    {
      "bvid": "BV1xx411c7m9",
      "title": "相机操作示例",
      "uploader": "演示账号",
      "parts": [
        {"page": 1, "cid": 11, "title": "原理", "duration": 30},
        {"page": 2, "cid": 22, "title": "操作", "duration": 60}
      ]
    }
  ],
  "recommend": ["BV1xx411c7m9"],
  "comments": {
    "BV1xx411c7m9": [
      {"rpid": "1", "author": "观众甲", "text": "第 12 秒开始演示。", "likes": 8},
      {"rpid": "2", "author": "观众乙", "text": "第 20 秒能看到结果。", "root_rpid": "1"}
    ]
  },
  "materials": {
    "BV1xx411c7m9": {
      "11": {"file": "part-1.mp4"},
      "22": {
        "file": "part-2.mp4",
        "transcript": {
          "cid": 22,
          "source": "asr",
          "lines": [{"start": 12.3, "end": 15.4, "text": "现在按下红色按钮。"}]
        }
      }
    }
  }
}
```

- `recommend` 和 `hot` 各接收一组按原顺序排列的 BV 号。`related` 按原稿件 BV 号列出相关推荐。给空数组表示已采集且为空，省略对应项表示数据源无此项。
- `comments` 按 BV 号记录。空数组表示已采集且为空。评论 ID 用字符串，楼中楼的 `root_rpid` 指向同稿件中的顶层评论。可附 `published_at`（Unix 秒）、`pinned` 和 `reply_count`。本地清单缺少 `reply_count` 时按已收录的回复数填充。
- `materials` 按 BV 号和 CID 对应分 P。字幕轨标记 `source = "subtitle"`，已生成的语音转写标记 `source = "asr"`。内置适配器读取现成的字幕或转写。需要自动转写时，在自定义适配器中实现。

## 工具行为

| 能力 | 调用示例与结果 |
| --- | --- |
| `feed` | `await me.phone.bilibili.feed(kind="recommend", limit=5)`。也支持 `hot` 和 `related`，后者需要 `video`。 |
| `search` | `await me.phone.bilibili.search(query="相机")`。本地快照对标题、UP 主、简介做关键词匹配。 |
| `watch` | `await me.phone.bilibili.watch(video="BV1xx411c7m9", page=2, start=12, end=28)`。按分 P 的秒数定位。省略时间时默认从起点看最多 120 秒，并返回 `window.next_start` 供续看。 |
| `inspect` | `await me.phone.bilibili.inspect(video="BV1xx411c7m9", page=2, start=12, end=16)`。最多 30 秒，分镜单格更清晰。 |
| `comments` | `await me.phone.bilibili.comments(video="BV1xx411c7m9", order="new", root_rpid="1")`。省略 `root_rpid` 获取顶层评论，默认按热度排序。 |

收到 `next_cursor` 后，保持筛选、排序和页大小，传给下一次调用的 `cursor`。观影接受 BV 号、完整 B 站视频链接及含该链接的分享文本。显式 `page` 和 `start` 覆盖链接里的 `p` 和 `t`。

观影按真实显示时间在 `[start, end)` 窗口内取最多六帧，字幕保留与窗口重叠的台词和原始时间。台词与 JPEG 分镜一起交给模型。某路素材缺失或台词达到单次长度上限时，结果会标明相应状态。评论独立读取，标题、台词、图片和评论都作为外部素材进入上下文。

## 替换数据源

将 `adapter` 设置为安装在 Engine 环境中的 `your_package:build_source`，在 `[plugins.config.adapter_config]` 传入该适配器的配置。异步工厂签名为 `build_source(base_dir: Path, config: JsonObject) -> BilibiliSource`。契约及返回类型见 [source.py](../plugins/bilibili/source.py)。

适配器负责数据访问、认证、分页、网络超时、字幕提取或转写。每次 `open_video` 交出一个独立、可 seek 且从分 P 起点开始的二进制流，插件解码后关闭。适配器用 `SourceError` 报告已知失败，缺失的字幕或视频流返回 `None`。数据源自身的连接由 Engine 在关闭插件时统一收回。
