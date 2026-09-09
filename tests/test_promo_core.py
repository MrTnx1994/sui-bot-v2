"""تست هستهٔ کد تخفیف (promo_core) — بدون شبکه."""

from __future__ import annotations

from pathlib import Path

from sui_bot.promo_core import DiscountStore, code_valid, normalize_code


def make_store(tmp_path: Path) -> DiscountStore:
    return DiscountStore(tmp_path / "discounts.json")


def test_normalize_and_format():
    assert normalize_code(" now20 ") == "NOW20"
    assert code_valid("NOW20")
    assert code_valid("ab-9_")
    assert not code_valid("ab")            # خیلی کوتاه
    assert not code_valid("فارسی")          # غیرلاتین


def test_create_and_validate(tmp_path):
    store = make_store(tmp_path)
    store.create("WELCOME20", percent=20, max_uses=10, days_valid=30)

    result = store.evaluate("welcome20", 111)
    assert result.ok and result.percent == 20

    # کاربر بدون استفاده → مبلغ تخفیف
    priced = store.discount_amount("WELCOME20", 111, 150_000)
    assert priced.ok and priced.amount == 30_000

    # کد ناموجود
    assert not store.evaluate("NOPE", 111).ok


def test_once_per_user_and_max_uses(tmp_path):
    store = make_store(tmp_path)
    store.create("ONE", percent=10, max_uses=2, days_valid=0)

    assert store.consume("one", 42, 1) is True
    assert store.consume("ONE", 42, 2) is False   # همان کاربر دوباره
    assert not store.evaluate("ONE", 42).ok       # used_by_user
    assert store.evaluate("ONE", 777).ok          # کاربر دیگر هنوز آزاد است

    # سقف ۲ نفره → استفادهٔ کاربر دوم پرش می‌کند
    assert store.consume("ONE", 777, 3) is True
    assert store.evaluate("ONE", 999).reason == "exhausted"


def test_toggle_and_delete(tmp_path):
    store = make_store(tmp_path)
    store.create("OFF", percent=5, max_uses=0, days_valid=0)
    assert store.toggle("OFF") is False
    assert store.evaluate("OFF", 1).reason == "inactive"
    assert store.toggle("OFF") is True
    assert store.evaluate("OFF", 1).ok
    assert store.delete("OFF") is True
    assert store.get("OFF") is None


def test_discount_never_zeroes_price(tmp_path):
    store = make_store(tmp_path)
    store.create("BIG", percent=90, max_uses=0, days_valid=0)
    priced = store.discount_amount("BIG", 5, 1_000)   # مبلغ پایین
    assert priced.ok and priced.amount == 900
    priced2 = store.discount_amount("BIG", 5, 200_000)
    assert priced2.amount == 180_000


def test_persistence(tmp_path):
    store = make_store(tmp_path)
    store.create("KEEP", percent=15, max_uses=5, days_valid=7)
    reopened = DiscountStore(tmp_path / "discounts.json")
    assert reopened.evaluate("KEEP", 1).ok
    assert reopened.get("KEEP")["percent"] == 15
