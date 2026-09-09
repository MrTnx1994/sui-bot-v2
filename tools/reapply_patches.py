"""Re-apply the multi-admin/plans/user-pricing changes to the clean files.

همهٔ جایگزینی‌ها فقط با متن ASCII انجام می‌شود تا انکودینگ سالم بماند.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sui_bot"


def patch(path: str, old: str, new: str, count: int = 1) -> None:
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences != count:
        raise SystemExit(f"{path}: expected {count} occurrence(s) of pattern, found {occurrences}:\n{old[:80]}")
    text = text.replace(old, new, count)
    p.write_text(text, encoding="utf-8")
    print(f"ok {path}:{old[:40]!r}")


# --- reseller_bot.py: multi-admin guards -------------------------------------
RESELLER_OLD = 'if update.effective_user.id != context.bot_data.get("store", {}).get("admin_id"):'
RESELLER_NEW = 'if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):'

patch("reseller_bot.py", RESELLER_OLD, RESELLER_NEW, count=5)

# --- store_core.py: make_expiry_days + amount override ------------------------
patch(
    "store_core.py",
    "def gb_to_bytes(gb: int) -> int:",
    '''def make_expiry_days(days: int) -> int:
    """انقضا بر حسب ثانیه برای مدت‌های دلخواه (نه لزوماً مضرب ۳۰ روز)."""
    return int((datetime.now(timezone.utc) + timedelta(days=int(days))).timestamp())


def gb_to_bytes(gb: int) -> int:''',
)
patch(
    "store_core.py",
    '''        client_name: str | None = None,
    ) -> dict[str, Any]:''',
    '''        client_name: str | None = None,
        amount_toman: int | None = None,
    ) -> dict[str, Any]:''',
)
patch(
    "store_core.py",
    '"amount_toman": plan.price_toman,',
    '"amount_toman": int(amount_toman) if amount_toman is not None else plan.price_toman,',
)

# --- subpage.py: E741 rename ---------------------------------------------------
patch(
    "subpage.py",
    "return [str(l.get(\"uri\")) for l in links if isinstance(l, dict) and l.get(\"uri\")]",
    "return [str(link.get(\"uri\")) for link in links if isinstance(link, dict) and link.get(\"uri\")]",
)

print("all patches applied")
