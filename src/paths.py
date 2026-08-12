from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """
    推导仓库根目录（用于默认路径）。

    约定：
    - 若设置了 POLYMARKET_HOME，则以其为准
    - 否则以当前工作目录为准（CI/本地启动时通常就是仓库根）
    """
    env = os.getenv("POLYMARKET_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def configs_dir() -> Path:
    return repo_root() / "configs"


def data_dir() -> Path:
    return repo_root() / "data"


def logs_dir() -> Path:
    return repo_root() / "logs"


def tmp_dir() -> Path:
    return repo_root() / "tmp"


def halt_dir() -> Path:
    return tmp_dir() / "halt"


def backtest_out_dir() -> Path:
    return data_dir() / "backtest_out"


def backtest_cache_dir() -> Path:
    return tmp_dir() / "backtest_cache"

