"""端到端验收测试：覆盖正常、冲突、追问、相似问题、越界、工具失败、预算与语料替换。"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from tests.support import actions, load_scenario, run_by_id, run_scenario, write_temp_corpus

from eqa.audit import audit_citations
from eqa.corpus import Corpus
from eqa.models import Citation

BASE_BUDGET = {"max_steps": 10, "max_tool_calls": 8}


class AgentFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {}
        for scenario_id in ("01_vpn_normal", "02_reimbursement_conflict"):
            cls.results[scenario_id] = run_by_id(scenario_id)

        prior = cls.results["02_reimbursement_conflict"]
        cls.results["03_conflict_followup"] = run_scenario(
            load_scenario("03_conflict_followup"),
            prior=prior,
            history=[
                {"role": "user", "content": prior["question"]},
                {"role": "assistant", "content": prior["answer"]},
            ],
        )
        for scenario_id in (
            "04_receipt_lost_paraphrase",
            "05_annual_leave_paraphrase",
            "06_tool_fault_recovered",
            "07_tool_fault_blocked",
            "08_budget_exceeded",
            "09_corpus_swap_resolved",
            "10_out_of_scope",
            "11_replay_offline",
        ):
            cls.results[scenario_id] = run_by_id(scenario_id)

    # ------------------------------------------------------------- 正常路径
    def test_normal_answer_is_cited_and_audited(self) -> None:
        payload = self.results["01_vpn_normal"]
        self.assertEqual(payload["status"], "answered")
        self.assertIn("180", payload["answer"])
        self.assertEqual(len(payload["citations"]), 1)
        citation = payload["citations"][0]
        self.assertEqual((citation["doc_id"], citation["section_id"]), ("D05", "D05-3.1"))
        self.assertIn("180 天", citation["quote"])
        self.assertTrue(payload["audit"]["passed"])
        self.assertEqual(actions(payload)[:2], ["search_docs", "read_snippet"])
        self.assertEqual(actions(payload)[-1], "finish")

    # --------------------------------------------------------------- 文档冲突
    def test_conflict_lists_both_values_and_asks_user(self) -> None:
        payload = self.results["02_reimbursement_conflict"]
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertIn("30", payload["answer"])
        self.assertIn("15", payload["answer"])
        self.assertIn("冲突", payload["answer"])
        self.assertEqual(
            {citation["doc_id"] for citation in payload["citations"]}, {"D01", "D02"}
        )
        self.assertIsNotNone(payload["clarification"])
        self.assertTrue(payload["clarification"]["options"])
        self.assertEqual(payload["conflicts"][0]["resolution"], "unresolved")
        self.assertTrue(payload["audit"]["conflict"]["detected"])
        self.assertTrue(payload["audit"]["conflict"]["acknowledged"])
        self.assertIn("search_docs", actions(payload))
        self.assertEqual(actions(payload)[-1], "ask_user")

    # ------------------------------------------------------------- 上下文追问
    def test_followup_uses_user_selected_policy(self) -> None:
        payload = self.results["03_conflict_followup"]
        self.assertEqual(payload["status"], "answered")
        self.assertIn("15", payload["answer"])
        self.assertIn("30", payload["answer"], "仍需说明冲突条款，不能悄悄改口")
        self.assertEqual(
            {citation["doc_id"] for citation in payload["citations"]}, {"D01", "D02"}
        )
        self.assertEqual(payload["conflicts"][0]["resolution"], "user_selected")
        self.assertNotIn("search_docs", actions(payload), "上下文已给定时应直接读指定制度")
        self.assertEqual(actions(payload)[:2], ["read_snippet", "read_snippet"])

    # --------------------------------------------------------- 相似问题/检索改进
    def test_paraphrased_question_reads_original_section(self) -> None:
        payload = self.results["04_receipt_lost_paraphrase"]
        self.assertEqual(payload["status"], "answered")
        self.assertEqual(
            [(c["doc_id"], c["section_id"]) for c in payload["citations"]], [("D09", "D09-1.1")]
        )
        self.assertIn("记账联复印件", payload["answer"])
        self.assertTrue(payload["audit"]["passed"])
        self.assertTrue(any("D01" in note for note in payload["run"]["notes"]))

    def test_retrieval_rewrite_improves_low_confidence_query(self) -> None:
        payload = self.results["05_annual_leave_paraphrase"]
        self.assertEqual(payload["status"], "answered")
        self.assertIn("5 天", payload["answer"])
        self.assertIn("满 1 年不满 10 年", payload["answer"])
        self.assertEqual(payload["citations"][0]["doc_id"], "D04")
        searches = [
            step for step in payload["trace"] if step["decision"]["action"] == "search_docs"
        ]
        self.assertEqual(len(searches), 2)
        self.assertTrue(searches[1]["decision"]["arguments"]["use_expansion"])
        self.assertIn("年休假", payload["citations"][0]["quote"])

    # --------------------------------------------------------------- 工具失败
    def test_transient_tool_failure_is_retried(self) -> None:
        payload = self.results["06_tool_fault_recovered"]
        self.assertEqual(payload["status"], "answered")
        self.assertFalse(payload["trace"][0]["observation"]["ok"])
        self.assertIn("模拟故障", payload["trace"][0]["observation"].get("error", ""))
        self.assertTrue(payload["trace"][1]["observation"]["ok"])
        self.assertEqual(payload["citations"][0]["section_id"], "D05-3.2")
        self.assertIn("12 位", payload["answer"])

    def test_persistent_tool_failure_blocks_answer(self) -> None:
        payload = self.results["07_tool_fault_blocked"]
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["citations"], [])
        self.assertTrue(any("read_snippet" in gap for gap in payload["evidence_gaps"]))
        read_steps = [
            step for step in payload["trace"] if step["decision"]["action"] == "read_snippet"
        ]
        self.assertGreaterEqual(len(read_steps), 2)
        self.assertTrue(all(not step["observation"]["ok"] for step in read_steps))
        self.assertNotIn("记账联", payload["answer"])

    # ------------------------------------------------------------- 预算与越界
    def test_budget_limit_returns_explicit_incomplete_status(self) -> None:
        payload = self.results["08_budget_exceeded"]
        self.assertEqual(payload["status"], "incomplete_budget_exceeded")
        self.assertTrue(payload["run"]["budget"]["exceeded"])
        self.assertEqual(payload["run"]["budget"]["used_tool_calls"], 1)
        self.assertIn("上限", payload["answer"])
        self.assertTrue(payload["evidence_gaps"])
        self.assertEqual(payload["citations"], [])

    def test_out_of_scope_question_is_not_answered(self) -> None:
        payload = self.results["10_out_of_scope"]
        self.assertEqual(payload["status"], "insufficient_evidence")
        self.assertEqual(payload["citations"], [])
        self.assertTrue(payload["evidence_gaps"])
        self.assertIn("未在知识库中找到", payload["answer"])

    def test_replay_planner_example(self) -> None:
        payload = self.results["11_replay_offline"]
        self.assertEqual(payload["status"], "answered")
        self.assertEqual(payload["run"]["planner"], "replay")
        self.assertEqual(payload["citations"][0]["section_id"], "D05-3.1")
        self.assertTrue(payload["audit"]["passed"])

    def test_budget_never_exceeded_in_any_flow(self) -> None:
        for scenario_id, payload in self.results.items():
            with self.subTest(scenario=scenario_id):
                budget = payload["run"]["budget"]
                self.assertLessEqual(budget["used_steps"], budget["max_steps"])
                self.assertLessEqual(budget["used_tool_calls"], budget["max_tool_calls"])

    # ----------------------------------------------------------- 语料替换对照
    def test_corpus_change_changes_answer_and_actions(self) -> None:
        baseline = self.results["02_reimbursement_conflict"]
        resolved = self.results["09_corpus_swap_resolved"]
        self.assertEqual(baseline["status"], "needs_clarification")
        self.assertEqual(resolved["status"], "answered")
        self.assertIn("15", resolved["answer"])
        self.assertIn("D03", {citation["doc_id"] for citation in resolved["citations"]})
        self.assertNotIn("ask_user", actions(resolved))
        self.assertIn("ask_user", actions(baseline))
        self.assertNotEqual(actions(baseline), actions(resolved))
        self.assertEqual(resolved["conflicts"][0]["resolution"], "resolved_by_memo")
        self.assertEqual(resolved["conflicts"][0]["selected_doc_id"], "D02")

    def test_answer_follows_changed_document_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)

            def mutate_vpn(raw):
                for document in raw["documents"]:
                    if document["doc_id"] == "D05":
                        for section in document["sections"]:
                            section["text"] = section["text"].replace("180 天", "240 天")

            corpus_path = write_temp_corpus(tmp_path, mutate_vpn)
            payload = run_scenario(
                {
                    "id": "swap_vpn",
                    "question": "VPN 账号多久需要重新认证？",
                    "corpus": str(corpus_path),
                    "planner": "mock",
                    "budget": BASE_BUDGET,
                }
            )
            self.assertEqual(payload["status"], "answered")
            self.assertIn("240", payload["answer"])
            self.assertNotIn("180", payload["answer"])

    def test_conflict_values_follow_changed_document_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)

            def mutate_deadline(raw):
                for document in raw["documents"]:
                    if document["doc_id"] == "D01":
                        for section in document["sections"]:
                            section["text"] = section["text"].replace(
                                "30 个自然日", "45 个自然日"
                            )

            corpus_path = write_temp_corpus(tmp_path, mutate_deadline)
            payload = run_scenario(
                {
                    "id": "swap_deadline",
                    "question": "报销期限是多久？",
                    "corpus": str(corpus_path),
                    "planner": "mock",
                    "budget": BASE_BUDGET,
                }
            )
            self.assertEqual(payload["status"], "needs_clarification")
            self.assertIn("45", payload["answer"])
            self.assertNotIn("30 个自然日", payload["answer"])

    # ------------------------------------------------------------- 引用一致性
    def test_all_example_citations_are_verbatim(self) -> None:
        for scenario_id, payload in self.results.items():
            with self.subTest(scenario=scenario_id):
                corpus = Corpus.load(payload["run"]["corpus"])
                citations = [
                    Citation(
                        doc_id=item["doc_id"],
                        title=item["title"],
                        section_id=item["section_id"],
                        heading=item["heading"],
                        version_date=item["version_date"],
                        quote=item["quote"],
                        supports=item["supports"],
                    )
                    for item in payload["citations"]
                ]
                checks = audit_citations(citations, corpus)
                self.assertTrue(all(check["supported"] for check in checks))


if __name__ == "__main__":
    unittest.main()
