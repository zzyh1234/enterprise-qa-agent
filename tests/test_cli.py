"""CLI 冒烟测试：保证 README 中的可复制命令真的能跑。"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.support import ROOT


class CliTests(unittest.TestCase):
    def run_cli(
        self,
        *args: str,
        timeout: int = 120,
        env_overrides: dict | None = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(
            [sys.executable, str(ROOT / "run.py"), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )

    def test_ask_json(self) -> None:
        proc = self.run_cli("ask", "VPN 账号多久需要重新认证？", "--planner", "mock", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "answered")
        self.assertEqual(payload["citations"][0]["doc_id"], "D05")
        self.assertTrue(payload["audit"]["passed"])

    def test_ask_budget_flag(self) -> None:
        proc = self.run_cli(
            "ask",
            "云盘免费版单文件上传上限是多少？",
            "--planner",
            "mock",
            "--max-tool-calls",
            "1",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "incomplete_budget_exceeded")
        self.assertEqual(payload["run"]["budget"]["used_tool_calls"], 1)

    def test_batch_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.run_cli(
                "batch", "--only", "01_vpn_normal,02_reimbursement_conflict", "--out", tmp
            )
            self.assertEqual(batch.returncode, 0, batch.stderr)
            for scenario_id in ("01_vpn_normal", "02_reimbursement_conflict"):
                directory = pathlib.Path(tmp) / scenario_id
                self.assertTrue((directory / "input.json").exists())
                self.assertTrue((directory / "output.json").exists())
                self.assertTrue((directory / "trace.json").exists())
                self.assertTrue((directory / "trace.md").exists())

            verify = self.run_cli("verify", str(pathlib.Path(tmp) / "*" / "output.json"))
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertEqual(verify.stdout.count("PASS"), 2)

    def test_context_followup_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli("batch", "--only", "02_reimbursement_conflict", "--out", tmp)
            context = pathlib.Path(tmp) / "02_reimbursement_conflict" / "output.json"
            self.assertTrue(context.exists())
            proc = self.run_cli(
                "ask",
                "那按最新发布的版本执行呢？",
                "--context",
                str(context),
                "--planner",
                "mock",
                "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "answered")
            self.assertIn("15", payload["answer"])
            self.assertIn("30", payload["answer"])

    def test_default_planner_falls_back_without_key(self) -> None:
        proc = self.run_cli(
            "ask",
            "VPN 账号多久需要重新认证？",
            "--json",
            env_overrides={"EQA_API_KEY": None, "EQA_BASE_URL": None, "EQA_MODEL": None},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["run"]["planner"], "mock")
        self.assertEqual(payload["run"]["planner_requested"], "openai")
        self.assertTrue(payload["run"]["planner_fallback"])
        self.assertIn("回退", proc.stderr)
        self.assertTrue(any("回退" in note for note in payload["run"]["notes"]))

    def test_no_fallback_without_key_fails_fast(self) -> None:
        proc = self.run_cli(
            "ask",
            "VPN 账号多久需要重新认证？",
            "--json",
            "--no-fallback",
            env_overrides={"EQA_API_KEY": None, "EQA_BASE_URL": None, "EQA_MODEL": None},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("EQA_API_KEY", proc.stderr)

    def test_batch_openai_without_key_is_marked_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(
                "batch",
                "--only",
                "01_vpn_normal",
                "--out",
                tmp,
                "--planner",
                "openai",
                env_overrides={"EQA_API_KEY": None, "EQA_BASE_URL": None, "EQA_MODEL": None},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = pathlib.Path(tmp) / "01_vpn_normal" / "output.json"
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["run"]["planner"], "mock")
            self.assertEqual(payload["run"]["planner_requested"], "openai")
            self.assertTrue(payload["run"]["planner_fallback"])
            self.assertIn("planner_requested", (pathlib.Path(tmp) / "01_vpn_normal" / "input.json").read_text(encoding="utf-8"))

    def test_corpus_subcommand(self) -> None:
        proc = self.run_cli("corpus")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("documents", proc.stdout)
        self.assertIn("D01", proc.stdout)


if __name__ == "__main__":
    unittest.main()
