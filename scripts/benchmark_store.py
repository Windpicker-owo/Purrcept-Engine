"""在独占临时库记录固定合成规模的启动、媒体查询和历史索引成本。

不接受运行库路径，不调用模型或打开设备。相同规模和环境的输出可做提交间对比；
完整检查与检查点恢复仅作为显式参考工作量，不能当成整机启动改善率。
临时库始终在 Store 归还后删除，结果可由调用者重定向到验收目录。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from contextlib import closing
from pathlib import Path

from PIL import Image
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ReminderState,
)

from purrcept_engine.domain.context.checkpoints import encode_checkpoint
from purrcept_engine.infrastructure.sqlite.store import Store


async def measure(directory: Path, *, actions: int, media: int, repeats: int) -> dict[str, object]:
    """只在新临时目录种入合成记录，重复测量同一组只读路径。

    Args:
        directory: 本入口创建并独占的空临时目录。
        actions: 历史行动条数，按固定时间和稳定身份生成。
        media: 成功检查点内的独立1024像素 PNG 数量。
        repeats: 同一查询的测量次数，用中位数避免偶然尖峰主导结果。
    """
    path = directory / "engine.sqlite3"
    store = await Store.open(path)
    try:
        blocks: list[ImageBlock] = []
        for index in range(media):
            image = Image.new("RGB", (1024, 1024), (index % 256, index // 256, 42))
            output = io.BytesIO()
            image.save(output, format="PNG", compress_level=0)
            blocks.append(ImageBlock(ImageBytes(output.getvalue(), "image/png")))
        checkpoint = ConversationCheckpoint(
            ConversationState(
                (Message(MessageRole.USER, tuple(blocks)), Message.assistant("合成轮次已完成。")),
                turn_index=1,
                turn_boundaries=(2,),
            ),
            ReminderState(),
        )
        turn = await store.begin_turn("benchmark", "foreground", 0, 1, ())
        await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
    finally:
        await store.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO actions(action_id,catalog_version,source,state,created_at,finished_at) "
            "VALUES(?,'synthetic','pass','completed',?,?)",
            ((f"action:{index:08d}", float(index), float(index)) for index in range(actions)),
        )
    timings: dict[str, list[float]] = {key: [] for key in ("open", "panel", "history", "restore")}
    for _ in range(repeats):
        started = time.perf_counter()
        store = await Store.open(path)
        timings["open"].append((time.perf_counter() - started) * 1000)
        try:
            started = time.perf_counter()
            await store.runtime_projection()
            timings["panel"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            await store.human_feedback.page()
            timings["history"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            await store.get_session("foreground")
            timings["restore"].append((time.perf_counter() - started) * 1000)
        finally:
            await store.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        started = time.perf_counter()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("合成库完整性检查失败。")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("合成库外键检查失败。")
        integrity = (time.perf_counter() - started) * 1000
        plans = connection.execute(
            "EXPLAIN QUERY PLAN SELECT action_id FROM actions "
            "ORDER BY created_at DESC,action_id DESC LIMIT 20"
        ).fetchall()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "actions": actions,
        "png_count": media,
        "png_dimensions": [1024, 1024],
        "repeats": repeats,
        "median_ms": {key: statistics.median(values) for key, values in timings.items()},
        "full_integrity_ms": integrity,
        "action_query_plan": plans,
        "database_bytes": path.stat().st_size,
        "synthetic_only": True,
    }


def main() -> None:
    """校验有限数据规模，完成后只输出本次环境和测量事实。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=int, default=10_000)
    parser.add_argument("--media", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 0 <= args.actions <= 100_000 or not 1 <= args.media <= 32 or not 1 <= args.repeats <= 20:
        parser.error("规模须为0—100000项行动、1—32份媒体和1—20次测量。")
    with tempfile.TemporaryDirectory(prefix="purrcept-benchmark-") as temporary:
        result = asyncio.run(
            measure(Path(temporary), actions=args.actions, media=args.media, repeats=args.repeats)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
