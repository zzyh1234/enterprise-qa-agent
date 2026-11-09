"""命令行入口：ask / batch / verify / corpus。"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, config
from .agent import KnowledgeQAAgent
from .analysis import IntentMatcher
from .audit import audit_result
from .corpus import Corpus
from .models import Citation
from .planners import ReplayPlanner
from .runner import load_scenarios, render_result_text, run_all_scenarios

CITATION_FIELDS = ("doc_id", "title", "section_id", "heading", "version_date", "quote", "supports")


def _load_json(path: str | pathlib.Path) -> Dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _display_path(path: pathlib.Path) -> str:
    """输出中尽量显示仓库相对路径，避免把本机绝对路径带进示例。"""
    try:
        return path.resolve().relative_to(config.PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def _load_context(path: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    payload = _load_json(path)
    history = [
        {"role": "user", "content": str(payload.get("question", ""))},
        {"role": "assistant", "content": str(payload.get("answer", ""))},
    ]
    return history, payload


def _save_payload(payload: Dict[str, Any], directory: str) -> None:
    target = pathlib.Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / "output.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "trace.json").write_text(
        json.dumps(payload.get("trace", []), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"已保存：{_display_path(target / 'output.json')}、{_display_path(target / 'trace.json')}",
        file=sys.stderr,
    )


# --------------------------------------------------------------------- ask
def cmd_ask(args: argparse.Namespace) -> int:
    history: List[Dict[str, str]] = []
    prior: Optional[Dict[str, Any]] = None
    if args.context:
        history, prior = _load_context(args.context)

    planner = None
    if args.planner == "replay":
        if not args.replay:
            print("--planner replay 需要同时提供 --replay <decisions.json>", file=sys.stderr)
            return 2
        planner = ReplayPlanner.load(args.replay)
    if args.planner == "openai" and not args.api_key:
        print(
            "警告：未提供 --api-key / EQA_API_KEY，模型接口可能失败；离线评测请使用默认 mock。",
            file=sys.stderr,
        )

    agent = KnowledgeQAAgent(
        corpus_path=args.corpus,
        planner=planner,
        planner_kind=args.planner,
        faults=[],
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
    )
    if args.planner == "openai":
        import os

        if args.api_key:
            os.environ["EQA_API_KEY"] = args.api_key
            agent.planner.api_key = args.api_key
        if args.base_url:
            os.environ["EQA_BASE_URL"] = args.base_url
            agent.planner.base_url = args.base_url.rstrip("/")
        if args.model:
            os.environ["EQA_MODEL"] = args.model
            agent.planner.model = args.model

    result = agent.answer(args.question, history=history, prior_result=prior)
    payload = result.to_dict()
    print(
        json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json
        else render_result_text(payload)
    )
    if args.save:
        _save_payload(payload, args.save)
    return 0


# -------------------------------------------------------------------- batch
def cmd_batch(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        scenarios = [scenario for scenario in scenarios if scenario["id"] in wanted]
        dependencies = {scenario.get("history_from") for scenario in scenarios}
        missing = dependencies - {scenario["id"] for scenario in scenarios} - {None}
        if missing:
            print(
                f"警告：被选场景依赖未执行的场景 {sorted(missing)}，追问将缺少上下文。",
                file=sys.stderr,
            )
    out_root = pathlib.Path(args.out)
    results = run_all_scenarios(scenarios, out_root=out_root)
    for scenario_id, payload in results.items():
        run = payload.get("run", {})
        budget = run.get("budget") or {}
        audit = payload.get("audit") or {}
        print(
            f"{scenario_id}: status={payload['status']} "
            f"citations={len(payload.get('citations', []))} audit={audit.get('passed')} "
            f"tools={budget.get('used_tool_calls')}/{budget.get('max_tool_calls')} "
            f"steps={budget.get('used_steps')}/{budget.get('max_steps')}"
        )
    print(f"已写入：{_display_path(out_root)}")
    return 0


# ------------------------------------------------------------------- verify
def cmd_verify(args: argparse.Namespace) -> int:
    paths: List[pathlib.Path] = []
    for pattern in args.paths:
        matches = [pathlib.Path(item) for item in glob.glob(pattern, recursive=True)]
        if matches:
            paths.extend(matches)
        else:
            candidate = pathlib.Path(pattern)
            if candidate.is_file():
                paths.append(candidate)
    if not paths:
        print("未找到可校验的输出文件", file=sys.stderr)
        return 2

    matcher = IntentMatcher.load()
    failures = 0
    for path in sorted(set(paths)):
        data = _load_json(path)
        corpus_path = (data.get("run") or {}).get("corpus") or args.corpus
        corpus = Corpus.load(corpus_path)
        citations = [
            Citation(**{field: str(item.get(field, "")) for field in CITATION_FIELDS})
            for item in data.get("citations", [])
        ]
        history_questions = [
            item.get("content", "")
            for item in data.get("history", [])
            if item.get("role") == "user"
        ]
        question = " ".join(history_questions + [str(data.get("question", ""))])
        report = audit_result(
            question, str(data.get("answer", "")), citations, matcher, corpus
        )
        status = "PASS" if report["passed"] else "FAIL"
        failures += 0 if report["passed"] else 1
        conflict = report["conflict"]
        print(
            f"[{status}] {_display_path(path)}: status={data.get('status')} "
            f"citations={len(citations)} "
            f"supported={report['citations_supported']} "
            f"conflict_detected={conflict['detected']} acknowledged={conflict['acknowledged']}"
        )
        for note in report["notes"]:
            print(f"    - {note}")
    return 1 if failures else 0


# ------------------------------------------------------------------- corpus
def cmd_corpus(args: argparse.Namespace) -> int:
    corpus = Corpus.load(args.corpus)
    print(json.dumps(corpus.stats(), ensure_ascii=False, indent=2))
    for document in corpus.documents:
        print(
            f"- {document.doc_id} 《{document.title}》 {document.doc_type} "
            f"版本 {document.version_date} 适用 {document.applies_to} "
            f"片段 {len(document.sections)}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eqa",
        description="企业知识问答 Agent：检索 → 读原文 → 冲突查证 → 回答/追问（离线 mock 可跑）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="回答单个问题")
    ask.add_argument("question", help="用户问题")
    ask.add_argument("--corpus", default=str(config.DEFAULT_CORPUS), help="语料 JSON 路径")
    ask.add_argument(
        "--planner", default="mock", choices=["mock", "openai", "replay"], help="决策器类型"
    )
    ask.add_argument("--replay", default=None, help="replay 决策文件（--planner replay 时必填）")
    ask.add_argument("--context", default=None, help="上一轮 output.json，用于上下文追问/冲突确认")
    ask.add_argument("--max-steps", type=int, default=config.DEFAULT_MAX_STEPS)
    ask.add_argument("--max-tool-calls", type=int, default=config.DEFAULT_MAX_TOOL_CALLS)
    ask.add_argument("--json", action="store_true", help="输出完整 JSON（含 trace）")
    ask.add_argument("--save", default=None, help="把 output.json / trace.json 保存到目录")
    ask.add_argument("--api-key", default=None, help="OpenAI 兼容接口 key（勿提交到仓库）")
    ask.add_argument("--base-url", default=None, help="如 https://api.openai.com/v1")
    ask.add_argument("--model", default=None, help="模型名，如 gpt-4o-mini")
    ask.set_defaults(func=cmd_ask)

    batch = sub.add_parser("batch", help="批量跑评测场景并写入 examples/")
    batch.add_argument("--scenarios", default=str(config.DEFAULT_SCENARIOS))
    batch.add_argument("--out", default=str(config.PROJECT_ROOT / "examples"))
    batch.add_argument("--only", default=None, help="逗号分隔的场景 ID 子集")
    batch.set_defaults(func=cmd_batch)

    verify = sub.add_parser("verify", help="校验输出文件的引用与冲突说明")
    verify.add_argument("paths", nargs="+", help="output.json 路径或 glob")
    verify.add_argument("--corpus", default=str(config.DEFAULT_CORPUS))
    verify.set_defaults(func=cmd_verify)

    corpus = sub.add_parser("corpus", help="查看语料统计")
    corpus.add_argument("--corpus", default=str(config.DEFAULT_CORPUS))
    corpus.set_defaults(func=cmd_corpus)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
