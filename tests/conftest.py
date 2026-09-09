"""Global test configuration: provide dummy env before sui_bot imports."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("SUI_HOST", "https://panel.example.com")
os.environ.setdefault("SUI_TOKEN", "test-token")
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("BOT_LOG_FILE", "")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sui-bot-test-"))
