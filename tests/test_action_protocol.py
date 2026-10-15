"""用真实管道验证 JSON 帧边界、部分 I/O 和并发发送完整性。"""

import os
import struct
import threading
from collections.abc import Generator
from contextlib import contextmanager
from math import inf, nan
from types import MappingProxyType

import pytest

from purrcept_engine.execution.protocol import FrameChannel, read_frame, write_frame
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object, encode_json


@contextmanager
def incoming(data: bytes) -> Generator[int, None, None]:
    """写入小型边界样本后关闭写端，避免畸形帧测试永久等待正文。"""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, data)
    finally:
        os.close(write_fd)
    try:
        yield read_fd
    finally:
        os.close(read_fd)


def test_json_preserves_pod_and_converts_legitimate_collections() -> None:
    shared = [True, None, -7, 1.25, "你好"]
    source = MappingProxyType({"shared": (shared, shared), "range": range(3)})
    encoded = encode_json(source)
    assert "你好".encode() in encoded
    assert b" " not in encoded
    assert decode_object(encoded) == {"shared": [shared, shared], "range": [0, 1, 2]}


def test_encoder_rejects_unknown_values_without_repr_or_guessing() -> None:
    class Unsupported:
        def __repr__(self) -> str:
            raise AssertionError("不得通过 repr 猜测传输值。")

    for malformed in (
        {"value": nan},
        {"value": inf},
        {"value": -inf},
        {"value": b"binary"},
        {True: "not a string key"},
        {"value": "\ud800"},
        {"\ud800": "value"},
        {"value": Unsupported()},
    ):
        with pytest.raises(ActionProtocolError):
            encode_json(malformed)
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ActionProtocolError, match="循环"):
        encode_json({"value": cyclic})


@pytest.mark.parametrize(
    "malformed",
    [
        b'{"a":1,"a":2}',
        b'{"nested":{"a":1,"\\u0061":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b'{"value":"\\ud800"}',
        b'{"\\ud800":"value"}',
        b'{"value":"\xf0\x9f"}',
        b'{"value":1',
        b"{} trailing",
        b"[]",
        b'"text"',
        b"null",
    ],
)
def test_decoder_rejects_ambiguous_or_incomplete_material(malformed: bytes) -> None:
    with pytest.raises(ActionProtocolError):
        decode_object(malformed)


def test_depth_limit_counts_containers_not_brackets_inside_strings() -> None:
    assert decode_object(b'{"value":"[[[[{\\"brackets\\":true}]]]]"}') == {
        "value": '[[[[{"brackets":true}]]]]'
    }
    allowed = b'{"value":' + b"[" * 63 + b"0" + b"]" * 63 + b"}"
    decode_object(allowed)
    too_deep = b'{"value":' + b"[" * 64 + b"0" + b"]" * 64 + b"}"
    with pytest.raises(ActionProtocolError) as depth:
        decode_object(too_deep)
    assert depth.value.code == "max_depth"
    value: object = 0
    for _ in range(64):
        value = [value]
    with pytest.raises(ActionProtocolError) as encoded_depth:
        encode_json({"value": value})
    assert encoded_depth.value.code == "max_depth"


def test_real_pipe_uses_network_length_and_decodes_frame() -> None:
    payload = {"kind": "result", "text": "你好"}
    body = encode_json(payload)
    with incoming(struct.pack("!I", len(body)) + body) as read_fd:
        assert read_frame(read_fd, max_bytes=len(body)) == payload
        with pytest.raises(EOFError):
            read_frame(read_fd, max_bytes=len(body))


def test_oversized_header_is_rejected_before_attempting_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    original_read = os.read

    def checked_read(fd: int, count: int) -> bytes:
        calls.append(count)
        if len(calls) > 1:
            raise AssertionError("超限帧不得尝试读取正文。")
        return original_read(fd, count)

    monkeypatch.setattr(os, "read", checked_read)
    with incoming(struct.pack("!I", (1 << 32) - 1)) as read_fd:
        with pytest.raises(ActionProtocolError) as oversized:
            read_frame(read_fd, max_bytes=1024)
    assert oversized.value.code == "frame_too_large"
    assert calls == [4]


def test_clean_eof_header_truncation_and_body_truncation_are_distinct() -> None:
    with incoming(b"") as read_fd, pytest.raises(EOFError):
        read_frame(read_fd, max_bytes=1024)
    with incoming(b"\x00\x00") as read_fd:
        with pytest.raises(ActionProtocolError) as header:
            read_frame(read_fd, max_bytes=1024)
    assert header.value.code == "truncated_header"
    with incoming(struct.pack("!I", 10) + b"{}") as read_fd:
        with pytest.raises(ActionProtocolError) as body:
            read_frame(read_fd, max_bytes=1024)
    assert body.value.code == "truncated_payload"


