#!/usr/bin/env python3
"""Rotate an existing Test Atelier administrator password.

The password is entered through a TTY (or read from stdin with the explicit
``--password-stdin`` switch); it is never accepted as a command-line value.
The configured password file and the database hash are updated together as
closely as the two storage systems allow, with the file restored if the DB
update fails.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "runtime.json"
# Direct script execution puts ``scripts`` (not the repository root) on
# sys.path; add the root before importing the application package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_config(path: Path) -> tuple[dict[str, object], Path, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or int(document.get("schema_version", 0)) != 1:
        raise ValueError("runtime.json schema 不受支持")
    atelier = document.get("test_atelier")
    if not isinstance(atelier, dict):
        raise ValueError("runtime.json 缺少 test_atelier 配置")
    password_file = atelier.get("admin_password_file")
    username = str(atelier.get("admin_user") or "admin").strip() or "admin"
    if not isinstance(password_file, str) or not password_file.strip():
        raise ValueError("runtime.json 未配置 admin_password_file")
    target = Path(password_file).expanduser()
    if not target.is_absolute():
        raise ValueError("admin_password_file 必须是绝对路径")
    return document, target, username


def _install_environment(document: dict[str, object]) -> None:
    # Importing api.db after this mapping makes it use the same JSON-selected
    # SQLite/PostgreSQL database as the API process.
    from api.runtime_config import environment_from_config

    values = environment_from_config(document, load_model_key=False)
    # api.db calls install_runtime_config() during import; point that internal
    # load at the same explicitly selected file instead of the repository
    # default when --config is used for maintenance or recovery.
    for name, value in values.items():
        os.environ[name] = value


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        value = sys.stdin.read()
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        return value
    return getpass.getpass("新管理员密码：")


def rotate(config_path: Path, username: str | None = None, *, password_stdin: bool = False) -> None:
    document, password_file, configured_username = _load_config(config_path)
    os.environ["TEST_ATELIER_RUNTIME_CONFIG"] = str(config_path)
    target_username = (username or configured_username).strip()
    if not target_username:
        raise ValueError("管理员用户名不能为空")
    password = _read_password(argparse.Namespace(password_stdin=password_stdin))
    if len(password) < 6 or any(char in password for char in "\r\n\x00"):
        raise ValueError("管理员密码至少需要 6 个字符，且不能包含换行或 NUL")
    if not password.strip():
        raise ValueError("管理员密码不能全为空白字符")
    if not password_stdin:
        confirmation = getpass.getpass("再次输入管理员密码：")
        if not secrets.compare_digest(password, confirmation):
            raise ValueError("两次输入的管理员密码不一致")

    old_content = password_file.read_bytes() if password_file.exists() else None
    _write_secret(password_file, password)
    try:
        _install_environment(document)
        from argon2 import PasswordHasher
        from sqlalchemy import select
        from api.db import SessionLocal, User

        with SessionLocal.begin() as db:
            user = db.scalar(select(User).where(User.username == target_username))
            if user is None:
                raise ValueError(f"管理员用户不存在：{target_username}")
            user.password_hash = PasswordHasher().hash(password)
            if user.role != "admin":
                raise ValueError(f"目标用户不是管理员：{target_username}")
    except Exception:
        if old_content is None:
            password_file.unlink(missing_ok=True)
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{password_file.name}.restore.", dir=password_file.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(old_content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, password_file)
            finally:
                Path(temporary).unlink(missing_ok=True)
        raise

    print(f"管理员密码已轮换：{target_username}")
    print(f"密码文件：{password_file}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="runtime.json 路径")
    parser.add_argument("--username", help="要轮换的管理员用户名，默认使用 runtime.json 的 admin_user")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读取一次密码，不进行交互确认")
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().resolve()
        os.chdir(ROOT)
        rotate(config_path, args.username, password_stdin=args.password_stdin)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"管理员密码轮换失败：{exc}", file=sys.stderr)
        return 2
    except Exception:
        # Do not expose database URLs, driver details, or other deployment
        # metadata. The transaction and password-file rollback already ran.
        print("管理员密码轮换失败：数据库更新未完成", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
