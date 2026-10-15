"""实际 FFmpeg 与生成 WAV 验证独立调音室的路由、PCM 边界与预设版本。

不打开声卡/扬声器，不启动主体或调用模型；HTTP 只使用明确 ASGI 传输。
文件保存在 Engine 下临时测试目录，付费和外部网络均禁止。
"""

from __future__ import annotations

import asyncio
import io
import math
import struct
import wave
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Literal

import httpx
import pytest
from test_web import local_asgi_only as local_asgi_only

from purrcept_engine.app.studio import server
from purrcept_engine.app.studio.profiles import ProfileConflict, load_profile, save_profile
from purrcept_engine.app.studio.server import create_studio
from purrcept_engine.plugins.audio.dsp import VoiceProfile, encode_wav
from purrcept_engine.values import JsonObject

PORT = 8766
HEADERS = {"X-Purrcept-Token": "token", "Origin": f"http://127.0.0.1:{PORT}"}


def tone(seconds: int = 1, rate: int = 16000) -> bytes:
    """生成有界低幅度正弦，不读取任何现有用户素材。"""
    pcm = b"".join(
        struct.pack("<h", round(2000 * math.sin(index * 2 * math.pi * 440 / rate)))
        for index in range(seconds * rate)
    )
    return encode_wav(pcm, rate)


def client(path: Path, *, remote: str = "127.0.0.1") -> httpx.AsyncClient:
    """只借用进程内 ASGI，不访问本机监听或外部网络。"""
    transport = httpx.ASGITransport(
        create_studio(path, port=PORT, token="token"), client=(remote, 123)
    )
    return httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{PORT}")


async def test_actual_upload_original_render_and_stale_clip(tmp_path: Path) -> None:
    async with client(tmp_path / "profile.json") as session:
        assert (await session.get("/")).status_code == 200
        assert (await session.get("/assets/purrcept.css")).status_code == 200
        assert (await session.get("/assets/unknown.css")).status_code == 404
        assert (await session.get("/api/session")).json()["clip_id"] is None
        uploaded = await session.post("/api/source", headers=HEADERS, content=tone())
        assert uploaded.status_code == 200 and uploaded.json()["duration"] == 1
        identity = uploaded.json()["clip_id"]
        original = await session.get("/api/source/" + identity)
        assert original.status_code == 200 and original.headers["cache-control"] == "no-store"
        with wave.open(io.BytesIO(original.content), "rb") as audio:
            assert audio.getframerate() == 32000 and audio.getnframes() == 32000
            raw = audio.readframes(32000)
        rendered = await session.post(
            "/api/render",
            headers=HEADERS,
            json={
                "clip_id": identity,
                "profile": {"enabled": True, "gain_db": -6.0, "limiter": False},
            },
        )
        assert rendered.status_code == 200
        with wave.open(io.BytesIO(rendered.content), "rb") as audio:
            processed = audio.readframes(audio.getnframes())
            assert audio.getnframes() == 32000
        original_peak = max(abs(value[0]) for value in struct.iter_unpack("<h", raw))
        processed_peak = max(abs(value[0]) for value in struct.iter_unpack("<h", processed))
        assert 0.49 < processed_peak / original_peak < 0.52
        new = await session.post("/api/source", headers=HEADERS, content=tone())
        assert new.json()["clip_id"] != identity
        assert (await session.get("/api/source/" + identity)).status_code == 404
        assert (
            await session.post(
                "/api/render", headers=HEADERS, json={"clip_id": identity, "profile": {}}
            )
        ).status_code == 409


async def test_real_pcm_duration_and_upload_bytes_do_not_replace_valid_clip(tmp_path: Path) -> None:
    async with client(tmp_path / "profile.json") as session:
        valid = (await session.post("/api/source", headers=HEADERS, content=tone())).json()[
            "clip_id"
        ]
        excessive = encode_wav(b"\0\0" * (16000 * 91), 16000)
        assert len(excessive) < server.MAX_UPLOAD_BYTES
        assert (
            await session.post("/api/source", headers=HEADERS, content=excessive)
        ).status_code == 400
        assert (
            await session.post("/api/source", headers=HEADERS, content=b"RIFFxxxxWAVEbroken")
        ).status_code == 400
        assert (
            await session.post(
                "/api/source", headers=HEADERS, content=b"#EXTM3U\nhttp://example.invalid/audio"
            )
        ).status_code == 400

        async def oversized() -> AsyncIterator[bytes]:
            for _ in range(21):
                yield b"x" * (1024 * 1024)

        assert (
            await session.post(
                "/api/source", headers=HEADERS | {"Content-Length": "1"}, content=oversized()
            )
        ).status_code == 400
        assert (await session.get("/api/session")).json()["clip_id"] == valid
        assert (await session.get("/api/source/" + valid)).status_code == 200


