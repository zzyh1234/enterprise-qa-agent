"""审计测试：引用必须逐字、冲突必须说明（含真实模型输出的兜底）。"""
from __future__ import annotations

import unittest

from tests.support import DATA

from eqa.agent import KnowledgeQAAgent
from eqa.analysis import IntentMatcher
from eqa.audit import audit_citations, audit_conflict, audit_result
from eqa.corpus import Corpus
from eqa.models import Citation
from eqa.planners import ReplayPlanner


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = Corpus.load(str(DATA / "documents.json"))
        cls.matcher = IntentMatcher.load()

    def citation(self, doc_id: str, section_id: str, quote: str | None = None) -> Citation:
        doc = self.corpus.get(doc_id)
        section = doc.section(section_id)
        return Citation(
            doc_id=doc_id,
            title=doc.title,
            section_id=section_id,
            heading=section.heading,
            version_date=doc.version_date,
            quote=quote if quote is not None else section.text,
            supports="测试",
        )

    def test_verbatim_citation_passes(self) -> None:
        checks = audit_citations([self.citation("D01", "D01-4.2")], self.corpus)
        self.assertTrue(checks[0]["supported"])

    def test_tampered_quote_is_rejected(self) -> None:
        tampered = self.citation("D01", "D01-4.2", "员工应当在费用发生之日起 45 个自然日内提交报销申请。")
        checks = audit_citations([tampered], self.corpus)
        self.assertFalse(checks[0]["supported"])
        self.assertIn("不一致", checks[0]["reason"])

    def test_unknown_document_is_rejected(self) -> None:
        fake = self.citation("D01", "D01-4.2")
        fake.doc_id = "D404"
        checks = audit_citations([fake], self.corpus)
        self.assertFalse(checks[0]["supported"])
        self.assertIn("不存在", checks[0]["reason"])

    def test_conflict_acknowledgement(self) -> None:
        citations = [self.citation("D01", "D01-4.2"), self.citation("D02", "D02-3.1")]
        acknowledged = audit_conflict(
            "报销期限是多久？", "D01 规定 30 天，D02 规定 15 天，存在冲突。", citations, self.matcher, self.corpus
        )
        self.assertTrue(acknowledged["detected"])
        self.assertTrue(acknowledged["acknowledged"])
        missing = audit_conflict(
            "报销期限是多久？", "报销期限为 15 天。", citations, self.matcher, self.corpus
        )
        self.assertFalse(missing["acknowledged"])
        self.assertEqual(missing["missing_in_answer"], ["30"])

    def test_audit_result_combines_checks(self) -> None:
        citations = [self.citation("D05", "D05-3.1")]
        report = audit_result(
            "VPN 账号多久需要重新认证？",
            "VPN 账号每 180 天需要重新认证一次，依据 D05-3.1。",
            citations,
            self.matcher,
            self.corpus,
        )
        self.assertTrue(report["passed"])
        tampered = [self.citation("D05", "D05-3.1", "VPN 账号每 90 天需要重新认证。")]
        report = audit_result(
            "VPN 账号多久需要重新认证？", "VPN 账号每 90 天需要重新认证。", tampered, self.matcher, self.corpus
        )
        self.assertFalse(report["passed"])
        self.assertFalse(report["citations_supported"])


