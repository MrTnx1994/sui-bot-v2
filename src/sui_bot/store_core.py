"""فروشگاه کارتبهکارت — هسته (محصولات، سفارش‌ها، بدون درگاه).

هیچ وابستگی به تلگرام یا aiohttp ندارد — فقط منطق خالص.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sui_bot.store")

# ---------------------------------------------------------------------------
# پلن‌های فروشگاه (محصولات) — قیمت به تومان
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    slug: str
    months: int
    gb: int
    price_toman: int
    title: str
    desc: str
    reseller_only: bool = False  # فقط برای زیرمجموعه‌های نماینده‌ها


PLANS: list[Plan] = [
    Plan("m1",   1,  50,  150_000, "۱ ماهه", "۵۰ گیگ حجم — ۱ ماه"),
    Plan("m3",   3, 150,  400_000, "۳ ماهه", "۱۵۰ گیگ حجم — ۳ ماه"),
    Plan("m6",   6, 300,  700_000, "۶ ماهه", "۳۰۰ گیگ حجم — ۶ ماه"),
    Plan("m12", 12, 600, 1_200_000, "۱۲ ماهه", "۶۰۰ گیگ حجم — ۱ سال"),
    # پلن‌های نمایندگی — فقط کاربرانِ دعوت‌شده (قیمت کمتر)
    Plan("r1",   1,  50,  110_000, "۱ ماهه", "۵۰ گیگ — ۱ ماه", reseller_only=True),
    Plan("r3",   3, 150,  290_000, "۳ ماهه", "۱۵۰ گیگ — ۳ ماه", reseller_only=True),
    Plan("r6",   6, 300,  500_000, "۶ ماهه", "۳۰۰ گیگ — ۶ ماه", reseller_only=True),
    Plan("r12", 12, 600,  850_000, "۱۲ ماهه", "۶۰۰ گیگ — ۱ سال", reseller_only=True),
]


def get_plan(slug: str) -> Plan | None:
    for plan in PLANS:
        if plan.slug == slug:
            return plan
    return None


def format_toman(amount: int) -> str:
    """۱۲۳٬۴۵۶ تومان — با جداکننده هزارگان."""
    return f"{amount:,} تومان"


# ---------------------------------------------------------------------------
# وضعیت سفارش
# ---------------------------------------------------------------------------
class OrderStatus:
    PENDING = "pending"        # کاربر پلن رو انتخاب کرده ولی هنوز ۴ رقم آخر رو نفرستاده
    SUBMITTED = "submitted"    # ۴ رقم آخر ارسال شده، منتظر تأیید ادمین
    APPROVED = "approved"      # ادمین تأیید کرده (در انتظار ساخت کلاینت)
    PROVISIONED = "provisioned"  # کلاینت ساخته شده و لینک ارسال شده
    REJECTED = "rejected"      # ادمین رد کرده
    FAILED = "failed"          # خطا در ساخت


# ---------------------------------------------------------------------------
# ذخیره‌سازی سفارش‌ها (JSON — اتمیک)
# ---------------------------------------------------------------------------
class OrderStore:
    """نگهداری سفارش‌ها در یک فایل JSON با نوشتن اتمیک."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"orders": [], "seq": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("orders"), list):
                return data
        except Exception as exc:
            logger.error("store file corrupt, starting fresh: %s", exc)
        return {"orders": [], "seq": 0}

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

    def create_order(
        self,
        telegram_id: int,
        plan_slug: str,
        *,
        client_name: str | None = None,
        amount_toman: int | None = None,
    ) -> dict[str, Any]:
        plan = get_plan(plan_slug)
        if plan is None:
            raise ValueError(f"unknown plan: {plan_slug}")
        data = self._load()
        data["seq"] += 1
        order = {
            "id": data["seq"],
            "telegram_id": int(telegram_id),
            "plan": plan_slug,
            "amount_toman": int(amount_toman) if amount_toman is not None else plan.price_toman,
            "status": OrderStatus.PENDING,
            "client_id": None,
            "client_name": client_name,  # None یعنی هنوز اسم انتخاب نشده
            "card_last4": None,
            "request_id": None,         # unique token for approve/reject
            "created_at": datetime.now(timezone.utc).isoformat(),
            "paid_at": None,
            "provisioned_at": None,
        }
        data["orders"].append(order)
        self._save(data)
        return order

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        data = self._load()
        for order in data["orders"]:
            if order["id"] == int(order_id):
                return order
        return None

    def find_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        data = self._load()
        for order in data["orders"]:
            if order.get("request_id") == request_id:
                return order
        return None

    def update_order(self, order_id: int, **fields: Any) -> dict[str, Any] | None:
        data = self._load()
        for order in data["orders"]:
            if order["id"] == int(order_id):
                order.update(fields)
                self._save(data)
                return order
        return None

    def orders_for(self, telegram_id: int, limit: int = 10) -> list[dict[str, Any]]:
        data = self._load()
        rows = [o for o in data["orders"] if o["telegram_id"] == int(telegram_id)]
        rows.sort(key=lambda o: o["id"], reverse=True)
        return rows[:limit]

    def all_orders(self) -> list[dict[str, Any]]:
        """همهٔ سفارش‌ها (برای آمار نمایندگی)."""
        return self._load()["orders"]

    def pending_for_admin(self) -> list[dict[str, Any]]:
        """سفارش‌های منتظر تأیید ادمین (SUBMITTED)."""
        data = self._load()
        return [o for o in data["orders"] if o.get("status") == OrderStatus.SUBMITTED]

    def name_taken(self, name: str) -> bool:
        """آیا این اسم قبلاً در سفارش‌های فروشگاه استفاده/رزرو شده؟"""
        data = self._load()
        for o in data["orders"]:
            cname = (o.get("client_name") or "").lower()
            if not cname:
                continue
            if o.get("status") in (OrderStatus.APPROVED, OrderStatus.PROVISIONED):
                if cname == name.lower():
                    return True
            elif o.get("status") == OrderStatus.PENDING and cname == name.lower():
                # اسم رزروشده توسط سفارش درجالانتظار (سفارشهای رد/ناموفق آزاد میشوند)
                return True
        return False


