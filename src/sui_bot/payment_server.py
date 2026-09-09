"""وب‌سرور callback زرین‌پال — تأیید خودکار پرداخت بعد از برگشت از درگاه.

زرین‌پال بعد از پرداخت، کاربر را به  {ZARINPAL_CALLBACK_BASE}/pay/callback
می‌فرستد (Authority + Status در query). این‌جا تراکنش را تأیید و تحویل می‌کنیم.

روی 127.0.0.1:ZARINPAL_CALLBACK_PORT بالا می‌آید (پشت nginx/NPM);
اگر ZARINPAL_CALLBACK_BASE خالی باشد، سرور اصلاً اجرا نمی‌شود و کاربر
با دکمهٔ «پرداخت کردم» داخل ربات تأیید می‌کند.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from .store_bot import finalize_successful_payment
from .store_core import OrderStatus
from .zarinpal_core import PaymentsStore, PaymentStatus, ZarinPalClient, ZarinPalError

logger = logging.getLogger("sui_bot.pay_server")

PAGE_OK = (
    "<!doctype html><html dir='rtl' lang='fa'><meta charset='utf-8'>"
    "<title>پرداخت موفق</title>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:60px'>"
    "<h1>✅ پرداخت با موفقیت انجام شد</h1>"
    "<p>به ربات برگردید — کانفیگ شما در حال ساخت است.</p>"
    "</body></html>"
)

PAGE_FAIL = (
    "<!doctype html><html dir='rtl' lang='fa'><meta charset='utf-8'>"
    "<title>پرداخت ناموفق</title>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:60px'>"
    "<h1>❌ پرداخت ناموفق بود</h1>"
    "<p>{reason}</p><p>اگر مبلغ کم شده با پشتیبانی در تماس باشید.</p>"
    "</body></html>"
)

PAGE_STALE = (
    "<!doctype html><html dir='rtl' lang='fa'><meta charset='utf-8'>"
    "<title>پردازش شده</title>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:60px'>"
    "<h1>ℹ️ این پرداخت قبلاً پردازش شده است</h1>"
    "<p>به ربات برگردید.</p>"
    "</body></html>"
)


def make_payment_app(bot, ctx: dict, payments: PaymentsStore, zarinpal: ZarinPalClient) -> web.Application:
    async def handle_callback(request: web.Request) -> web.Response:
        try:
            query = parse_qs(urlsplit(str(request.rel_url)).query)
            authority = (query.get("Authority") or query.get("authority") or [""])[0].strip()
            status = (query.get("Status") or query.get("status") or [""])[0].strip().upper()
            if not authority:
                return web.Response(text=PAGE_FAIL.format(reason="لینک نامعتبر است."), content_type="text/html")

            payment = payments.find_by_authority(authority)
            if payment is None:
                return web.Response(text=PAGE_FAIL.format(reason="تراکنشی با این شناسه پیدا نشد."), content_type="text/html", status=404)
            if payment["status"] != PaymentStatus.PENDING:
                return web.Response(text=PAGE_STALE, content_type="text/html")

            if status != "OK":
                payments.mark_failed(payment["id"], "user canceled / not OK")
                return web.Response(text=PAGE_FAIL.format(reason="پرداخت انجام نشد یا لغو شد."), content_type="text/html")

            try:
                result = await zarinpal.verify_payment(payment["amount_toman"], authority)
            except ZarinPalError as exc:
                logger.warning("callback verify error: %s", exc)
                return web.Response(
                    text=PAGE_FAIL.format(reason=html.escape(str(exc))),
                    content_type="text/html",
                )
            if not result.ok:
                payments.mark_failed(payment["id"], result.message)
                return web.Response(text=PAGE_FAIL.format(reason=html.escape(result.message)), content_type="text/html")

            payments.mark_paid(payment["id"], result.ref_id)
            if payment["kind"] == "order":
                store = ctx["store"]
                order = store.get_order(payment.get("order_id")) if payment.get("order_id") else None
                if order is not None and order.get("status") != OrderStatus.PROVISIONED:
                    store.update_order(order["id"], status=OrderStatus.APPROVED)
                    order = store.get_order(order["id"])
                    await finalize_successful_payment(_BotShim(bot, ctx), ctx, order)
            else:  # deposit → کیف پول
                resellers = ctx.get("resellers")
                if resellers is not None:
                    resellers.ensure_member(payment["tg_id"])
                    resellers.adjust_balance(payment["tg_id"], int(payment["amount_toman"]))
                    try:
                        await bot.send_message(
                            chat_id=payment["tg_id"],
                            text=(
                                f"✅ شارژ کیف پول انجام شد!\n💰 مبلغ: {int(payment['amount_toman']):,} تومان"
                                + (f"\n🧾 کد پیگیری: {result.ref_id}" if result.ref_id else "")
                            ),
                        )
                    except Exception:
                        logger.exception("deposit notify failed (payment %s)", payment["id"])
            return web.Response(text=PAGE_OK, content_type="text/html")
        except Exception:
            logger.exception("payment callback crashed")
            return web.Response(text=PAGE_FAIL.format(reason="خطای داخلی سرور."), content_type="text/html", status=500)

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/pay/callback", handle_callback)
    app.router.add_get("/pay/health", handle_health)
    return app


class _BotShim:
    """شبیه‌سازی context برای finalize_successful_payment (فقط context.bot لازم است)."""

    def __init__(self, bot, _ctx: dict):
        self.bot = bot


async def start_payment_server(bot, ctx: dict, payments: PaymentsStore, zarinpal: ZarinPalClient,
                               bind: str, port: int) -> web.AppRunner:
    """سرور callback را در همان event loop ربات بالا بیاورد."""
    runner = web.AppRunner(make_payment_app(bot, ctx, payments, zarinpal))
    await runner.setup()
    site = web.TCPSite(runner, host=bind, port=port)
    await site.start()
    logger.info("ZarinPal callback server listening on %s:%s", bind, port)
    return runner
