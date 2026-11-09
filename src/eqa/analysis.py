"""问题意图、证据判定与冲突检测（确定性逻辑）。

职责边界：
- MockPlanner 用它决定「读到哪一段才算回答了问题」；
- audit 模块用它做最终一致性检查（引用是否支持结论、冲突是否被说明）。
- 真实模型模式下，意图规则不参与生成，只参与事后审计。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import config
from .corpus import Corpus
from .models import Conflict, ConflictValue, Document, Section
from .text import normalize, sentence_with


@dataclass
class IntentRule:
    intent_id: str
    label: str
    topic: str
    evidence_keywords: List[str] = field(default_factory=list)
    value_patterns: List[str] = field(default_factory=list)
    numeric_unit: Optional[str] = None
    tiebreak_query: str = ""
    match_all: List[str] = field(default_factory=list)
    match_any: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict) -> "IntentRule":
        match = raw.get("match") or {}
        return cls(
            intent_id=str(raw["id"]),
            label=str(raw.get("label", raw["id"])),
            topic=str(raw.get("topic", raw["id"])),
            evidence_keywords=[str(k) for k in raw.get("evidence_keywords", [])],
            value_patterns=[str(p) for p in raw.get("value_patterns", [])],
            numeric_unit=raw.get("numeric_unit"),
            tiebreak_query=str(raw.get("tiebreak_query", "")),
            match_all=[str(p) for p in match.get("all", [])],
            match_any=[str(p) for p in match.get("any", [])],
        )

    def matches(self, text: str) -> bool:
        norm = normalize(text)
        if not norm:
            return False
        if any(not re.search(pattern, norm) for pattern in self.match_all):
            return False
        if self.match_any and not any(re.search(pattern, norm) for pattern in self.match_any):
            return False
        return bool(self.match_all or self.match_any)

    def has_evidence(self, text: str) -> bool:
        """原文片段是否真的回答了「这一类问题」。

        要求：至少命中一个证据关键词；若有数值模式，则必须能抽出数值。
        注意：仅以《制度名称》形式引用其他文档，不算本片段给出了规定。
        """
        norm = normalize(text)
        scrubbed = re.sub(r"《[^》]*》", " ", norm)
        if self.evidence_keywords:
            if not any(normalize(key) in scrubbed for key in self.evidence_keywords):
                return False
        if self.value_patterns and not self.extract_values(text):
            return False
        return True

    def extract_values(self, text: str) -> List[Tuple[str, str]]:
        """抽取 (value, 所在原句)。"""
        found: List[Tuple[str, str]] = []
        seen = set()
        for pattern in self.value_patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                if match.groups():
                    value = next((g for g in match.groups() if g), None)
                else:
                    value = match.group(0)
                if value is None:
                    continue
                key = (value, match.group(0))
                if key in seen:
                    continue
                seen.add(key)
                found.append((str(value), sentence_with(text, match.group(0))))
        return found


class IntentMatcher:
    def __init__(self, rules: Sequence[IntentRule]):
        self.rules = list(rules)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "IntentMatcher":
        raw = config.load_json(path or config.DEFAULT_INTENTS)
        return cls([IntentRule.from_dict(item) for item in raw.get("intents", [])])

    def detect(self, text: str) -> Optional[IntentRule]:
        for rule in self.rules:
            if rule.matches(text):
                return rule
        return None

    def get(self, intent_id: str) -> Optional[IntentRule]:
        for rule in self.rules:
            if rule.intent_id == intent_id:
                return rule
        return None


@dataclass
class Evidence:
    doc: Document
    section: Section
    values: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.section.text

    def value_strings(self) -> List[str]:
        return [value for value, _ in self.values]


def build_evidence(doc: Document, section: Section, rule: Optional[IntentRule]) -> Evidence:
    values = rule.extract_values(section.text) if rule else []
    return Evidence(doc=doc, section=section, values=values)


def detect_conflict(
    evidences: Sequence[Evidence], rule: Optional[IntentRule]
) -> Optional[Conflict]:
    """同一属性在多个文档中取值不同 → 冲突（分档标准等超集情况除外）。"""
    groups: Dict[str, Evidence] = {}
    for evidence in evidences:
        if evidence.values:
            groups.setdefault(evidence.doc.doc_id, evidence)
    if len(groups) < 2:
        return None

    value_sets = {
        doc_id: sorted({value for value, _ in evidence.values})
        for doc_id, evidence in groups.items()
    }
    if len({frozenset(values) for values in value_sets.values()}) == 1:
        return None
    sets = [set(values) for values in value_sets.values()]
    # 某个文档给出更细的分档（包含其余文档全部取值）时，按细分理解而非冲突
    if any(all(other <= candidate for other in sets) for candidate in sets):
        return None

    conflict_values: List[ConflictValue] = []
    for doc_id, values in value_sets.items():
        evidence = groups[doc_id]
        quotes: Dict[str, str] = {}
        for value, sentence in evidence.values:
            quotes.setdefault(value, sentence)
        for value in values:
            conflict_values.append(
                ConflictValue(
                    doc_id=doc_id,
                    title=evidence.doc.title,
                    section_id=evidence.section.section_id,
                    version_date=evidence.doc.version_date,
                    value=value,
                    quote=quotes.get(value, evidence.section.text),
                )
            )
    return Conflict(topic=rule.topic if rule else "未识别主题", values=conflict_values)


def parse_precedence(memo_text: str, corpus: Corpus) -> Optional[Document]:
    """从「以《X》为准」这类制度解释中解析出被指定为准的文档。"""
    match = re.search(r"以《([^》]+)》为准", memo_text)
    if not match:
        return None
    target = normalize(match.group(1))
    for doc in corpus.documents:
        title = normalize(doc.title)
        if title == target or target in title or title in target:
            return doc
    return None


_TITLE_MARKERS = [
    "操作指引",
    "实施细则",
    "管理规定",
    "管理办法",
    "补充通知",
    "制度解释",
    "规范",
    "标准",
]


def detect_user_choice(text: str, candidates: Sequence[Document]) -> Optional[Document]:
    """从用户追问中识别其指定的适用制度（只认显式选择，不做猜测）。"""
    norm = normalize(text)
    if not candidates:
        return None
    for doc in candidates:
        if normalize(doc.doc_id) in norm:
            return doc
    prefers_latest = bool(re.search(r"最新|新版|新版本|最近发布", norm)) and bool(
        re.search(r"版本|版|发布", norm)
    )
    for doc in candidates:
        for marker in _TITLE_MARKERS:
            if marker in doc.title and marker in norm:
                return doc
    if prefers_latest:
        return max(candidates, key=lambda d: d.version_date)
    return None


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def cn_to_int(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(value)


def parse_years(text: str) -> Optional[int]:
    match = re.search(
        r"(?:工作|工龄|累计工作)\s*([一二三四五六七八九十两\d]+)\s*年", normalize(text)
    )
    if not match:
        return None
    return cn_to_int(match.group(1))


def annual_leave_tier(section_text: str, years: Optional[int]) -> Optional[Tuple[str, int]]:
    """按年休假分档规则，返回 (档位描述, 天数)。"""
    if years is None:
        return None
    for match in re.finditer(
        r"满\s*(\d+)\s*年不满\s*(\d+)\s*年的?\s*[，,]?\s*年休假\s*(\d+)\s*天", section_text
    ):
        low, high, days = (int(g) for g in match.groups())
        if low <= years < high:
            return f"满 {low} 年不满 {high} 年", days
    for match in re.finditer(
        r"(?<!不)满\s*(\d+)\s*年的?\s*[，,]?\s*年休假\s*(\d+)\s*天", section_text
    ):
        low, days = int(match.group(1)), int(match.group(2))
        if years >= low:
            return f"满 {low} 年", days
    return None
