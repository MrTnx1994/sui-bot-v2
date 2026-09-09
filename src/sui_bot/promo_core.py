"""کد تخفیف — هستهٔ خالص (بدون تلگرام).

هر کد: درصد تخفیف، سقف تعداد استفاده، تاریخ انقضا، فعال/غیرفعال.
داده در discounts.json ذخیره می‌شود (نوشتن اتمیک — هم‌الگو با بقیهٔ استورها).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sui_bot.promo")

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def normalize_code(raw: str) -> str:
    """کد کاربر → شکل ذخیره‌سازی (حروف بزرگ، بدون فاصله)."""
    return (raw or "").strip().upper()


def code_valid(raw: str) -> bool:
    return bool(CODE_RE.match(normalize_code(raw)))


@dataclass(frozen=True, slots=True)
class PromoResult:
    """نتیجهٔ اعتبارسنجی یک کد برای یک کاربر."""

    ok: bool
    percent: int = 0
    reason: str = ""  # not_found | inactive | expired | exhausted | used_by_user | bad_format
    amount: int = 0   # مبلغ تخفیف به تومان (وقتی price داده شده باشد)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DiscountStore:
    """discounts.json — لیست کدها + مصرف هر کاربر."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # --- persistence ----------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"codes": [], "seq": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("codes"), list):
                return data
        except Exception as exc:
            logger.error("discounts file corrupt, starting fresh: %s", exc)
        return {"codes": [], "seq": 0}

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

    # --- queries --------------------------------------------------------------
    def all(self) -> list[dict[str, Any]]:
        return self._load()["codes"]

    def get(self, code: str) -> dict[str, Any] | None:
        key = normalize_code(code)
        for item in self._load()["codes"]:
            if item.get("code") == key:
                return item
        return None

    @staticmethod
    def _expired(item: dict[str, Any]) -> bool:
        raw = item.get("expires_at")
        if not raw:
            return False
        try:
            return datetime.fromisoformat(str(raw)) <= _utcnow()
        except ValueError:
            return False

    def evaluate(self, code: str, user_id: int) -> PromoResult:
        """آیا این کد برای این کاربر قابل استفاده است؟"""
        if not code_valid(code):
            return PromoResult(False, 0, "bad_format")
        item = self.get(code)
        if item is None:
            return PromoResult(False, 0, "not_found")
        if not item.get("active", True):
            return PromoResult(False, 0, "inactive")
        if self._expired(item):
            return PromoResult(False, 0, "expired")
        max_uses = int(item.get("max_uses") or 0)
        if max_uses > 0 and int(item.get("used_count") or 0) >= max_uses:
            return PromoResult(False, 0, "exhausted")
        used_by = item.get("used_by") or {}
        if str(int(user_id)) in used_by:
            return PromoResult(False, 0, "used_by_user")
        return PromoResult(True, int(item.get("percent") or 0), "")

    def discount_amount(self, code: str, user_id: int, price_toman: int) -> PromoResult:
        """اعتبارسنجی + محاسبهٔ مبلغ تخفیف (تخفیف نمی‌تواند مبلغ را زیر ۱۰۰۰ تومان ببرد)."""
        result = self.evaluate(code, user_id)
        if not result.ok or price_toman <= 0:
            return result
        amount = int(round(price_toman * result.percent / 100))
        if price_toman > 1000:
            amount = max(0, min(amount, price_toman - 1000))
        return PromoResult(True, result.percent, "", amount)

    def consume(self, code: str, user_id: int, order_id: int) -> bool:
        """ثبت یک استفاده (فقط وقتی پرداخت قطعی شد صدا زده شود)."""
        key = normalize_code(code)
        data = self._load()
        for item in data["codes"]:
            if item.get("code") == key:
                used_by = item.setdefault("used_by", {})
                if str(int(user_id)) in used_by:
                    return False  # قبلاً مصرف شده
                used_by[str(int(user_id))] = int(order_id)
                item["used_count"] = int(item.get("used_count") or 0) + 1
                self._save(data)
                return True
        return False

    # --- mutations (admin) ------------------------------------------------------
    def create(self, code: str, percent: int, max_uses: int, days_valid: int) -> dict[str, Any]:
        key = normalize_code(code)
        if not CODE_RE.match(key):
            raise ValueError("کد باید ۳ تا ۳۲ کاراکتر لاتین/عدد/خط‌تیره باشد")
        if not 1 <= int(percent) <= 90:
            raise ValueError("درصد باید بین ۱ تا ۹۰ باشد")
        if int(max_uses) < 0:
            raise ValueError("سقف استفاده نامعتبر است")
        data = self._load()
        for item in data["codes"]:
            if item.get("code") == key:
                raise ValueError("این کد قبلاً ساخته شده است")
        expires_at = ""
        if int(days_valid) > 0:
            expires_at = (_utcnow() + timedelta(days=int(days_valid))).isoformat()
        entry = {
            "code": key,
            "percent": int(percent),
            "max_uses": int(max_uses),
            "used_count": 0,
            "used_by": {},
            "active": True,
            "expires_at": expires_at,
            "created_at": _utcnow().isoformat(),
        }
        data["codes"].append(entry)
        self._save(data)
        return entry

    def toggle(self, code: str) -> bool | None:
        data = self._load()
        for item in data["codes"]:
            if item.get("code") == normalize_code(code):
                item["active"] = not item.get("active", True)
                self._save(data)
                return item["active"]
        return None

    def delete(self, code: str) -> bool:
        key = normalize_code(code)
        data = self._load()
        before = len(data["codes"])
        data["codes"] = [item for item in data["codes"] if item.get("code") != key]
        if len(data["codes"]) == before:
            return False
        self._save(data)
        return True
