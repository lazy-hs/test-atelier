"""Private filesystem helpers for uploaded and generated project artifacts."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def is_link_like(path: Path) -> bool:
    """Reject both symlinks and Windows directory junctions at trust boundaries."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def ensure_private_directory(path: Path) -> Path:
    """Create a directory and remove group/other access where supported."""
    if is_link_like(path) or is_link_like(path.parent):
        raise OSError(f"目录不允许是符号链接或 junction：{path}")
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # Some mounted filesystems do not expose chmod; the caller still gets
        # normal filesystem errors if writing is not allowed.
        pass
    return path


def _reject_symlink_components(root: Path | None, path: Path, *, include_leaf: bool = True) -> None:
    """Reject symlinked components between a trusted storage root and path."""
    if root is None:
        return
    root = Path(root)
    path = Path(path)
    if is_link_like(root) or is_link_like(root.parent):
        raise OSError(f"存储根目录不允许是符号链接或 junction：{root}")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise OSError("目标路径必须位于存储根目录内") from exc
    if any(component in {".", ".."} for component in relative.parts):
        raise OSError("目标路径不能包含目录回退")
    current = root
    components = relative.parts if include_leaf else relative.parts[:-1]
    for component in components:
        current /= component
        if is_link_like(current):
            raise OSError(f"存储路径不允许包含符号链接或 junction：{current}")


def _fsync_directory(path: Path) -> None:
    """Persist a completed rename when the filesystem exposes directory FDs."""
    flags = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except (AttributeError, NotImplementedError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_private_atomic(path: Path, data: bytes, *, root: Path | None = None) -> None:
    """Write bytes atomically with private permissions before publication."""
    # The final artifact may already be a symlink from a previous unsafe
    # version; os.replace replaces that directory entry without following it.
    # Intermediate components must never redirect the write outside root.
    _reject_symlink_components(root, path, include_leaf=False)
    ensure_private_directory(path.parent)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        # Use an exclusive, randomly named file. A predictable ``.tmp`` path
        # could be pre-created as a symlink and make the write follow it.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def is_private_regular_file(path: Path) -> bool:
    """Return whether an artifact is a regular file, not a symlink."""
    try:
        return not is_link_like(path) and path.is_file()
    except OSError:
        return False


def remove_private_file(path: Path, *, root: Path) -> None:
    """Delete an owned artifact only when no path component redirects outside root."""
    _reject_symlink_components(root, path)
    if path.exists() and not path.is_file():
        raise OSError("私有文件不是普通文件")
    path.unlink(missing_ok=True)


def open_private_regular_file(path: Path, *, root: Path | None = None):
    """Open an artifact without following a symlink at open time.

    The descriptor is opened before a response is returned, closing the
    check-then-open race that a separate ``is_file`` check would leave.
    Callers own the returned file object and must close it after streaming.
    """
    _reject_symlink_components(root, path)
    if is_link_like(path):
        raise OSError("私有文件不允许是符号链接或 junction")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("私有文件必须是普通文件")
        return os.fdopen(descriptor, "rb", closefd=True), metadata.st_size
    except Exception:
        os.close(descriptor)
        raise