def test_partial_native_reads_and_writes_preserve_complete_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write
    original_read = os.read
    write_counts: list[int] = []
    read_counts: list[int] = []

    def partial_write(fd: int, data: bytes | memoryview) -> int:
        write_counts.append(len(data))
        return original_write(fd, data[:3])

    def partial_read(fd: int, count: int) -> bytes:
        read_counts.append(count)
        return original_read(fd, min(count, 2))

    monkeypatch.setattr(os, "write", partial_write)
    monkeypatch.setattr(os, "read", partial_read)
    read_fd, write_fd = os.pipe()
    try:
        write_frame(write_fd, {"text": "完整消息"}, max_bytes=128)
        assert read_frame(read_fd, max_bytes=128) == {"text": "完整消息"}
        assert len(write_counts) > 2 and len(read_counts) > 2
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_oversized_send_does_not_write_even_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_write(fd: int, data: bytes | memoryview) -> int:
        raise AssertionError("超限发送不得写入长度头。")

    monkeypatch.setattr(os, "write", unexpected_write)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ActionProtocolError) as oversized:
            write_frame(write_fd, {"text": "too big"}, max_bytes=2)
        assert oversized.value.code == "frame_too_large"
        with pytest.raises(ActionProtocolError):
            write_frame(write_fd, [1, 2], max_bytes=128)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_concurrent_channel_writers_do_not_interleave_partial_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write

    def one_byte_write(fd: int, data: bytes | memoryview) -> int:
        return original_write(fd, data[:1])

    monkeypatch.setattr(os, "write", one_byte_write)
    channel = FrameChannel(*os.pipe(), max_bytes=256)
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            for sequence in range(10):
                channel.send({"writer": index, "sequence": sequence, "text": "你好"})
        except (ActionProtocolError, threading.BrokenBarrierError) as error:
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    try:
        received = [channel.read() for _ in range(50)]
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert not errors
        assert {(item["writer"], item["sequence"]) for item in received} == {
            (writer_index, sequence) for writer_index in range(5) for sequence in range(10)
        }
        for writer_index in range(5):
            assert [
                item["sequence"] for item in received if item["writer"] == writer_index
            ] == list(range(10))
    finally:
        channel.close()
    channel.close()
    with pytest.raises(ActionProtocolError) as closed:
        channel.send({})
    assert closed.value.code == "channel_closed"


def test_channel_cannot_close_a_live_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    from purrcept_engine.execution import protocol

    original_read_frame = read_frame
    reading = threading.Event()

    def observed_read(fd: int, *, max_bytes: int) -> JsonObject:
        reading.set()
        return original_read_frame(fd, max_bytes=max_bytes)

    monkeypatch.setattr(protocol, "read_frame", observed_read)
    channel = FrameChannel(*os.pipe(), max_bytes=128)
    results: list[JsonObject] = []
    receiver = threading.Thread(target=lambda: results.append(channel.read()))
    receiver.start()
    try:
        assert reading.wait(timeout=2)
        with pytest.raises(ActionProtocolError) as active:
            channel.close()
        assert active.value.code == "active_reader"
        with pytest.raises(ActionProtocolError) as duplicate:
            channel.read()
        assert duplicate.value.code == "active_reader"
        channel.send({"done": True})
        receiver.join(timeout=2)
        assert not receiver.is_alive()
        assert results == [{"done": True}]
    finally:
        channel.close()


@pytest.mark.parametrize("max_bytes", [True, 0, -1, 1 << 32])
def test_invalid_frame_limits_are_rejected(max_bytes: int) -> None:
    with pytest.raises(ActionProtocolError) as invalid:
        read_frame(0, max_bytes=max_bytes)
    assert invalid.value.code == "invalid_channel"


def test_native_io_error_remains_visible() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    with pytest.raises(ActionProtocolError) as failed:
        write_frame(write_fd, {}, max_bytes=128)
    assert failed.value.code == "io_error"
    assert isinstance(failed.value.__cause__, OSError)


def test_failed_close_retains_result_without_closing_reused_fds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    channel = FrameChannel(read_fd, write_fd, max_bytes=128)
    original_close = os.close
    closed: list[int] = []

    def close_with_ambiguous_failure(fd: int) -> None:
        original_close(fd)
        closed.append(fd)
        if fd == read_fd:
            raise OSError("injected close failure")

    monkeypatch.setattr(os, "close", close_with_ambiguous_failure)
    with pytest.raises(ActionProtocolError) as first:
        channel.close()
    assert first.value.code == "close_failed"
    assert closed == [read_fd, write_fd]
    with pytest.raises(ActionProtocolError) as repeated:
        channel.close()
    assert repeated.value is first.value
    assert closed == [read_fd, write_fd]
