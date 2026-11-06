import os
from unittest.mock import patch

import pytest

from scripts.safe_secret_file import read_secret_bytes


def test_rejects_replacement_between_path_check_and_open(tmp_path):
    secret = tmp_path / "secret"
    replacement = tmp_path / "replacement"
    secret.write_bytes(b"original-secret-12345")
    replacement.write_bytes(b"replacement-secret-1")
    real_open = os.open

    def swap_then_open(path, flags):
        secret.unlink()
        replacement.replace(secret)
        return real_open(path, flags)

    with patch("scripts.safe_secret_file.os.open", side_effect=swap_then_open):
        with pytest.raises(ValueError, match="invalid secret file"):
            read_secret_bytes(secret)


def test_rejects_secret_changed_during_read(tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"original-secret-12345")
    real_fstat = os.fstat
    calls = 0

    def mutate_before_final_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            secret.write_bytes(b"modified-secret-12345")
            modified = secret.stat()
            os.utime(secret, ns=(modified.st_atime_ns, modified.st_mtime_ns + 2_000_000_000))
        return real_fstat(descriptor)

    with patch("scripts.safe_secret_file.os.fstat", side_effect=mutate_before_final_stat):
        with pytest.raises(ValueError, match="changed while reading"):
            read_secret_bytes(secret)
