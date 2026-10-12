"""模型安装用纯 MockTransport 验证完整校验和及原子替换，绝不实际下载。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from purrcept_engine.plugins.voice_input import setup


def test_explicit_model_install_checksum_and_target_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"generated model fixture"
    path = tmp_path / "vad.onnx"
    path.write_bytes(b"existing")

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == setup.URL
        return httpx.Response(200, content=data)

    transport = httpx.MockTransport(respond)

    def selected(client: httpx.Client, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    monkeypatch.setattr(httpx.Client, "_transport_for_url", selected)
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(ValueError, match="校验和"):
            setup.install_model(path, client)
        assert path.read_bytes() == b"existing"
        monkeypatch.setattr(setup, "SHA256", hashlib.sha256(data).hexdigest())
        setup.install_model(path, client)
    assert path.read_bytes() == data and list(tmp_path.glob("*.tmp")) == []
