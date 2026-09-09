"""سرویس وب صفحه اشتراک — نمایش حجم باقیمانده/انقضا/کانفیگ‌ها مستقیم از پنل S-UI.

منبع داده: API خود S-UI (apiv2/clients?id=...) — نه هدرهای انجنیکس/پروکسی.
مسیر: /sub/<name> → صفحه فارسی RTL با حجم، انقضا و وضعیت کانفیگ‌ها.

اجرا: python3 -m sui_bot.subpage  (یا systemd: subpage.service)
پورت: SUBPAGE_PORT (پیش‌فرض 8790) — nginx فقط 2096 را به این سرویس پروکسی می‌کند.
"""

from __future__ import annotations

import html
import logging
import os
import re
import socket
import ssl as ssl_module
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver

logger = logging.getLogger("sui_bot.subpage")


class _StaticResolver(AbstractResolver):
    """DNS resolver ثابت — همیشه 127.0.0.1 (پنل روی همین سرور است).

    hostname را دست‌نخورده به aiohttp می‌دهیم تا SNI و verify هاست واقعی بماند؛
    فقط اتصال TCP به لوکال‌هاست می‌رود.
    """

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        pass


def _server_hostname_ctx() -> ssl_module.SSLContext:
    """TLS context با SNI دامنهٔ پنل — برای اتصال لوکال به 127.0.0.1:2095."""
    ctx = ssl_module.create_default_context()
    return ctx

# ---------------------------------------------------------------------------
# تنظیمات از env
# ---------------------------------------------------------------------------
SUI_API_BASE = os.getenv("SUI_HOST", "").rstrip("/")        # مثلا https://tnt.traviann.ir:2095/app
SUI_API_TOKEN = os.getenv("SUI_TOKEN", "")
# داخل همان سرور → لوکال‌هاست سریع‌تر و بدون عبور از فایروال خارجی
SUI_API_LOCAL = os.getenv("SUI_API_LOCAL", "https://127.0.0.1:2095/app").rstrip("/")
SUBPAGE_PORT = int(os.getenv("SUBPAGE_PORT", "8790"))
SUBPAGE_BIND = os.getenv("SUBPAGE_BIND", "127.0.0.1")
SUBPAGE_SUB_BASE = os.getenv("SUBPAGE_SUB_BASE", "").rstrip("/")  # https://tnt.traviann.ir:2096
SUBPAGE_TITLE = os.getenv("SUBPAGE_TITLE", "اشتراک Vpnfiy")
# کش کوتاه برای جلوگیری از ضربه به پنل (ثانیه)
CACHE_TTL = float(os.getenv("SUBPAGE_CACHE_TTL", "10"))
_cache: dict[str, tuple[float, dict | None]] = {}


def _fmt_gb(num_bytes: int | float | None) -> str:
    if num_bytes is None:
        return "—"
    gb = num_bytes / (1024 ** 3)
    if gb >= 100:
        return f"{gb:.0f} گیگابایت"
    if gb >= 10:
        return f"{gb:.1f} گیگابایت"
    return f"{gb:.2f} گیگابایت"


def _fmt_days_left(expiry_s: int | None) -> tuple[str, str]:
    """(متن، رنگ). expiry برحسب ثانیه."""
    if not expiry_s:
        return ("نامحدود", "ok")
    now = time.time()
    days = (expiry_s - now) / 86400
    if days <= 0:
        return ("منقضی شده", "bad")
    if days < 1:
        hours = max(1, int(days * 24))
        return (f"{hours} ساعت مانده", "warn")
    d = int(days)
    if d < 3:
        return (f"{d} روز مانده", "warn")
    return (f"{d} روز مانده", "ok")


def _pct(used: int | None, total: int | None) -> int:
    if not total:
        return 0
    return max(0, min(100, round(used / total * 100)))


