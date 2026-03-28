from __future__ import annotations

import json
from pathlib import Path

from constants import CONFIG_PATH


def load_app_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_app_config(config: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_last_start_dir() -> Path | None:
    data = load_app_config()
    value = data.get("last_start_dir")
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.exists() and path.is_dir():
        return path
    return None


def save_last_start_dir(path: Path):
    config = load_app_config()
    config["last_start_dir"] = str(path.resolve())
    save_app_config(config)
