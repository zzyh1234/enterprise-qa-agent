"""BM25 片段检索与查询改写。

取舍：
- 片段级（section）建索引，命中后聚合到文档级，便于「先看命中、再读原文」的两步工具设计。
- 中文用单字 + 二元组，不引入分词依赖；同义词表只在「改写检索」动作里使用，
  这样 Trace 能清楚看出：低置信度 → 改写 → 命中变化。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import config
from .corpus import Corpus
from .models import Document, Section
from .text import bigrams, normalize, tokenize


class QueryExpander:
    def __init__(self, mapping: Dict[str, Sequence[str]]):
        self.mapping = {
            normalize(key): [normalize(v) for v in values if normalize(v)]
            for key, values in mapping.items()
            if normalize(key)
        }
        self._keys = sorted(self.mapping, key=len, reverse=True)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "QueryExpander":
        raw = config.load_json(path or config.DEFAULT_EXPANSIONS)
        return cls(raw.get("expansions", {}))

    def expand(self, query: str) -> Tuple[str, List[str]]:
        """返回 (改写后查询, 新增词)。未命中任何同义词时原样返回。"""
        norm = normalize(query)
        added: List[str] = []
        for key in self._keys:
            if key in norm:
                for synonym in self.mapping[key]:
                    if synonym not in norm and synonym not in added:
                        added.append(synonym)
        if not added:
            return query, []
        return f"{query} {' '.join(added)}", added


class _BM25:
    def __init__(self, token_lists: List[List[str]], k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.tfs = [Counter(tokens) for tokens in token_lists]
        self.lengths = [max(len(tokens), 1) for tokens in token_lists]
        self.n = max(len(token_lists), 1)
        self.avgdl = sum(self.lengths) / self.n
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score(self, query_terms: Sequence[str], idx: int) -> float:
        tf = self.tfs[idx]
        length = self.lengths[idx]
        total = 0.0
        for term in query_terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = self.idf.get(term, 0.0)
            denom = freq + self.k1 * (1 - self.b + self.b * length / self.avgdl)
            total += idf * freq * (self.k1 + 1) / denom
        return total


@dataclass
class SectionHit:
    doc: Document
    section: Section
    score: float


@dataclass
class DocHit:
    doc: Document
    section: Section
    score: float
    section_scores: List[Tuple[str, float]] = field(default_factory=list)
    matched_terms: List[str] = field(default_factory=list)

    @property
    def doc_id(self) -> str:
        return self.doc.doc_id


class RetrievalIndex:
    def __init__(self, corpus: Corpus, expander: Optional[QueryExpander] = None):
        self.corpus = corpus
        self.expander = expander or QueryExpander.load()
        self.items: List[Tuple[Document, Section]] = list(corpus.iter_sections())
        self.bm25 = _BM25([tokenize(f"{section.heading} {section.text}") for _, section in self.items])

    def search_sections(self, query: str, top_k: int = 8) -> List[SectionHit]:
        terms = tokenize(query)
        if not terms:
            return []
        scored = [(self.bm25.score(terms, i), i) for i in range(len(self.items))]
        scored = [(score, i) for score, i in scored if score > 0]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            SectionHit(doc=self.items[i][0], section=self.items[i][1], score=score)
            for score, i in scored[:top_k]
        ]

    def search_docs(self, query: str, top_k: int = 5) -> List[DocHit]:
        section_hits = self.search_sections(query, top_k=max(top_k * 3, 12))
        grouped: Dict[str, List[SectionHit]] = {}
        for hit in section_hits:
            grouped.setdefault(hit.doc.doc_id, []).append(hit)

        query_terms = set(bigrams(query))
        docs: List[DocHit] = []
        for doc_id, hits in grouped.items():
            hits.sort(key=lambda h: h.score, reverse=True)
            best = hits[0]
            # 文档分数 = 最佳片段 + 0.2 * 其余片段（最多再计 2 个），避免长文档占优
            score = best.score + 0.2 * sum(h.score for h in hits[1:3])
            section_tokens = set(tokenize(f"{best.section.heading} {best.section.text}"))
            matched = sorted(query_terms & section_tokens)
            docs.append(
                DocHit(
                    doc=best.doc,
                    section=best.section,
                    score=score,
                    section_scores=[(h.section.section_id, round(h.score, 3)) for h in hits[:3]],
                    matched_terms=matched[:10],
                )
            )
        docs.sort(key=lambda d: (-d.score, d.doc.doc_id))
        return docs[:top_k]
