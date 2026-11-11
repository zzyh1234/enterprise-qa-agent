"""决策器（Planner）：把「观察 → 下一步动作」的决策从执行循环里解耦。

三种实现：
- MockPlanner：离线规则决策器（默认）。用确定性规则模拟「模型读结果→改动作」，
  只验证流程与校验逻辑，不代表真实模型的决策质量。
- OpenAICompatiblePlanner：调用 OpenAI Chat Completions 兼容接口，由真实模型输出 JSON 动作；
  可用 EQA_BASE_URL / EQA_API_KEY / EQA_MODEL 配置，也可注入 transport 做离线测试。
- ReplayPlanner：回放预先记录的决策序列，用于离线回归测试循环与校验逻辑。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .analysis import (
    IntentMatcher,
    IntentRule,
    annual_leave_tier,
    build_evidence,
    detect_conflict,
    detect_user_choice,
    parse_precedence,
    parse_years,
)
from .corpus import Corpus
from .models import (
    STATUS_ANSWERED,
    STATUS_BLOCKED,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_NEEDS_CLARIFICATION,
    Citation,
    Clarification,
    Conflict,
    ToolResult,
)
from .text import normalize, split_sentences
from .tools import TOOL_SPECS

ALLOWED_ACTIONS = {"search_docs", "read_snippet", "ask_user", "finish"}
MAX_SECTION_READS_PER_DOC = 2
MAX_CANDIDATE_DOCS = 3


class PlannerError(Exception):
    """决策不可解析或动作非法；由 Agent 记录并触发一次纠正。"""


@dataclass
class Decision:
    action: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class Observation:
    step: int
    action: str
    arguments: Dict[str, Any]
    result: ToolResult


@dataclass
class PlannerInput:
    question: str
    history: List[Dict[str, str]]
    prior_result: Optional[Dict[str, Any]]
    observations: List[Observation]
    corpus: Corpus
    matcher: IntentMatcher
    remaining_tool_calls: int = 0
    remaining_steps: int = 0

    def contextual_question(self) -> str:
        """把上一轮用户问题与当前问题拼接，用于识别追问指向的主题。"""
        previous = [h.get("content", "") for h in self.history if h.get("role") == "user"]
        if previous:
            return f"{previous[-1]} {self.question}".strip()
        return self.question.strip()

    def detect_intent(self) -> Optional[IntentRule]:
        return self.matcher.detect(self.contextual_question()) or self.matcher.detect(self.question)

class MockPlanner:
    """离线规则决策器（默认）。规则是「模型策略」的替身，仅验证流程与校验逻辑。"""

    name = "mock"

    def __init__(
        self,
        matcher: Optional[IntentMatcher] = None,
        min_score: float = 15.0,
        rewrite_threshold: float = 30.0,
        intentless_min_score: float = 60.0,
        max_candidates: int = MAX_CANDIDATE_DOCS,
    ):
        self.matcher = matcher or IntentMatcher.load()
        self.min_score = min_score
        self.rewrite_threshold = rewrite_threshold
        self.intentless_min_score = intentless_min_score
        self.max_candidates = max_candidates

    def _threshold(self, intent: Optional[IntentRule]) -> float:
        return self.min_score if intent is not None else self.intentless_min_score

    # ------------------------------------------------------------------ 入口
    def decide(self, inp: PlannerInput) -> Decision:
        intent = inp.detect_intent()
        question = inp.contextual_question()

        context_decision = self._context_choice(inp, intent, question)
        if context_decision is not None:
            return context_decision

        observations = inp.observations
        if observations and not observations[-1].result.ok:
            return self._recover(inp, intent, question, observations[-1])

        searches = [o for o in observations if o.action == "search_docs"]
        if not searches:
            return Decision(
                "search_docs",
                {"query": question, "top_k": 5},
                "首轮检索：用用户问题原文查询，不预设有答案",
            )

        best = self._best_search(inp, intent)
        if best is None:
            return self._blocked_decision(inp, intent, "search_docs", [])

        hits = best.result.payload.get("hits", [])
        top = hits[0]["score"] if hits else 0.0
        expanded = any(o.arguments.get("use_expansion") for o in self._main_searches(inp, intent))
        threshold = self._threshold(intent)
        if intent is not None and top < self.rewrite_threshold and not expanded:
            return Decision(
                "search_docs",
                {"query": question, "top_k": 5, "use_expansion": True},
                f"最高检索分 {top:.2f} 低于改写阈值 {self.rewrite_threshold:.2f}，"
                "改写检索（补充同义词/口语词）后重检，避免漏召回",
            )
        if top < threshold:
            return Decision(
                "finish",
                self._insufficient_payload(inp, intent, question, best),
                f"原查询与改写查询的最高分仅 {top:.2f}，低于可信阈值 {threshold:.2f}，"
                "停止检索并说明资料不足",
            )

        conflict = self._current_conflict(inp, intent)
        if conflict is not None:
            conflict_decision = self._conflict_flow(inp, intent, conflict)
            if conflict_decision is not None:
                return conflict_decision

        read_decision = self._next_read(inp, intent, best)
        if read_decision is not None:
            return read_decision
        return self._finalize(inp, intent, question)

    # ------------------------------------------------------------- 失败恢复
    def _recover(
        self, inp: PlannerInput, intent: Optional[IntentRule], question: str, last: Observation
    ) -> Decision:
        failures = [o for o in inp.observations if not o.result.ok]
        error = last.result.error or last.result.summary
        if last.action == "search_docs":
            same = [o for o in failures if o.action == "search_docs"]
            if len(same) == 1:
                return Decision(
                    "search_docs",
                    dict(last.arguments),
                    f"检索失败（{error}），原样重试一次以排除瞬时故障",
                )
            return Decision(
                "finish",
                self._blocked_payload(inp, intent, "search_docs", same),
                "检索工具连续失败，无法取得任何资料，按未完成处理",
            )
        if last.action == "read_snippet":
            doc_id = str(last.arguments.get("doc_id", ""))
            doc_failures = [
                o
                for o in failures
                if o.action == "read_snippet" and o.arguments.get("doc_id") == doc_id
            ]
            if len(doc_failures) == 1:
                return Decision(
                    "read_snippet",
                    dict(last.arguments),
                    f"读取 {doc_id} 失败（{error}），重试一次以排除瞬时故障",
                )
            best = self._best_search(inp, intent)
            following = self._next_read(inp, intent, best) if best else None
            if following is not None:
                following.reason = f"{doc_id} 连续读取失败，改为读取其他候选：{following.reason}"
                return following
            return Decision(
                "finish",
                self._blocked_payload(inp, intent, "read_snippet", failures),
                "片段读取持续失败且无其他可读候选，拒绝在无原文的情况下作答",
            )
        return Decision(
            "finish",
            self._blocked_payload(inp, intent, last.action, failures),
            f"工具 {last.action} 失败且没有替代动作",
        )

    # --------------------------------------------------------- 检索与读取
    @staticmethod
    def _tiebreak_query(intent: Optional[IntentRule]) -> str:
        return intent.tiebreak_query if intent else ""

    def _main_searches(self, inp: PlannerInput, intent: Optional[IntentRule]) -> List[Observation]:
        tie = self._tiebreak_query(intent)
        return [
            o
            for o in inp.observations
            if o.action == "search_docs" and o.result.ok and o.arguments.get("query") != tie
        ]

    def _best_search(
        self, inp: PlannerInput, intent: Optional[IntentRule]
    ) -> Optional[Observation]:
        searches = self._main_searches(inp, intent)
        if not searches:
            return None

        def top_score(observation: Observation) -> float:
            hits = observation.result.payload.get("hits", [])
            return hits[0]["score"] if hits else 0.0

        return max(searches, key=top_score)

    def _next_read(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        best: Optional[Observation],
    ) -> Optional[Decision]:
        if best is None:
            return None
        if intent is not None and not intent.value_patterns:
            # 没有可数值比对的意图（如「票据遗失流程」）：找到直接证据即可作答，无需继续读其余候选
            if any(
                self._has_evidence(o, intent)
                for o in inp.observations
                if o.action == "read_snippet" and o.result.ok
            ):
                return None
        hits = [
            hit
            for hit in best.result.payload.get("hits", [])
            if hit["score"] >= self._threshold(intent)
        ][: self.max_candidates]
        reads = [o for o in inp.observations if o.action == "read_snippet"]
        for hit in hits:
            doc_id = hit["doc_id"]
            doc_reads = [o for o in reads if o.arguments.get("doc_id") == doc_id]
            failed = [o for o in doc_reads if not o.result.ok]
            if len(failed) >= 2:
                continue
            ok_reads = [o for o in doc_reads if o.result.ok]
            if any(self._has_evidence(o, intent) for o in ok_reads):
                continue
            tried = {str(o.arguments.get("section_id")) for o in ok_reads}
            for section_id in self._sections_for(hit):
                if section_id in tried:
                    continue
                if failed:
                    reason = f"{doc_id} 有一次读取失败，换片段 {section_id} 继续核实"
                else:
                    reason = (
                        f"命中文档 {doc_id}（{hit['score']:.2f} 分，最佳片段 {hit['section_id']}），"
                        f"读取 {section_id} 原文，确认是否直接回答该问题"
                    )
                return Decision(
                    "read_snippet", {"doc_id": doc_id, "section_id": section_id}, reason
                )
        return None

    @staticmethod
    def _sections_for(hit: Dict[str, Any]) -> List[str]:
        sections: List[str] = []
        for item in hit.get("section_scores") or []:
            section_id = item[0] if isinstance(item, (list, tuple)) else item
            if section_id and section_id not in sections:
                sections.append(str(section_id))
        best = hit.get("section_id")
        if best:
            if best in sections:
                sections.remove(best)
            sections.insert(0, str(best))
        return sections[:MAX_SECTION_READS_PER_DOC]

    @staticmethod
    def _has_evidence(observation: Observation, intent: Optional[IntentRule]) -> bool:
        if not observation.result.ok:
            return False
        payload = observation.result.payload or {}
        text = payload.get("text", "")
        if not text:
            return False
        return intent.has_evidence(text) if intent is not None else True

    def _evidences(self, inp: PlannerInput, intent: Optional[IntentRule]) -> List[Any]:
        evidences = []
        seen = set()
        for observation in inp.observations:
            if observation.action != "read_snippet" or not observation.result.ok:
                continue
            payload = observation.result.payload or {}
            doc = inp.corpus.get(str(payload.get("doc_id", "")))
            if doc is None:
                continue
            section = doc.section(str(payload.get("section_id", "")))
            if section is None:
                continue
            if (doc.doc_id, section.section_id) in seen:
                continue
            if intent is not None and not intent.has_evidence(section.text):
                continue
            seen.add((doc.doc_id, section.section_id))
            evidences.append(build_evidence(doc, section, intent))
        return evidences

    def _current_conflict(
        self, inp: PlannerInput, intent: Optional[IntentRule]
    ) -> Optional[Conflict]:
        return detect_conflict(self._evidences(inp, intent), intent)

    # --------------------------------------------------------------- 冲突处理
    def _conflict_flow(
        self, inp: PlannerInput, intent: Optional[IntentRule], conflict: Conflict
    ) -> Optional[Decision]:
        if intent is None or not intent.tiebreak_query:
            return Decision(
                "ask_user",
                self._clarify_payload(inp, intent, conflict, "未配置用于查证的检索词"),
                "缺少优先级/适用范围信息且无法自动查证，转为向用户确认",
            )
        tie = intent.tiebreak_query
        tie_searches = [
            o
            for o in inp.observations
            if o.action == "search_docs" and o.arguments.get("query") == tie
        ]
        if not tie_searches:
            values = "、".join(f"{value.doc_id}:{value.value}" for value in conflict.values)
            return Decision(
                "search_docs",
                {"query": tie, "top_k": 5},
                f"读到冲突取值（{values}），先查证是否存在制度解释/优先级说明，不擅自选择其一",
            )
        tie_search = tie_searches[-1]
        if not tie_search.result.ok:
            return None
        memo_hits = [
            hit for hit in tie_search.result.payload.get("hits", []) if self._looks_like_memo(hit)
        ]
        read_docs = {
            o.result.payload.get("doc_id"): o.result.payload
            for o in inp.observations
            if o.action == "read_snippet" and o.result.ok
        }
        for hit in memo_hits:
            if hit["doc_id"] not in read_docs:
                return Decision(
                    "read_snippet",
                    {"doc_id": hit["doc_id"], "section_id": hit["section_id"]},
                    f"检索到制度解释类文档 {hit['doc_id']}，读取原文确认是否给出优先级",
                )
        for hit in memo_hits:
            payload = read_docs.get(hit["doc_id"])
            if not payload:
                continue
            chosen = parse_precedence(payload.get("text", ""), inp.corpus)
            if chosen is None:
                continue
            if chosen.doc_id not in read_docs:
                section_id = self._conflict_section(conflict, chosen.doc_id) or (
                    chosen.sections[0].section_id if chosen.sections else ""
                )
                return Decision(
                    "read_snippet",
                    {"doc_id": chosen.doc_id, "section_id": section_id},
                    f"制度解释指定以 {chosen.doc_id} 为准，读取其原文作为最终依据",
                )
            return self._answer_resolved(inp, intent, conflict, payload, chosen)
        note = (
            "语料中没有制度解释类文档"
            if not memo_hits
            else "制度解释文档未给出明确的优先级规则"
        )
        return Decision(
            "ask_user",
            self._clarify_payload(inp, intent, conflict, note),
            "冲突无法在语料内消解，向用户确认适用制度",
        )

    @staticmethod
    def _looks_like_memo(hit: Dict[str, Any]) -> bool:
        return hit.get("doc_type") in ("memo", "notice") or "为准" in (hit.get("preview") or "")

    @staticmethod
    def _conflict_section(conflict: Conflict, doc_id: str) -> str:
        for value in conflict.values:
            if value.doc_id == doc_id:
                return value.section_id
        return ""

    def _answer_resolved(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        conflict: Conflict,
        memo_payload: Dict[str, Any],
        chosen: Any,
    ) -> Decision:
        evidences = {ev.doc.doc_id: ev for ev in self._evidences(inp, intent)}
        chosen_ev = evidences.get(chosen.doc_id)
        if chosen_ev is None:
            section_id = self._conflict_section(conflict, chosen.doc_id)
            return Decision(
                "read_snippet",
                {"doc_id": chosen.doc_id, "section_id": section_id},
                f"缺少 {chosen.doc_id} 的原文，无法引用，先读取",
            )
        memo_doc = inp.corpus.get(memo_payload.get("doc_id", ""))
        memo_section = memo_doc.section(memo_payload.get("section_id", "")) if memo_doc else None
        if memo_doc is None or memo_section is None:
            return Decision(
                "ask_user",
                self._clarify_payload(inp, intent, conflict, "制度解释原文缺失"),
                "制度解释原文缺失，无法完成查证",
            )
        citations = [
            self._citation(memo_doc, memo_section, f"制度解释明确冲突时以 {chosen.doc_id} 为准"),
            self._citation(chosen_ev.doc, chosen_ev.section, "被指定为准的条款，给出最终答案"),
        ]
        others = [ev for ev in evidences.values() if ev.doc.doc_id != chosen.doc_id]
        citations.extend(
            self._citation(ev.doc, ev.section, "与之冲突的条款，保留以便复核") for ev in others
        )
        conclusion = self._conclusion(intent, chosen_ev, inp.contextual_question())
        other_desc = "、".join(
            f"{ev.doc.doc_id}（{'、'.join(ev.value_strings())}）" for ev in others
        ) or "其他条款"
        answer = (
            f"根据《{memo_doc.title}》（{memo_doc.doc_id}，版本 {memo_doc.version_date}）"
            f"第 {memo_section.section_id} 节：\n「{memo_section.text}」\n\n"
            f"结合被指定为准的《{chosen_ev.doc.title}》（{chosen_ev.doc.doc_id}，"
            f"版本 {chosen_ev.doc.version_date}）第 {chosen_ev.section.section_id} 节：\n"
            f"「{chosen_ev.section.text}」\n"
            f"结论：{conclusion}\n"
            f"说明：{other_desc} 的取值与本答案不同，已依据 {memo_doc.doc_id} 的优先级规则采用 {chosen.doc_id}。"
        )
        conflict.resolution = "resolved_by_memo"
        conflict.selected_doc_id = chosen.doc_id
        conflict.note = f"依据 {memo_doc.doc_id}《{memo_doc.title}》"
        return Decision(
            "finish",
            {
                "status": STATUS_ANSWERED,
                "answer": answer,
                "citations": citations,
                "evidence_gaps": [],
                "conflicts": [conflict.to_dict()],
                "notes": [],
            },
            f"制度解释已明确冲突时以 {chosen.doc_id} 为准，冲突可解，给出有依据的答案",
        )

    def _clarify_payload(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        conflict: Conflict,
        note: str,
    ) -> Dict[str, Any]:
        docs = []
        for value in conflict.values:
            doc = inp.corpus.get(value.doc_id)
            if doc and all(existing.doc_id != doc.doc_id for existing in docs):
                docs.append(doc)
        lines = [f"资料存在冲突，暂无法给出唯一答案（主题：{conflict.topic}）："]
        for value in conflict.values:
            lines.append(
                f"- {value.doc_id}《{value.title}》（版本 {value.version_date}）"
                f"第 {value.section_id} 节：{value.quote}"
            )
        scopes = {doc.applies_to for doc in docs}
        if len(scopes) == 1:
            lines.append(
                f"以上文件均标注适用于「{next(iter(scopes))}」，语料中未给出优先级或适用范围差异。"
            )
        else:
            lines.append(
                "以上文件的适用范围不同："
                + "；".join(f"{doc.doc_id}：{doc.applies_to}" for doc in docs)
            )
        lines.append(f"查证情况：{note}。")
        lines.append("请确认以哪份制度为准，或由制度归口部门解释后再答复。")
        citations = []
        for value in conflict.values:
            doc = inp.corpus.get(value.doc_id)
            section = doc.section(value.section_id) if doc else None
            if doc is not None and section is not None:
                citations.append(self._citation(doc, section, "冲突条款原文"))
        options = [f"按 {doc.doc_id}《{doc.title}》执行" for doc in docs]
        options.extend(["按最新发布的版本执行", "由财务共享中心解释"])
        return {
            "status": STATUS_NEEDS_CLARIFICATION,
            "answer": "\n".join(lines),
            "citations": citations,
            "evidence_gaps": ["需要确认适用制度（或由制度归口部门解释）后才能给出唯一答案"],
            "conflicts": [conflict.to_dict()],
            "notes": [f"查证：{note}"],
            "clarification": {
                "question": f"请确认适用于您情形的制度（{conflict.topic}）：",
                "options": options,
                "reason": note,
            },
        }

    # --------------------------------------------------------------- 答案生成
    def _finalize(
        self, inp: PlannerInput, intent: Optional[IntentRule], question: str
    ) -> Decision:
        evidences = self._evidences(inp, intent)
        if not evidences:
            return Decision(
                "finish",
                self._insufficient_payload(inp, intent, question, self._best_search(inp, intent)),
                "已读完候选片段，但没有片段直接回答该问题，停止检索并说明资料不足",
            )
        return self._answer_evidences(inp, intent, evidences, question)

    def _answer_evidences(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        evidences: List[Any],
        question: str,
    ) -> Decision:
        primary = evidences[0]
        citations = [self._citation(primary.doc, primary.section, "直接回答问题的条款")]
        for evidence in evidences[1:]:
            citations.append(self._citation(evidence.doc, evidence.section, "同主题条款，供交叉核对"))
        conclusion = self._conclusion(intent, primary, question)
        answer = (
            f"根据《{primary.doc.title}》（{primary.doc.doc_id}，版本 {primary.doc.version_date}）"
            f"第 {primary.section.section_id} 节《{primary.section.heading}》：\n"
            f"「{primary.section.text}」\n"
            f"结论：{conclusion}"
        )
        if len(evidences) > 1:
            answer += "\n补充：" + "；".join(
                f"{ev.doc.doc_id} 第 {ev.section.section_id} 节也给出相关取值（见引用）"
                for ev in evidences[1:]
            )
        gaps: List[str] = []
        if (
            intent is not None
            and intent.intent_id == "annual_leave_days"
            and parse_years(question) is None
        ):
            gaps.append("问题未说明累计工作年限，答案只能列出原文全部分档")
        if (
            intent is not None
            and intent.intent_id == "hotel_standard"
            and not re.search(r"北京|上海|广州|深圳|一线", normalize(question))
        ):
            gaps.append("问题未说明出差城市，答案包含一线/其他城市两档标准")
        return Decision(
            "finish",
            {
                "status": STATUS_ANSWERED,
                "answer": answer,
                "citations": citations,
                "evidence_gaps": gaps,
                "conflicts": [],
                "notes": self._rejected_notes(inp, intent),
            },
            f"已取得 {primary.doc.doc_id}/{primary.section.section_id} 的可核验原文"
            "且无未解决冲突，结束",
        )

    def _conclusion(
        self, intent: Optional[IntentRule], evidence: Any, question: str
    ) -> str:
        text = evidence.section.text
        if intent is not None and intent.intent_id == "annual_leave_days":
            years = parse_years(question)
            tier = annual_leave_tier(text, years)
            if tier is not None:
                return f"您累计工作 {years} 年，属于「{tier[0]}」档，年休假 {tier[1]} 天。"
            return "年休假天数按累计工作年限分档，详见引用原文。"
        values: List[str] = []
        for value, _ in evidence.values:
            if value not in values:
                values.append(value)
        if len(values) == 1 and intent is not None and intent.numeric_unit:
            return f"{intent.label}为 {values[0]} {intent.numeric_unit}。"
        if evidence.values:
            sentences: List[str] = []
            for _, sentence in evidence.values:
                if sentence not in sentences:
                    sentences.append(sentence)
            return "；".join(sentences)
        sentences = split_sentences(text)
        if sentences:
            return "按原文执行：" + sentences[0]
        return "以上原文即为现行处理方式，请按原文要求办理。"

    @staticmethod
    def _citation(doc: Any, section: Any, supports: str) -> Dict[str, Any]:
        return {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "section_id": section.section_id,
            "heading": section.heading,
            "version_date": doc.version_date,
            "quote": section.text,
            "supports": supports,
        }



    # ------------------------------------------------------------- 上下文追问
    def _context_choice(
        self, inp: PlannerInput, intent: Optional[IntentRule], question: str
    ) -> Optional[Decision]:
        if not inp.prior_result:
            return None
        prior_conflicts = inp.prior_result.get("conflicts") or []
        if not prior_conflicts:
            return None
        prior = prior_conflicts[0]
        doc_ids: List[str] = []
        section_by_doc: Dict[str, str] = {}
        for value in prior.get("values", []):
            doc_id = value.get("doc_id")
            if not doc_id:
                continue
            if doc_id not in doc_ids:
                doc_ids.append(doc_id)
            section_by_doc.setdefault(doc_id, value.get("section_id", ""))
        docs = [inp.corpus.get(doc_id) for doc_id in doc_ids]
        docs = [doc for doc in docs if doc is not None]
        chosen = detect_user_choice(inp.question, docs)
        if chosen is None:
            return None
        reads = {
            o.result.payload.get("doc_id"): o.result.payload
            for o in inp.observations
            if o.action == "read_snippet" and o.result.ok
        }
        ordered = [chosen] + [doc for doc in docs if doc.doc_id != chosen.doc_id]
        for doc in ordered:
            if doc.doc_id in reads:
                continue
            section_id = section_by_doc.get(doc.doc_id) or self._first_section_id(doc)
            if not section_id:
                continue
            return Decision(
                "read_snippet",
                {"doc_id": doc.doc_id, "section_id": section_id},
                f"上下文追问：用户已指定按 {chosen.doc_id}《{chosen.title}》执行；"
                f"读取 {doc.doc_id} 原文，指定制度与冲突条款都需要可核验引用",
            )
        return self._answer_user_choice(inp, intent, chosen, docs, reads, prior)

    def _answer_user_choice(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        chosen: Any,
        docs: List[Any],
        reads: Dict[str, Dict[str, Any]],
        prior: Dict[str, Any],
    ) -> Decision:
        chosen_read = reads.get(chosen.doc_id) or {}
        section = chosen.section(chosen_read.get("section_id", ""))
        if section is None:
            return Decision(
                "read_snippet",
                {"doc_id": chosen.doc_id, "section_id": self._first_section_id(chosen)},
                "指定制度的原文尚未取得，先读取",
            )
        evidence = build_evidence(chosen, section, intent)
        conclusion = self._conclusion(intent, evidence, inp.contextual_question())
        citations = [self._citation(chosen, section, "用户确认的适用制度条款，给出最终答案")]
        for doc in docs:
            if doc.doc_id == chosen.doc_id:
                continue
            payload = reads.get(doc.doc_id) or {}
            other = doc.section(payload.get("section_id", ""))
            if other is not None:
                citations.append(self._citation(doc, other, "与之冲突的条款，保留以便复核"))
        prior_values = {v.get("doc_id"): v.get("value") for v in prior.get("values", [])}
        others = [doc.doc_id for doc in docs if doc.doc_id != chosen.doc_id]
        other_text = (
            "、".join(
                f"{doc_id}（{prior_values[doc_id]}）"
                for doc_id in others
                if doc_id in prior_values
            )
            or "其他条款"
        )
        answer = (
            f"按您确认的适用制度《{chosen.title}》（{chosen.doc_id}，版本 {chosen.version_date}）"
            f"第 {section.section_id} 节：\n「{section.text}」\n"
            f"结论：{conclusion}\n"
            f"说明：{other_text} 与本答案存在冲突，本次按您指定的 {chosen.doc_id} 执行。"
        )
        conflict = Conflict(
            topic=str(prior.get("topic", "")),
            values=[],
            resolution="user_selected",
            selected_doc_id=chosen.doc_id,
            note=f"用户在追问中指定 {chosen.doc_id}",
        )
        return Decision(
            "finish",
            {
                "status": STATUS_ANSWERED,
                "answer": answer,
                "citations": citations,
                "evidence_gaps": [],
                "conflicts": [conflict.to_dict()],
                "notes": [f"答案依据用户显式指定的 {chosen.doc_id}；制度层面的冲突仍未消解"],
            },
            "已按用户指定制度读取原文并给出答案，冲突说明一并保留",
        )

    @staticmethod
    def _first_section_id(doc: Any) -> str:
        return doc.sections[0].section_id if doc.sections else ""


    # ------------------------------------------------------- 未完成/资料不足
    def _rejected_notes(self, inp: PlannerInput, intent: Optional[IntentRule]) -> List[str]:
        notes: List[str] = []
        for observation in inp.observations:
            if observation.action != "read_snippet" or not observation.result.ok:
                continue
            payload = observation.result.payload or {}
            text = payload.get("text", "")
            if intent is not None and text and not intent.has_evidence(text):
                note = (
                    f"{payload.get('doc_id')}/{payload.get('section_id')} 已读取，"
                    f"但未命中『{intent.label}』的证据词/取值，未作为答案依据"
                )
                if note not in notes:
                    notes.append(note)
        return notes

    def _insufficient_payload(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        question: str,
        best: Optional[Observation],
    ) -> Dict[str, Any]:
        searched: List[str] = []
        for observation in self._main_searches(inp, intent):
            query = str(observation.arguments.get("query", ""))
            if query and query not in searched:
                searched.append(query)
        hits = best.result.payload.get("hits", []) if best is not None else []
        gaps = [f"知识库中未检索到能直接回答『{question}』的制度或 FAQ 片段"]
        if searched:
            gaps.append("已尝试的检索词：" + "；".join(searched))
        if hits:
            gaps.append(
                "低置信候选（均未达到证据阈值）："
                + "、".join(f"{hit['doc_id']}({hit['score']:.2f})" for hit in hits[:3])
            )
        return {
            "status": STATUS_INSUFFICIENT_EVIDENCE,
            "answer": "未在知识库中找到能回答该问题的制度或 FAQ，因此不作答。"
            "若问题涉及公司制度，请补充制度名称或联系制度归口部门。",
            "citations": [],
            "evidence_gaps": gaps,
            "conflicts": [],
            "notes": self._rejected_notes(inp, intent),
        }

    def _blocked_payload(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        tool: str,
        failed_observations: List[Observation],
    ) -> Dict[str, Any]:
        errors = [o.result.error for o in failed_observations if o.result.error]
        errors = list(dict.fromkeys(errors))
        return {
            "status": STATUS_BLOCKED,
            "answer": f"工具 {tool} 连续失败，未取得可核验的原文，因此不给出答案。"
            "请稍后重试，或联系制度归口部门人工确认。",
            "citations": [],
            "conflicts": [],
            "evidence_gaps": [
                f"{tool} 失败：{errors[-1] if errors else '未知错误'}",
                "缺少可核验原文，任何结论都不可靠",
            ],
            "failures": [f"{tool}: {error}" for error in errors],
            "notes": self._rejected_notes(inp, intent),
        }

    def _blocked_decision(
        self,
        inp: PlannerInput,
        intent: Optional[IntentRule],
        tool: str,
        failed_observations: List[Observation],
    ) -> Decision:
        return Decision(
            "finish",
            self._blocked_payload(inp, intent, tool, failed_observations),
            f"工具 {tool} 未返回可用结果，停止并返回未完成状态",
        )


# ============================================================================
# 真实模型决策器（OpenAI Chat Completions 兼容接口）
# ============================================================================
SYSTEM_PROMPT = """你是企业知识问答 Agent 的决策器。你只能通过工具取证，禁止凭常识作答。

