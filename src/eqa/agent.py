"""企业知识问答 Agent：ReAct 循环 + 预算上限 + 引用/冲突审计。

循环结构（与框架无关）：
    决策 → 执行工具 → 观察结果 → 再决策 … → finish/ask_user
预算：步数与工具调用次数都有硬上限，超限返回明确的未完成状态。
"""
from __future__ import annotations

import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .analysis import IntentMatcher
from .audit import audit_result
from .corpus import Corpus
from .models import (
    STATUS_ANSWERED,
    STATUS_AUDIT_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_FAILED,
    STATUS_LABELS,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_PARTIAL,
    AgentResult,
    Budget,
    Citation,
    Clarification,
    Conflict,
    ConflictValue,
    ToolResult,
    TraceStep,
)
from .planners import (
    SEARCH_CANDIDATE_MIN_SCORE,
    Decision,
    Observation,
    PlannerError,
    PlannerInput,
    build_planner,
)
from .retrieval import QueryExpander, RetrievalIndex
from .tools import KnowledgeTools

AUDITED_STATUSES = {STATUS_ANSWERED, STATUS_PARTIAL}


class KnowledgeQAAgent:
    def __init__(
        self,
        corpus: Optional[Corpus] = None,
        corpus_path: Optional[str] = None,
        planner: Any = None,
        planner_kind: str = "mock",
        matcher: Optional[IntentMatcher] = None,
        faults: Optional[Sequence[Dict[str, Any]]] = None,
        max_steps: int = 8,
        max_tool_calls: int = 8,
        max_planner_retries: int = 1,
    ):
        self.corpus = corpus or Corpus.load(corpus_path)
        self.matcher = matcher or IntentMatcher.load()
        self.index = RetrievalIndex(self.corpus, QueryExpander.load())
        self.planner = planner or build_planner(planner_kind, matcher=self.matcher)
        self.faults = [dict(item) for item in (faults or [])]
        self.max_steps = max(1, int(max_steps))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.max_planner_retries = max(0, int(max_planner_retries))

    # ------------------------------------------------------------------ 主循环
    def answer(
        self,
        question: str,
        history: Optional[Sequence[Dict[str, str]]] = None,
        prior_result: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> AgentResult:
        history = [dict(item) for item in (history or [])]
        budget = Budget(max_steps=self.max_steps, max_tool_calls=self.max_tool_calls)
        tools = KnowledgeTools(self.corpus, self.index, faults=self.faults)
        steps: List[TraceStep] = []
        observations: List[Observation] = []
        self._read_sections: List[tuple] = []
        self._search_candidates: List[tuple] = []
        self._best_search_score: Optional[float] = None
        started = time.perf_counter()
        planner_errors = 0
        task_id = task_id or f"task-{uuid.uuid4().hex[:10]}"

        while True:
            if budget.used_steps >= budget.max_steps:
                budget.exceeded = True
                budget.exceeded_reason = f"步数上限 {budget.max_steps} 已用尽"
                return self._finish_budget(
                    task_id, question, history, steps, observations, budget, started,
                    "步数上限",
                )

            planner_input = PlannerInput(
                question=question,
                history=history,
                prior_result=prior_result,
                observations=observations,
                corpus=self.corpus,
                matcher=self.matcher,
                remaining_tool_calls=max(0, budget.max_tool_calls - budget.used_tool_calls),
                remaining_steps=max(0, budget.max_steps - budget.used_steps),
            )
            step_no = budget.used_steps + 1
            try:
                decision = self.planner.decide(planner_input)
            except PlannerError as exc:
                budget.used_steps += 1
                planner_errors += 1
                steps.append(
                    TraceStep(
                        step=step_no,
                        reason="决策输出不可解析，记录错误并准备纠正",
                        action="planner_error",
                        arguments={},
                        ok=False,
                        observation=str(exc),
                        error=str(exc),
                    )
                )
                observations.append(
                    Observation(
                        step=step_no,
                        action="planner_error",
                        arguments={},
                        result=ToolResult(
                            tool="planner", arguments={}, ok=False, error=str(exc), summary=str(exc)
                        ),
                    )
                )
                if planner_errors > self.max_planner_retries:
                    return self._finish_failed(
                        task_id, question, history, steps, budget, started, str(exc)
                    )
                continue

            if decision.action == "finish":
                budget.used_steps += 1
                return self._finish_from_payload(
                    task_id, question, history, steps, budget, started, decision
                )
            if decision.action == "ask_user":
                budget.used_steps += 1
                return self._finish_clarification(
                    task_id, question, history, steps, budget, started, decision
                )
            if decision.action not in ("search_docs", "read_snippet"):
                budget.used_steps += 1
                return self._finish_failed(
                    task_id, question, history, steps, budget, started,
                    f"非法动作：{decision.action}",
                )

            if budget.used_tool_calls >= budget.max_tool_calls:
                budget.exceeded = True
                budget.exceeded_reason = (
                    f"工具调用上限 {budget.max_tool_calls} 已用尽"
                    f"（下一步需要执行 {decision.action}）"
                )
                return self._finish_budget(
                    task_id, question, history, steps, observations, budget, started,
                    "工具调用上限", pending_decision=decision,
                )

            call_started = time.perf_counter()
            tool_result = tools.call(decision.action, decision.arguments)
            elapsed_ms = int((time.perf_counter() - call_started) * 1000)
            budget.used_steps += 1
            budget.used_tool_calls += 1
            steps.append(
                TraceStep(
                    step=step_no,
                    reason=decision.reason,
                    action=decision.action,
                    arguments=decision.arguments,
                    ok=tool_result.ok,
                    observation=tool_result.summary,
                    error=tool_result.error,
                    elapsed_ms=elapsed_ms,
                )
            )
            observations.append(
                Observation(
                    step=step_no,
                    action=decision.action,
                    arguments=decision.arguments,
                    result=tool_result,
                )
            )
            if decision.action == "read_snippet" and tool_result.ok:
                payload = tool_result.payload or {}
                doc_id = str(payload.get("doc_id", ""))
                section_id = str(payload.get("section_id", ""))
                if doc_id and section_id:
                    self._read_sections.append((doc_id, section_id))
            if decision.action == "search_docs" and tool_result.ok:
                hits = (tool_result.payload or {}).get("hits", []) or []
                if hits:
                    top = float(hits[0].get("score") or 0.0)
                    if self._best_search_score is None or top > self._best_search_score:
                        self._best_search_score = top
                for hit in hits:
                    doc_id = str(hit.get("doc_id", ""))
                    section_id = str(hit.get("section_id", ""))
                    score = float(hit.get("score") or 0.0)
                    if (
                        doc_id
                        and section_id
                        and score >= SEARCH_CANDIDATE_MIN_SCORE
                        and (doc_id, section_id) not in self._search_candidates
                    ):
                        self._search_candidates.append((doc_id, section_id))

    # ------------------------------------------------------------ 结束处理
    def _finish_from_payload(
        self,
        task_id: str,
        question: str,
        history: List[Dict[str, str]],
        steps: List[TraceStep],
        budget: Budget,
        started: float,
        decision: Decision,
    ) -> AgentResult:
        payload = dict(decision.arguments or {})
        status = str(payload.get("status") or STATUS_ANSWERED).strip()
        gaps = self._as_text_list(payload.get("evidence_gaps"))
        if status not in STATUS_LABELS:
            gaps.append(f"返回了未知状态 {status!r}，按失败处理")
            status = STATUS_FAILED
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            answer = "（未提供答案文本）"
            gaps.append("缺少答案文本")
        citations = [self._coerce_citation(item) for item in payload.get("citations") or []]
        conflicts = [self._coerce_conflict(item) for item in payload.get("conflicts") or []]
        notes = self._as_text_list(payload.get("notes"))
        failures = self._as_text_list(payload.get("failures"))
        clarification = self._coerce_clarification(payload.get("clarification"))
        steps.append(
            TraceStep(
                step=budget.used_steps,
                reason=decision.reason,
                action="finish",
                arguments={"status": status, "cited_docs": [c.doc_id for c in citations]},
                ok=status != STATUS_FAILED,
                observation=f"结束：status={status}，引用 {len(citations)} 条",
            )
        )
        result = self._build_result(
            task_id, question, history, status, answer, citations, conflicts,
            gaps, notes, failures, clarification, steps, budget, started,
        )
        return self._apply_audit(result)

    def _finish_clarification(
        self,
        task_id: str,
        question: str,
        history: List[Dict[str, str]],
        steps: List[TraceStep],
        budget: Budget,
        started: float,
        decision: Decision,
    ) -> AgentResult:
        payload = dict(decision.arguments or {})
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            answer = str(payload.get("question") or "需要您补充信息后才能继续。")
        citations = [self._coerce_citation(item) for item in payload.get("citations") or []]
        conflicts = [self._coerce_conflict(item) for item in payload.get("conflicts") or []]
        clarification = self._coerce_clarification(payload.get("clarification"))
        if clarification is None:
            clarification = self._coerce_clarification(payload)
        if clarification is None:
            clarification = Clarification(
                question=str(payload.get("question") or answer),
                options=[str(item) for item in payload.get("options") or []],
                reason=str(payload.get("reason") or ""),
            )
        steps.append(
            TraceStep(
                step=budget.used_steps,
                reason=decision.reason,
                action="ask_user",
                arguments={
                    "question": clarification.question,
                    "options": clarification.options,
                },
                ok=True,
                observation="暂停并向用户提问：" + clarification.question,
            )
        )
        result = self._build_result(
            task_id, question, history, STATUS_NEEDS_CLARIFICATION, answer, citations, conflicts,
            self._as_text_list(payload.get("evidence_gaps")),
            self._as_text_list(payload.get("notes")),
            self._as_text_list(payload.get("failures")),
            clarification, steps, budget, started,
        )
        return self._apply_audit(result)

    def _finish_budget(
        self,
        task_id: str,
        question: str,
        history: List[Dict[str, str]],
        steps: List[TraceStep],
        observations: List[Observation],
        budget: Budget,
        started: float,
        reason: str,
        pending_decision: Optional[Decision] = None,
    ) -> AgentResult:
        citations: List[Citation] = []
        for observation in observations:
            if observation.action != "read_snippet" or not observation.result.ok:
                continue
            payload = observation.result.payload or {}
            citation = self._coerce_citation(
                {**payload, "supports": "达到上限前读取的原文（未完成核验）"}
            )
            duplicate = any(
                item.doc_id == citation.doc_id and item.section_id == citation.section_id
                for item in citations
            )
            if not duplicate:
                citations.append(citation)
        answer = f"已达到执行上限，任务未完成：{budget.exceeded_reason}。"
        if pending_decision is not None:
            answer += (
                f"\n预算用尽时本应执行的下一个动作：{pending_decision.action} "
                f"{pending_decision.arguments}。"
            )
        if citations:
            answer += "\n以下为达到上限前已读取的原文，尚未完成冲突/完整性核验：\n" + "\n".join(
                f"- {item.doc_id}/{item.section_id}：「{item.quote}」" for item in citations
            )
        answer += "\n请提高执行上限或缩小问题范围后重试。"
        gaps = [budget.exceeded_reason, "任务未完成，未给出确定性结论"]
        notes = []
        if pending_decision is not None:
            notes.append(f"计划中的下一步：{pending_decision.action}（预算不足未执行）")
        steps.append(
            TraceStep(
                step=budget.used_steps,
                reason=f"{reason}触发，预算耗尽，循环强制停止",
                action="finish",
                arguments={"status": STATUS_BUDGET_EXCEEDED},
                ok=False,
                observation=budget.exceeded_reason,
            )
        )
        result = self._build_result(
            task_id, question, history, STATUS_BUDGET_EXCEEDED, answer, citations, [],
            gaps, notes, [budget.exceeded_reason], None, steps, budget, started,
        )
        return self._apply_audit(result)

    def _finish_failed(
        self,
        task_id: str,
        question: str,
        history: List[Dict[str, str]],
        steps: List[TraceStep],
        budget: Budget,
        started: float,
        message: str,
    ) -> AgentResult:
        steps.append(
            TraceStep(
                step=budget.used_steps,
                reason="无法继续决策",
                action="finish",
                arguments={"status": STATUS_FAILED},
                ok=False,
                observation=message,
            )
        )
        result = self._build_result(
            task_id, question, history, STATUS_FAILED, f"任务失败：{message}", [], [],
            [message], [], [message], None, steps, budget, started,
        )
        return self._apply_audit(result)

    def _build_result(
        self,
        task_id: str,
        question: str,
        history: List[Dict[str, str]],
        status: str,
        answer: str,
        citations: List[Citation],
        conflicts: List[Conflict],
        gaps: List[str],
        notes: List[str],
        failures: List[str],
        clarification: Optional[Clarification],
        steps: List[TraceStep],
        budget: Budget,
        started: float,
    ) -> AgentResult:
        self._link_follow_ups(steps)
        return AgentResult(
            task_id=task_id,
            question=question,
            status=status,
            answer=answer,
            citations=citations,
            evidence_gaps=gaps,
            conflicts=conflicts,
            clarification=clarification,
            steps=steps,
            budget=budget,
            history=list(history),
            corpus=self._corpus_display_path(),
            planner=str(getattr(self.planner, "name", "unknown")),
            model=getattr(self.planner, "model", None),
            failures=failures,
            notes=notes,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------ 结果规范化
    def _corpus_display_path(self) -> str:
        """输出里尽量使用仓库相对路径，方便别人 clone 后直接 verify。"""
        source = str(self.corpus.source or "")
        if not source:
            return ""
        try:
            return pathlib.Path(source).resolve().relative_to(config.PROJECT_ROOT).as_posix()
        except (ValueError, OSError):
            return source

    def _coerce_citation(self, raw: Any) -> Citation:
        if not isinstance(raw, dict):
            return Citation(
                doc_id="",
                title="",
                section_id="",
                heading="",
                version_date="",
                quote=str(raw or ""),
                supports="格式非法的引用（已拒绝）",
            )
        item = dict(raw)
        doc_id = str(item.get("doc_id", "") or "")
        section_id = str(item.get("section_id", "") or "")
        doc = self.corpus.get(doc_id)
        section = doc.section(section_id) if doc else None
        return Citation(
            doc_id=doc_id,
            title=str(item.get("title") or (doc.title if doc else "")),
            section_id=section_id,
            heading=str(item.get("heading") or (section.heading if section else "")),
            version_date=str(item.get("version_date") or (doc.version_date if doc else "")),
            quote=str(item.get("quote") or (section.text if section else "")),
            supports=str(item.get("supports") or ""),
        )

    def _coerce_conflict(self, raw: Any) -> Conflict:
        if not isinstance(raw, dict):
            return Conflict(
                topic="",
                values=[],
                resolution="unresolved",
                note=f"模型返回了格式非法的冲突描述（已忽略）：{str(raw)[:200]}",
            )
        item = dict(raw)
        values: List[ConflictValue] = []
        for raw_value in item.get("values") or []:
            if not isinstance(raw_value, dict):
                continue
            value = dict(raw_value)
            doc = self.corpus.get(str(value.get("doc_id", "")))
            section = doc.section(str(value.get("section_id", ""))) if doc else None
            values.append(
                ConflictValue(
                    doc_id=str(value.get("doc_id", "")),
                    title=str(value.get("title") or (doc.title if doc else "")),
                    section_id=str(value.get("section_id", "")),
                    version_date=str(value.get("version_date") or (doc.version_date if doc else "")),
                    value=str(value.get("value", "")),
                    quote=str(value.get("quote") or (section.text if section else "")),
                )
            )
        return Conflict(
            topic=str(item.get("topic", "")),
            values=values,
            resolution=str(item.get("resolution") or "unresolved"),
            selected_doc_id=item.get("selected_doc_id"),
            note=str(item.get("note") or ""),
        )

    @staticmethod
    def _as_text_list(raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw]
        return [str(raw)]

    @staticmethod
    def _coerce_clarification(raw: Any) -> Optional[Clarification]:
        if not raw:
            return None
        if isinstance(raw, str):
            return Clarification(question=raw)
        if not isinstance(raw, dict):
            return None
        item = dict(raw)
        question = str(item.get("question") or "")
        if not question:
            return None
        raw_options = item.get("options")
        if isinstance(raw_options, (list, tuple)):
            options = [str(option) for option in raw_options]
        elif raw_options:
            options = [str(raw_options)]
        else:
            options = []
        return Clarification(
            question=question,
            options=options,
            reason=str(item.get("reason") or ""),
        )

    # ------------------------------------------------------------------ 审计
    def _apply_audit(self, result: AgentResult) -> AgentResult:
        previous = [item.get("content", "") for item in result.history if item.get("role") == "user"]
        contextual = " ".join(previous + [result.question]).strip()
        audit = audit_result(
            contextual,
            result.answer,
            result.citations,
            self.matcher,
            self.corpus,
            extra_sections=getattr(self, "_read_sections", ()),
        )
        result.audit = audit
        if result.status not in AUDITED_STATUSES:
            return result

        problems = list(audit["notes"])
        problems.extend(self._unverified_conflicts(result, contextual))
        if (
            self._best_search_score is not None
            and self._best_search_score < SEARCH_CANDIDATE_MIN_SCORE
        ):
            problems.append(
                f"检索置信度不足（最高分 {self._best_search_score:.2f} < "
                f"{SEARCH_CANDIDATE_MIN_SCORE:.0f}），不接受 answered"
            )
        if not result.citations:
            problems.append("状态为 answered/partial 但没有提供任何逐字引用")
        if problems:
            result.status = STATUS_AUDIT_FAILED
            result.audit["passed"] = False
            result.audit.setdefault("notes", []).extend(problems)
            note = "；".join(dict.fromkeys(problems))
            result.answer += f"\n\n【审计未通过】{note}。该答案不可采信，需人工复核。"
            result.evidence_gaps.extend(dict.fromkeys(problems))
        return result

    def _unverified_conflicts(self, result: AgentResult, question: str) -> List[str]:
        """检索到高分文档但从未读取，且其取值与已引用条款不同 → 不能算已回答。

        这是对「模型只读了其中一份就下结论」的确定性兜底：
        即使引用本身逐字正确，只要存在未核实的高分不同取值，就不接受 answered。
        """
        intent = self.matcher.detect(question)
        if intent is None or not intent.value_patterns or not result.citations:
            return []
        read = set(getattr(self, "_read_sections", ()))
        cited_docs = {citation.doc_id for citation in result.citations}
        cited_values = set()
        for citation in result.citations:
            doc = self.corpus.get(citation.doc_id)
            section = doc.section(citation.section_id) if doc else None
            if doc is not None and section is not None:
                cited_values.update(value for value, _ in intent.extract_values(section.text))
        if not cited_values:
            return []

        notes: List[str] = []
        for doc_id, section_id in getattr(self, "_search_candidates", ()):
            if doc_id in cited_docs or (doc_id, section_id) in read:
                continue
            doc = self.corpus.get(doc_id)
            section = doc.section(section_id) if doc else None
            if doc is None or section is None or not intent.has_strong_evidence(section.text):
                continue
            differing = sorted(
                {value for value, _ in intent.extract_values(section.text)} - cited_values
            )
            if differing:
                notes.append(
                    f"存在未核实的潜在冲突：{doc_id}/{section_id} 含不同取值 "
                    f"{'、'.join(differing)}，但本次未读取核实"
                )
        return notes

    @staticmethod
    def _link_follow_ups(steps: List[TraceStep]) -> None:
        for index, step in enumerate(steps[:-1]):
            following = steps[index + 1]
            if following.action == "finish":
                status = following.arguments.get("status", "")
                step.follow_up = f"结束（{status}）：{following.reason}"
            else:
                step.follow_up = f"{following.action}：{following.reason}"
        if steps:
            steps[-1].follow_up = "任务结束"
