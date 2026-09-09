"""Tests for per-customer pricing & Persian numerals."""

from __future__ import annotations

import pytest

from sui_bot.plans_store import PlansStore, fa_num, fa_toman


@pytest.fixture()
def store(tmp_path):
    s = PlansStore(tmp_path / "p.json")
    s.set_mode("per_gb")
    s.set_per_gb(5_000)
    return s


class TestFaNum:
    def test_digits(self):
        assert fa_num(150000) == "۱۵۰٬۰۰۰"

    def test_toman_suffix(self):
        assert fa_toman(5000) == "۵٬۰۰۰ تومان"


class TestUserPricing:
    def test_discount_percent(self, store):
        plan = store.get("m1") or store.add_plan("آزمون", 10, 30, 0)
        store.set_user_discount(777, -20)
        base = store.pricing.price_for_plan(plan)
        assert store.pricing.price_for_plan(plan, user_id=777) == int(base * 0.8)

    def test_extra_charge(self, store):
        plan = store.add_plan("گران", 10, 30, 0)
        store.set_user_discount(888, 10)
        base = store.pricing.price_for_plan(plan)
        assert store.pricing.price_for_plan(plan, user_id=888) == int(base * 1.1)

    def test_absolute_override_beats_discount(self, store):
        plan = store.add_plan("ویژه", 10, 30, 0)
        store.set_user_discount(999, -20)
        store.set_user_plan_price(999, plan.slug, 100_000)
        assert store.pricing.price_for_plan(plan, user_id=999) == 100_000

    def test_other_users_unaffected(self, store):
        plan = store.add_plan("عادی", 10, 30, 0)
        store.set_user_discount(777, -50)
        assert store.pricing.price_for_plan(plan, user_id=777) != store.pricing.price_for_plan(plan, user_id=555)

    def test_discount_bounds(self, store):
        with pytest.raises(ValueError):
            store.set_user_discount(1, -95)
        with pytest.raises(ValueError):
            store.set_user_discount(1, 500)

    def test_clear_user_pricing(self, store):
        plan = store.add_plan("حذفی", 10, 30, 0)
        store.set_user_discount(111, -10)
        store.set_user_plan_price(111, plan.slug, 50_000)
        store.set_user_plan_price(111, plan.slug, None)
        store.set_user_discount(111, None)
        assert store.user_pricing_summary(111) is None

    def test_persistence(self, tmp_path):
        path = tmp_path / "p.json"
        s1 = PlansStore(path)
        s1.set_user_discount(777, -20)
        s2 = PlansStore(path)
        assert s2.pricing.user_discounts.get("777") == -20

    def test_summary_text(self, store):
        plan = store.add_plan("سومی", 10, 30, 0)
        store.set_user_discount(222, -15)
        summary = store.user_pricing_summary(222)
        assert "تخفیف" in summary and "۱۵" in summary
        store.set_user_plan_price(222, plan.slug, 80_000)
        assert "۸۰٬۰۰۰" in store.user_pricing_summary(222)