# ---------------------------------------------------------------------------
# دریافت کلاینت از API پنل
# ---------------------------------------------------------------------------
async def fetch_client_by_name(name: str) -> dict | None:
    """کلاینت را از API پنل بر اساس نام برمی‌گرداند (با کش کوتاه).

    نکته: پاسخ لیست کامل، links را خالی برمی‌گرداند؛ برای گرفتن لینک‌های کانفیگ
    باید درخواست تکی apiv2/clients?id=<id> زد. پس: اول لیست → پیدا کردن id →
    بعد درخواست تکی برای دادهٔ کامل.
    """
    hit = _cache.get(name)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]

    if not SUI_API_BASE or not SUI_API_TOKEN:
        logger.error("SUI_HOST / SUI_TOKEN not configured for subpage")
        return None

    # به 127.0.0.1 وصل شو ولی SNI/cert دامنهٔ واقعی پنل را چک کن
    conn = aiohttp.TCPConnector(
        ssl=_server_hostname_ctx(),
        resolver=_StaticResolver(),
    )
    headers = {"Token": SUI_API_TOKEN, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=8)

    wanted = (name or "").lower()
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=conn) as session:
            # ۱) لیست کلاینت‌ها → پیدا کردن id
            async with session.get(f"{SUI_API_BASE}/apiv2/clients", headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            obj = (data or {}).get("obj") or {}
            clients = obj.get("clients") or []
            client_id = None
            for c in clients:
                if str(c.get("name") or "").lower() == wanted:
                    client_id = c.get("id")
                    break
            if client_id is None:
                _cache[name] = (time.time(), None)
                return None

            # ۲) درخواست تکی → دادهٔ کامل شامل links
            async with session.get(
                f"{SUI_API_BASE}/apiv2/clients", params={"id": client_id}, headers=headers
            ) as resp2:
                resp2.raise_for_status()
                data2 = await resp2.json(content_type=None)
            obj2 = (data2 or {}).get("obj") or {}
            clients2 = obj2.get("clients") or []
            found = None
            for c in clients2:
                if str(c.get("name") or "").lower() == wanted:
                    found = c
                    break
            _cache[name] = (time.time(), found)
            return found
    except Exception:
        logger.exception("subpage: panel API request failed")
        return None


async def fetch_links_by_name(name: str) -> list[str] | None:
    """فقط لینک‌های کانفیگ (برای فید اپ‌های VPN) — None یعنی کاربر نیست."""
    client = await fetch_client_by_name(name)
    if client is None:
        return None
    links = client.get("links") or []
    return [str(link.get("uri")) for link in links if isinstance(link, dict) and link.get("uri")]


# ---------------------------------------------------------------------------
# رندر صفحه
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Vazirmatn, Tahoma, sans-serif;
    background: linear-gradient(160deg, #0f172a 0%, #1e293b 100%);
    color: #e2e8f0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 16px;
  }}
  .card {{
    width: 100%; max-width: 420px; background: rgba(30,41,59,.85);
    border: 1px solid rgba(148,163,184,.18); border-radius: 20px;
    padding: 28px 22px; backdrop-filter: blur(8px);
    box-shadow: 0 18px 50px rgba(0,0,0,.45);
  }}
  .head {{ text-align: center; margin-bottom: 22px; }}
  .head .logo {{ font-size: 40px; }}
  .head h1 {{ font-size: 19px; margin-top: 6px; color: #f1f5f9; }}
  .head .sub {{ font-size: 12.5px; color: #94a3b8; margin-top: 4px; }}
  .row {{
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(15,23,42,.55); border: 1px solid rgba(148,163,184,.12);
    border-radius: 12px; padding: 12px 14px; margin-bottom: 10px;
  }}
  .row .lbl {{ font-size: 13px; color: #94a3b8; }}
  .row .val {{ font-size: 14.5px; font-weight: 600; color: #f1f5f9; }}
  .row .val.small {{ font-size: 12.5px; }}
  .bar {{ background: rgba(15,23,42,.55); border-radius: 12px; padding: 14px; margin-bottom: 12px;
         border: 1px solid rgba(148,163,184,.12); }}
  .bar .top {{ display: flex; justify-content: space-between; font-size: 12.5px; color: #94a3b8; margin-bottom: 8px; }}
  .bar .track {{ height: 10px; background: rgba(51,65,85,.9); border-radius: 99px; overflow: hidden; }}
  .bar .fill {{ height: 100%; border-radius: 99px; background: linear-gradient(90deg,#22c55e,#4ade80);
                transition: width .6s ease; }}
  .bar .fill.warn {{ background: linear-gradient(90deg,#f59e0b,#fbbf24); }}
  .bar .fill.bad  {{ background: linear-gradient(90deg,#ef4444,#f87171); }}
  .cfg {{ margin-top: 16px; }}
  .cfg h2 {{ font-size: 14px; color: #cbd5e1; margin-bottom: 8px; }}
  .cfg .item {{
    background: rgba(15,23,42,.55); border: 1px solid rgba(148,163,184,.12);
    border-radius: 10px; padding: 10px 12px; margin-bottom: 8px;
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }}
  .cfg .item .name {{ font-size: 13px; color: #e2e8f0; overflow-wrap: anywhere; text-align: right; }}
  .cfg .item .proto {{ font-size: 11px; color: #7dd3fc; background: rgba(14,116,144,.25);
                      padding: 2px 8px; border-radius: 99px; white-space: nowrap; }}
  .note {{ text-align: center; font-size: 11.5px; color: #64748b; margin-top: 14px; }}
  .actions {{ display: flex; gap: 8px; margin-top: 14px; }}
  .btn {{
    flex: 1; border: 1px solid rgba(148,163,184,.25); background: rgba(51,65,85,.6);
    color: #e2e8f0; font-family: inherit; font-size: 12.5px; font-weight: 600;
    padding: 11px 6px; border-radius: 12px; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }}
  .btn:active {{ background: rgba(56,189,248,.2); }}
  .btn.primary {{ background: rgba(34,197,94,.18); border-color: rgba(34,197,94,.4); color: #4ade80; }}
  .cfg .item {{ cursor: pointer; -webkit-tap-highlight-color: transparent; }}
  .cfg .item:active {{ background: rgba(56,189,248,.15); }}
  .copytoast {{
    position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
    background: #22c55e; color: #052e16; font-size: 13px; font-weight: 700;
    padding: 10px 20px; border-radius: 99px; box-shadow: 0 8px 24px rgba(0,0,0,.4);
    opacity: 0; transition: opacity .25s; pointer-events: none; z-index: 9;
  }}
  .copytoast.show {{ opacity: 1; }}
  .err {{ text-align: center; padding: 30px 10px; }}
  .err .icon {{ font-size: 44px; }}
  .err p {{ margin-top: 10px; color: #cbd5e1; font-size: 14px; }}
  .pill {{ display:inline-block; padding: 2px 10px; border-radius: 99px; font-size: 12px; font-weight:600; }}
  .pill.ok {{ background: rgba(34,197,94,.15); color: #4ade80; }}
  .pill.warn {{ background: rgba(245,158,11,.15); color: #fbbf24; }}
  .pill.bad {{ background: rgba(239,68,68,.15); color: #f87171; }}
</style>
</head>
<body>
<div class="card">
  {body}
</div>
<div class="copytoast" id="toast">✓ کپی شد</div>
<script>
var ALL_URIS = [];
document.querySelectorAll('.item[data-uri]').forEach(function(el) {{
  ALL_URIS.push(el.getAttribute('data-uri'));
}});
var SUBLINK = '{sublink}';

function showToast(msg) {{
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function() {{ t.classList.remove('show'); }}, 1600);
}}
function copyText(text) {{
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(function() {{}}, function() {{ fallbackCopy(text); }});
  }} else {{ fallbackCopy(text); }}
}}
function copyCfg(el) {{ copyText(el.getAttribute('data-uri')); showToast('✓ کانفیگ کپی شد'); }}
function copyAll() {{
  if (!ALL_URIS.length) {{ showToast('کانفیگی نیست'); return; }}
  copyText(ALL_URIS.join('\\n'));
  showToast('✓ ' + ALL_URIS.length + ' کانفیگ کپی شد');
}}
function copySubLink() {{
  copyText(SUBLINK);
  showToast('✓ لینک اشتراک کپی شد');
}}
function shareAll() {{
  var text = ALL_URIS.join('\\n');
  if (navigator.share) {{
    navigator.share({{ title: 'اشتراک Vpnfiy', text: text }}).catch(function() {{}});
  }} else {{
    copyText(text); showToast('✓ کپی شد (اشتراک‌گذاری پشتیبانی نمی‌شود)');
  }}
}}
function fallbackCopy(text) {{
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try {{ document.execCommand('copy'); }} catch (e) {{}}
  document.body.removeChild(ta);
}}
</script>
</body>
</html>
"""


def render_error(message: str, icon: str = "⚠️") -> str:
    body = f'<div class="err"><div class="icon">{icon}</div><p>{html.escape(message)}</p></div>'
    return PAGE.format(title=SUBPAGE_TITLE, body=body)


def _protocol_of(link: str) -> str:
    low = link.split("://", 1)[0].lower() if "://" in link else "?"
    return {"vless": "VLESS", "vmess": "VMess", "trojan": "Trojan", "ss": "Shadowsocks",
            "hysteria2": "Hysteria2", "hy2": "Hysteria2", "tuic": "TUIC"}.get(low, low.upper())


def render_page(client: dict) -> str:
    name = str(client.get("name") or "?")
    volume = client.get("volume") or 0
    down = client.get("down") or 0
    up = client.get("up") or 0
    used = down + up
    remaining = max(0, volume - used) if volume else None
    expiry_s = client.get("expiry") or None
    expiry_text, expiry_cls = _fmt_days_left(expiry_s)
    enabled = bool(client.get("enable", False))

    if not enabled:
        status_pill = '<span class="pill bad">غیرفعال</span>'
    elif expiry_text == "منقضی شده":
        status_pill = '<span class="pill bad">منقضی</span>'
    else:
        status_pill = '<span class="pill ok">فعال</span>'

    used_pct = _pct(used, volume) if volume else 0
    fill_cls = "bad" if used_pct >= 90 else ("warn" if used_pct >= 75 else "")

    if volume:
        remain_text = _fmt_gb(remaining)
        vol_text = _fmt_gb(volume)
        used_text = _fmt_gb(used)
        usage_block = (
            f'<div class="bar">'
            f'<div class="top"><span>📊 مصرف: {used_text} از {vol_text}</span>'
            f'<span>{used_pct}٪</span></div>'
            f'<div class="track"><div class="fill {fill_cls}" style="width:{used_pct}%"></div></div>'
            f'</div>'
        )
    else:
        usage_block = (
            '<div class="row"><span class="lbl">📊 مصرف</span>'
            f'<span class="val">{_fmt_gb(used)} (نامحدود)</span></div>'
        )

    # کانفیگ‌ها از links — هر آیتم {remark, uri} یا رشتهٔ خام
    cfg_items = ""
    links = client.get("links") or []
    rendered = 0
    for link in links:
        uri, label = "", name
        if isinstance(link, dict):
            uri = str(link.get("uri") or "")
            label = str(link.get("remark") or name)
        elif isinstance(link, str) and "://" in link:
            uri = link
            label = link.split("#", 1)[1] if "#" in link else name
            from urllib.parse import unquote
            label = unquote(label)
        if not uri or "://" not in uri:
            continue
        proto = _protocol_of(uri)
        rendered += 1
        cfg_items += (
            f'<div class="item" onclick="copyCfg(this)" data-uri="{html.escape(uri, quote=True)}">'
            f'<span class="name">{html.escape(label[:40])}</span>'
            f'<span class="proto">{proto}</span></div>'
        )
    if not rendered:
        cfg_items = '<div class="item"><span class="name">کانفیگی یافت نشد</span></div>'

    # انقضای شمسی
    expiry_date = "—"
    if expiry_s:
        try:
            expiry_date = datetime.fromtimestamp(expiry_s, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:  # noqa: S110 - intentional fallback
            pass

    body = (
        f'<div class="head">'
        f'<div class="logo">⚡</div>'
        f'<h1>{SUBPAGE_TITLE}</h1>'
        f'<div class="sub">حساب: <b>{html.escape(name)}</b> {status_pill}</div>'
        f'</div>'
        f'{usage_block}'
        f'<div class="row"><span class="lbl">📉 حجم باقیمانده</span>'
        f'<span class="val">{remain_text if volume else "نامحدود"}</span></div>'
        f'<div class="row"><span class="lbl">📅 تاریخ انقضا</span>'
        f'<span class="val">{expiry_date}</span></div>'
        f'<div class="row"><span class="lbl">⏳ وضعیت انقضا</span>'
        f'<span class="val {"" if expiry_cls == "ok" else "small"}">'
        f'<span class="pill {expiry_cls}">{expiry_text}</span></span></div>'
        f'<div class="cfg"><h2>🔌 کانفیگ‌ها ({rendered})</h2>{cfg_items}</div>'
        f'<div class="actions">'
        f'<button class="btn primary" onclick="copyAll()">📋 کپی همهٔ کانفیگ‌ها</button>'
        f'<button class="btn" onclick="copySubLink()">🔗 کپی لینک اشتراک</button>'
        f'<button class="btn" onclick="shareAll()">📤 اشتراک‌گذاری</button>'
        f'</div>'
        f'<div class="note">برای به‌روزرسانی صفحه را دوباره باز کنید — داده مستقیم از سرور</div>'
    )
    # لینک ساب خودِ این کاربر (همان آدرس صفحه) برای دکمهٔ کپی
    sub_link = ""
    try:
        from urllib.parse import quote
        sub_link = f"https://tnt.traviann.ir:2096/sub/{quote(name)}"
    except Exception:
        sub_link = f"https://tnt.traviann.ir:2096/sub/{name}"
    page = PAGE.format(title=SUBPAGE_TITLE, body=body, sublink=html.escape(sub_link, quote=True))
    return page


# ---------------------------------------------------------------------------
# روت‌ها
# ---------------------------------------------------------------------------
async def handle_sub(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "").strip()
    if not name:
        return web.Response(text=render_error("لینک نامعتبر است.", "🔗"), content_type="text/html")

    # UA اپ‌های VPN (SFA, ClashMeta, Streisand…) معمولا کلمهٔ Android/iPhone/Mobile دارد
    # و nginx اشتباهی به اینجا می‌فرستد — برای همهٔ UAهای غیر مرورگرِ واقعی، فید خام بده.
    ua = request.headers.get("User-Agent", "")
    BROWSER_RE = re.compile(r"Mozilla|Chrome|Safari|Edge|Firefox|Headless", re.I)
    if not BROWSER_RE.search(ua):
        links = await fetch_links_by_name(name)
        if links is None:
            return web.Response(
                text=render_error("اشتراکی با این نام پیدا نشد.", "🔍"),
                content_type="text/html", status=404,
            )
        return web.Response(text="\n".join(links) + "\n", content_type="text/plain", charset="utf-8")

    client = await fetch_client_by_name(name)
    if client is None:
        return web.Response(
            text=render_error("اشتراکی با این نام پیدا نشد.", "🔍"),
            content_type="text/html", status=404,
        )

    return web.Response(text=render_page(client), content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ts": int(time.time())})


# ---------------------------------------------------------------------------
# مینی‌اپ منوی اصلی — دکمهٔ مربعی کنار کادر تایپ تلگرام این صفحه را باز می‌کند
# ---------------------------------------------------------------------------
MENU_PAGE = """<!doctype html>
<html dir="rtl" lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>__TITLE__</title>
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
  <h1>__TITLE__</h1>
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


def _menu_page() -> str:
    return MENU_PAGE.replace("__TITLE__", html.escape(SUBPAGE_TITLE))


async def handle_menu(request: web.Request) -> web.Response:
    return web.Response(text=_menu_page(), content_type="text/html")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/sub/{name}", handle_sub)
    app.router.add_get("/sub/menu", handle_menu)   # قبل از /sub/{name} مهم نیست — aiohttp ثابت را ترجیح می‌دهد
    app.router.add_get("/menu", handle_menu)
    app.router.add_get("/health", handle_health)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not SUI_API_BASE or not SUI_API_TOKEN:
        logger.error("SUI_HOST / SUI_TOKEN must be set (panel API base + token)")
        raise SystemExit(2)
    app = make_app()
    logger.info("subpage listening on %s:%s (panel: %s)", SUBPAGE_BIND, SUBPAGE_PORT, SUI_API_BASE)
    web.run_app(app, host=SUBPAGE_BIND, port=SUBPAGE_PORT, print=None)


if __name__ == "__main__":
    main()