可用工具（每步只能选一个动作）：
1) search_docs {"query": str, "top_k": int=5, "use_expansion": bool=false}
   - 关键词检索，返回文档元数据与最佳片段摘要（不含全文）。
   - 置信度低或问题口语化时，改写 query 或置 use_expansion=true 重检。
2) read_snippet {"doc_id": str, "section_id": str}
   - 读取逐字原文；只有读过原文的片段才能引用。
3) ask_user {"question": str, "options": [str], "reason": str, "answer": str, "citations": [...], "conflicts": [...]}
   - 信息不足、需要用户确认，或存在无法消解的冲突时使用。
4) finish {"status": str, "answer": str, "citations": [{"doc_id","section_id","quote","supports"}],
           "evidence_gaps": [str], "conflicts": [{"topic","values":[...],"resolution","selected_doc_id","note"}],
           "clarification": null, "notes": [str], "failures": [str]}
   - status 取值：answered / needs_clarification / insufficient_evidence / partial / blocked /
     incomplete_budget_exceeded / failed。

决策原则：
- 先检索；命中后必须 read_snippet 取得原文再作答，不得根据检索摘要（preview）作答。
- 若检索结果中有多篇文档可能规定同一事项（例如都涉及「报销/时限」），至少读取其中 2 篇再判断，
  避免漏掉文档冲突。
