"""离线生成带限定 POST 重试补丁的 LiteLLM Windows wheel。

输入仅接受已核验的官方 1.105.0 wheel。本脚本不下载、不安装、不导入 LiteLLM；
只修改 patches 中三个明确源码文件及 wheel 版本/RECORD 元数据，原生扩展保持
原字节。源码摘要和补丁上下文必须精确匹配，不能模糊套用到另一发行版本。

产物版本为 1.105.0+purrcept.1，输入、补丁和输出摘要另存清单。相同输入和补丁
可以重复复用；已有不同产物明确拒绝，临时 wheel 完成后才作为正式路径发布。
"""

import argparse
import base64
import copy
import csv
import difflib
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_VERSION = "1.105.0"
PATCHED_VERSION = "1.105.0+purrcept.1"
UPSTREAM_FILENAME = "litellm-1.105.0-cp310-abi3-win_amd64.whl"
OUTPUT_FILENAME = "litellm-1.105.0+purrcept.1-cp310-abi3-win_amd64.whl"
UPSTREAM_SHA256 = "6ff237f1fe64a086d91b1453116347e0c1b2aed0fe8c8b9014d5a019c6a6b5d6"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/c5/58/"
    "a5096a89d5ac394f60a9fe813160052364f9afc217bfc2ac61b4240f9c3a/" + UPSTREAM_FILENAME
)
SOURCE_SHA256 = {
    "litellm/decisions/main.py": "34c2233ca4da2479bff1094674ffe3b2da7727613723357c15e449468c32273b",
    "litellm/decisions/call.py": "4004bdf8a998d0dfe362651a282e39cc7a27e18fcc067b402320869410f95c15",
    "litellm/llms/custom_httpx/http_handler.py": (
        "90a4e70d8e0f4e329bc2e15d7bf5c5dfdd23ba43e4ae06265f230b5a210a2901"
    ),
}
_ROOT = Path(__file__).resolve().parents[1]
_DIST_INFO = f"litellm-{UPSTREAM_VERSION}.dist-info/"
_PATCHED_DIST_INFO = f"litellm-{PATCHED_VERSION}.dist-info/"
_HUNK = re.compile(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@(?:.*)\n")


@dataclass(frozen=True, slots=True)
class PreparedWheel:
    """可供依赖锁定引用的隔离产物及其构建证据。"""

    input_path: Path
    input_sha256: str
    output_path: Path
    output_sha256: str
    patch_sha256: str
    manifest_path: Path


def file_sha256(path: Path) -> str:
    """流式计算本地构建输入或产物的 SHA-256。

    Args:
        path: 需要核验的现有文件，不下载或替换其内容。

    Returns:
        完整文件字节的十六进制摘要。

    Raises:
        OSError: 文件不存在、不可读或实际读取失败。

    Note:
        逐块读取，避免为了摘要额外持有整个原生 wheel。
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def apply_source_patch(sources: Mapping[str, bytes], patch: str) -> dict[str, bytes]:
    """对限定三文件应用标准 unified diff，拒绝错位和不匹配上下文。

    Args:
        sources: 已核验官方摘要的源码字节，键为 wheel 内的完整相对路径。
        patch: 只覆盖 SOURCE_SHA256 中文件的完整 unified diff 文本。

    Returns:
        按精确上下文生成的新源码映射，不修改传入字节或磁盘文件。

    Raises:
        ValueError: 补丁修改范围外文件、重命名、位置或上下文不匹配。
        UnicodeError: 源码不是已约定的 UTF-8 文本。

    Note:
        不支持模糊定位、重命名、二进制补丁或无换行标记。发行源码使用
        UTF-8/CRLF，保留原换行，避免上游内容漂移后仍看似应用成功。
    """
    lines = patch.splitlines(keepends=True)
    patched: dict[str, bytes] = {}
    index = 0
    while index < len(lines):
        old_header = lines[index]
        if not old_header.startswith("--- a/") or index + 1 >= len(lines):
            raise ValueError("补丁缺少明确的原文件头。")
        path = old_header.removeprefix("--- a/").removesuffix("\n")
        if path not in SOURCE_SHA256 or path in patched:
            raise ValueError("补丁修改了范围外或重复的文件。")
        if lines[index + 1] != f"+++ b/{path}\n":
            raise ValueError("补丁不得重命名文件。")
        original = sources[path].decode("utf-8").splitlines(keepends=True)
        result: list[str] = []
        cursor = 0
        index += 2
        hunk_count = 0
        while index < len(lines) and not lines[index].startswith("--- a/"):
            match = _HUNK.fullmatch(lines[index])
            if match is None:
                raise ValueError(f"{path} 的补丁 hunk 格式无效。")
            old_start, old_count, new_start, new_count = (int(value) for value in match.groups())
            old_offset = old_start - 1
            if old_offset < cursor or old_offset > len(original):
                raise ValueError(f"{path} 的补丁位置重叠或越界。")
            result.extend(original[cursor:old_offset])
            cursor = old_offset
            if new_start != len(result) + 1:
                raise ValueError(f"{path} 的补丁新位置不匹配。")
            index += 1
            consumed = produced = 0
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- a/")):
                line = lines[index]
                if not line or line[0] not in " +-":
                    raise ValueError(f"{path} 包含不支持的补丁行。")
                marker, content = line[0], line[1:]
                if marker in " -":
                    if cursor >= len(original) or original[cursor] != content:
                        raise ValueError(f"{path} 的补丁上下文不匹配。")
                    cursor += 1
                    consumed += 1
                if marker in " +":
                    result.append(content)
                    produced += 1
                index += 1
            if consumed != old_count or produced != new_count:
                raise ValueError(f"{path} 的补丁行数不匹配。")
            hunk_count += 1
        if not hunk_count:
            raise ValueError(f"{path} 没有实际补丁。")
        result.extend(original[cursor:])
        patched[path] = "".join(result).encode("utf-8")
        compile(patched[path], path, "exec")
    if set(patched) != set(SOURCE_SHA256):
        raise ValueError("补丁必须恰好覆盖已确认的三个源码文件。")

    # 标准 diff 是补丁的唯一源码变更表示，不接受额外零效果 hunk 或另一份替换表。
    canonical = "".join(
        "".join(
            difflib.unified_diff(
                sources[path].decode("utf-8").splitlines(keepends=True),
                content.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        for path, content in patched.items()
    )
    if canonical != patch:
        raise ValueError("补丁不是限定源码变化的规范 unified diff。")
    return patched


def _record_digest(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _record_bytes(rows: list[tuple[str, str, str]], record_path: str) -> bytes:
    """wheel RECORD 覆盖全部正式成员，自身摘要按 wheel 格式留空。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    writer.writerow((record_path, "", ""))
    return buffer.getvalue().encode("utf-8")


def _write_wheel(
    source: zipfile.ZipFile, destination: Path, patched_sources: Mapping[str, bytes]
) -> None:
    """保留原成员顺序和 ZIP 元信息，逐成员重写并生成准确 RECORD。

    除三个源码和 METADATA 外直接复制原字节，原生扩展不会经过解码或编译。
    一次只持有一个成员，避免复制整个展开后的依赖树到内存。
    """
    metadata_path = _DIST_INFO + "METADATA"
    metadata = source.read(metadata_path)
    metadata_newline = b"\r\n" if metadata.splitlines(keepends=True)[0].endswith(b"\r\n") else b"\n"
    version_line = f"Version: {UPSTREAM_VERSION}".encode("ascii") + metadata_newline
    if metadata.count(version_line) != 1 or b"Name: litellm" + metadata_newline not in metadata:
        raise ValueError("输入 wheel 的项目或版本元数据不匹配。")
    metadata = metadata.replace(
        version_line, f"Version: {PATCHED_VERSION}".encode("ascii") + metadata_newline, 1
    )
    record_path = _PATCHED_DIST_INFO + "RECORD"
    rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for info in source.infolist():
            if info.filename == _DIST_INFO + "RECORD":
                continue
            content = patched_sources.get(info.filename)
            if content is None:
                content = source.read(info.filename)
            if info.filename == metadata_path:
                content = metadata
            output_info = copy.copy(info)
            if info.filename.startswith(_DIST_INFO):
                output_info.filename = _PATCHED_DIST_INFO + info.filename.removeprefix(_DIST_INFO)
            target.writestr(output_info, content)
            rows.append((output_info.filename, _record_digest(content), str(len(content))))
        record_info = copy.copy(source.getinfo(_DIST_INFO + "RECORD"))
        record_info.filename = record_path
        target.writestr(record_info, _record_bytes(rows, record_path))


def prepare_wheel(
    input_path: Path,
    *,
    output_dir: Path,
    patch_path: Path = _ROOT / "patches" / "litellm-1.105.0.patch",
) -> PreparedWheel:
    """核验官方输入与精确补丁后生成或复用隔离 local wheel。

    Args:
        input_path: 预先下载的官方 Windows wheel，完整字节须匹配固定摘要。
        output_dir: 仅保存派生产物与核验清单的目录，不是安装环境。
        patch_path: 精确绑定当前官方源码的补丁文件，默认使用仓库内补丁。

    Returns:
        输入、补丁和输出摘要，以及可供依赖锁定的产物与清单路径。

    Raises:
        ValueError: 输入摘要、源码、补丁或已有产物不符合冻结构建要求。
        OSError: 输入、补丁、产物或清单无法读取和写入。
        zipfile.BadZipFile: 输入不是可读取的 wheel ZIP。

    Note:
        已有不同内容不覆盖，失败不安装依赖。清单不加入时间戳或机器路径，
        使同一输入和补丁可以确定复用。
    """
    input_path = input_path.resolve()
    input_hash = file_sha256(input_path)
    if input_hash != UPSTREAM_SHA256:
        raise ValueError("输入不是已核验 SHA-256 的官方 LiteLLM 1.105.0 Windows wheel。")
    patch_bytes = patch_path.read_bytes()
    patch_hash = hashlib.sha256(patch_bytes).hexdigest()
    output_dir = output_dir.resolve()
    output_path = output_dir / OUTPUT_FILENAME
    manifest_path = output_dir / f"litellm-{PATCHED_VERSION}.json"

    with zipfile.ZipFile(input_path) as source:
        if len(source.namelist()) != len(set(source.namelist())):
            raise ValueError("输入 wheel 含重复成员。")
        sources = {path: source.read(path) for path in SOURCE_SHA256}
        if any(
            hashlib.sha256(content).hexdigest() != SOURCE_SHA256[path]
            for path, content in sources.items()
        ):
            raise ValueError("输入源码摘要不匹配。")
        patched_sources = apply_source_patch(sources, patch_bytes.decode("utf-8"))
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output_dir, suffix=".whl.tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            _write_wheel(source, temporary_path, patched_sources)
            output_hash = file_sha256(temporary_path)
            # 同目录硬链接以不覆盖方式发布完整文件，避免 POSIX rename 覆盖旧产物。
            try:
                os.link(temporary_path, output_path)
            except FileExistsError:
                if file_sha256(output_path) != output_hash:
                    raise FileExistsError("已有不同 wheel，拒绝覆盖。") from None
        finally:
            temporary_path.unlink()

    manifest = {
        "upstream_version": UPSTREAM_VERSION,
        "patched_version": PATCHED_VERSION,
        "upstream_url": UPSTREAM_URL,
        "input_filename": UPSTREAM_FILENAME,
        "input_sha256": input_hash,
        "source_sha256": SOURCE_SHA256,
        "patch_sha256": patch_hash,
        "output_filename": OUTPUT_FILENAME,
        "output_sha256": output_hash,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with manifest_path.open("xb") as stream:
            stream.write(manifest_bytes)
    except FileExistsError:
        if manifest_path.read_bytes() != manifest_bytes:
            raise FileExistsError("已有不同构建清单，拒绝覆盖。") from None
    return PreparedWheel(
        input_path, input_hash, output_path, output_hash, patch_hash, manifest_path
    )


def main(argv: list[str] | None = None) -> int:
    """离线构建补丁 wheel，打印用于依赖锁定的输入与产物摘要。

    Args:
        argv: wheel、输出目录及补丁路径参数；None 读取当前进程命令行。

    Returns:
        构建或复用成功时返回 0。

    Raises:
        SystemExit: 参数解析失败，或输入校验与构建被明确拒绝。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="已下载的官方 Windows wheel 路径。")
    parser.add_argument("--output-dir", type=Path, default=_ROOT / "build" / "litellm-patched")
    parser.add_argument("--patch", type=Path, default=_ROOT / "patches" / "litellm-1.105.0.patch")
    arguments = parser.parse_args(argv)
    try:
        prepared = prepare_wheel(
            arguments.wheel, output_dir=arguments.output_dir, patch_path=arguments.patch
        )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"构建拒绝：{error}\n")
    print(f"input_sha256={prepared.input_sha256}")
    print(f"patch_sha256={prepared.patch_sha256}")
    print(f"output_sha256={prepared.output_sha256}")
    print(f"wheel={prepared.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
