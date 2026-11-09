"""数据结构：语料、工具调用、Trace 与最终结果。

设计说明：
- TraceStep 只保留「决策依据 + 工具参数 + 返回摘要 + 后续动作」，不记录模型内部思维过程。
- AgentResult 是可序列化的单一结果对象，CLI / 批量评测 / 测试都复用它。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 最终任务状态 -------------------------------------------------------------
STATUS_ANSWERED = "answered"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
STATUS_BUDGET_EXCEEDED = "incomplete_budget_exceeded"
STATUS_AUDIT_FAILED = "audit_failed"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_ANSWERED: "已回答",
    STATUS_NEEDS_CLARIFICATION: "需要澄清（存在未解决冲突或缺少上下文）",
    STATUS_INSUFFICIENT_EVIDENCE: "资料不足，未回答",
    STATUS_PARTIAL: "部分完成（有已核验证据，也有缺口）",
    STATUS_BLOCKED: "未完成（工具失败，无可用证据）",
    STATUS_BUDGET_EXCEEDED: "未完成（达到执行上限）",
    STATUS_AUDIT_FAILED: "未通过引用/冲突审计",
    STATUS_FAILED: "失败",
}


@dataclass
class Section:
    section_id: str
    heading: str
    text: str

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Section":
        return cls(
            section_id=str(raw["section_id"]),
            heading=str(raw.get("heading", "")),
            text=str(raw.get("text", "")),
        )


@dataclass
class Document:
    doc_id: str
    title: str
    doc_type: str
    version_date: str
    owner: str
    applies_to: str
    priority: Optional[int]
    sections: List[Section] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Document":
        return cls(
            doc_id=str(raw["doc_id"]),
            title=str(raw.get("title", "")),
            doc_type=str(raw.get("doc_type", "policy")),
            version_date=str(raw.get("version_date", "")),
            owner=str(raw.get("owner", "")),
            applies_to=str(raw.get("applies_to", "")),
            priority=raw.get("priority"),
            sections=[Section.from_dict(s) for s in raw.get("sections", [])],
            tags=[str(t) for t in raw.get("tags", [])],
        )

    def section(self, section_id: str) -> Optional[Section]:
        for item in self.sections:
            if item.section_id == section_id:
                return item
        return None

    def section_ids(self) -> List[str]:
        return [s.section_id for s in self.sections]

    def meta(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "version_date": self.version_date,
            "owner": self.owner,
            "applies_to": self.applies_to,
            "priority": self.priority,
        }


@dataclass
class ToolResult:
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    payload: Any = None
    summary: str = ""
    error: Optional[str] = None
    fault_injected: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "summary": self.summary,
        }
        if self.error:
            data["error"] = self.error
        if self.fault_injected:
            data["fault_injected"] = self.fault_injected
        return data


@dataclass
class TraceStep:
    step: int
    reason: str
    action: str
    arguments: Dict[str, Any]
    ok: bool
    observation: str
    error: Optional[str] = None
    follow_up: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "decision": {
                "action": self.action,
                "arguments": self.arguments,
                "reason": self.reason,
            },
            "observation": {
                "ok": self.ok,
                "summary": self.observation,
                **({"error": self.error} if self.error else {}),
            },
            "follow_up": self.follow_up,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class Citation:
    doc_id: str
    title: str
    section_id: str
    heading: str
    version_date: str
    quote: str
    supports: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "section_id": self.section_id,
            "heading": self.heading,
            "version_date": self.version_date,
            "quote": self.quote,
            "supports": self.supports,
        }


@dataclass
class ConflictValue:
    doc_id: str
    title: str
    section_id: str
    version_date: str
    value: str
    quote: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "section_id": self.section_id,
            "version_date": self.version_date,
            "value": self.value,
            "quote": self.quote,
        }


@dataclass
class Conflict:
    topic: str
    values: List[ConflictValue] = field(default_factory=list)
    resolution: str = "unresolved"
    selected_doc_id: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "values": [v.to_dict() for v in self.values],
            "resolution": self.resolution,
            "selected_doc_id": self.selected_doc_id,
            "note": self.note,
        }


@dataclass
class Clarification:
    question: str
    options: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "options": self.options,
            "reason": self.reason,
        }


@dataclass
class Budget:
    max_steps: int
    max_tool_calls: int
    used_steps: int = 0
    used_tool_calls: int = 0
    exceeded: bool = False
    exceeded_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "used_steps": self.used_steps,
            "used_tool_calls": self.used_tool_calls,
            "exceeded": self.exceeded,
            "exceeded_reason": self.exceeded_reason,
        }


@dataclass
class AgentResult:
    task_id: str
    question: str
    status: str
    answer: str
    citations: List[Citation] = field(default_factory=list)
    evidence_gaps: List[str] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    clarification: Optional[Clarification] = None
    steps: List[TraceStep] = field(default_factory=list)
    budget: Optional[Budget] = None
    audit: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, str]] = field(default_factory=list)
    corpus: str = ""
    planner: str = "mock"
    model: Optional[str] = None
    failures: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "history": self.history,
            "status": self.status,
            "status_label": self.status_label(),
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "evidence_gaps": self.evidence_gaps,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "audit": self.audit,
            "run": {
                "planner": self.planner,
                "model": self.model,
                "corpus": self.corpus,
                "budget": self.budget.to_dict() if self.budget else None,
                "tool_calls": sum(
                    1 for s in self.steps if s.action in ("search_docs", "read_snippet")
                ),
                "elapsed_ms": self.elapsed_ms,
                "failures": self.failures,
                "notes": self.notes,
            },
            "trace": [s.to_dict() for s in self.steps],
        }
