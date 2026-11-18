# 适配库冻结构建产物

`purrcept_litellm-0.2.3-py3-none-any.whl` 是本轮 LiteLLM 升级及缓存策略修复的发行构建，
来自 Purrcept LiteLLM 的 `b5d9ef20689cf7365c9de49ef221d3ce185f86be` 加本轮 0.2.3 源码修改，尚未发布到 PyPI。
源码许可证为 MIT，wheel 保留原许可证。Engine 使用普通版本依赖，uv 开发环境
通过仓内 source 固定该产物；离线或独立安装使用 `--find-links vendor`。
不包含 editable 或兄弟目录路径，也没有私有接口回退。

SHA-256：`8f9fc8ef702d389b8afa6a4a752993cf8e5ac2c62d82ffb5fa76ac80858a7617`。
