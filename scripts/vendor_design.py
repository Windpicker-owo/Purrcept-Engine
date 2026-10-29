"""从显式 Git 提交制作自包含的 Cel 开发依赖，不读取 Design 的工作区改动。

维护者用 --source 和 --revision 更新依赖；普通开发只需 npm ci。包内保留原目录、
Token、类型和样式校验器，来源清单及 SHA-256 随 tarball 提交。归档时间固定，
相同提交生成相同字节；本入口不修改上游，也不从未提交文件取材。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path


def main() -> None:
    """解析来源并将固定提交的组件与工具导出至 Engine 的 vendor 目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()

    def git(*arguments: str) -> bytes:
        """Git 只作为只读对象库使用，路径和提交不拼接成 shell 代码。"""
        return subprocess.check_output(["git", "-C", str(source), *arguments])

    revision = git("rev-parse", f"{args.revision}^{{commit}}").decode().strip()
    paths = git("ls-tree", "-r", "--name-only", revision).decode().splitlines()
    selected = [
        name
        for name in paths
        if name.startswith(("react/src/", "dist/"))
        or name in ("DESIGN.md", "scripts/lint_styles.py", "scripts/export_tokens.py")
        or name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
    ]
    files = {name: git("show", f"{revision}:{name}") for name in selected}
    for name in ("motion.js", "motion.d.ts"):
        files[f"react/src/generated/{name}"] = files[f"dist/{name}"]
    upstream = json.loads(git("show", f"{revision}:react/package.json"))
    manifest = {
        "name": "@purrcept/cel",
        "version": f"0.1.0-snapshot.{revision[:12]}",
        "private": True,
        "type": "module",
        "sideEffects": ["**/*.css"],
        "exports": {
            ".": {"types": "./react/src/index.ts", "default": "./react/src/index.ts"},
            "./styles.css": "./react/src/styles/index.css",
            "./tokens.css": "./dist/purrcept.css",
        },
        "dependencies": upstream["dependencies"],
        "peerDependencies": upstream["peerDependencies"],
    }
    files["package.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    files["UPSTREAM.json"] = (
        json.dumps(
            {
                "repository": "https://github.com/Windpicker-owo/Purrcept-Design",
                "commit": revision,
                "license_files": [
                    name
                    for name in selected
                    if name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
                ],
                "license_note": "上游该提交未声明许可证时，本快照不另行授予许可。",
                "generated_copies": {
                    "react/src/generated/": "dist/motion.js 与 dist/motion.d.ts 的原样副本"
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()

    output = Path(__file__).resolve().parents[1] / "frontend" / "vendor"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "purrcept-cel.tgz"
    with (
        target.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, content in sorted(files.items()):
                entry = tarfile.TarInfo(f"package/{name}")
                entry.size, entry.mode = len(content), 0o644
                archive.addfile(entry, io.BytesIO(content))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "README.md").write_text(
        f"# Cel 固定依赖\n\n来源提交：`{revision}`。\n\n"
        f"SHA-256：`{digest}`。\n\n"
        "来源与许可声明见包内 `UPSTREAM.json`；原始源码和 Token 均来自 Git 对象，"
        "不包含上游未提交修改。生成副本保持原字节。\n\n"
        "更新：在 Engine 根目录运行 `python scripts/vendor_design.py --source <Design仓库> "
        "--revision <完整提交>`，然后在 frontend 运行 `npm install` 并提交锁文件。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
