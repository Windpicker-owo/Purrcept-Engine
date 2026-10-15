"""VTS 真正请求编排、失败分类与令牌线程所有权的离线协议样例。

替身保留 requestID、响应类型和回执先后，不启动 VTS 或真实设备。
线程样例使用独立临时令牌，验证取消后 close 仍等实际文件 IO 归还。
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from pathlib import Path

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConcurrencyError,
    ConnectionClosedError,
    InvalidHandshake,
    InvalidState,
    InvalidStatus,
)
from websockets.frames import Close
from websockets.http11 import Response

from purrcept_engine.plugins.live2d.models import (
    BodyError,
    BodyUnavailable,
    ModelProfile,
    ParameterBinding,
)
from purrcept_engine.plugins.live2d.vts import VTSDriver
from purrcept_engine.values import JsonObject, decode_object, encode_json


class Socket:
    """按真实请求身份应答，显式注入断线、拒绝或无响应。"""

    def __init__(self) -> None:
        self.requests: list[JsonObject] = []
        self.messages: list[str] = []
        self.overrides: dict[str, JsonObject] = {}
        self.failures: dict[str, Exception] = {}
        self.raw_response: str | None = None
        self.blocked_kind: str | None = None
        self.request_seen = asyncio.Event()
        self.closed = 0
        self.close_failure: OSError | None = None

    async def send(self, raw: str) -> None:
        self.requests.append(decode_object(raw.encode()))
        self.request_seen.set()

    async def recv(self) -> str:
        await asyncio.sleep(0)
        request = self.requests[-1]
        kind = request["messageType"]
        assert isinstance(kind, str)
        if error := self.failures.get(kind):
            raise error
        if self.blocked_kind == kind:
            await asyncio.Event().wait()
        if self.raw_response is not None:
            return self.raw_response
        if not self.messages:
            payloads: dict[str, JsonObject] = {
                "AuthenticationTokenRequest": {"authenticationToken": "granted-token"},
                "AuthenticationRequest": {"authenticated": True},
                "CurrentModelRequest": {"modelLoaded": True, "modelID": "model"},
                "InputParameterListRequest": {"customParameters": []},
                "ParameterCreationRequest": {},
                "InjectParameterDataRequest": {},
            }
            response: JsonObject = {
                "requestID": request["requestID"],
                "messageType": kind.removesuffix("Request") + "Response",
                "data": payloads[kind],
            }
            response.update(self.overrides.get(kind, {}))
            # 每个请求先遇到一个无关通知，不能把通知当作成功回执。
            self.messages.extend(
                (
                    encode_json({"requestID": "notification", "messageType": "Event"}).decode(),
                    encode_json(response).decode(),
                )
            )
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed += 1
        if self.close_failure is not None:
            raise self.close_failure


class InspectableDriver(VTSDriver):
    """只为失败收尾样例观察驱动仍持有的真实连接。"""

    @property
    def socket_retained(self) -> bool:
        return self._socket is not None


@pytest.fixture
def profile() -> ModelProfile:
    return ModelProfile(
        model_id="model",
        bindings=(ParameterBinding(parameter="PurrceptSmile", sources={"valence": 1.0}),),
    )


def driver_with_socket(
    monkeypatch: pytest.MonkeyPatch, token: Path, profile: ModelProfile, socket: Socket
) -> tuple[InspectableDriver, dict[str, object]]:
    options: dict[str, object] = {}

    async def connect(url: str, **kwargs: object) -> Socket:
        assert url == "ws://127.0.0.1:8001"
        options.update(kwargs)
        return socket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    return InspectableDriver("ws://127.0.0.1:8001", token, profile), options


async def test_native_authorization_calibration_and_frame_have_real_matching_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    token, socket = tmp_path / "token", Socket()
    driver, options = driver_with_socket(monkeypatch, token, profile, socket)
    await driver.connect()
    await driver.send({"valence": 0.5})
    assert token.read_text() == "granted-token"
    assert options == {"open_timeout": 3, "close_timeout": 1, "max_size": 1_048_576, "proxy": None}
    assert [request["messageType"] for request in socket.requests] == [
        "AuthenticationTokenRequest",
        "AuthenticationRequest",
        "CurrentModelRequest",
        "InputParameterListRequest",
        "ParameterCreationRequest",
        "CurrentModelRequest",
        "InjectParameterDataRequest",
    ]
    identities: list[str] = []
    for request in socket.requests:
        identity = request["requestID"]
        assert isinstance(identity, str)
        identities.append(identity)
    assert len(set(identities)) == len(socket.requests)
    assert socket.requests[-1]["data"] == {
        "faceFound": True,
        "mode": "set",
        "parameterValues": [{"id": "PurrceptSmile", "value": 0.5}],
    }
    await driver.close()
    assert socket.closed == 1


async def test_saved_token_and_existing_parameters_do_not_repeat_authorization_or_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text(" saved-token ", encoding="utf-8")
    socket.overrides["InputParameterListRequest"] = {
        "data": {"customParameters": [{"name": "PurrceptSmile"}]}
    }
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    await driver.connect()
    assert [request["messageType"] for request in socket.requests] == [
        "AuthenticationRequest",
        "CurrentModelRequest",
        "InputParameterListRequest",
    ]
    assert socket.requests[0]["data"] == {
        "pluginName": "Purrcept Live2D",
        "pluginDeveloper": "Purrcept",
        "authenticationToken": "saved-token",
    }
    await driver.close()


@pytest.mark.parametrize("error", [ConnectionRefusedError(), TimeoutError()])
async def test_only_temporary_open_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, error: Exception
) -> None:
    async def connect(url: str, **kwargs: object) -> Socket:
        raise error

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    driver = VTSDriver("ws://127.0.0.1:8001", tmp_path / "token", profile)
    with pytest.raises(BodyUnavailable) as caught:
        await driver.connect()
    assert caught.value.__cause__ is error
    await driver.close()


@pytest.mark.parametrize(
    "error",
    [
        InvalidHandshake("bad handshake"),
        InvalidStatus(Response(403, "Denied", Headers())),
        ssl.SSLCertVerificationError("untrusted certificate"),
    ],
)
async def test_handshake_rejection_terminates_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, error: Exception
) -> None:
    async def connect(url: str, **kwargs: object) -> Socket:
        raise error

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    driver = VTSDriver("ws://127.0.0.1:8001", tmp_path / "token", profile)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    assert caught.value.__cause__ is error
    await driver.close()


@pytest.mark.parametrize(
    "kind,override",
    [
        ("AuthenticationRequest", {"data": {"authenticated": False}}),
        ("CurrentModelRequest", {"data": {"modelLoaded": True, "modelID": "other"}}),
        ("InputParameterListRequest", {"data": {"customParameters": [7]}}),
        ("AuthenticationRequest", {"messageType": "APIError", "data": {}}),
        ("AuthenticationRequest", {"messageType": "WrongResponse", "data": {}}),
        ("AuthenticationRequest", {"data": []}),
    ],
)
async def test_authentication_model_and_protocol_rejections_are_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: ModelProfile,
    kind: str,
    override: JsonObject,
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    socket.overrides[kind] = override
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    await driver.close()


async def test_invalid_json_is_terminal_even_before_response_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    socket.raw_response = "{broken"
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    await driver.close()


@pytest.mark.parametrize(
    "code,retryable", [(1008, False), (1002, False), (1001, True), (1006, True)]
)
async def test_websocket_close_preserves_protocol_refusal_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: ModelProfile,
    code: int,
    retryable: bool,
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    await driver.connect()
    socket.failures["CurrentModelRequest"] = ConnectionClosedError(Close(code, "closed"), None)
    with pytest.raises(BodyError) as caught:
        await driver.send({"valence": 0.0})
    assert isinstance(caught.value, BodyUnavailable) is retryable
    assert socket.requests[-1]["messageType"] == "CurrentModelRequest"
    await driver.close()


@pytest.mark.parametrize("error", [ConcurrencyError("parallel recv"), InvalidState("wrong state")])
async def test_program_errors_are_not_translated_to_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, error: Exception
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    socket.failures["AuthenticationRequest"] = error
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    with pytest.raises(type(error)) as caught:
        await driver.connect()
    assert caught.value is error
    await driver.close()


async def test_native_authorization_uses_120_seconds_and_timeout_does_not_repeat_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    socket = Socket()
    socket.blocked_kind = "AuthenticationTokenRequest"
    driver, _ = driver_with_socket(monkeypatch, tmp_path / "token", profile, socket)
    deadlines: list[float | None] = []
    original_timeout = asyncio.timeout

    def timeout(delay: float | None) -> asyncio.Timeout:
        deadlines.append(delay)
        return original_timeout(0.01 if delay == 120 else delay)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    assert isinstance(caught.value.__cause__, BodyUnavailable)
    assert deadlines == [120]
    assert len(socket.requests) == 1
    await driver.close()


async def test_unrelated_notifications_still_expire_at_request_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    await driver.connect()
    socket.raw_response = encode_json(
        {"requestID": "notification", "messageType": "Event"}
    ).decode()
    deadlines: list[float | None] = []
    original_timeout = asyncio.timeout

    def timeout(delay: float | None) -> asyncio.Timeout:
        deadlines.append(delay)
        return original_timeout(0.01)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    with pytest.raises(BodyUnavailable):
        await driver.send({"valence": 0.0})
    assert deadlines == [2.0]
    await driver.close()


@pytest.mark.parametrize("content", [b"\xff", b" \n "])
async def test_invalid_local_token_is_terminal_and_never_requests_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, content: bytes
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_bytes(content)
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    assert socket.requests == []
    await driver.close()


@pytest.mark.parametrize("operation", ["read", "save"])
async def test_token_file_errors_terminate_recovery_and_keep_socket_for_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, operation: str
) -> None:
    token, socket = tmp_path / "token", Socket()
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    if operation == "read":
        token.mkdir()
    else:

        def save(value: str) -> None:
            raise PermissionError("token path denied")

        monkeypatch.setattr(driver, "_save_token", save)
    with pytest.raises(BodyError) as caught:
        await driver.connect()
    assert not isinstance(caught.value, BodyUnavailable)
    assert isinstance(caught.value.__cause__, OSError)
    assert driver.socket_retained is True
    count = sum(
        request["messageType"] == "AuthenticationTokenRequest" for request in socket.requests
    )
    assert count == (0 if operation == "read" else 1)
    await driver.close()
    assert socket.closed == 1


@pytest.mark.parametrize("operation", ["read", "save"])
async def test_cancelled_token_io_failure_is_reported_after_socket_reclamation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, operation: str
) -> None:
    token, socket = tmp_path / "token", Socket()
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    entered, release = threading.Event(), threading.Event()
    failure = PermissionError("token IO failed")
    if operation == "read":
        original_read = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == token:
                entered.set()
                release.wait()
                raise failure
            return original_read(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", read)
    else:

        def save(value: str) -> None:
            entered.set()
            release.wait()
            raise failure

        monkeypatch.setattr(driver, "_save_token", save)
    connecting = asyncio.create_task(driver.connect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
    with pytest.raises(ExceptionGroup) as caught:
        await driver.close()
    assert len(caught.value.exceptions) == 1
    error = caught.value.exceptions[0]
    assert isinstance(error, BodyError)
    assert error.__cause__ is failure
    assert socket.closed == 1
    assert driver.socket_retained is False
    assert driver.resources_released is True


@pytest.mark.parametrize("operation", ["read", "save"])
async def test_cancelled_token_io_is_owned_until_close_really_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile, operation: str
) -> None:
    token, socket = tmp_path / "token", Socket()
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    entered, release = threading.Event(), threading.Event()
    if operation == "read":
        token.write_text("saved-token", encoding="utf-8")
        original_read = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == token:
                entered.set()
                release.wait()
            return original_read(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", read)
    else:

        def save(value: str) -> None:
            entered.set()
            release.wait()
            token.write_text(value, encoding="utf-8")

        monkeypatch.setattr(driver, "_save_token", save)

    connecting = asyncio.create_task(driver.connect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        closing = asyncio.create_task(driver.close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done()
        assert socket.closed == 0
        assert driver.resources_released is False
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        release.set()
        await driver.close()
        assert socket.closed == 1
        assert driver.resources_released is True
        assert token.read_text() == ("saved-token" if operation == "read" else "granted-token")
    finally:
        release.set()
        if not connecting.done():
            connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        await driver.close()


async def test_failed_socket_close_keeps_resource_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: ModelProfile
) -> None:
    token, socket = tmp_path / "token", Socket()
    token.write_text("saved-token", encoding="utf-8")
    driver, _ = driver_with_socket(monkeypatch, token, profile, socket)
    await driver.connect()
    socket.close_failure = OSError("socket close failed")
    with pytest.raises(ExceptionGroup) as caught:
        await driver.close()
    assert caught.value.exceptions == (socket.close_failure,)
    assert driver.socket_retained is True
    assert driver.resources_released is False
