#!/usr/bin/env python3
"""پچ دکمهٔ منو — روی **سرور** اجرا شود (نه روی ویندوز).

این اسکریپت فایل نصب‌شدهٔ sui_bot/subpage.py را پیدا می‌کند و به‌صورت
idempotent صفحات و روت‌های مینی‌اپ منو (/menu و /sub/menu) را به آن اضافه می‌کند.

استفاده روی سرور:
    sudo python3 patch_menu_webapp.py          # پچ (خودکار بکاپ می‌گیرد)
    sudo systemctl restart sui-subpage         # ری‌استارت سرویس صفحهٔ اشتراک

بعد از آن در env ربات (سمت ویندوز/سرویس ربات):
    MENU_WEBAPP_URL=https://<دامنه پنل>:2096/sub/menu
و ری‌استارت ربات.

تمام شدن کار: دکمهٔ مربعی کنار کادر تایپ تلگرام → منوی اصلی مینی‌اپ.
"""

from __future__ import annotations

import importlib
import importlib.util
import py_compile
import shutil
import sys
from pathlib import Path

MARKER = "MENU_PAGE"

MENU_BLOCK = '''

# ---------------------------------------------------------------------------
# مینی‌اپ منوی اصلی — دکمهٔ مربعی کنار کادر تایپ تلگرام این صفحه را باز می‌کند
# (پچ‌شده توسط patch_menu_webapp.py)
# ---------------------------------------------------------------------------
MENU_PAGE = """<!doctype html>
<html dir="rtl" lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>Vpnfiy</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    font-family: "Vazirmatn", Tahoma, sans-serif;
    background: linear-gradient(160deg, #0b1220 0%, #101a2e 55%, #0d1526 100%);
    color:#e6edf7; min-height:100vh; padding:18px 14px 28px;
  }
  h1 { text-align:center; font-size:20px; margin:6px 0 2px; }
  .sub { text-align:center; color:#8fa3bd; font-size:12.5px; margin-bottom:16px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; max-width:420px; margin:0 auto; }
  .card {
    background:linear-gradient(145deg,#16233c,#131f36);
    border:1px solid #223354; border-radius:16px;
    padding:16px 10px; text-align:center; cursor:pointer;
    transition:transform .08s ease, border-color .15s ease;
    user-select:none;
  }
  .card:active { transform:scale(.96); border-color:#3b82f6; }
  .card.wide { grid-column:1 / -1; }
  .ico { font-size:30px; display:block; margin-bottom:8px; }
  .lbl { font-size:14px; font-weight:600; }
  .hint { text-align:center; color:#64748b; font-size:11px; margin-top:18px; }
</style>
</head>
<body>
  <h1>Vpnfiy</h1>
  <div class="sub">منوی اصلی — هر گزینه را بزنید تا داخل ربات باز شود</div>
  <div class="grid">
    <div class="card wide"   data-act="shop"><span class="ico">🛍️</span><span class="lbl">خرید اشتراک</span></div>
    <div class="card"        data-act="usage"><span class="ico">📊</span><span class="lbl">اشتراک‌های من</span></div>
    <div class="card"        data-act="wallet"><span class="ico">💼</span><span class="lbl">کیف پول</span></div>
    <div class="card"        data-act="trial"><span class="ico">🎁</span><span class="lbl">تست رایگان</span></div>
    <div class="card"        data-act="support"><span class="ico">🆘</span><span class="lbl">پشتیبانی</span></div>
  </div>
  <div class="hint">اگر دکمه‌ها کار نکردند، ربات را باز کنید و /start بزنید</div>
<script>
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();
  document.querySelectorAll(".card").forEach(card => {
    card.addEventListener("click", () => {
      try {
        tg.sendData(JSON.stringify({ act: card.dataset.act }));
      } catch (e) { console.error(e); }
    });
  });
</script>
</body>
</html>"""


async def handle_menu(request: web.Request) -> web.Response:
    page = MENU_PAGE.replace("__TITLE__", html.escape(SUBPAGE_TITLE))
    return web.Response(text=page, content_type="text/html")

'''


def find_subpage_path() -> Path:
    spec = importlib.util.find_spec("sui_bot.subpage")
    if spec and spec.origin:
        return Path(spec.origin)
    raise SystemExit("sui_bot.subpage پیدا نشد — sui-bot روی سرور نصب نیست؟")


def main() -> int:
    path = find_subpage_path()
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print(f"[=] {path} قبلاً پچ شده — کاری انجام نشد.")
        return 0

    backup = path.with_suffix(".py.bak-menu")
    shutil.copy2(path, backup)
    print(f"[+] بکاپ: {backup}")

    # ۱) بلوک صفحهٔ منو را قبل از make_app اضافه کن
    needle = "def make_app() -> web.Application:"
    if needle not in source:
        print("[!] make_app در subpage.py پیدا نشد — نسخهٔ غیرمنتظره؛ پچ لغو شد.")
        return 1
    source = source.replace(needle, MENU_BLOCK.strip() + "\n\n\n" + needle, 1)

    # ۲) روت‌های منو را داخل make_app اضافه کن
    route_needle = 'app.router.add_get("/sub/{name}", handle_sub)'
    route_new = (
        route_needle
        + '\n    app.router.add_get("/sub/menu", handle_menu)'
        + '\n    app.router.add_get("/menu", handle_menu)'
    )
    if route_needle not in source:
        print("[!] روت /sub/{name} پیدا نشد — پچ لغو شد (بکاپ سر جایش است).")
        return 1
    source = source.replace(route_needle, route_new, 1)

    path.write_text(source, encoding="utf-8")

    # ۳) صحت سینتکس را بگیر؛ خراب شد بکاپ را برگردان
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, path)
        print(f"[!] کامپایل بعد از پچ شکست خورد؛ فایل اصلی برگردانده شد:\n{exc}")
        return 1

    importlib.invalidate_caches()
    print("[✓] پچ موفق. حالا اجرا کن:")
    print("      sudo systemctl restart sui-subpage")
    print("   و در env ربات: MENU_WEBAPP_URL=https://<دامنه>:2096/sub/menu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
