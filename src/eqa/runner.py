"""场景执行与示例落盘：CLI 与测试共用同一套入口。"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .agent import KnowledgeQAAgent
from .models import AgentResult


def load_scenarios(path: Optional[str] = None) -> List[Dict[str, Any]]:
    raw = config.load_json(path or config.DEFAULT_SCENARIOS)
    return list(raw.get("scenarios", []))


def build_history(prior: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not prior:
        return []
    return [
        {"role": "user", "content": str(prior.get("question", ""))},
        {"role": "assistant", "content": str(prior.get("answer", ""))},
    ]


def execute_scenario(
    scenario: Dict[str, Any],
    prior: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Dict[str, str]]] = None,
) -> AgentResult:
    corpus_path = scenario.get("corpus") or str(config.DEFAULT_CORPUS)
    budget = scenario.get("budget") or {}
    planner = None
    if scenario.get("planner") == "replay":
        from .planners import ReplayPlanner

        planner = ReplayPlanner.load(scenario["replay"])
    agent = KnowledgeQAAgent(
        corpus_path=corpus_path,
        planner=planner,
        planner_kind=scenario.get("planner", "mock"),
        faults=scenario.get("tool_faults") or [],
        max_steps=int(budget.get("max_steps", config.DEFAULT_MAX_STEPS)),
        max_tool_calls=int(budget.get("max_tool_calls", config.DEFAULT_MAX_TOOL_CALLS)),
    )
    return agent.answer(
        scenario["question"],
        history=list(history or []),
        prior_result=prior,
        task_id=scenario.get("id"),
    )


def _resolve_order(scenarios: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保证 history_from 的依赖场景先执行。"""
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    ordered: List[Dict[str, Any]] = []
    visiting: set = set()
    done: set = set()

    def visit(scenario_id: str) -> None:
        if scenario_id in done or scenario_id not in by_id:
            return
        if scenario_id in visiting:
            raise ValueError(f"场景依赖存在环：{scenario_id}")
        visiting.add(scenario_id)
        dependency = by_id[scenario_id].get("history_from")
        if dependency:
            visit(dependency)
        visiting.discard(scenario_id)
        done.add(scenario_id)
        ordered.append(by_id[scenario_id])

    for scenario in scenarios:
        visit(scenario["id"])
    return ordered


def run_all_scenarios(
    scenarios: Optional[Sequence[Dict[str, Any]]] = None,
    out_root: Optional[pathlib.Path] = None,
) -> Dict[str, Dict[str, Any]]:
    scenarios = list(scenarios or load_scenarios())
    results: Dict[str, Dict[str, Any]] = {}
    for scenario in _resolve_order(scenarios):
        prior = results.get(scenario.get("history_from") or "")
        result = execute_scenario(scenario, prior=prior, history=build_history(prior))
        payload = result.to_dict()
        results[scenario["id"]] = payload
        if out_root is not None:
            write_example(payload, scenario, out_root / scenario["id"])
    return results


