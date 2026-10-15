"""数据目录的操作系统锁。

宿主在打开权威库之前取得锁，存储连接实际关闭后才释放。锁文件只是锁的
载体，文件存在或其中的 PID 都不作为占用依据；异常退出由操作系统解除锁。
Windows 使用字节范围锁，其他平台的基础路径使用 flock，不承诺平台验收。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

from purrcept_engine.errors import InstanceLocked


class InstanceLock:
    """由单个宿主独占的目录锁，acquire/release 在同一宿主生命周期调用。

    不共享给插件，不支持实例重入；重复 release 无效果。获取失败保留原
    目录和文件，不能用删除锁文件来抢占仍在运行的宿主。
    """

    def __init__(self, directory: Path) -> None:
        """保存目录位置，构造不创建文件或取得锁。

        Args:
            directory: 本次操作使用的明确目录。
        """
        self._directory = directory
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """非阻塞地取得目录独占权。

        Raises:
            InstanceLocked: 该宿主已经取得数据目录锁；数据目录正在被另一宿主使用。

        Note:
            真实文件系统错误原样传播。
        """
        if self._file is not None:
            raise InstanceLocked("该宿主已经取得数据目录锁。")

        self._directory.mkdir(parents=True, exist_ok=True)
        lock_file = (self._directory / "instance.lock").open("a+b")
        acquired = False
        try:
            # Windows 字节锁需要一个稳定的可锁字节。内容不会在每次启动改写。
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)

            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise InstanceLocked("数据目录正在被另一宿主使用。") from error
                raise
            acquired = True
        finally:
            # 生命周期：初始化失败时句柄还未交给 self，必须在这里回收。
            if not acquired:
                lock_file.close()

        self._file = lock_file

    def release(self) -> None:
        """释放操作系统锁和句柄。

        Note:
            仅在所有目录借用者已退出后调用。
        """
        if self._file is None:
            return
        # 关闭句柄解除锁，也覆盖正常 shutdown 的平台释放路径。
        self._file.close()
        self._file = None
