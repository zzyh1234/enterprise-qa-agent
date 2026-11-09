"""业务工具：search_docs（文档检索）与 read_snippet（片段读取）。

工具只负责「取资料」，不做任何结论；结论由 Planner（模型/规则）给出。
故障注入用于演示工具失败场景：mode=error/timeout 返回失败，mode=empty 返回空结果。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .corpus import Corpus
from .models import ToolResult
from .retrieval import DocHit, RetrievalIndex


class ToolError(Exception):
    """工具参数错误或可预期的业务错误；由 call() 转为失败结果交给 Planner 观察。"""


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "search_docs",
        "description": "在制度/FAQ 语料中做关键词检索，返回命中文档与最佳片段的摘要（不含全文）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词；可先用原问题，置信度低时改写"},
                "top_k": {"type": "integer", "description": "返回文档数，1-10，默认 5"},
                "use_expansion": {
                    "type": "boolean",
                    "description": "是否启用同义词改写（检索置信度低时置 true）",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_snippet",
        "description": "按 doc_id + section_id 读取原文片段，返回逐字文本与文档元数据（版本日期/适用范围/优先级）。",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "文档 ID，如 D01"},
                "section_id": {"type": "string", "description": "片段 ID，如 D01-4.2"},
            },
            "required": ["doc_id", "section_id"],
        },
    },
]


class KnowledgeTools:
    def __init__(
        self,
        corpus: Corpus,
        index: Optional[RetrievalIndex] = None,
        faults: Optional[List[Dict[str, Any]]] = None,
        preview_chars: int = 90,
    ):
        self.corpus = corpus
        self.index = index or RetrievalIndex(corpus)
        self.faults = [dict(item) for item in (faults or [])]
        self.preview_chars = preview_chars
        self.triggered: List[str] = []

    def fault_report(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.faults]

    # 对外唯一入口 ---------------------------------------------------------
    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        arguments = dict(arguments or {})
        fault = self._take_fault(name)
        if fault is not None:
            mode = str(fault.get("mode", "error"))
            message = str(fault.get("message") or f"{name} 服务不可用（模拟故障）")
            self.triggered.append(f"{name}:{mode}")
            if mode == "empty":
                payload = {
                    "query": arguments.get("query", ""),
                    "query_used": arguments.get("query", ""),
                    "expansion_added": [],
                    "total": 0,
                    "hits": [],
                    "note": "工具返回空结果（模拟故障）",
                }
                return ToolResult(
                    tool=name,
                    arguments=arguments,
                    ok=True,
                    payload=payload,
                    summary="命中 0 篇文档（模拟空结果）",
                    fault_injected=mode,
                )
            return ToolResult(
                tool=name,
                arguments=arguments,
                ok=False,
                error=message,
                summary=f"{name} 调用失败：{message}",
                fault_injected=mode,
            )

        try:
            if name == "search_docs":
                return self._search_docs(arguments)
            if name == "read_snippet":
                return self._read_snippet(arguments)
            raise ToolError(f"未知工具 {name}；可用工具：search_docs、read_snippet")
        except ToolError as exc:
            return ToolResult(
                tool=name,
                arguments=arguments,
                ok=False,
                error=str(exc),
                summary=f"{name} 调用失败：{exc}",
            )

    # 工具实现 -------------------------------------------------------------
    def _search_docs(self, arguments: Dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("search_docs 需要非空 query")
        try:
            top_k = int(arguments.get("top_k") or 5)
        except (TypeError, ValueError):
            raise ToolError("top_k 必须是整数")
        top_k = max(1, min(top_k, 10))
        use_expansion = bool(arguments.get("use_expansion", False))
        if use_expansion:
            used_query, added = self.index.expander.expand(query)
        else:
            used_query, added = query, []
        hits = self.index.search_docs(used_query, top_k=top_k)
        payload = {
            "query": query,
            "query_used": used_query,
            "expansion_added": added,
            "total": len(hits),
            "hits": [self._hit_to_dict(hit) for hit in hits],
        }
        if hits:
            summary = (
                f"命中 {len(hits)} 篇文档，最高分 {hits[0].score:.2f}："
                + "、".join(f"{hit.doc.doc_id}({hit.score:.2f})" for hit in hits)
            )
        else:
            summary = f"未命中任何文档（查询：{used_query}）"
        return ToolResult(
            tool="search_docs", arguments=arguments, ok=True, payload=payload, summary=summary
        )

    def _read_snippet(self, arguments: Dict[str, Any]) -> ToolResult:
        doc_id = str(arguments.get("doc_id") or "").strip()
        section_id = arguments.get("section_id")
        section_id = str(section_id).strip() if section_id else ""
        if not doc_id:
            raise ToolError("read_snippet 需要 doc_id")
        doc = self.corpus.get(doc_id)
        if doc is None:
            available = "、".join(d.doc_id for d in self.corpus.documents)
            raise ToolError(f"文档不存在：{doc_id}；可用文档：{available}")
        if not section_id:
            raise ToolError(
                f"read_snippet 需要 section_id；{doc_id} 可用片段：{'、'.join(doc.section_ids())}"
            )
        section = doc.section(section_id)
        if section is None:
            raise ToolError(
                f"片段不存在：{doc_id}/{section_id}；该文档可用片段：{'、'.join(doc.section_ids())}"
            )
        payload = {
            **doc.meta(),
            "section_id": section.section_id,
            "heading": section.heading,
            "text": section.text,
            "sections_in_doc": doc.section_ids(),
        }
        return ToolResult(
            tool="read_snippet",
            arguments=arguments,
            ok=True,
            payload=payload,
            summary=f"读取 {doc_id}/{section.section_id}《{section.heading}》共 {len(section.text)} 字",
        )

    # 内部 -----------------------------------------------------------------
    def _hit_to_dict(self, hit: DocHit) -> Dict[str, Any]:
        return {
            **hit.doc.meta(),
            "score": round(hit.score, 3),
            "section_id": hit.section.section_id,
            "heading": hit.section.heading,
            "preview": hit.section.text[: self.preview_chars],
            "matched_terms": hit.matched_terms,
            "section_scores": hit.section_scores,
        }

    def _take_fault(self, name: str) -> Optional[Dict[str, Any]]:
        for fault in self.faults:
            if fault.get("tool") != name:
                continue
            remaining = int(fault.get("remaining", 1))
            if remaining <= 0:
                continue
            fault["remaining"] = remaining - 1
            return fault
        return None
