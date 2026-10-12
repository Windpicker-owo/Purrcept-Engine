"""验证离线依赖补丁的输入边界、源码范围、原生字节和 wheel 完整性。

封包场景需要已下载且摘要固定的官方 Windows wheel；不会在测试中联网。
真实传输次数与客户端生命周期由模型接入测试验证，本模块核验交付产物。
"""

import ast
import base64
import csv
import difflib
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_litellm import (
    OUTPUT_FILENAME,
    PATCHED_VERSION,
    SOURCE_SHA256,
    UPSTREAM_FILENAME,
    UPSTREAM_SHA256,
    PreparedWheel,
    apply_source_patch,
    file_sha256,
    prepare_wheel,
)

_ROOT = Path(__file__).resolve().parents[1]
_PATCH = _ROOT / "patches" / "litellm-1.105.0.patch"


@pytest.fixture(scope="module")
def upstream_wheel() -> Path:
    """借用离线官方输入；缺少输入明确跳过外部构建工件场景。"""
    configured = os.environ.get("PURRCEPT_LITELLM_WHEEL")
    wheel = (
        Path(configured)
        if configured is not None
        else _ROOT / "build" / "litellm-patched" / "upstream" / UPSTREAM_FILENAME
    )
    if not wheel.is_file():
        pytest.skip("需要离线官方 wheel；可用 PURRCEPT_LITELLM_WHEEL 指定输入。")
    assert file_sha256(wheel) == UPSTREAM_SHA256
    return wheel


