"""Private snapshot copies: APFS copy-on-write when available, bounded fallback."""
import ctypes
import os
import shutil
import sys
from pathlib import Path


def copy_snapshot(source: Path, destination: Path) -> None:
    # clonefile creates a distinct inode; modifying the clone cannot affect the
    # immutable base or a reader's published generation. Never use hard links
    # for a file that will subsequently be modified.
    if sys.platform == 'darwin':
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return
    shutil.copyfile(source, destination)
