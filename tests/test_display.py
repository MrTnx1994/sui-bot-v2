"""Tests for display helpers (Jalali date, usage bar)."""

from __future__ import annotations

from sui_bot.display import fa_num, gregorian_to_jalali, jalali_date, jalali_date_long, usage_bar, usage_percent


class TestFaNum:
    def test_digits(self):
        assert fa_num(123) == "۱۲۳"

    def test_zero(self):
        assert fa_num(0) == "۰"


class TestJalali:
    def test_known_date(self):
        # 6 سپتامبر 2026 = 15 شهریور 1405
        assert gregorian_to_jalali(2026, 9, 6) == (1405, 6, 15)

    def test_nowruz(self):
        # 21 مارس 2024 = 1 فروردین 1403
        assert gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)

    def test_jalali_date_from_ts(self):
        # 2026-09-06 00:00 UTC → +3:30 → همان روز ایران
        ts = 1788748800  # 2026-09-06 12:00 UTC تقریباً
        result = jalali_date(ts)
        assert result is not None
        assert "/" in result

    def test_zero_is_none(self):
        assert jalali_date(0) is None
        assert jalali_date(None) is None

    def test_long_format_has_month_name(self):
        ts = 1788748800
        result = jalali_date_long(ts)
        assert result is not None
        assert any(m in result for m in ("فروردین", "شهریور", "مهر"))


class TestUsageBar:
    def test_half(self):
        bar = usage_bar(50, 100)
        assert "█" * 5 in bar and "░" * 5 in bar
        assert "۵۰٪" in bar

    def test_full(self):
        bar = usage_bar(100, 100)
        assert "۱۰۰٪" in bar
        assert "░" not in bar

    def test_over_limit_clamps(self):
        assert "۱۰۰٪" in usage_bar(150, 100)

    def test_unlimited(self):
        assert usage_bar(10, 0) == "♾"

    def test_percent_helper(self):
        assert usage_percent(25, 100) == 25
        assert usage_percent(10, 0) == 0
