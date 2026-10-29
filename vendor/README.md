# 适配库冻结构建产物

`purrcept_litellm-0.2.1-py3-none-any.whl` 是本轮公共纯编译接口的发行构建，
来自 Purrcept LiteLLM 的 `ec8decf` 加本轮 0.2.1 源码修改，尚未发布到 PyPI。
源码许可证为 MIT，wheel 保留原许可证。Engine 使用普通版本依赖，uv 开发环境
通过仓内 source 固定该产物；离线或独立安装使用 `--find-links vendor`。
不包含 editable 或兄弟目录路径，也没有私有接口回退。

SHA-256：`30d46254d3835bcc3d80aaea79877162e85c6e99e3bdf780bffe28877ca4a413`。
