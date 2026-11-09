"""运行全部测试，并把 UTF-8 结果写入 examples/test-results.txt。

执行：python scripts/run_tests.py
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "examples" / "test-results.txt"


def main() -> int:
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = (
        f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)} -> {'OK' if result.wasSuccessful() else 'FAILED'}"
    )
    print(summary)
    print(f"written: {OUTPUT}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