def write_example(
    payload: Dict[str, Any], scenario: Dict[str, Any], directory: pathlib.Path
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "scenario_id": scenario["id"],
        "title": scenario.get("title", ""),
        "question": scenario.get("question", ""),
        "history": payload.get("history", []),
        "prior_scenario": scenario.get("history_from"),
        "corpus": scenario.get("corpus", "data/documents.json"),
        "planner": scenario.get("planner", "mock"),
        "planner_requested": scenario.get(
            "planner_requested", scenario.get("planner", "mock")
        ),
        "planner_fallback": bool(scenario.get("planner_fallback", False)),
        "replay": scenario.get("replay"),
        "budget": scenario.get("budget"),
        "tool_faults": scenario.get("tool_faults", []),
        "coverage": scenario.get("coverage", []),
    }
    (directory / "input.json").write_text(
        json.dumps(input_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_payload = dict(payload)
    run_info = dict(output_payload.get("run") or {})
    run_info["planner_requested"] = scenario.get(
        "planner_requested", scenario.get("planner", "mock")
    )
    run_info["planner_fallback"] = bool(scenario.get("planner_fallback", False))
    output_payload["run"] = run_info
    output_payload["scenario"] = {
        "id": scenario["id"],
        "title": scenario.get("title", ""),
        "coverage": scenario.get("coverage", []),
        "baseline": scenario.get("baseline"),
    }
    (directory / "output.json").write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    trace_payload = {
        "task_id": payload.get("task_id"),
        "scenario_id": scenario["id"],
        "question": payload.get("question"),
        "status": payload.get("status"),
        "planner": payload.get("run", {}).get("planner"),
        "budget": payload.get("run", {}).get("budget"),
        "steps": payload.get("trace", []),
    }
    (directory / "trace.json").write_text(
        json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "trace.md").write_text(
        render_trace_markdown(payload, scenario), encoding="utf-8"
    )


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def render_trace_markdown(payload: Dict[str, Any], scenario: Dict[str, Any]) -> str:
    run = payload.get("run", {})
    budget = run.get("budget") or {}
    audit = payload.get("audit") or {}
    planner_line = f"- Planner：`{run.get('planner')}`"
    if run.get("planner_fallback"):
        planner_line += (
            f"（请求 `{run.get('planner_requested')}`，未配置 EQA_API_KEY，已回退到离线 mock）"
        )
    elif run.get("planner_requested") and run.get("planner_requested") != run.get("planner"):
        planner_line += f"（请求 `{run.get('planner_requested')}`）"
    lines = [
        f"# Trace：{scenario['id']}",
        "",
        f"- 标题：{scenario.get('title', '')}",
        f"- 问题：{payload.get('question', '')}",
        f"- 状态：`{payload.get('status')}`（{payload.get('status_label', '')}）",
        planner_line,
        f"- 语料：`{run.get('corpus')}`",
        f"- 预算：步数 {budget.get('used_steps')}/{budget.get('max_steps')}，"
        f"工具调用 {budget.get('used_tool_calls')}/{budget.get('max_tool_calls')}",
        f"- 审计通过：{audit.get('passed')}",
        "",
        "| 步骤 | 决策依据 | 动作 | 工具参数 | 返回摘要 | 后续动作 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for step in payload.get("trace", []):
        decision = step.get("decision", {})
        observation = step.get("observation", {})
        summary = observation.get("summary", "")
        if observation.get("error"):
            summary = f"{summary}（{observation['error']}）"
        lines.append(
            "| {step} | {reason} | `{action}` | `{args}` | {obs} | {follow} |".format(
                step=step.get("step"),
                reason=_cell(decision.get("reason", "")),
                action=decision.get("action", ""),
                args=_cell(decision.get("arguments", {})),
                obs=_cell(summary),
                follow=_cell(step.get("follow_up", "")),
            )
        )
    lines.extend(["", "## 答案", "", payload.get("answer", ""), ""])
    if payload.get("citations"):
        lines.append("## 引用")
        lines.append("")
        for citation in payload["citations"]:
            lines.append(
                f"- `{citation['doc_id']}/{citation['section_id']}`"
                f"《{citation['title']}》版本 {citation['version_date']}："
                f"「{citation['quote']}」"
            )
        lines.append("")
    if payload.get("conflicts"):
        lines.append("## 冲突")
        lines.append("")
        for conflict in payload["conflicts"]:
            lines.append(
                f"- 主题：{conflict.get('topic')}；resolution={conflict.get('resolution')}；"
                f"selected={conflict.get('selected_doc_id')}；note={conflict.get('note')}"
            )
        lines.append("")
    if payload.get("evidence_gaps"):
        lines.append("## 资料缺口/限制")
        lines.append("")
        lines.extend(f"- {gap}" for gap in payload["evidence_gaps"])
        lines.append("")
    return "\n".join(lines)


def render_result_text(payload: Dict[str, Any]) -> str:
    run = payload.get("run", {})
    budget = run.get("budget") or {}
    audit = payload.get("audit") or {}
    lines = [
        f"任务：{payload.get('task_id')}",
        f"问题：{payload.get('question')}",
        f"状态：{payload.get('status')}（{payload.get('status_label')}）",
        "决策器：" + str(run.get("planner") or "")
        + (
            f"（回退：请求 {run.get('planner_requested')}，未配置 key）"
            if run.get("planner_fallback")
            else ""
        ),
        "",
        "答案：",
        payload.get("answer", ""),
        "",
        f"引用：{len(payload.get('citations', []))} 条，逐字校验通过：{audit.get('citations_supported')}，"
        f"冲突检查：{audit.get('conflict', {}).get('detected')}，整体审计通过：{audit.get('passed')}",
    ]
    for citation in payload.get("citations", []):
        lines.append(
            f"- {citation['doc_id']}/{citation['section_id']}"
            f"（版本 {citation['version_date']}）「{citation['quote']}」"
        )
    if payload.get("clarification"):
        clarification = payload["clarification"]
        lines.extend(
            [
                "",
                f"需要确认：{clarification.get('question')}",
                "可选：" + "；".join(clarification.get("options", [])),
            ]
        )
    if payload.get("evidence_gaps"):
        lines.extend(["", "资料缺口："])
        lines.extend(f"- {gap}" for gap in payload["evidence_gaps"])
    lines.extend(
        [
            "",
            f"预算：步数 {budget.get('used_steps')}/{budget.get('max_steps')}，"
            f"工具调用 {budget.get('used_tool_calls')}/{budget.get('max_tool_calls')}"
            + ("（已超限停止）" if budget.get("exceeded") else ""),
            "执行步骤：",
        ]
    )
    for step in payload.get("trace", []):
        decision = step.get("decision", {})
        observation = step.get("observation", {})
        lines.append(
            f"  {step.get('step')}. [{decision.get('action')}] {decision.get('reason')} "
            f"→ {observation.get('summary')}"
        )
        if step.get("follow_up"):
            lines.append(f"     后续：{step['follow_up']}")
    return "\n".join(lines)
