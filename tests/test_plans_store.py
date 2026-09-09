"""Tests for plans & pricing store."""

from __future__ import annotations

import pytest

from sui_bot.plans_store import PlansStore


@pytest.fixture()
def store(tmp_path):
    return PlansStore(tmp_path / "plans.json")


class TestDefaultsAndPersistence:
    def test_default_plans_created(self, store):
        assert len(store.plans) == 8
        assert store.get("m1").gb == 50
        assert store.get("r1").reseller_only is True

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "p.json"
        s1 = PlansStore(path)
        s1.add_plan("ویژه", 25, 15, 90_000)
        s2 = PlansStore(path)
        assert s2.get("m1") is not None
        added = [p for p in s2.plans if p.title == "ویژه"]
        assert len(added) == 1 and added[0].gb == 25 and added[0].days == 15


class TestPricing:
    def test_manual_mode_uses_plan_price(self, store):
        plan = store.get("m1")
        assert store.pricing.price_for_plan(plan) == 150_000

    def test_per_gb_mode(self, store):
        store.set_mode("per_gb")
        store.set_per_gb(5_000)
        assert store.pricing.price_for_plan(store.get("m1")) == 250_000
        assert store.pricing.price_for_plan(store.get("m3")) == 750_000

    def test_monthly_default_multiplier(self, store):
        store.set_mode("monthly")
        store.set_monthly(100_000)
        # m3 = 90 روز → 3 ماه
        assert store.pricing.price_for_plan(store.get("m3")) == 300_000

    def test_monthly_custom_overrides(self, store):
        store.set_mode("monthly")
        store.set_monthly(100_000)
        store.set_month_price(3, 250_000)
        assert store.pricing.price_for_plan(store.get("m3")) == 250_000
        # بدون override → پایه × تعداد
        assert store.pricing.price_for_plan(store.get("m6")) == 600_000

    def test_month_price_delete(self, store):
        store.set_mode("monthly")
        store.set_month_price(3, 250_000)
        store.set_month_price(3, None)
        store.set_monthly(100_000)
        assert store.pricing.price_for_plan(store.get("m3")) == 300_000

    def test_manual_price_beats_auto(self, store):
        # در حالت manual، قیمت خودِ پلن ملاک است
        plan = store.get("m1")
        plan.price_toman = 999
        store.pricing.mode = "manual"
        assert store.pricing.price_for_plan(plan) == 999

    def test_renewal_price_monthly_only(self, store):
        assert store.pricing.price_for_renewal(3) is None  # manual → تمدید دستی
        store.set_mode("monthly")
        store.set_monthly(100_000)
        assert store.pricing.price_for_renewal(3) == 300_000
        store.set_month_price(2, 180_000)
        assert store.pricing.price_for_renewal(2) == 180_000

    def test_invalid_mode_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_mode("free")


class TestPlanCRUD:
    def test_add_and_delete(self, store):
        plan = store.add_plan("تستی", 10, 7, 30_000)
        assert store.get(plan.slug) is not None
        assert store.delete_plan(plan.slug) is True
        assert store.get(plan.slug) is None

    def test_toggle(self, store):
        assert store.toggle_plan("m1") is False
        assert "m1" not in [p.slug for p in store.visible_plans()]
        store.toggle_plan("m1")
        assert "m1" in [p.slug for p in store.visible_plans()]

    def test_visibility_reseller(self, store):
        plain = [p.slug for p in store.visible_plans(reseller=False)]
        reseller = [p.slug for p in store.visible_plans(reseller=True)]
        assert "r1" not in plain
        assert "r1" in reseller
        assert "m1" in plain

    def test_update_plan(self, store):
        store.update_plan("m1", gb=75, days=45, price_toman=0)
        plan = store.get("m1")
        assert plan.gb == 75 and plan.days == 45 and plan.price_toman == 0

    def test_validation(self, store):
        with pytest.raises(ValueError):
            store.add_plan("", 10, 30, 1000)
        with pytest.raises(ValueError):
            store.add_plan("x", 0, 30, 1000)
        with pytest.raises(ValueError):
            store.add_plan("x", 10, 0, 1000)
        with pytest.raises(ValueError):
            store.add_plan("x", 10, 30, -5)
