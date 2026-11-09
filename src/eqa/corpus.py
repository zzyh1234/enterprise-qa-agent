"""语料加载与查询。"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config
from .models import Document, Section


class Corpus:
    def __init__(self, documents: List[Document], source: str = "", note: str = ""):
        self._documents = list(documents)
        self._index: Dict[str, Document] = {doc.doc_id: doc for doc in self._documents}
        self.source = source
        self.note = note

    @classmethod
    def load(cls, path: str | None = None) -> "Corpus":
        target = path or config.DEFAULT_CORPUS
        raw = config.load_json(target)
        documents = [Document.from_dict(item) for item in raw.get("documents", [])]
        if not documents:
            raise ValueError(f"语料为空或格式不正确: {target}")
        return cls(
            documents,
            source=str(config.resolve_path(target)),
            note=str(raw.get("note", "")),
        )

    # 查询 -----------------------------------------------------------------
    @property
    def documents(self) -> List[Document]:
        return list(self._documents)

    def get(self, doc_id: str) -> Optional[Document]:
        return self._index.get(doc_id)

    def find_section(self, doc_id: str, section_id: str) -> Tuple[Optional[Document], Optional[Section]]:
        doc = self._index.get(doc_id)
        if doc is None:
            return None, None
        return doc, doc.section(section_id)

    def iter_sections(self) -> Iterator[Tuple[Document, Section]]:
        for doc in self._documents:
            for section in doc.sections:
                yield doc, section

    def section_ids(self, doc_id: str) -> List[str]:
        doc = self._index.get(doc_id)
        return doc.section_ids() if doc else []

    def stats(self) -> Dict[str, Any]:
        doc_count = len(self._documents)
        section_count = sum(len(d.sections) for d in self._documents)
        char_count = sum(len(s.text) for _, s in self.iter_sections())
        return {
            "source": self.source,
            "documents": doc_count,
            "sections": section_count,
            "chars": char_count,
            "doc_ids": [d.doc_id for d in self._documents],
        }
