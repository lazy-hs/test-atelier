import os

import pytest

from api.storage import ensure_private_directory, is_private_regular_file, open_private_regular_file, write_private_atomic


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable on Windows")
def test_private_atomic_write_sets_file_and_directory_modes(tmp_path):
    target = tmp_path / "nested" / "artifact.bin"
    write_private_atomic(target, b"secret")
    assert target.read_bytes() == b"secret"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_private_atomic_write_ignores_predictable_temp_symlink(tmp_path):
    target = tmp_path / "artifact.bin"
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay unchanged", encoding="utf-8")
    predictable = tmp_path / "artifact.bin.tmp"
    predictable.symlink_to(outside)

    from api.storage import write_private_atomic
    write_private_atomic(target, b"private")

    assert target.read_bytes() == b"private"
    assert outside.read_text(encoding="utf-8") == "must stay unchanged"
    assert predictable.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable on Windows")
def test_private_directory_is_idempotent(tmp_path):
    directory = ensure_private_directory(tmp_path / "data")
    assert directory.is_dir()
    ensure_private_directory(directory)
    assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_private_directory_rejects_symlink_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError, match="符号链接"):
        ensure_private_directory(linked)


def test_private_regular_file_rejects_symlink(tmp_path):
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"private")
    assert is_private_regular_file(target)
    linked = tmp_path / "linked.bin"
    linked.symlink_to(target)
    assert not is_private_regular_file(linked)


def test_open_private_regular_file_reads_descriptor_before_streaming(tmp_path):
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"private")
    stream, size = open_private_regular_file(target)
    try:
        assert size == 7
        assert stream.read() == b"private"
    finally:
        stream.close()


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_open_private_regular_file_rejects_symlink(tmp_path):
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"private")
    linked = tmp_path / "linked.bin"
    linked.symlink_to(target)
    with pytest.raises(OSError):
        open_private_regular_file(linked)


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_storage_rejects_symlinked_intermediate_component(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_project = root / "project-1"
    linked_project.symlink_to(outside, target_is_directory=True)
    target = linked_project / "artifact.bin"

    with pytest.raises(OSError, match="符号链接"):
        write_private_atomic(target, b"must stay inside", root=root)
    outside_target = outside / "artifact.bin"
    outside_target.write_bytes(b"private")
    with pytest.raises(OSError, match="符号链接"):
        open_private_regular_file(target, root=root)


def test_storage_rejects_path_escape_from_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    escaped = root / ".." / "outside.bin"
    with pytest.raises(OSError, match="目录回退"):
        write_private_atomic(escaped, b"must stay inside", root=root)
