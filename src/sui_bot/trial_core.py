"""اکانت تست رایگان — ۵۰۰ مگابایت / ۱ روز، فقط یک‌بار برای هر کاربر.

هستهٔ خالص: رکورد کاربرانی که تست گرفته‌اند در trial_users.json.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sui_bot.trial")

TRIAL_GB = 0.5               # ۵۰۰ مگابایت
TRIAL_DAYS = 1


def trial_volume_bytes() -> int:
    return int(TRIAL_GB * 1024 * 1024 * 1024)


def trial_expiry_ts() -> int:
    from datetime import timedelta
    return int((datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).timestamp())


class TrialStore:
    """trial_users.json — {"users": {"123456": {...}}}"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"users": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
        except Exception as exc:
            logger.error("trial file corrupt, starting fresh: %s", exc)
        return {"users": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def has_used(self, tg_id: int) -> bool:
        return str(int(tg_id)) in self._load()["users"]

    def record(self, tg_id: int, client_name: str) -> None:
        data = self._load()
        data["users"][str(int(tg_id))] = {
            "client_name": client_name,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def count(self) -> int:
        return len(self._load()["users"])
