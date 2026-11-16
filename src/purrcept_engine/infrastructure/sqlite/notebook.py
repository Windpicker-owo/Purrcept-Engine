"""在 Store 唯一线程保存 Notebook 的可重建目录、索引和可靠工作进度。

所有查询绑定插件身份。文件正文仍归 Markdown；原文快照由 evidence 保存。
register 合并同路径版本，advance 的版本条件防止迟到整理吞掉新变更。
发布意图仅记录跨文件/数据库边界的观察，不能代替实际文件哈希核验。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable

from purrcept_engine.domain.evidence.models import EvidenceSnapshot
from purrcept_engine.domain.notebook.contracts import Document, Publication, Work
from purrcept_engine.domain.notebook.search import index_terms, query_terms
from purrcept_engine.infrastructure.sqlite import evidence
from purrcept_engine.infrastructure.sqlite.evidence_port import SqliteSubmit
from purrcept_engine.values import JsonObject

SCHEMA = (
    "CREATE TABLE notebook_documents(plugin TEXT NOT NULL,path TEXT NOT NULL,file_version TEXT "
    "NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(plugin,path))",
    "CREATE VIRTUAL TABLE notebook_search USING fts5(plugin UNINDEXED,path UNINDEXED,"
    "file_version UNINDEXED,terms)",
    "CREATE TABLE notebook_work(plugin TEXT NOT NULL,path TEXT NOT NULL,file_version TEXT NOT "
    "NULL,cursor TEXT,queued_at REAL NOT NULL,changes INTEGER NOT NULL,next_attempt_at REAL "
    "NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,reason TEXT,PRIMARY KEY(plugin,"
    "path))",
    "CREATE TABLE notebook_publications(plugin TEXT NOT NULL,identity TEXT NOT NULL,"
    "payload_json TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(plugin,identity))",
    "CREATE TABLE notebook_vectors(plugin TEXT NOT NULL,path TEXT NOT NULL,file_version TEXT "
    "NOT NULL,model TEXT NOT NULL,vector_json TEXT NOT NULL,PRIMARY KEY(plugin,path,model))",
)


class NotebookLedger:
    """应用为单个 Notebook 实例构造的有限账本，不拥有连接或后台任务。

    各方法在宿主事件循环调用，SQL 由借入提交器串行执行。权威故障保持原型，
    取消等待不会撤销已经接管的事务；实际关闭归母 Store。
    """

    def __init__(self, submit: SqliteSubmit, instance_id: str) -> None:
        """绑定提交队列和不可变的插件命名空间。"""
        self._submit, self.instance_id = submit, instance_id

    async def _run[T](
        self, operation: Callable[[sqlite3.Connection], T], *, write: bool = False
    ) -> T:
        """在唯一线程执行短操作；写入通知不能早于提交完成。"""
        return await self._submit(operation, logical_bytes=0, writing=write)

    async def documents(self) -> tuple[Document, ...]:
        """读取本实例的派生文件目录，用于完整扫描对账。"""
        return await self._run(
            lambda c: tuple(
                Document.model_validate_json(row[0])
                for row in c.execute(
                    "SELECT payload_json FROM notebook_documents WHERE plugin=? ORDER BY path",
                    (self.instance_id,),
                )
            )
        )

    async def register(self, document: Document, *, needs_work: bool) -> None:
        """登记稳定文件；新版本重置分页但保留首次排队时刻及变更计数。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                c.execute("BEGIN IMMEDIATE")
                prior = c.execute(
                    "SELECT file_version FROM notebook_documents WHERE plugin=? AND path=?",
                    (self.instance_id, document.path),
                ).fetchone()
                if prior is not None and prior[0] == document.file_version:
                    return
                snapshot = evidence.get_evidence(c, document.evidence_id, version=document.version)
                if snapshot is None:
                    raise ValueError("Notebook 登记缺少确切原文快照。")
                c.execute(
                    "INSERT INTO notebook_documents VALUES(?,?,?,?) ON CONFLICT(plugin,path) "
                    "DO UPDATE SET file_version=excluded.file_version,"
                    "payload_json=excluded.payload_json",
                    (
                        self.instance_id,
                        document.path,
                        document.file_version,
                        document.model_dump_json(),
                    ),
                )
                c.execute(
                    "DELETE FROM notebook_search WHERE plugin=? AND path=?",
                    (self.instance_id, document.path),
                )
                c.execute(
                    "INSERT INTO notebook_search VALUES(?,?,?,?)",
                    (
                        self.instance_id,
                        document.path,
                        document.file_version,
                        " ".join(index_terms(snapshot.text)),
                    ),
                )
                c.execute(
                    "DELETE FROM notebook_vectors WHERE plugin=? AND path=?",
                    (self.instance_id, document.path),
                )
                if needs_work:
                    c.execute(
                        "INSERT INTO notebook_work VALUES(?,?,?,NULL,?,1,0,0,NULL) ON "
                        "CONFLICT(plugin,path) DO UPDATE SET "
                        "file_version=excluded.file_version,cursor=NULL,"
                        "changes=notebook_work.changes+1,next_attempt_at=0,attempts=0,"
                        "reason=NULL",
                        (self.instance_id, document.path, document.file_version, time.time()),
                    )
                else:
                    c.execute(
                        "DELETE FROM notebook_work WHERE plugin=? AND path=?",
                        (self.instance_id, document.path),
                    )

        await self._run(commit, write=True)

    async def remove(self, paths: tuple[str, ...]) -> None:
        """删除当前目录和索引资格，保留历史证据及发布审计。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                for path in paths:
                    for table in (
                        "notebook_documents",
                        "notebook_search",
                        "notebook_work",
                        "notebook_vectors",
                    ):
                        c.execute(
                            f"DELETE FROM {table} WHERE plugin=? AND path=?",
                            (self.instance_id, path),
                        )

        if paths:
            await self._run(commit, write=True)

    async def pending(self, *, limit: int = 32) -> tuple[Work, ...]:
        """返回有界待办；冷却和触发条件由 Notebook worker 判断。"""
        if not 1 <= limit <= 32:
            raise ValueError("Notebook 每批来源必须在 1 至 32 之间。")

        def query(c: sqlite3.Connection) -> tuple[Work, ...]:
            return tuple(
                Work(
                    document=Document.model_validate_json(row[0]),
                    cursor=row[1],
                    queued_at=row[2],
                    changes=row[3],
                    next_attempt_at=row[4],
                )
                for row in c.execute(
                    "SELECT d.payload_json,w.cursor,w.queued_at,w.changes,w.next_attempt_at "
                    "FROM notebook_work w JOIN notebook_documents d USING(plugin,path,"
                    "file_version) WHERE w.plugin=? ORDER BY w.queued_at,w.path LIMIT ?",
                    (self.instance_id, limit),
                )
            )

        return await self._run(query)

    async def advance(self, work: Work, cursor: str | None) -> None:
        """只结案原版本；新版本在生成期间进入时保持未处理。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                keys = (self.instance_id, work.document.path, work.document.file_version)
                if cursor is None:
                    c.execute(
                        "DELETE FROM notebook_work WHERE plugin=? AND path=? AND file_version=?",
                        keys,
                    )
                else:
                    c.execute(
                        "UPDATE notebook_work SET cursor=?,queued_at=0,next_attempt_at=0 WHERE "
                        "plugin=? AND path=? AND file_version=?",
                        (cursor, *keys),
                    )

        await self._run(commit, write=True)

    async def postpone(self, works: tuple[Work, ...], reason: str) -> None:
        """按版本持久冷却，最多一小时；失败不会重新获得立即派发资格。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                for work in works:
                    c.execute(
                        "UPDATE notebook_work SET next_attempt_at=?+min(3600,"
                        "60*(1<<min(attempts,6))),attempts=attempts+1,reason=? WHERE plugin=? "
                        "AND path=? AND file_version=?",
                        (
                            time.time(),
                            reason,
                            self.instance_id,
                            work.document.path,
                            work.document.file_version,
                        ),
                    )

        await self._run(commit, write=True)

    async def publications(self) -> tuple[Publication, ...]:
        """恢复未确认发布，保留已结案审计但不重复返回。"""
        return await self._run(
            lambda c: tuple(
                Publication.model_validate_json(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM notebook_publications WHERE plugin=? AND "
                    "state='pending'",
                    (self.instance_id,),
                )
            )
        )

    async def intend(self, publication: Publication) -> None:
        """先持久意图，再允许插件进入磁盘发布阶段。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO notebook_publications VALUES(?,?,?,'pending')",
                    (self.instance_id, publication.identity, publication.model_dump_json()),
                )

        await self._run(commit, write=True)

    async def settle(self, identity: str, state: str) -> None:
        """记录实际恢复观察，不回放可能已生效的文件操作。"""
        if state not in ("confirmed", "conflict", "unknown"):
            raise ValueError("未知 Notebook 发布状态。")

        def commit(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE notebook_publications SET state=? WHERE plugin=? AND identity=?",
                    (state, self.instance_id, identity),
                )

        await self._run(commit, write=True)

    async def search(
        self, query: str, *, path: str = "", limit: int = 40
    ) -> tuple[EvidenceSnapshot, ...]:
        """按双字或明确单字进行 FTS5 排名，插件在交付片段前再次核文件版本。"""
        terms = query_terms(query)
        if not 1 <= limit <= 100:
            raise ValueError("Notebook 检索条数必须在 1 至 100 之间。")
        if not terms:
            return ()
        expression = " OR ".join('"' + term + '"' for term in terms)

        def select(c: sqlite3.Connection) -> tuple[EvidenceSnapshot, ...]:
            documents = tuple(
                Document.model_validate_json(r[0])
                for r in c.execute(
                    "SELECT d.payload_json FROM notebook_search s JOIN notebook_documents d ON "
                    "s.plugin=d.plugin AND s.path=d.path AND s.file_version=d.file_version "
                    "WHERE notebook_search MATCH ? AND s.plugin=? AND (?='' OR d.path=? OR "
                    "substr(d.path,1,length(?)+1)=?||'/') ORDER BY bm25(notebook_search),"
                    "s.path LIMIT ?",
                    (expression, self.instance_id, path, path, path, path, limit),
                )
            )
            return tuple(
                snapshot
                for document in documents
                if (
                    snapshot := evidence.get_evidence(
                        c, document.evidence_id, version=document.version
                    )
                )
                is not None
            )

        return await self._run(select)

    async def vectors(self, model: str) -> tuple[tuple[Document, tuple[float, ...]], ...]:
        """读取本实例当前版本和指定空间的向量，不复用其他模型空间。"""
        return await self._run(
            lambda c: tuple(
                (Document.model_validate_json(r[0]), tuple(json.loads(r[1])))
                for r in c.execute(
                    "SELECT d.payload_json,v.vector_json FROM notebook_vectors v JOIN "
                    "notebook_documents d USING(plugin,path,file_version) WHERE v.plugin=? AND "
                    "v.model=?",
                    (self.instance_id, model),
                )
            )
        )

    async def install_vector(
        self, document: Document, model: str, vector: tuple[float, ...]
    ) -> None:
        """等待网络后仅安装仍匹配的当前版本，不复活已删除路径。"""

        def commit(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO notebook_vectors SELECT plugin,path,file_version,?,"
                    "? FROM notebook_documents WHERE plugin=? AND path=? AND file_version=?",
                    (
                        model,
                        json.dumps(vector, allow_nan=False),
                        self.instance_id,
                        document.path,
                        document.file_version,
                    ),
                )

        await self._run(commit, write=True)

    async def status(self) -> JsonObject:
        """返回任务数量和最早期限，正文与原生诊断不进入此投影。"""

        def query(c: sqlite3.Connection) -> JsonObject:
            row = c.execute(
                "SELECT count(*),min(queued_at),min(next_attempt_at) FROM notebook_work WHERE "
                "plugin=?",
                (self.instance_id,),
            ).fetchone()
            return {"pending": row[0], "oldest_queued_at": row[1], "next_attempt_at": row[2]}

        return await self._run(query)
