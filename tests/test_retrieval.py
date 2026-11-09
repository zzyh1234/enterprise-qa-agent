"""检索与查询改写测试。"""
from __future__ import annotations

import unittest

from tests.support import DATA

from eqa.corpus import Corpus
from eqa.retrieval import QueryExpander, RetrievalIndex
from eqa.text import normalize, tokenize


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = Corpus.load(str(DATA / "documents.json"))
        cls.index = RetrievalIndex(cls.corpus, QueryExpander.load())

    def test_tokenize_handles_chinese_and_ascii(self) -> None:
        tokens = tokenize("VPN 账号每 180 天")
        self.assertIn("vpn", tokens)
        self.assertIn("180", tokens)
        self.assertIn("账号", tokens)
        self.assertIn("账", tokens)

    def test_normalize_converts_fullwidth_and_lowercases(self) -> None:
        self.assertEqual(normalize("ＶＰＮ　账号"), "vpn 账号")

    def test_rewrite_improves_score_and_recall(self) -> None:
        question = "我工作六年了，一年能休几天带薪假？"
        raw = self.index.search_docs(question, top_k=3)
        used, added = self.index.expander.expand(question)
        self.assertTrue(added, "同义词改写应至少新增一个检索词")
        self.assertIn("年休假", added)
        expanded = self.index.search_docs(used, top_k=3)
        self.assertGreater(expanded[0].score, raw[0].score)
        self.assertEqual(expanded[0].doc_id, "D04")
        self.assertEqual(expanded[0].section.section_id, "D04-2.1")

    def test_out_of_scope_stays_below_conservative_threshold(self) -> None:
        hits = self.index.search_docs("公司年会抽奖的奖品是什么？", top_k=5)
        self.assertTrue(hits)
        self.assertTrue(all(hit.score < 60 for hit in hits))

    def test_doc_aggregation_keeps_best_section_and_metadata(self) -> None:
        hits = self.index.search_docs("VPN 账号多久需要重新认证？", top_k=3)
        self.assertEqual(hits[0].doc_id, "D05")
        self.assertEqual(hits[0].section.section_id, "D05-3.1")
        self.assertEqual(hits[0].doc.version_date, "2024-05-20")
        self.assertEqual(hits[0].doc.applies_to, "全体员工")


if __name__ == "__main__":
    unittest.main()
