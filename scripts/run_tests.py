"""运行全部测试，并把 UTF-8 结果写入 examples/test-results.txt。

执行：python scripts/run_tests.py
说明：结果里的耗时会被归一化为 <elapsed>s，避免每次运行都产生无意义的 diff。
"""
from __future__ import annotations

import io
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "examples" / "test-results.txt"


def main() -> int:
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    text = re.sub(r"in \d+\.\d+s", "in <elapsed>s", buffer.getvalue())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")
    summary = (
        f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)} -> {'OK' if result.wasSuccessful() else 'FAILED'}"
    )
    print(summary)
    print(f"written: {OUTPUT.relative_to(ROOT).as_posix()}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
