"""决策器测试：JSON 解析、OpenAI 兼容协议循环（假 transport）、回放与纠正。"""
from __future__ import annotations

import json
import unittest

from tests.support import DATA

from eqa.agent import KnowledgeQAAgent
from eqa.analysis import IntentMatcher
from eqa.corpus import Corpus
from eqa.planners import (
    OpenAICompatiblePlanner,
    PlannerError,
    PlannerInput,
    ReplayPlanner,
    build_planner,
    parse_decision,
)


def scripted_transport(contents):
    """按顺序返回预设 content，同时记录请求，模拟 OpenAI 兼容接口。"""
    calls = []

    def transport(request):
        calls.append(request)
        index = len(calls) - 1
        content = contents[index] if index < len(contents) else contents[-1]
        return {"choices": [{"message": {"content": content}}]}

    return transport, calls


FINISH_DECISION = {
    "action": "finish",
    "arguments": {
        "status": "answered",
        "answer": "VPN 账号每 180 天需要进行一次重新认证（依据 D05-3.1）。",
        "citations": [
            {
                "doc_id": "D05",
                "section_id": "D05-3.1",
                "quote": "VPN 账号每 180 天需要进行一次重新认证",
                "supports": "直接回答",
            }
        ],
        "evidence_gaps": [],
    },
    "reason": "已读取原文，可以回答",
}


class DecisionParsingTests(unittest.TestCase):
    def test_parse_plain_json(self) -> None:
        decision = parse_decision(json.dumps(FINISH_DECISION, ensure_ascii=False))
        self.assertEqual(decision.action, "finish")
        self.assertEqual(decision.arguments["status"], "answered")

    def test_parse_fenced_json(self) -> None:
        content = "```json\n" + json.dumps(FINISH_DECISION, ensure_ascii=False) + "\n```"
        decision = parse_decision(content)
        self.assertEqual(decision.action, "finish")

    def test_parse_json_with_surrounding_text(self) -> None:
        content = "我认为应该这样：" + json.dumps(FINISH_DECISION, ensure_ascii=False) + " 谢谢"
        decision = parse_decision(content)
        self.assertEqual(decision.action, "finish")

    def test_invalid_action_rejected(self) -> None:
        with self.assertRaises(PlannerError):
            parse_decision('{"action": "delete_all", "arguments": {}}')

    def test_missing_query_rejected(self) -> None:
        with self.assertRaises(PlannerError):
            parse_decision('{"action": "search_docs", "arguments": {}}')

    def test_finish_without_status_rejected(self) -> None:
        with self.assertRaises(PlannerError):
            parse_decision('{"action": "finish", "arguments": {"answer": "x"}}')

    def test_build_planner_kinds(self) -> None:
        self.assertEqual(build_planner("mock").name, "mock")
        self.assertEqual(build_planner("openai", api_key="k").name, "openai")
        self.assertEqual(build_planner("replay", decisions=[]).name, "replay")
        with self.assertRaises(ValueError):
            build_planner("unknown")


