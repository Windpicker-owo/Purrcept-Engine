"""共享 B 站稿件身份和视频网页主机，供观影与 QQ 分享分别校验输入。"""

BVID_PATTERN = r"BV[0-9A-Za-z]{10}"
VIDEO_HOSTS = frozenset({"bilibili.com", "www.bilibili.com", "m.bilibili.com"})
