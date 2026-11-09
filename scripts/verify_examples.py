"""对 examples/ 下所有 output.json 做引用/冲突审计，写入 examples/verify-results.txt。

执行：python scripts/verify_examples.py
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eqa import cli  # noqa: E402


def main() -> int:
    pattern = str(ROOT / "examples" / "*" / "output.json")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["verify", pattern])
    text = buffer.getvalue()
    target = ROOT / "examples" / "verify-results.txt"
    target.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {target}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
