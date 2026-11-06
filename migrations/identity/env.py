"""Alembic 环境入口：逻辑收敛在 migrations/_common，这里只做分发。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import run_migrations  # noqa: E402

run_migrations()