- 若多个文档对同一问题给出不同取值，且没有适用范围/优先级说明：先检索是否存在制度解释
  （如「以……为准」）；仍无法消解则 ask_user，列出冲突取值与引用，绝不擅自选一个。
- 资料不足或问题超出知识库：finish，status=insufficient_evidence，不要编造。
- 工具报错可重试一次；持续失败：finish，status=blocked。
- 引用必须逐字来自 read_snippet 的原文，quote 不得改写；answered 至少要有 1 条引用。
- 每个动作都必须给出 reason：一句话说明「基于上一条工具结果」为什么这样做（不是思考过程）。
- 工具调用次数有上限，请高效决策。

只输出一个 JSON 对象，格式：
{"action": "search_docs|read_snippet|ask_user|finish", "arguments": {...}, "reason": "一句话决策依据"}
不要输出 Markdown、解释或思考过程。"""


def _truncate(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


class OpenAICompatiblePlanner:
    """由真实模型输出 JSON 动作。未配置密钥时不要使用（默认走 mock）。"""

    name = "openai"

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        timeout: Optional[int] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ):
        self.model = model or os.environ.get("EQA_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("EQA_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("EQA_API_KEY", "")
        self.transport = transport or _http_transport
        self.timeout = int(timeout or os.environ.get("EQA_TIMEOUT", "30"))
        self.temperature = temperature
        self.max_tokens = int(max_tokens or os.environ.get("EQA_MAX_TOKENS", "1200"))

    def decide(self, inp: PlannerInput) -> Decision:
        payload = {
            "model": self.model,
            "messages": self._messages(inp),
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.transport(
            {
                "url": f"{self.base_url}/chat/completions",
                "headers": headers,
                "payload": payload,
                "timeout": self.timeout,
            }
        )
        return parse_decision(_extract_content(response))

    def _messages(self, inp: PlannerInput) -> List[Dict[str, str]]:
        lines: List[str] = [f"用户问题：{inp.question}"]
        if inp.history:
            history = "\n".join(f"- {h.get('role')}: {_truncate(h.get('content', ''), 200)}" for h in inp.history[-6:])
            lines.append(f"对话历史：\n{history}")
        if inp.prior_result:
            prior = inp.prior_result
            lines.append(
                "上一轮结果摘要：\n"
                f"- status: {prior.get('status')}\n"
                f"- answer: {_truncate(prior.get('answer', ''), 400)}\n"
                f"- conflicts: {_truncate(prior.get('conflicts', []), 500)}"
            )
        if inp.observations:
            lines.append("已执行步骤（观察 → 结果）：")
            for observation in inp.observations:
                result = observation.result
                state = "ok" if result.ok else f"error: {result.error}"
                if result.ok:
                    detail = f"{result.summary} | {_truncate(result.payload, 400)}"
                else:
                    detail = result.summary
                lines.append(
                    f"- step {observation.step}: {observation.action} {_truncate(observation.arguments, 200)} "
                    f"→ {state}; {detail}"
                )
        else:
            lines.append("尚未执行任何工具，请给出第一步动作。")
        lines.append(
            f"剩余预算：步数 {inp.remaining_steps}，工具调用 {inp.remaining_tool_calls}。"
        )
        return [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
                + "\n\n工具 schema（JSON，仅用于说明参数，不需要 function calling）：\n"
                + json.dumps(TOOL_SPECS, ensure_ascii=False),
            },
            {"role": "user", "content": "\n\n".join(lines)},
        ]


def _http_transport(request: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(request["payload"], ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        request["url"], data=body, headers=request["headers"], method="POST"
    )
    try:
        with urllib.request.urlopen(http_request, timeout=request.get("timeout", 30)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise PlannerError(f"模型接口 HTTP {exc.code}：{detail}")
    except urllib.error.URLError as exc:
        raise PlannerError(f"无法连接模型接口：{exc.reason}")


def _extract_content(response: Dict[str, Any]) -> str:
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PlannerError(
            f"模型响应格式不符合 OpenAI 兼容协议：{_truncate(response, 300)}"
        ) from exc

    content = message.get("content")
    if content:
        return content

    # 某些 thinking 模型（如 Ollama 上的 qwen3/deepseek-r1）只返回 reasoning，content 为空。
    # 不把推理过程当答案解析，直接给出可操作的错误。
    reasoning = message.get("reasoning") or message.get("thinking") or message.get("reasoning_content")
    if reasoning:
        raise PlannerError(
            "模型只返回了 reasoning/thinking，content 为空（finish_reason 可能为 length）。"
            "请关闭 thinking（Ollama 原生 /api/chat 传 think=false）或改用非 thinking 模型；"
            f"reasoning 片段：{_truncate(str(reasoning), 160)}"
        )
    raise PlannerError("模型返回了空内容（content 为空），请检查模型是否支持 JSON 输出。")


def parse_decision(content: str) -> Decision:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match is None:
            raise PlannerError(f"模型输出不是 JSON：{_truncate(text, 200)}")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise PlannerError(f"模型输出 JSON 无法解析：{exc}")
    if not isinstance(data, dict):
        raise PlannerError("模型输出必须是 JSON 对象")
    action = str(data.get("action", "")).strip()
    if action not in ALLOWED_ACTIONS:
        raise PlannerError(f"非法动作 {action!r}；允许：{sorted(ALLOWED_ACTIONS)}")
    arguments = data.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise PlannerError("arguments 必须是 JSON 对象")
    reason = str(data.get("reason") or "模型未提供决策依据")
    if action == "search_docs" and not str(arguments.get("query", "")).strip():
        raise PlannerError("search_docs 缺少 query")
    if action == "read_snippet" and not (
        arguments.get("doc_id") and arguments.get("section_id")
    ):
        raise PlannerError("read_snippet 需要 doc_id 与 section_id")
    if action == "ask_user" and not str(arguments.get("question", "")).strip():
        raise PlannerError("ask_user 缺少 question")
    if action == "finish":
        if not str(arguments.get("status", "")).strip():
            raise PlannerError("finish 缺少 status")
        if not str(arguments.get("answer", "")).strip():
            raise PlannerError("finish 缺少 answer")
    return Decision(action=action, arguments=arguments, reason=reason)


class ReplayPlanner:
    """离线回放预记录动作序列，用于回归测试循环与校验逻辑（不代表真实模型）。"""

    name = "replay"

    def __init__(self, decisions: Sequence[Dict[str, Any]], source: str = ""):
        self._decisions = [dict(item) for item in decisions]
        self._index = 0
        self.source = source

    @classmethod
    def load(cls, path: str) -> "ReplayPlanner":
        from . import config as _config

        raw = _config.load_json(path)
        decisions = raw.get("decisions", raw) if isinstance(raw, dict) else raw
        return cls(decisions, source=str(_config.resolve_path(path)))

    def decide(self, inp: PlannerInput) -> Decision:
        if self._index >= len(self._decisions):
            raise PlannerError(
                f"回放决策已用尽（{len(self._decisions)} 条）：{self.source or '未命名回放文件'}"
            )
        item = self._decisions[self._index]
        self._index += 1
        action = str(item.get("action", "")).strip()
        if action not in ALLOWED_ACTIONS:
            raise PlannerError(f"回放决策动作非法：{action!r}")
        return Decision(
            action=action,
            arguments=dict(item.get("arguments") or {}),
            reason=str(item.get("reason") or "回放决策"),
        )


def build_planner(kind: str = "mock", **kwargs: Any) -> Any:
    normalized = (kind or "mock").lower()
    if normalized in ("mock", "offline"):
        allowed = {
            key: value
            for key, value in kwargs.items()
            if key in {"matcher", "min_score", "rewrite_threshold", "max_candidates"}
            and value is not None
        }
        return MockPlanner(**allowed)
    if normalized in ("openai", "llm"):
        allowed = {
            key: value
            for key, value in kwargs.items()
            if key in {"model", "base_url", "api_key", "transport", "timeout", "temperature"}
            and value is not None
        }
        return OpenAICompatiblePlanner(**allowed)
    if normalized == "replay":
        return ReplayPlanner(
            kwargs.get("decisions") or [], source=str(kwargs.get("source", ""))
        )
    raise ValueError(f"未知 planner：{kind}；可选 mock / openai / replay")
