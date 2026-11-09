"""测试公共设置：把 src 加入 sys.path，并提供场景执行助手。"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DATA = ROOT / "data"
EXAMPLES = ROOT / "examples"


def load_scenario(scenario_id: str) -> Dict[str, Any]:
    raw = json.loads((DATA / "scenarios.json").read_text(encoding="utf-8"))
    for scenario in raw["scenarios"]:
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def run_scenario(
    scenario: Dict[str, Any],
    prior: Optional[Dict[str, Any]] = None,
    history: Optional[list] = None,
) -> Dict[str, Any]:
    from eqa.runner import execute_scenario

    return execute_scenario(scenario, prior=prior, history=history).to_dict()


def run_by_id(scenario_id: str, **kwargs: Any) -> Dict[str, Any]:
    return run_scenario(load_scenario(scenario_id), **kwargs)


def actions(payload: Dict[str, Any]) -> list:
    return [step["decision"]["action"] for step in payload["trace"]]


def write_temp_corpus(tmp_dir: pathlib.Path, mutate) -> pathlib.Path:
    """复制默认语料并交给 mutate 修改，返回临时语料路径。"""
    raw = json.loads((DATA / "documents.json").read_text(encoding="utf-8"))
    mutate(raw)
    target = tmp_dir / "documents_custom.json"
    target.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
