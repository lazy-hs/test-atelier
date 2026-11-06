"""Read a small regular secret file from the same object that was checked."""

from __future__ import annotations

import os
import stat
from pathlib import Path


MAX_SECRET_BYTES = 4096


def read_secret_bytes(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SECRET_BYTES:
        raise ValueError("invalid secret file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino)
        if (not stat.S_ISREG(opened.st_mode) or
                (opened.st_dev, opened.st_ino) != identity or
                opened.st_size > MAX_SECRET_BYTES):
            raise ValueError("invalid secret file")
        # Windows lacks O_NOFOLLOW; reject a replaced path after opening too.
        if not hasattr(os, "O_NOFOLLOW"):
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                raise ValueError("invalid secret file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(MAX_SECRET_BYTES + 1)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise ValueError("secret file changed while reading")
        return content
    finally:
        os.close(descriptor)
