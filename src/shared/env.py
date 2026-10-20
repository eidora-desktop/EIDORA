from __future__ import annotations

import os


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_flag(name: str) -> bool:
    return get_env(name).strip() == "1"
