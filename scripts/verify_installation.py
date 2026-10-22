"""核对非 editable 安装的 SDK、包内资源、统一容量预检与真实 Worker。

由待验收环境的 Python 以 -I 执行；临时目录由本脚本创建并在真实关闭后删除。
合成图片和控制台显示替身不接触用户数据、设备或真实消息，模型后端禁止调用。
SDK 导入使用调用者明确选择的本地价格表模式，不请求在线元数据。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import sys
import tempfile
from importlib.resources import files
from pathlib import Path


def installed_packages() -> dict[str, str]:
    """核对冻结 SDK 的安装版本与实际导入位置。

    Returns:
        当前独立环境的版本记录，供最终机器验收结果关联。

    Raises:
        RuntimeError: 未明确启用 SDK 本地元数据模式。
        AssertionError: 包来自源码树，或安装来源和版本不满足要求。

    Note:
        版本号不能替代非 editable 的实际路径证据。
    """
    if os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True":
        raise RuntimeError("请显式设置 LITELLM_LOCAL_MODEL_COST_MAP=True，以离线导入 SDK。")
    import purrcept_core
    import purrcept_litellm

    import purrcept_engine

    expected = {
        "purrcept_core": "0.6.0",
        "purrcept_litellm": "0.2.0",
        "purrcept_engine": "0.1.0",
        "litellm": "1.105.0",
    }
    for module in (purrcept_core, purrcept_engine, purrcept_litellm):
        if module.__file__ is None or "site-packages" not in Path(module.__file__).parts:
            raise AssertionError(f"{module.__name__} 未从独立安装加载。")
        direct = importlib.metadata.distribution(module.__name__).read_text("direct_url.json")
        if direct is not None and json.loads(direct).get("dir_info", {}).get("editable", False):
            raise AssertionError(f"{module.__name__} 不能引用 editable 源码。")
    actual = {name: importlib.metadata.version(name) for name in expected}
    if actual != expected:
        raise AssertionError(f"安装版本不匹配：{actual}")
    return actual


async def verify_host(directory: Path) -> dict[str, object]:
    """用生产入口验证统一预检、临时宿主、真实 Worker 与同库媒体重开。

    Args:
        directory: 本次验收独占的临时目录，不得传入现有用户数据目录。

    Returns:
        统一容量估算、实际行动、完整媒体与目录锁归还的验收结果。

    Raises:
        AssertionError: 实际 Worker、媒体重开或资源归还不满足验收条件。

    Note:
        不替换执行器或存储，不调用真实模型、消息或设备。
    """
    from PIL import Image
    from purrcept_core.models import ModelEventSink, ModelRequest, ModelResponse

    from purrcept_engine import EngineConfig, EngineHost
    from purrcept_engine.domain.context.capacity import GenerationCapacity, check_capacity
    from purrcept_engine.infrastructure.audio.wav import encode_wav
    from purrcept_engine.infrastructure.sqlite.store import Store
    from purrcept_engine.serialization import encode

    class OfflineBackend:
        """仅禁止模型网络路径；本场景没有输入或到期反思，不需合成回复。"""

        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            """实际请求一旦误启动，验收立即失败，不能用正常回复掩盖。"""
            raise AssertionError("独立安装冒烟不允许发起模型调用。")

    with Image.new("RGB", (17, 9), (10, 20, 30)) as image, io.BytesIO() as stream:
        image.save(stream, format="PNG")
        pixels = stream.getvalue()
    source = directory / "synthetic.png"
    source.write_bytes(pixels)
    config = EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "console": True,
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/never-called",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
        },
        base_dir=directory,
    )
    # 包内预检必须读取实际图片尺寸和音频时长，不能把内联编码当正文。
    audio = encode_wav(bytes(32_000 * 60), 16_000)
    payload = {
        "model": "openai/never-called",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(pixels).decode()
                        },
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"format": "wav", "data": base64.b64encode(audio).decode()},
                    },
                ],
            }
        ],
    }
    measured = check_capacity(payload, GenerationCapacity(10_000, 100))
    assert measured.estimate.method == "multimodal-heuristic-v1"
    assert 1920 + 512 <= measured.estimate.input_units < 3000
    assert measured.payload_sha256 == hashlib.sha256(encode(payload).encode()).hexdigest()
    assert importlib.util.find_spec("purrcept_engine.infrastructure.models.capacity") is None

    shown: list[str] = []
    host = EngineHost(
        config, generation_backends={"main": OfflineBackend()}, console_output=shown.append
    )
    await host.start()
    try:
        assert host.query().phase == "running" and "me.view_image" in host.catalog.bindings
        assert (await host.query_panel())["authority_available"] is True
        action = await host.execute(
            'from world import me\nawait me.console.output(text="installed-worker")'
        )
        assert action.state == "completed" and len(action.calls) == 1
        assert action.calls[0].state == "confirmed" and shown == ["installed-worker"]
        image_action = await host.invoke("me.view_image", {"source": str(source)})
        assert image_action.state == "completed" and len(image_action.calls) == 1
        call = image_action.calls[0]
        assert call.state == "confirmed" and len(call.media) == 1
        reference = call.media[0]
        assert reference.media_id == hashlib.sha256(pixels).hexdigest()
        assert reference.byte_length == len(pixels) and reference.media_type == "image/png"
    finally:
        assert (await host.close()).phase == "closed"

    # 原宿主已结束唯一数据库线程，再打开同一临时库核对媒体关系与实际 BLOB。
    store = await Store.open(directory / "engine.sqlite3")
    try:
        saved = await store.get_action(image_action.action_id)
        assert saved is not None and saved.calls[0].media == call.media
        assert await store.get_media(reference.media_id) == pixels
    finally:
        await store.close()
    reopened = EngineHost(
        config, generation_backends={"main": OfflineBackend()}, console_output=shown.append
    )
    await reopened.start()
    try:
        assert await reopened.query_action(action.action_id) == action
    finally:
        assert (await reopened.close()).phase == "closed"
    return {
        "worker": "confirmed",
        "image": "pixels-and-reference-reopened",
        "capacity": measured.estimate.method,
        "models_called": 0,
    }


async def main() -> None:
    """使用独立环境与临时副本验收，不修改现有配置、数据或安装。"""
    if not sys.flags.isolated or sys.platform != "win32":
        raise RuntimeError("请使用待验收的 Windows Python 3.12，以 -I 运行本脚本。")
    versions = installed_packages()
    for folder in ("app/static", "app/studio/static"):
        assets = tuple(files("purrcept_engine").joinpath(folder).iterdir())
        assert len(assets) == 4 and all(asset.read_bytes() for asset in assets)
    with tempfile.TemporaryDirectory(prefix="purrcept-installed-") as temporary:
        async with asyncio.timeout(60.0):
            result = await verify_host(Path(temporary))
    print(
        json.dumps(
            {
                "versions": versions,
                "assets": 8,
                "web_installed": importlib.util.find_spec("starlette") is not None,
                **result,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