class ModelGuardrailTests(unittest.TestCase):
    """即使由「模型」直接给出答案，审计也能拦住编造引用与未说明的冲突。"""

    def run_with_decisions(self, decisions):
        corpus_path = str(DATA / "documents.json")
        agent = KnowledgeQAAgent(corpus_path=corpus_path, planner=ReplayPlanner(decisions))
        return agent.answer("报销期限是多久？")

    def test_fabricated_citation_downgrades_status(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天。",
                        "citations": [
                            {
                                "doc_id": "D02",
                                "section_id": "D02-3.1",
                                "quote": "员工应在费用发生之日起 15 天内提交报销单据（模型改写）",
                            }
                        ],
                    },
                    "reason": "模型认为自己知道答案",
                }
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertIn("审计未通过", result.answer)
        self.assertTrue(result.evidence_gaps)

    def test_unacknowledged_conflict_downgrades_status(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天（依据 D02）。",
                        "citations": [
                            {"doc_id": "D02", "section_id": "D02-3.1", "quote": "15 天"},
                            {"doc_id": "D01", "section_id": "D01-4.2", "quote": "30 个自然日"},
                        ],
                    },
                    "reason": "模型擅自选择了较新的文件",
                }
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertTrue(result.audit["conflict"]["detected"])
        self.assertFalse(result.audit["conflict"]["acknowledged"])

    def test_answer_without_any_citation_is_rejected(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天。",
                        "citations": [],
                    },
                    "reason": "模型凭记忆直接作答",
                }
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertIn("没有提供任何逐字引用", result.answer)

    def test_malformed_model_payload_does_not_crash(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天。",
                        "citations": ["D02"],
                        "conflicts": ["D01 与 D02 不一致"],
                        "evidence_gaps": "无",
                    },
                    "reason": "模型输出结构不规范",
                }
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertTrue(result.evidence_gaps)

    def test_malformed_ask_user_payload_does_not_crash(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "ask_user",
                    "arguments": {
                        "question": "请确认适用制度",
                        "options": "D01 或 D02",
                        "conflicts": ["两份文件不一致"],
                        "reason": "需要用户确认",
                    },
                    "reason": "信息不足，转为追问",
                }
            ]
        )
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.options, ["D01 或 D02"])

    def test_low_retrieval_confidence_is_rejected(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "search_docs",
                    "arguments": {"query": "公司年会抽奖的奖品是什么？"},
                    "reason": "先检索",
                },
                {
                    "action": "read_snippet",
                    "arguments": {"doc_id": "D04", "section_id": "D04-2.3"},
                    "reason": "随便读一段",
                },
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "年会奖品是 300% 日工资。",
                        "citations": [
                            {
                                "doc_id": "D04",
                                "section_id": "D04-2.3",
                                "quote": "按员工日工资收入的 300% 支付未休年假工资报酬",
                            }
                        ],
                    },
                    "reason": "硬答",
                },
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertTrue(any("检索置信度不足" in gap for gap in result.evidence_gaps))

    def test_unread_high_score_document_with_different_value_is_rejected(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "search_docs",
                    "arguments": {"query": "报销期限是多久？", "use_expansion": True},
                    "reason": "先检索",
                },
                {
                    "action": "read_snippet",
                    "arguments": {"doc_id": "D02", "section_id": "D02-3.1"},
                    "reason": "只读 D02",
                },
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天。",
                        "citations": [
                            {
                                "doc_id": "D02",
                                "section_id": "D02-3.1",
                                "quote": "员工应在费用发生之日起 15 天内提交报销单据",
                            }
                        ],
                    },
                    "reason": "只读了一份就作答",
                },
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertTrue(any("潜在冲突" in gap for gap in result.evidence_gaps))
        self.assertTrue(any("D01" in gap for gap in result.evidence_gaps))
        self.assertFalse(any("D06" in gap for gap in result.evidence_gaps))

    def test_conflict_read_but_only_one_side_cited_is_rejected(self) -> None:
        result = self.run_with_decisions(
            [
                {
                    "action": "read_snippet",
                    "arguments": {"doc_id": "D01", "section_id": "D01-4.2"},
                    "reason": "先读 D01",
                },
                {
                    "action": "read_snippet",
                    "arguments": {"doc_id": "D02", "section_id": "D02-3.1"},
                    "reason": "再读 D02",
                },
                {
                    "action": "finish",
                    "arguments": {
                        "status": "answered",
                        "answer": "报销期限为 15 天。",
                        "citations": [
                            {
                                "doc_id": "D02",
                                "section_id": "D02-3.1",
                                "quote": "员工应在费用发生之日起 15 天内提交报销单据",
                            }
                        ],
                    },
                    "reason": "模型只引用较新的那一份",
                },
            ]
        )
        self.assertEqual(result.status, "audit_failed")
        self.assertTrue(result.audit["conflict"]["detected"])
        self.assertFalse(result.audit["conflict"]["acknowledged"])
        self.assertIn("30", " ".join(result.audit["conflict"]["missing_in_answer"]))


if __name__ == "__main__":
    unittest.main()
