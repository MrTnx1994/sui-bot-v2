"""Rename brand نبض → Vpnfiy across user-facing strings."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sui_bot"

REPLACEMENTS: dict[str, list[tuple[str, str]]] = {
    "localization.py": [
        ("به ربات نبض خوش آمدید", "به ربات Vpnfiy خوش آمدید"),
    ],
    "reseller_bot.py": [
        ("پنل نمایندگی نبض", "پنل نمایندگی Vpnfiy"),
    ],
    "subpage.py": [
        ('SUBPAGE_TITLE = os.getenv("SUBPAGE_TITLE", "اشتراک نبض | Pulse VPN")',
         'SUBPAGE_TITLE = os.getenv("SUBPAGE_TITLE", "اشتراک Vpnfiy")'),
        ("navigator.share({{ title: 'اشتراک نبض | Pulse VPN', text: text }})",
         "navigator.share({{ title: 'اشتراک Vpnfiy', text: text }})"),
    ],
}

for filename, pairs in REPLACEMENTS.items():
    path = ROOT / filename
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            print(f"MISS {filename}: {old[:50]}")
            continue
        text = text.replace(old, new)
        print(f"ok   {filename}: {old[:40]} → {new[:40]}")
    path.write_text(text, encoding="utf-8")

# باقی‌مانده‌ها؟
for path in ROOT.glob("*.py"):
    text = path.read_text(encoding="utf-8")
    count = text.count("نبض")
    if count:
        print(f"remaining نبض in {path.name}: {count}")
print("done")
