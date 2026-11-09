"""路径与配置解析。所有相对路径默认相对仓库根目录，避免依赖当前工作目录。"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_CORPUS = DATA_DIR / "documents.json"
DEFAULT_INTENTS = DATA_DIR / "intents.json"
DEFAULT_EXPANSIONS = DATA_DIR / "query_expansions.json"
DEFAULT_SCENARIOS = DATA_DIR / "scenarios.json"

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOOL_CALLS = 8


def resolve_path(value: str | os.PathLike[str]) -> pathlib.Path:
    """把相对路径解析到仓库根目录，绝对路径原样返回。"""
    path = pathlib.Path(value)
    if path.is_absolute():
        return path
    candidate = PROJECT_ROOT / path
    if candidate.exists():
        return candidate
    return pathlib.Path.cwd() / path


def load_json(path: str | os.PathLike[str]) -> Any:
    return json.loads(resolve_path(path).read_text(encoding="utf-8"))
