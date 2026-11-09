"""无需安装的入口：python run.py <subcommand> ..."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from eqa.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
