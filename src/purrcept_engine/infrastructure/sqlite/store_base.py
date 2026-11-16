"""Store 各职责子混入共享的底层队列与配额基类。"""

from pathlib import Path

from purrcept_engine.infrastructure.sqlite.evidence_port import EvidenceLedger
from purrcept_engine.infrastructure.sqlite.queue import SqliteExecutor
from purrcept_engine.infrastructure.sqlite.rows import validate_id


class StoreBase:
    """持有唯一 SqliteExecutor 提交队列与输入额度配置。"""

    def __init__(self, path: Path, max_inputs: int, max_input_bytes: int) -> None:
        self._max_inputs = max_inputs
        self._max_input_bytes = max_input_bytes
        self._queue = SqliteExecutor(path, max_inputs, max_input_bytes)
        self._submit = self._queue.submit
        self._evidence = EvidenceLedger(self._submit)

    @staticmethod
    def _validate_id(value: str) -> None:
        validate_id(value)