class OpenAIProtocolLoopTests(unittest.TestCase):
    def make_agent(self, contents, **kwargs):
        transport, calls = scripted_transport(contents)
        planner = OpenAICompatiblePlanner(
            model="test-model", api_key="test-key", transport=transport
        )
        agent = KnowledgeQAAgent(
            corpus_path=str(DATA / "documents.json"), planner=planner, **kwargs
        )
        return agent, calls

    def test_model_driven_react_loop(self) -> None:
        contents = [
            json.dumps(
                {
                    "action": "search_docs",
                    "arguments": {"query": "VPN 账号多久需要重新认证？"},
                    "reason": "先用用户问题检索",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "action": "read_snippet",
                    "arguments": {"doc_id": "D05", "section_id": "D05-3.1"},
                    "reason": "命中 D05，读取原文再回答",
                },
                ensure_ascii=False,
            ),
            json.dumps(FINISH_DECISION, ensure_ascii=False),
        ]
        agent, calls = self.make_agent(contents, max_steps=8, max_tool_calls=8)
        result = agent.answer("VPN 账号多久需要重新认证？")

        self.assertEqual(result.status, "answered")
        self.assertEqual(len(result.citations), 1)
        self.assertTrue(result.audit["passed"])
        self.assertEqual(
            [step.action for step in result.steps], ["search_docs", "read_snippet", "finish"]
        )
        self.assertEqual(len(calls), 3)

        first_request = calls[0]
        self.assertEqual(first_request["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(first_request["headers"]["Authorization"], "Bearer test-key")
        messages = first_request["payload"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("search_docs", messages[0]["content"])
        # 第 2 次请求应包含上一步工具观察，体现「看结果再决策」
        second_prompt = calls[1]["payload"]["messages"][-1]["content"]
        self.assertIn("step 1", second_prompt)
        self.assertIn("命中", second_prompt)

    def test_invalid_output_triggers_single_repair(self) -> None:
        contents = ["这不是 JSON", json.dumps(FINISH_DECISION, ensure_ascii=False)]
        agent, calls = self.make_agent(contents)
        result = agent.answer("VPN 账号多久需要重新认证？")
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.steps[0].action, "planner_error")
        self.assertEqual(len(calls), 2)
        self.assertIn("不是 JSON", calls[1]["payload"]["messages"][-1]["content"])

    def test_persistent_invalid_output_fails_explicitly(self) -> None:
        agent, _ = self.make_agent(["坏输出", "还是坏输出", "仍然坏输出"])
        result = agent.answer("VPN 账号多久需要重新认证？")
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.failures)
        self.assertIn("失败", result.answer)

    def test_tool_failure_is_visible_to_model(self) -> None:
        contents = [
            json.dumps(
                {"action": "search_docs", "arguments": {"query": "VPN"}, "reason": "先检索"},
                ensure_ascii=False,
            ),
            json.dumps(FINISH_DECISION, ensure_ascii=False),
        ]
        transport, calls = scripted_transport(contents)
        planner = OpenAICompatiblePlanner(model="m", api_key="k", transport=transport)
        agent = KnowledgeQAAgent(
            corpus_path=str(DATA / "documents.json"),
            planner=planner,
            faults=[{"tool": "search_docs", "mode": "error", "remaining": 1, "message": "超时"}],
        )
        result = agent.answer("VPN 账号多久需要重新认证？")
        self.assertIn("超时", calls[1]["payload"]["messages"][-1]["content"])
        self.assertFalse(result.steps[0].ok)
        self.assertIn("调用失败", result.steps[0].observation)

    def test_malformed_provider_response_is_rejected(self) -> None:
        planner = OpenAICompatiblePlanner(transport=lambda request: {"choices": []})
        planner_input = PlannerInput(
            question="VPN 账号多久需要重新认证？",
            history=[],
            prior_result=None,
            observations=[],
            corpus=Corpus.load(str(DATA / "documents.json")),
            matcher=IntentMatcher.load(),
        )
        with self.assertRaises(PlannerError):
            planner.decide(planner_input)


class ReplayPlannerTests(unittest.TestCase):
    def test_replay_runs_recorded_actions(self) -> None:
        decisions = [
            {
                "action": "search_docs",
                "arguments": {"query": "VPN 账号多久需要重新认证？"},
                "reason": "回放：检索",
            },
            {
                "action": "read_snippet",
                "arguments": {"doc_id": "D05", "section_id": "D05-3.1"},
                "reason": "回放：读原文",
            },
            dict(FINISH_DECISION, reason="回放：结束"),
        ]
        agent = KnowledgeQAAgent(
            corpus_path=str(DATA / "documents.json"), planner=ReplayPlanner(decisions)
        )
        result = agent.answer("VPN 账号多久需要重新认证？")
        self.assertEqual(result.status, "answered")
        self.assertEqual(len(result.citations), 1)

    def test_replay_exhaustion_returns_failed(self) -> None:
        agent = KnowledgeQAAgent(
            corpus_path=str(DATA / "documents.json"),
            planner=ReplayPlanner([{"action": "search_docs", "arguments": {"query": "VPN"}}]),
        )
        result = agent.answer("VPN 账号多久需要重新认证？")
        self.assertEqual(result.status, "failed")
        self.assertIn("回放决策已用尽", result.answer)


if __name__ == "__main__":
    unittest.main()
