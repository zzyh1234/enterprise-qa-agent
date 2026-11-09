"""工具层测试：检索、片段读取、故障注入。"""
from __future__ import annotations

import unittest

from tests.support import DATA

from eqa.corpus import Corpus
from eqa.retrieval import QueryExpander, RetrievalIndex
from eqa.tools import KnowledgeTools


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = Corpus.load(str(DATA / "documents.json"))
        self.index = RetrievalIndex(self.corpus, QueryExpander.load())

    def make_tools(self, faults=None) -> KnowledgeTools:
        return KnowledgeTools(self.corpus, self.index, faults=faults)

    def test_search_returns_hits_with_metadata(self) -> None:
        result = self.make_tools().call("search_docs", {"query": "报销期限", "top_k": 3})
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.payload["total"], 1)
        hit = result.payload["hits"][0]
        for field in ("doc_id", "title", "version_date", "applies_to", "score", "section_id", "preview"):
            self.assertIn(field, hit)
        self.assertNotIn("text", hit, "检索工具不应直接返回全文")

    def test_search_with_expansion_reports_added_terms(self) -> None:
        result = self.make_tools().call(
            "search_docs", {"query": "年假有几天", "use_expansion": True}
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.payload["expansion_added"])
        self.assertNotEqual(result.payload["query_used"], result.payload["query"])

    def test_read_snippet_returns_verbatim_text(self) -> None:
        result = self.make_tools().call("read_snippet", {"doc_id": "D01", "section_id": "D01-4.2"})
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["doc_id"], "D01")
        self.assertIn("30 个自然日", result.payload["text"])
        self.assertIn("逾期提交", result.payload["text"])

    def test_read_unknown_section_lists_available_ids(self) -> None:
        result = self.make_tools().call("read_snippet", {"doc_id": "D01", "section_id": "D99-9.9"})
        self.assertFalse(result.ok)
        self.assertIn("D01-4.2", result.error)

    def test_read_unknown_document_lists_documents(self) -> None:
        result = self.make_tools().call("read_snippet", {"doc_id": "D404", "section_id": "X"})
        self.assertFalse(result.ok)
        self.assertIn("D01", result.error)

    def test_unknown_tool_is_rejected(self) -> None:
        result = self.make_tools().call("delete_everything", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)

    def test_fault_injection_error_then_recovery(self) -> None:
        tools = self.make_tools(
            [{"tool": "search_docs", "mode": "error", "remaining": 1, "message": "超时（测试）"}]
        )
        first = tools.call("search_docs", {"query": "VPN"})
        second = tools.call("search_docs", {"query": "VPN"})
        self.assertFalse(first.ok)
        self.assertEqual(first.error, "超时（测试）")
        self.assertEqual(first.fault_injected, "error")
        self.assertTrue(second.ok)
        self.assertEqual(tools.triggered, ["search_docs:error"])

    def test_fault_injection_empty_mode(self) -> None:
        tools = self.make_tools([{"tool": "search_docs", "mode": "empty", "remaining": 1}])
        result = tools.call("search_docs", {"query": "报销"})
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["hits"], [])
        self.assertEqual(result.payload["total"], 0)

    def test_empty_query_is_rejected(self) -> None:
        result = self.make_tools().call("search_docs", {"query": "   "})
        self.assertFalse(result.ok)
        self.assertIn("非空 query", result.error)


if __name__ == "__main__":
    unittest.main()
