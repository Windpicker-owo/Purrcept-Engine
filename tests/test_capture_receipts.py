"""实际 Host/Provider/Store 验证显式媒体捕获与原调用的迟到确认。

截图替身返回固定 PNG，只有实际工具取得的 BLOB 可进模型；媒体引用单独
随真实 call 版本保存，不解析 Worker 或任意 JSON 声称的附件。
"""

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import ImageBlock, ImageBytes, Message, ModelResponse, ToolResultBlock
from pydantic import BaseModel, ConfigDict
from test_host_subject import Backend, proposal, wait_processed

from purrcept_engine.app.config import EngineConfig, PluginSettings
from purrcept_engine.app.host import EngineHost
from purrcept_engine.app.receipts import LateCallResults
from purrcept_engine.domain.context.capacity import CapacityDeclaration, CapacityEstimate
from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.domain.inputs.models import (
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aT1sAAAAASUVORK5CYII="
)


class Parameters(BaseModel):
    """无模型参数的测试能力，媒体只能由宿主 Provider 真实取得。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


@dataclass
class Capture:
    """可信插件保存自己借用的端口和真实调用身份，不提供模型输入反射。"""

    ports: PluginPorts | None = None
    context: CallContext | None = None

    def factory(self) -> PluginFactory:
        """返回纯工厂，真实调用才捕获本地固定媒体。"""
        declaration = CapabilityDeclaration(
            "capture.image",
            "capture",
            "image",
            "取得测试原生截图",
            Parameters,
            5,
            "await me.capture.image()",
        )

        def prepare(identity: str, raw: object, base: Path) -> PluginBlueprint:
            async def build(ports: PluginPorts) -> PluginRuntime:
                self.ports = ports

                async def image(parameters: Parameters, context: CallContext) -> CallOutcome:
                    self.context = context
                    evidence = ports.evidence
                    assert evidence is not None
                    reference = MediaReference(
                        hashlib.sha256(PNG).hexdigest(), "image/png", len(PNG), "screen.png"
                    )
                    snapshot = await evidence.capture(
                        EvidenceSnapshot(
                            evidence_id="capture:" + context.call_id,
                            version=1,
                            kind="screenshot",
                            text="取得的原生截图",
                            source_json=json.dumps({"call_id": context.call_id}),
                            occurred_at=datetime.now(UTC).timestamp(),
                            media=(reference,),
                        ),
                        (MediaAsset(PNG, "image/png", "screen.png"),),
                    )
                    return CallOutcome("confirmed", {"captured": True}, media=snapshot.media)

                return PluginRuntime((FunctionCapability(declaration, image),))

            return PluginBlueprint(
                (ObjectDeclaration("capture", "capture", "截图替身", "subject"),),
                (declaration,),
                build,
            )

        return PluginFactory(prepare)


def native_reference(
    payload: Mapping[str, object], declaration: CapacityDeclaration
) -> CapacityEstimate:
    """隔离测试明确注册完整 JSON 计量，不宣称生产 Provider 的媒体公式。"""
    return CapacityEstimate(
        len(json.dumps(payload).encode()), "test-native-v1", "complete_test_payload", "test_units"
    )


@pytest.mark.asyncio
async def test_action_native_media_reaches_core_without_creating_a_second_input(
    tmp_path: Path,
) -> None:
    capture = Capture()
    backend = Backend(
        (
            proposal(
                "capture",
                "play_action",
                {"action_code": "from world import me\nreturn await me.capture.image()"},
            ),
            ModelResponse(Message.assistant("内部结束")),
        )
    )
    configuration = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path},
            "console": False,
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-native-v1",
                    "media_rule": "test-native-v1",
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
            "plugins": [PluginSettings(instance_id="capture", entry="capture")],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(
        configuration,
        plugin_factories={"capture": capture.factory()},
        generation_backends={"main": backend},
        capacity_rules={"test-native-v1": native_reference},
    )
    await host.start()
    try:
        now = datetime.now(UTC)
        admission = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "input"), "看截图", now, now)
        )
        await wait_processed(host, admission.input_id)
        assert len(backend.requests) == 2
        media = [
            item
            for message in backend.requests[1].messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
            for item in block.content
            if isinstance(item, ImageBlock)
        ]
        assert len(media) == 1 and isinstance(media[0].source, ImageBytes)
        assert media[0].source.data == PNG
        assert capture.context is not None
        action = await host.query_action(capture.context.action_id)
        assert action is not None and action.calls[0].media[0].byte_length == len(PNG)
        assert action.calls[0].history[0].media == action.calls[0].media
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_late_protocol_confirmation_keeps_action_terminal_and_is_plugin_scoped(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "late.sqlite3")
    try:
        await store.begin_action(
            "action", catalog_version="catalog", source="source", input_id="source-id"
        )
        await store.begin_call(
            "call",
            action_id="action",
            sequence=0,
            path="me.qq.send",
            target_id="qq",
            arguments_json="{}",
        )
        await store.mark_call_started("call")
        await store.record_call_result(
            "call", state="unknown", result_json=None, error_type="TimeoutError", error="未确认"
        )
        await store.finish_action(
            "action", state="timed_out", result_json=None, output="", error="期限"
        )
        context = CallContext("call", "action", 1, "source-id")
        wrong = LateCallResults(store, frozenset(("me.other.send",)))
        with pytest.raises(CapabilityInputError):
            await wrong.record(context, CallOutcome("confirmed", {"message_id": "actual"}))
        correct = LateCallResults(store, frozenset(("me.qq.send",)))
        late = await correct.record(context, CallOutcome("confirmed", {"message_id": "actual"}))
        duplicate = await correct.record(
            context, CallOutcome("confirmed", {"message_id": "actual"})
        )
        assert late.receipt_revision == duplicate.receipt_revision == 2
        assert tuple(item.state for item in late.history) == ("unknown", "confirmed")
        action = await store.get_action("action")
        assert action is not None and action.state == "timed_out" and action.error == "期限"
    finally:
        await store.close()