@pytest.fixture(scope="module")
def prepared(upstream_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> PreparedWheel:
    """在独立目录构建，不安装或改写现有 SDK 环境。"""
    return prepare_wheel(upstream_wheel, output_dir=tmp_path_factory.mktemp("patched-wheel"))


def sample_patch() -> tuple[dict[str, bytes], str, dict[str, bytes]]:
    """小型语法有效样本检验错位与上下文约束，避免错误输入仍模糊套用。"""
    sources = {path: b"first = 1\nsecond = 2\nthird = 3\n" for path in SOURCE_SHA256}
    changed = {path: value.replace(b"second = 2", b"second = 4") for path, value in sources.items()}
    patch = "".join(
        "".join(
            difflib.unified_diff(
                sources[path].decode().splitlines(keepends=True),
                changed[path].decode().splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        for path in sources
    )
    return sources, patch, changed


def test_patch_applies_exact_context_without_fuzzy_matching() -> None:
    sources, patch, expected = sample_patch()
    assert apply_source_patch(sources, patch) == expected
    drifted = dict(sources)
    path = next(iter(drifted))
    drifted[path] = drifted[path].replace(b"first = 1", b"first = 9")
    with pytest.raises(ValueError, match="上下文不匹配"):
        apply_source_patch(drifted, patch)
    with pytest.raises(ValueError, match="行数不匹配"):
        apply_source_patch(sources, patch.replace("-1,3 +1,3", "-1,4 +1,3", 1))
    with pytest.raises(ValueError, match="范围外"):
        apply_source_patch(sources, patch.replace("decisions/main.py", "unapproved.py", 1))


def test_untrusted_wheel_is_rejected_before_output_creation(tmp_path: Path) -> None:
    wrong = tmp_path / UPSTREAM_FILENAME
    wrong.write_bytes(b"not the official wheel")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_wheel(wrong, output_dir=output)
    assert output.exists() is False


def test_metadata_and_all_record_hashes_are_complete(prepared: PreparedWheel) -> None:
    with zipfile.ZipFile(prepared.output_path) as archive:
        dist_info = f"litellm-{PATCHED_VERSION}.dist-info/"
        assert (
            f"Version: {PATCHED_VERSION}"
            in archive.read(dist_info + "METADATA").decode().splitlines()
        )
        assert (
            "Tag: cp310-abi3-win_amd64" in archive.read(dist_info + "WHEEL").decode().splitlines()
        )
        record_path = dist_info + "RECORD"
        rows = list(csv.reader(io.StringIO(archive.read(record_path).decode())))
        assert {row[0] for row in rows} == set(archive.namelist())
        assert len(rows) == len(archive.namelist())
        for path, digest, size in rows:
            if path == record_path:
                assert digest == size == ""
                continue
            content = archive.read(path)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            assert digest == "sha256=" + expected.decode()
            assert int(size) == len(content)
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    assert manifest["input_sha256"] == UPSTREAM_SHA256
    assert manifest["output_sha256"] == file_sha256(prepared.output_path)
    assert manifest["patch_sha256"] == hashlib.sha256(_PATCH.read_bytes()).hexdigest()
    assert manifest["source_sha256"] == SOURCE_SHA256


def test_changes_cover_only_approved_sources_and_metadata(
    prepared: PreparedWheel, upstream_wheel: Path
) -> None:
    with (
        zipfile.ZipFile(upstream_wheel) as original,
        zipfile.ZipFile(prepared.output_path) as patched,
    ):
        old_dist = "litellm-1.105.0.dist-info/"
        new_dist = f"litellm-{PATCHED_VERSION}.dist-info/"
        changed: set[str] = set()
        native: list[str] = []
        for path in original.namelist():
            mapped = new_dist + path.removeprefix(old_dist) if path.startswith(old_dist) else path
            if original.read(path) != patched.read(mapped):
                changed.add(path)
            if path.endswith(".pyd"):
                native.append(path)
                assert original.read(path) == patched.read(mapped)
        assert native, "官方 Windows wheel 应包含原生扩展。"
        assert changed == set(SOURCE_SHA256) | {old_dist + "METADATA", old_dist + "RECORD"}
        assert len(original.namelist()) == len(patched.namelist())


def _declarations(body: list[ast.stmt]) -> dict[str, str]:
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_existing_protocol_and_unrelated_http_methods_keep_their_ast(
    prepared: PreparedWheel, upstream_wheel: Path
) -> None:
    expected = {
        "litellm/decisions/main.py": {"adecisions"},
        "litellm/decisions/call.py": {"asend"},
        "litellm/llms/custom_httpx/http_handler.py": {"AsyncHTTPHandler"},
    }
    with (
        zipfile.ZipFile(upstream_wheel) as original,
        zipfile.ZipFile(prepared.output_path) as patched,
    ):
        for path, changed_names in expected.items():
            old_ast = ast.parse(original.read(path))
            new_ast = ast.parse(patched.read(path))
            old = _declarations(old_ast.body)
            new = _declarations(new_ast.body)
            assert old.keys() == new.keys()
            assert {name for name in old if old[name] != new[name]} == changed_names
            if "AsyncHTTPHandler" not in changed_names:
                continue
            old_handler = next(
                node
                for node in old_ast.body
                if isinstance(node, ast.ClassDef) and node.name == "AsyncHTTPHandler"
            )
            new_handler = next(
                node
                for node in new_ast.body
                if isinstance(node, ast.ClassDef) and node.name == "AsyncHTTPHandler"
            )
            old_methods = _declarations(old_handler.body)
            new_methods = _declarations(new_handler.body)
            assert old_methods.keys() == new_methods.keys()
            assert {name for name in old_methods if old_methods[name] != new_methods[name]} == {
                "__init__",
                "post",
            }
            constructor = next(
                node
                for node in new_handler.body
                if isinstance(node, ast.FunctionDef) and node.name == "__init__"
            )
            assert constructor.args.args[-1].arg == "retry_connection_errors"
            assert ast.literal_eval(constructor.args.defaults[-1]) is True


def test_build_is_deterministic_and_reuses_identical_output(
    prepared: PreparedWheel, upstream_wheel: Path, tmp_path: Path
) -> None:
    fresh = prepare_wheel(upstream_wheel, output_dir=tmp_path / "fresh")
    assert fresh.output_sha256 == prepared.output_sha256
    before = fresh.output_path.stat().st_mtime_ns
    reused = prepare_wheel(upstream_wheel, output_dir=fresh.output_path.parent)
    assert reused == fresh
    assert reused.output_path.stat().st_mtime_ns == before
    assert reused.manifest_path.read_bytes() == prepared.manifest_path.read_bytes()


def test_existing_different_artifacts_are_not_overwritten(
    upstream_wheel: Path, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    existing = output / OUTPUT_FILENAME
    existing.write_bytes(b"preserve this existing artifact")
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        prepare_wheel(upstream_wheel, output_dir=output)
    assert existing.read_bytes() == b"preserve this existing artifact"
    assert tuple(path.name for path in output.iterdir()) == (OUTPUT_FILENAME,)
