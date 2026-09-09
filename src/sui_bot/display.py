"""Display helpers: Jalali dates & usage bars (pure, no deps)."""

from __future__ import annotations

import datetime as dt

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def fa_num(value: int | str) -> str:
    return "".join(_FA_DIGITS[int(ch)] if ch.isdigit() else ch for ch in str(value))


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2, gm2, gd2 = gy - 1600, gm - 1, gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_d_m[gm2] + gd2
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        jm = 7 + (j_day_no - 186) // 30
        jd = 1 + (j_day_no - 186) % 30
    return jy, jm, jd


def jalali_date(ts: int | float | None) -> str | None:
    """تاریخ شمسی از ثانیهٔ اپوک؛ None برای 0/None (نامحدود)."""
    if not ts or int(ts) <= 0:
        return None
    utc = dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)
    local = utc + dt.timedelta(hours=3, minutes=30)  # ایران
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    return f"{fa_num(jy)}/{fa_num(jm):0>2}/{fa_num(jd):0>2}"


def jalali_date_long(ts: int | float | None) -> str | None:
    if not ts or int(ts) <= 0:
        return None
    utc = dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)
    local = utc + dt.timedelta(hours=3, minutes=30)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    return f"{fa_num(jd)} {_JALALI_MONTHS[jm - 1]} {fa_num(jy)}"


def usage_bar(used: int, total: int, width: int = 10) -> str:
    """نوار بصری مصرف؛ total=0 → نامحدود."""
    if not total or total <= 0:
        return "♾"
    ratio = min(max(used / total, 0.0), 1.0)
    filled = int(round(ratio * width))
    pct = int(ratio * 100)
    return f"{'█' * filled}{'░' * (width - filled)} {fa_num(pct)}٪"


def usage_percent(used: int, total: int) -> int:
    if not total or total <= 0:
        return 0
    return int(min(max(used / total, 0.0), 1.0) * 100)
