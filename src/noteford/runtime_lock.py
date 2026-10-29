"""OS-owned writer lease; automatically released after a process crash."""
import os
from pathlib import Path


class RuntimeLock:
    def __init__(self, root: Path):
        self.path = root / 'data' / 'runtime' / 'writer.lock'
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('data_root_in_use') from None
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()
            self.handle = None
