"""查看各评测问题的检索分数，用于校准 MockPlanner 的可信阈值。

执行：python scripts/inspect_retrieval.py
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eqa.corpus import Corpus  # noqa: E402
from eqa.retrieval import QueryExpander, RetrievalIndex  # noqa: E402


def main() -> None:
    scenarios = json.loads((ROOT / "data" / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
    for corpus_path in ("data/documents.json", "data/documents_v2.json"):
        corpus = Corpus.load(corpus_path)
        index = RetrievalIndex(corpus, QueryExpander.load())
        print(f"\n=== {corpus_path} ===")
        for scenario in scenarios:
            if scenario.get("corpus", "data/documents.json") != corpus_path:
                continue
            question = scenario["question"]
            print(f"- {scenario['id']}: {question}")
            for expanded in (False, True):
                used, added = index.expander.expand(question) if expanded else (question, [])
                hits = index.search_docs(used, top_k=5)
                rendered = " | ".join(
                    f"{hit.doc_id}/{hit.section.section_id}:{hit.score:.2f}" for hit in hits
                ) or "（无命中）"
                print(f"    expanded={expanded} added={added} -> {rendered}")


if __name__ == "__main__":
    main()