async def test_profile_versions_and_invalid_file_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    async with client(path) as session:
        loaded = (await session.get("/api/profile")).json()
        assert loaded["revision"] == "missing" and loaded["profile"]["version"] == 1
        saved = await session.post(
            "/api/profile",
            headers=HEADERS,
            json={
                "revision": loaded["revision"],
                "profile": {"enabled": True, "pitch_semitones": 0.5},
            },
        )
        assert saved.status_code == 200 and path.is_file()
        original = path.read_bytes()
        assert (
            await session.post(
                "/api/profile", headers=HEADERS, json={"revision": "missing", "profile": {}}
            )
        ).status_code == 409
        assert (
            await session.post(
                "/api/profile",
                headers=HEADERS,
                json={"revision": saved.json()["revision"], "profile": {"version": 2}},
            )
        ).status_code == 400
        assert path.read_bytes() == original
        path.write_bytes(b"{broken}")
        assert (await session.get("/api/profile")).status_code == 400
        assert path.read_bytes() == b"{broken}"


@pytest.mark.parametrize(
    "headers,remote",
    [
        ({"Host": "evil.invalid:8766"}, "127.0.0.1"),
        ({"Origin": "http://evil.invalid"}, "127.0.0.1"),
        ({"Sec-Fetch-Site": "cross-site"}, "127.0.0.1"),
        ({"X-Forwarded-For": "127.0.0.1"}, "192.0.2.1"),
    ],
)
async def test_shared_local_boundary_rejects_wrong_source(
    tmp_path: Path, headers: dict[str, str], remote: str
) -> None:
    async with client(tmp_path / "profile.json", remote=remote) as session:
        assert (await session.get("/api/session", headers=headers)).status_code == 403
        assert (
            await session.post("/api/source", headers=HEADERS | headers, content=tone())
        ).status_code == 403


async def test_write_needs_token_and_profile_json_has_closed_boundary(tmp_path: Path) -> None:
    async with client(tmp_path / "profile.json") as session:
        assert (await session.post("/api/source", content=tone())).status_code == 403
        assert (
            await session.post(
                "/api/profile",
                headers=HEADERS,
                content=b'{"revision":"missing","revision":"x","profile":{}}',
            )
        ).status_code == 400
        assert (
            await session.post(
                "/api/profile",
                headers=HEADERS,
                json={"revision": "missing", "profile": {}, "extra": 1},
            )
        ).status_code == 400
        assert (await session.post("/api/chat", headers=HEADERS)).status_code == 404


async def test_serial_render_cancel_releases_decoder_before_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path / "profile.json") as session:
        identity = (await session.post("/api/source", headers=HEADERS, content=tone())).json()[
            "clip_id"
        ]
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed(
            encoded: AsyncIterator[bytes],
            *,
            executable: str,
            sample_rate: int,
            profile: VoiceProfile,
            input_format: Literal["mp3", "wav", "flac"] | None = None,
            max_seconds: int = 180,
        ) -> AsyncGenerator[bytes]:
            del encoded, executable, sample_rate, profile, input_format, max_seconds
            entered.set()
            try:
                await release.wait()
                yield b"\0\0" * 32000
            finally:
                closed.set()

        monkeypatch.setattr(server, "decode_pcm", delayed)
        value: JsonObject = {"clip_id": identity, "profile": {}}
        first = asyncio.create_task(session.post("/api/render", headers=HEADERS, json=value))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await session.post("/api/render", headers=HEADERS, json=value)).status_code == 409
        assert (
            await session.post("/api/source", headers=HEADERS, content=tone())
        ).status_code == 409
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert closed.is_set()
        release.set()
        assert (await session.post("/api/render", headers=HEADERS, json=value)).status_code == 200


async def test_internal_decoder_failure_is_not_invalid_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(
        encoded: AsyncIterator[bytes],
        *,
        executable: str,
        sample_rate: int,
        profile: VoiceProfile,
        input_format: Literal["mp3", "wav", "flac"] | None = None,
        max_seconds: int = 180,
    ) -> AsyncGenerator[bytes]:
        del encoded, executable, sample_rate, profile, input_format, max_seconds
        yield b"\0\0"
        raise RuntimeError("internal decoder defect")

    monkeypatch.setattr(server, "decode_pcm", broken)
    async with client(tmp_path / "profile.json") as session:
        with pytest.raises(RuntimeError, match="internal decoder defect"):
            await session.post("/api/source", headers=HEADERS, content=tone())


def test_profile_conflict_preserves_file_and_bad_version(tmp_path: Path) -> None:
    path = tmp_path / "voice.json"
    profile, revision = load_profile(path)
    saved = save_profile(path, profile, revision)
    with pytest.raises(ProfileConflict):
        save_profile(path, VoiceProfile(enabled=True), revision)
    assert load_profile(path)[1] == saved
