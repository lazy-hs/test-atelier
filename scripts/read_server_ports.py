"""Print the configured API and Vite ports for platform launchers.

This helper intentionally uses only the Python standard library so the shell
launchers can read settings before importing the application package.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _port(value: object, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if 1 <= result <= 65535 else fallback


def main() -> None:
    configured = os.getenv("AI_STUDIO_CONFIG_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")).expanduser() / "config" / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    server = payload.get("server", {}) if isinstance(payload, dict) else {}
    if not isinstance(server, dict):
        server = {}
    api_port = _port(os.getenv("AI_STUDIO_PORT") or server.get("port"), 8000)
    web_port = _port(server.get("web_port"), 5173)
    print(f"{api_port} {web_port}")


if __name__ == "__main__":
    main()
