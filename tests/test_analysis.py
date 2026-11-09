"""意图识别、证据判定、冲突与优先级解析测试。"""
from __future__ import annotations

import unittest

from tests.support import DATA

from eqa.analysis import (
    IntentMatcher,
    annual_leave_tier,
    build_evidence,
    detect_conflict,
    detect_user_choice,
    parse_precedence,
    parse_years,
)
from eqa.corpus import Corpus


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matcher = IntentMatcher.load()
        cls.corpus = Corpus.load(str(DATA / "documents.json"))
        cls.corpus_v2 = Corpus.load(str(DATA / "documents_v2.json"))

    def test_intents_are_detected_for_acceptance_questions(self) -> None:
        cases = {
            "报销期限是多久？": "reimbursement_deadline",
            "费用发生以后最晚什么时候交报销单？": "reimbursement_deadline",
            "我工作六年了，一年能休几天带薪假？": "annual_leave_days",
            "VPN 账号多久需要重新认证？": "vpn_reauth",
            "VPN 密码有什么要求？": "vpn_password",
            "报销的发票不小心弄没了还能报吗？": "receipt_lost",
            "出差住酒店一晚能报多少钱？": "hotel_standard",
            "云盘免费版单文件上传上限是多少？": "cloud_upload_limit",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                rule = self.matcher.detect(question)
                self.assertIsNotNone(rule)
                self.assertEqual(rule.intent_id, expected)

    def test_out_of_scope_question_has_no_intent(self) -> None:
        self.assertIsNone(self.matcher.detect("公司年会抽奖的奖品是什么？"))

    def test_vpn_password_and_reauth_are_distinguished(self) -> None:
        self.assertEqual(self.matcher.detect("VPN 密码多久换一次？").intent_id, "vpn_password")
        self.assertEqual(self.matcher.detect("VPN 账号多久需要重新认证？").intent_id, "vpn_reauth")

    def test_cross_reference_alone_is_not_evidence(self) -> None:
        rule = self.matcher.get("receipt_lost")
        doc, section = self.corpus.find_section("D01", "D01-4.3")
        self.assertFalse(
            rule.has_evidence(section.text),
            "D01-4.3 只以《…》形式引用其他制度，不应被视为票据遗失流程的证据",
        )
        doc, section = self.corpus.find_section("D09", "D09-1.1")
        self.assertTrue(rule.has_evidence(section.text))

    def test_conflict_between_d01_and_d02(self) -> None:
        rule = self.matcher.get("reimbursement_deadline")
        evidences = []
        for doc_id, section_id in (("D01", "D01-4.2"), ("D02", "D02-3.1")):
            doc, section = self.corpus.find_section(doc_id, section_id)
            evidences.append(build_evidence(doc, section, rule))
        conflict = detect_conflict(evidences, rule)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.topic, "报销期限")
        self.assertEqual({value.value for value in conflict.values}, {"30", "15"})
        self.assertEqual(conflict.resolution, "unresolved")

    def test_single_document_tiers_are_not_a_conflict(self) -> None:
        rule = self.matcher.get("annual_leave_days")
        doc, section = self.corpus.find_section("D04", "D04-2.1")
        evidences = [build_evidence(doc, section, rule)]
        self.assertIsNone(detect_conflict(evidences, rule))
        self.assertEqual({value for value, _ in evidences[0].values}, {"5", "10", "15"})

    def test_memo_precedence_resolves_conflict(self) -> None:
        memo_doc, memo_section = self.corpus_v2.find_section("D03", "D03-1.1")
        chosen = parse_precedence(memo_section.text, self.corpus_v2)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.doc_id, "D02")
        # 默认语料里根本没有 D03，因此这条解释在默认语料中不可见
        self.assertIsNone(self.corpus.get("D03"))

    def test_user_choice_only_accepts_explicit_selection(self) -> None:
        docs = [self.corpus.get("D01"), self.corpus.get("D02")]
        self.assertEqual(detect_user_choice("那按最新发布的版本执行呢？", docs).doc_id, "D02")
        self.assertEqual(detect_user_choice("按 D01 执行", docs).doc_id, "D01")
        self.assertEqual(detect_user_choice("按操作指引来", docs).doc_id, "D02")
        self.assertIsNone(detect_user_choice("那我到底该听谁的？", docs))

    def test_annual_leave_tier_and_year_parsing(self) -> None:
        doc, section = self.corpus.find_section("D04", "D04-2.1")
        self.assertEqual(parse_years("我工作六年了，一年能休几天带薪假？"), 6)
        self.assertEqual(annual_leave_tier(section.text, 6), ("满 1 年不满 10 年", 5))
        self.assertEqual(annual_leave_tier(section.text, 12), ("满 10 年不满 20 年", 10))
        self.assertEqual(annual_leave_tier(section.text, 25), ("满 20 年", 15))
        self.assertIsNone(annual_leave_tier(section.text, None))


if __name__ == "__main__":
    unittest.main()
