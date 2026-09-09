"""Plans & pricing store — کامل قابل مدیریت از ربات.

سه حالت قیمت‌گذاری:
- manual  → قیمتِ ثابت هر پلن (price_toman داخل خود پلن)
- per_gb  → قیمت = حجم × قیمت هر گیگ
- monthly → قیمت ماهانه: برای هر مدت می‌توان قیمت جدا گذاشت (month_prices)؛
            اگر مدتِ خاصی تعریف نشده باشد = monthly_toman × تعداد ماه

پلن‌ها در plans_config.json ذخیره می‌شوند (atomic).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

PRICING_MODES = {"manual", "per_gb", "monthly"}
MIN_GB, MAX_GB = 1, 100_000
MIN_DAYS, MAX_DAYS = 1, 3650

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def fa_num(value: int | str) -> str:
    """اعداد فارسی با جداکنندهٔ فارسی (٬) — خوانا در متن RTL تلگرام."""
    s = f"{int(value):,}".replace(",", "٬")
    return "".join(_FA_DIGITS[int(ch)] if ch.isdigit() else ch for ch in s)


def fa_toman(value: int) -> str:
    return f"{fa_num(value)} تومان"


@dataclass(slots=True)
class Plan:
    slug: str
    title: str
    gb: int
    days: int          # 30 = یک ماه
    price_toman: int   # 0 = خودکار طبق حالت قیمت‌گذاری
    active: bool = True
    reseller_only: bool = False


@dataclass(slots=True)
class Pricing:
    mode: str = "manual"                      # manual | per_gb | monthly
    per_gb_toman: int = 5_000                 # قیمت هر گیگ
    monthly_toman: int = 100_000              # قیمت هر ماه (پیش‌فرض)
    month_prices: dict[str, int] = field(default_factory=dict)  # "3": 400000
    # تخفیف/اضافه اختصاصی هر مشتری: "123456" → درصد (منفی = تخفیف، مثبت = اضافه)
    user_discounts: dict[str, int] = field(default_factory=dict)
    # قیمت اختصاصی مطلق هر مشتری: "123456" → {"<slug>": 120000}
    user_prices: dict[str, dict[str, int]] = field(default_factory=dict)

    def validate(self) -> None:
        if self.mode not in PRICING_MODES:
            raise ValueError(f"unsupported pricing mode: {self.mode}")
        if self.per_gb_toman < 0 or self.monthly_toman < 0:
            raise ValueError("prices must be non-negative")
        for key, value in self.month_prices.items():
            if not key.isdigit() or int(key) < 1 or value < 0:
                raise ValueError(f"invalid month price entry: {key}={value}")
        for key, value in self.user_discounts.items():
            if not key.isdigit() or not -90 <= int(value) <= 200:
                raise ValueError(f"invalid user discount: {key}={value}")
        for user, prices in self.user_prices.items():
            if not user.isdigit():
                raise ValueError(f"invalid user price key: {user}")
            for slug, value in prices.items():
                if value < 0:
                    raise ValueError(f"invalid user price: {user}/{slug}={value}")

    def months_for(self, days: int) -> int:
        return max(1, round(days / 30))

    def adjust_for_user(self, user_id: int | None, price: int) -> int:
        """اعمال قیمت اختصاصی (اولویت) یا درصد تخفیف/اضافه مشتری."""
        if user_id is None:
            return int(price)
        prices = self.user_prices.get(str(int(user_id)))
        if prices is not None and str(int(user_id)) in self.user_prices:
            _ = prices  # slug-aware در price_for_plan انجام می‌شود
        discount = self.user_discounts.get(str(int(user_id)))
        if discount:
            price = int(round(price * (100 + discount) / 100))
        return int(price)

    def user_plan_price(self, user_id: int, slug: str) -> int | None:
        """قیمت مطلق اختصاصی این مشتری برای این پلن (اگر ثبت شده)."""
        prices = self.user_prices.get(str(int(user_id)))
        if prices:
            return prices.get(slug)
        return None

    def price_for_plan(self, plan: Plan, user_id: int | None = None) -> int:
        """قیمت نهایی پلن — حالت قیمت‌گذاری سراسری تعیین‌کننده است.
        - manual  → قیمت ثبت‌شدهٔ خود پلن
        - per_gb  → حجم × قیمت هر گیگ
        - monthly → قیمت اختصاصی آن مدت، یا پایه × تعداد ماه
        سپس قیمت مطلق اختصاصی مشتری (اگر باشد) و بعد درصد تخفیف/اضافه اعمال می‌شود.
        """
        if self.mode == "per_gb":
            price = int(plan.gb * self.per_gb_toman)
        elif self.mode == "monthly":
            months = self.months_for(plan.days)
            override = self.month_prices.get(str(months))
            price = int(override if override is not None else self.monthly_toman * months)
        else:
            price = int(plan.price_toman)
        if user_id is not None:
            absolute = self.user_plan_price(user_id, plan.slug)
            if absolute is not None:
                return int(absolute)
            price = self.adjust_for_user(user_id, price)
        return int(price)

    def price_for_renewal(self, months: int) -> int | None:
        """قیمت تمدید برای N ماه؛ None یعنی حالت جاری برای تمدید معنا ندارد."""
        if self.mode == "monthly":
            override = self.month_prices.get(str(months))
            return int(override if override is not None else self.monthly_toman * months)
        return None


def _default_plans() -> tuple[list[Plan], Pricing]:
    """پلن‌های پیش‌فرض — همان لیست قبلی hardcode شده."""
    plans = [
        Plan("m1", "۱ ماهه", 50, 30, 150_000),
        Plan("m3", "۳ ماهه", 150, 90, 400_000),
        Plan("m6", "۶ ماهه", 300, 180, 700_000),
        Plan("m12", "۱۲ ماهه", 600, 365, 1_200_000),
        Plan("r1", "۱ ماهه نمایندگی", 50, 30, 110_000, True, True),
        Plan("r3", "۳ ماهه نمایندگی", 150, 90, 290_000, True, True),
        Plan("r6", "۶ ماهه نمایندگی", 300, 180, 500_000, True, True),
        Plan("r12", "۱۲ ماهه نمایندگی", 600, 365, 850_000, True, True),
    ]
    return plans, Pricing(mode="manual")


class PlansStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.plans: list[Plan] = []
        self.pricing = Pricing()
        self.load()

    # --- persistence ----------------------------------------------------------
    def load(self) -> None:
        if not self.path.is_file():
            self.plans, self.pricing = _default_plans()
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            plans = [
                Plan(
                    slug=str(item["slug"]),
                    title=str(item["title"]),
                    gb=int(item["gb"]),
                    days=int(item["days"]),
                    price_toman=int(item.get("price_toman", 0)),
                    active=bool(item.get("active", True)),
                    reseller_only=bool(item.get("reseller_only", False)),
                )
                for item in data.get("plans", [])
            ]
            pricing = Pricing(
                mode=str(data.get("pricing", {}).get("mode", "manual")),
                per_gb_toman=int(data.get("pricing", {}).get("per_gb_toman", 5_000)),
                monthly_toman=int(data.get("pricing", {}).get("monthly_toman", 100_000)),
                month_prices={
                    str(k): int(v)
                    for k, v in (data.get("pricing", {}).get("month_prices") or {}).items()
                },
                user_discounts={
                    str(k): int(v)
                    for k, v in (data.get("pricing", {}).get("user_discounts") or {}).items()
                },
                user_prices={
                    str(k): {str(s): int(p) for s, p in (prices or {}).items()}
                    for k, prices in (data.get("pricing", {}).get("user_prices") or {}).items()
                },
            )
            pricing.validate()
            self.plans, self.pricing = plans, pricing
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            self.plans, self.pricing = _default_plans()

    def save(self) -> None:
        self.pricing.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "plans": [asdict(p) for p in self.plans],
            "pricing": {
                "mode": self.pricing.mode,
                "per_gb_toman": self.pricing.per_gb_toman,
                "monthly_toman": self.pricing.monthly_toman,
                "month_prices": self.pricing.month_prices,
                "user_discounts": self.pricing.user_discounts,
                "user_prices": self.pricing.user_prices,
            },
        }
        fd, tmp = tempfile_name(self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- queries --------------------------------------------------------------
    def get(self, slug: str) -> Plan | None:
        for plan in self.plans:
            if plan.slug == slug:
                return plan
        return None

    def visible_plans(self, reseller: bool = False) -> list[Plan]:
        out = []
        for plan in self.plans:
            if not plan.active:
                continue
            if plan.reseller_only and not reseller:
                continue
            out.append(plan)
        return out

    # --- mutations --------------------------------------------------------------
    def add_plan(self, title: str, gb: int, days: int, price_toman: int, *, reseller_only: bool = False) -> Plan:
        gb, days, price_toman = int(gb), int(days), int(price_toman)
        if not title or not title.strip():
            raise ValueError("plan title is required")
        if not MIN_GB <= gb <= MAX_GB:
            raise ValueError(f"gb must be {MIN_GB}..{MAX_GB}")
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise ValueError(f"days must be {MIN_DAYS}..{MAX_DAYS}")
        if price_toman < 0:
            raise ValueError("price must be non-negative")
        with self._lock:
            slug = "p" + secrets.token_hex(3)
            while self.get(slug) is not None:
                slug = "p" + secrets.token_hex(3)
            plan = Plan(
                slug=slug,
                title=title.strip()[:48],
                gb=gb,
                days=days,
                price_toman=price_toman,
                active=True,
                reseller_only=bool(reseller_only),
            )
            self.plans.append(plan)
            self.save()
        return plan

    def update_plan(self, slug: str, *, title: str | None = None, gb: int | None = None,
                    days: int | None = None, price_toman: int | None = None,
                    reseller_only: bool | None = None) -> Plan:
        plan = self.get(slug)
        if plan is None:
            raise KeyError(f"unknown plan slug: {slug}")
        if title is not None:
            if not title.strip():
                raise ValueError("plan title is required")
            plan.title = title.strip()[:48]
        if gb is not None:
            if not MIN_GB <= int(gb) <= MAX_GB:
                raise ValueError(f"gb must be {MIN_GB}..{MAX_GB}")
            plan.gb = int(gb)
        if days is not None:
            if not MIN_DAYS <= int(days) <= MAX_DAYS:
                raise ValueError(f"days must be {MIN_DAYS}..{MAX_DAYS}")
            plan.days = int(days)
        if price_toman is not None:
            if int(price_toman) < 0:
                raise ValueError("price must be non-negative")
            plan.price_toman = int(price_toman)
        if reseller_only is not None:
            plan.reseller_only = bool(reseller_only)
        with self._lock:
            self.save()
        return plan

    def delete_plan(self, slug: str) -> bool:
        plan = self.get(slug)
        if plan is None:
            return False
        with self._lock:
            self.plans.remove(plan)
            self.save()
        return True

    def toggle_plan(self, slug: str) -> bool:
        plan = self.get(slug)
        if plan is None:
            raise KeyError(f"unknown plan slug: {slug}")
        plan.active = not plan.active
        with self._lock:
            self.save()
        return plan.active

    # --- pricing mutations ------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in PRICING_MODES:
            raise ValueError(f"unsupported pricing mode: {mode}")
        self.pricing.mode = mode
        with self._lock:
            self.save()

    def set_per_gb(self, price: int) -> None:
        if int(price) <= 0:
            raise ValueError("per-GB price must be positive")
        self.pricing.per_gb_toman = int(price)
        with self._lock:
            self.save()

    def set_monthly(self, price: int) -> None:
        if int(price) <= 0:
            raise ValueError("monthly price must be positive")
        self.pricing.monthly_toman = int(price)
        with self._lock:
            self.save()

    def set_month_price(self, months: int, price: int | None) -> None:
        """قیمت اختصاصی برای مدتِ N ماه؛ None = حذف (fallback به monthly_toman)."""
        if months < 1 or months > 36:
            raise ValueError("months must be 1..36")
        if price is None:
            self.pricing.month_prices.pop(str(months), None)
        else:
            if int(price) <= 0:
                raise ValueError("price must be positive")
            self.pricing.month_prices[str(months)] = int(price)
        with self._lock:
            self.save()

    # --- قیمت اختصاصی مشتری -----------------------------------------------------
    def set_user_discount(self, user_id: int, percent: int | None) -> None:
        """درصد تخفیف (منفی) یا اضافه‌قیمت (مثبت) برای یک مشتری؛ None = حذف."""
        if percent is None:
            self.pricing.user_discounts.pop(str(int(user_id)), None)
        else:
            if not -90 <= int(percent) <= 200:
                raise ValueError("percent must be -90..200")
            self.pricing.user_discounts[str(int(user_id))] = int(percent)
        with self._lock:
            self.save()

    def set_user_plan_price(self, user_id: int, slug: str, price: int | None) -> None:
        """قیمت مطلق یک مشتری برای یک پلن؛ None = حذف."""
        if price is None:
            self.pricing.user_prices.get(str(int(user_id)), {}).pop(slug, None)
            if not self.pricing.user_prices.get(str(int(user_id))):
                self.pricing.user_prices.pop(str(int(user_id)), None)
        else:
            if int(price) < 0:
                raise ValueError("price must be non-negative")
            self.pricing.user_prices.setdefault(str(int(user_id)), {})[slug] = int(price)
        with self._lock:
            self.save()

    def user_pricing_summary(self, user_id: int) -> str | None:
        """خلاصهٔ تنظیمات اختصاصی این مشتری؛ None یعنی قیمت عادی می‌گیرد."""
        uid = str(int(user_id))
        discount = self.pricing.user_discounts.get(uid)
        prices = self.pricing.user_prices.get(uid)
        if not discount and not prices:
            return None
        parts = []
        if discount:
            kind = "تخفیف" if discount < 0 else "اضافه"
            parts.append(f"{kind} {fa_num(abs(discount))}٪")
        if prices:
            for slug, value in prices.items():
                plan = self.get(slug)
                title = plan.title if plan else slug
                parts.append(f"{title}: {fa_toman(value)}")
        return "؛ ".join(parts)


def tempfile_name(path: Path) -> tuple[int, str]:
    fd, tmp = __import__("tempfile").mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    return fd, tmp
