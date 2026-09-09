"""نمایندگی فروش — هر کاربر لینک دعوت دارد، سود درصدی از خریدهای زیرمجموعه.

هستهٔ خالص (بدون تلگرام) — داده در resellers.json کنار shop_orders.json.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("sui_bot.reseller")

DEFAULT_COMMISSION_PCT = 20  # پیش‌فرض ۲۰٪

# پلن‌های مخصوص زیرمجموعه‌ها — فقط کاربرانِ دعوت‌شده می‌بینند
RESELLER_PLANS: list[dict[str, Any]] = [
    {"slug": "r1",  "months": 1,  "gb": 50,  "price_toman": 110_000, "title": "۱ ماهه نمایندگی", "desc": "۵۰ گیگ — ۱ ماه (قیمت زیرمجموعه)"},
    {"slug": "r3",  "months": 3,  "gb": 150, "price_toman": 290_000, "title": "۳ ماهه نمایندگی", "desc": "۱۵۰ گیگ — ۳ ماه (قیمت زیرمجموعه)"},
    {"slug": "r6",  "months": 6,  "gb": 300, "price_toman": 500_000, "title": "۶ ماهه نمایندگی", "desc": "۳۰۰ گیگ — ۶ ماه (قیمت زیرمجموعه)"},
    {"slug": "r12", "months": 12, "gb": 600, "price_toman": 850_000, "title": "۱۲ ماهه نمایندگی", "desc": "۶۰۰ گیگ — ۱ سال (قیمت زیرمجموعه)"},
]


def reseller_plan(slug: str) -> dict[str, Any] | None:
    for p in RESELLER_PLANS:
        if p["slug"] == slug:
            return p
    return None


def format_reseller_toman(amount: int | float) -> str:
    """۱۲۳٬۴۵۶ تومان — با جداکنندهٔ هزارگان (نمایندگی)."""
    return f"{int(amount):,} تومان"


class ResellerStore:
    """resellers.json — هر رکورد: telegram_id, invited_by, balance_toman, pct, created_at, earned_total."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"members": [], "payouts": [], "seq": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("members"), list):
                return data
        except Exception as exc:
            logger.error("resellers file corrupt, starting fresh: %s", exc)
        return {"members": [], "payouts": [], "seq": 0}

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

    # --- عضویت ---
    def ensure_member(self, telegram_id: int, invited_by: int | None = None) -> dict[str, Any]:
        """ساخت رکورد اگر نیست؛ invited_by فقط اولین بار ثبت می‌شود."""
        data = self._load()
        for m in data["members"]:
            if m["telegram_id"] == int(telegram_id):
                return m
        member = {
            "telegram_id": int(telegram_id),
            "invited_by": int(invited_by) if invited_by else None,
            "balance_toman": 0,
            "earned_total": 0,
            "pct": None,  # None = پیش‌فرض
            "fixed_toman": None,  # اگر ست باشد به‌جای درصد، مبلغ ثابت از هر خرید
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        }
        data["members"].append(member)
        self._save(data)
        return member

    def get(self, telegram_id: int) -> dict[str, Any] | None:
        for m in self._load()["members"]:
            if m["telegram_id"] == int(telegram_id):
                return m
        return None

    def inviter_of(self, telegram_id: int) -> int | None:
        m = self.get(telegram_id)
        return m.get("invited_by") if m else None

    def invited_by_member(self, inviter_id: int) -> list[dict[str, Any]]:
        return [m for m in self._load()["members"] if m.get("invited_by") == int(inviter_id)]

    # --- سود ---
    def credit_commission(self, buyer_id: int, amount_toman: int, order_id: int) -> tuple[int, int] | None:
        """خریدِ buyer → سود به دعوت‌کننده. (member_id, سود) برمی‌گرداند؛ بدون دعوت‌کننده None."""
        inviter = self.inviter_of(buyer_id)
        if not inviter:
            return None
        m = self.get(inviter)
        if m is None:
            return None
        fixed = m.get("fixed_toman")
        if fixed:
            commission = int(fixed)
        else:
            pct = m.get("pct") if m.get("pct") is not None else DEFAULT_COMMISSION_PCT
            commission = int(amount_toman * pct / 100)
        if commission <= 0:
            return None
        data = self._load()
        for mem in data["members"]:
            if mem["telegram_id"] == int(inviter):
                mem["balance_toman"] = mem.get("balance_toman", 0) + commission
                mem["earned_total"] = mem.get("earned_total", 0) + commission
        data["payouts"].append({
            "order_id": order_id,
            "buyer_id": int(buyer_id),
            "member_id": int(inviter),
            "commission_toman": commission,
            "at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        })
        self._save(data)
        return inviter, commission

    def settle(self, telegram_id: int) -> int:
        """تسویه دستی: موجودی صفر، سابقهٔ پرداخت ثبت. مبلغ تسویه‌شده برمی‌گردد."""
        data = self._load()
        paid = 0
        for m in data["members"]:
            if m["telegram_id"] == int(telegram_id):
                paid = m.get("balance_toman", 0)
                m["balance_toman"] = 0
                data["payouts"].append({
                    "order_id": None,
                    "buyer_id": None,
                    "member_id": int(telegram_id),
                    "commission_toman": -paid,
                    "at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                })
        self._save(data)
        return paid

    def set_pct(self, telegram_id: int, pct: int) -> bool:
        data = self._load()
        for m in data["members"]:
            if m["telegram_id"] == int(telegram_id):
                m["pct"] = int(pct)
                m["fixed_toman"] = None  # درصد جای مبلغ ثابت را می‌گیرد
                self._save(data)
                return True
        return False

    def set_fixed(self, telegram_id: int, amount_toman: int) -> bool:
        """سود مبلغ ثابت از هر خرید (جای درصد)."""
        data = self._load()
        for m in data["members"]:
            if m["telegram_id"] == int(telegram_id):
                m["fixed_toman"] = int(amount_toman)
                m["pct"] = None
                self._save(data)
                return True
        return False

    def commission_label(self, telegram_id: int) -> str:
        m = self.get(telegram_id)
        if not m:
            return f"{DEFAULT_COMMISSION_PCT}٪"
        if m.get("fixed_toman"):
            return f"{format_reseller_toman(m['fixed_toman'])} ثابت"
        pct = m.get("pct") if m.get("pct") is not None else DEFAULT_COMMISSION_PCT
        return f"{pct}٪"

    def adjust_balance(self, telegram_id: int, amount_toman: int) -> int | None:
        """ویرایش دستی کیف پول (مثبت/منفی). موجودی جدید یا None اگر کاربر نیست."""
        data = self._load()
        for m in data["members"]:
            if m["telegram_id"] == int(telegram_id):
                m["balance_toman"] = max(0, m.get("balance_toman", 0) + int(amount_toman))
                if amount_toman > 0:
                    m["earned_total"] = m.get("earned_total", 0) + int(amount_toman)
                self._save(data)
                return m["balance_toman"]
        return None

    def totals(self) -> dict[str, int]:
        """جمع کل سود پرداخت‌شده به نماینده‌ها (از سابقه)."""
        data = self._load()
        total = 0
        for p in data["payouts"]:
            c = int(p.get("commission_toman") or 0)
            if c > 0:
                total += c
        return {"paid_out": total}

    def payouts_for(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """سابقهٔ سود یک نماینده — جدیدترین اول."""
        data = self._load()
        rows = [p for p in data["payouts"] if p.get("member_id") == int(telegram_id)]
        rows.sort(key=lambda p: p.get("at") or "", reverse=True)
        return rows[:limit]

    def all(self) -> list[dict[str, Any]]:
        return self._load()["members"]
