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
    def run_cli(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
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
        proc = self.run_cli("ask", "VPN 账号多久需要重新认证？", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "answered")
        self.assertEqual(payload["citations"][0]["doc_id"], "D05")
        self.assertTrue(payload["audit"]["passed"])

    def test_ask_budget_flag(self) -> None:
        proc = self.run_cli(
            "ask", "云盘免费版单文件上传上限是多少？", "--max-tool-calls", "1", "--json"
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
                "ask", "那按最新发布的版本执行呢？", "--context", str(context), "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "answered")
            self.assertIn("15", payload["answer"])
            self.assertIn("30", payload["answer"])

    def test_corpus_subcommand(self) -> None:
        proc = self.run_cli("corpus")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("documents", proc.stdout)
        self.assertIn("D01", proc.stdout)


if __name__ == "__main__":
    unittest.main()
