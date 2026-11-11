"""引用审计与冲突审计。

- 引用审计：每条引用必须能在对应片段里逐字找到；否则视为编造/改写，答案不可采信。
- 冲突审计：若引用之间对同一属性给出不同取值，答案必须提到全部取值；否则视为「擅自选一个」。
审计对 Mock 与真实模型模式都生效，是模型输出的最后一道防线。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .analysis import IntentMatcher, build_evidence, detect_conflict
from .corpus import Corpus
from .models import Citation


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def audit_citations(citations: Sequence[Citation], corpus: Corpus) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for citation in citations:
        doc = corpus.get(citation.doc_id)
        section = doc.section(citation.section_id) if doc else None
        quote = _squash(citation.quote)
        text = _squash(section.text) if section else ""
        supported = bool(doc is not None and section is not None and quote and quote in text)
        if supported:
            reason = "原文逐字包含引用"
        elif doc is None:
            reason = "文档 ID 不存在"
        elif section is None:
            reason = "片段 ID 不存在"
        else:
            reason = "引用与原文不一致（疑似改写或编造）"
        checks.append(
            {
                "doc_id": citation.doc_id,
                "section_id": citation.section_id,
                "supported": supported,
                "reason": reason,
            }
        )
    return checks


def audit_conflict(
    question: str,
    answer: str,
    citations: Sequence[Citation],
    matcher: IntentMatcher,
    corpus: Corpus,
    extra_sections: Sequence[tuple] = (),
) -> Dict[str, Any]:
    intent = matcher.detect(question)
    evidences = []
    seen = set()
    for citation in citations:
        doc = corpus.get(citation.doc_id)
        section = doc.section(citation.section_id) if doc else None
        if doc is None or section is None:
            continue
        if (doc.doc_id, section.section_id) in seen:
            continue
        seen.add((doc.doc_id, section.section_id))
        evidences.append(build_evidence(doc, section, intent))
    # 模型可能读过冲突条款却只引用其中一份：把「已读片段」也纳入冲突检查
    for doc_id, section_id in extra_sections:
        doc = corpus.get(doc_id)
        section = doc.section(section_id) if doc else None
        if doc is None or section is None or (doc.doc_id, section.section_id) in seen:
            continue
        seen.add((doc.doc_id, section.section_id))
        evidences.append(build_evidence(doc, section, intent))
    conflict = detect_conflict(evidences, intent)
    if conflict is None:
        return {"detected": False, "acknowledged": True, "values": [], "topic": ""}
    values = sorted({value.value for value in conflict.values})
    missing = [value for value in values if value not in answer]
    return {
        "detected": True,
        "acknowledged": not missing,
        "topic": conflict.topic,
        "values": values,
        "doc_ids": sorted({value.doc_id for value in conflict.values}),
        "missing_in_answer": missing,
    }


def audit_result(
    question: str,
    answer: str,
    citations: Sequence[Citation],
    matcher: IntentMatcher,
    corpus: Corpus,
    extra_sections: Sequence[tuple] = (),
) -> Dict[str, Any]:
    citation_checks = audit_citations(citations, corpus)
    conflict_check = audit_conflict(
        question, answer, citations, matcher, corpus, extra_sections=extra_sections
    )
    citations_supported = all(check["supported"] for check in citation_checks)
    conflicts_acknowledged = conflict_check["acknowledged"]
    passed = citations_supported and conflicts_acknowledged
    notes: List[str] = []
    if not citations_supported:
        bad = [f"{c['doc_id']}/{c['section_id']}" for c in citation_checks if not c["supported"]]
        notes.append("引用未通过逐字校验：" + "、".join(bad))
    if not conflicts_acknowledged:
        notes.append(
            "冲突未在答案中说明，缺少取值：" + "、".join(conflict_check["missing_in_answer"])
        )
    return {
        "passed": passed,
        "citations": citation_checks,
        "citations_supported": citations_supported,
        "conflict": conflict_check,
        "notes": notes,
    }