# ---------------------------------------------------------------------------
# اسم کاربر-مانند برای لینک ساب (به‌جای shop-<tg>-<order>)
# ---------------------------------------------------------------------------
SUB_NAME_RE = re.compile(r"^[a-z0-9_]{3,16}$")
SUB_NAME_RESERVED = {
    "admin", "support", "sub", "rawsub", "api", "shop", "bot", "panel",
    "root", "sui", "test", "www", "mail", "info", "help", "login", "app",
}


def normalize_sub_name(raw: str) -> str:
    """اسم دلخواه کاربر → اسم معتبر lowercase؛ None یعنی نامعتبر."""
    text = (raw or "").strip().lower()
    # فاصله/خط‌تیره به آندرلاین، حروف غیرمجاز حذف
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    return text or ""


def sub_name_valid(name: str) -> bool:
    if not SUB_NAME_RE.match(name):
        return False
    if name in SUB_NAME_RESERVED:
        return False
    return True


def suggest_names(base: str, is_taken) -> list[str]:
    """۳ پیشنهاد برای اسم اشغال‌شده — مثلاً farzaneh2 / farzaneh_x7."""
    out: list[str] = []
    candidates = [f"{base}2", f"{base}_x", f"{base}{secrets.randbelow(90) + 10}"]
    for cand in candidates:
        if len(cand) <= 16 and sub_name_valid(cand) and not is_taken(cand):
            out.append(cand)
        if len(out) == 3:
            break
    # fallback: اسم رندوم کامل
    if not out:
        out = [f"u{secrets.token_hex(3)}"]
    return out


def random_sub_name() -> str:
    return f"u{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# توابع کمکی برای ساخت کلاینت در پنل
# ---------------------------------------------------------------------------
def make_client_name(telegram_id: int, order_id: int) -> str:
    """نام یکتا برای کلاینت پنل — بدون کاراکتر خطرناک."""
    return f"shop-{telegram_id}-{order_id}"


def make_expiry(months: int) -> int:
    """تاریخ انقضا بر حسب **ثانیه**ی اپوک — هماهنگ با بقیهٔ کلاینت‌های پنل.

    نکته: پنل S-UI همهٔ expiryهای موجود را بر حسب ثانیه نگه می‌دارد؛
    فرستادن میلی‌ثانیه مانیتور انقضا را با «year out of range» می‌کشد.
    """
    return int((datetime.now(timezone.utc) + timedelta(days=30 * months)).timestamp())


def make_expiry_days(days: int) -> int:
    """انقضا بر حسب ثانیه برای مدت‌های دلخواه (نه لزوماً مضرب ۳۰ روز)."""
    return int((datetime.now(timezone.utc) + timedelta(days=int(days))).timestamp())


def gb_to_bytes(gb: int) -> int:
    return int(gb) * 1024 * 1024 * 1024
