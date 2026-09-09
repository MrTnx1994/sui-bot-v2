"""فروشگاه کارتبه‌کارت — هندلرهای ربات تلگرام (v2).

جریان:
  1) /shop → کاربر پلن را انتخاب می‌کند
  2) کاربر یک اسم انگلیسی برای اشتراکش انتخاب می‌کند (ضدتکرار — چک پنل + سفارش‌ها)
  3) شماره کارت + مبلغ دقیق نمایش داده می‌شود
  4) کاربر ۴ رقم آخر کارتش را می‌فرستد → برای ادمین ارسال می‌شود + دکمه ✅/❌
  5) ادمین ✅ → کلاینت با همان اسم در پنل ساخته می‌شود → لینک ساب ارسال می‌شود

این ماژول به bot.py وصل می‌شود (register_store_handlers) و از توابع موجود
bot.py استفاده می‌کند — بدون تغییر رفتار فعلی ربات.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .store_core import (
    OrderStatus,
    OrderStore,
    gb_to_bytes,
    make_expiry_days,
    normalize_sub_name,
    random_sub_name,
    sub_name_valid,
    suggest_names,
)
from .plans_store import PlansStore, fa_num, fa_toman
from .reseller_core import format_reseller_toman
from .promo_core import PromoResult
from .trial_core import TrialStore, trial_expiry_ts, trial_volume_bytes
from .zarinpal_core import PaymentsStore, PaymentStatus, ZarinPalClient, ZarinPalError

logger = logging.getLogger("sui_bot.store_bot")


def _plans(ctx: dict | None) -> PlansStore:
    """PlansStore از bot_data (ثبت‌شده در register_store_handlers)."""
    if ctx and ctx.get("plans_store") is not None:
        return ctx["plans_store"]
    raise RuntimeError("plans store is not registered")

# ---------------------------------------------------------------------------
# متن‌های فارسی (HTML parse mode — قرارداد bot.py)
# ---------------------------------------------------------------------------
SHOP_INTRO = (
    "🛍️ <b>فروشگاه اشتراک</b>\n\n"
    "پلن مورد نظرت رو انتخاب کن:\n\n"
    "بعد از انتخاب یه اسم دلخواه برای اشتراکت می‌ذاری، واریز می‌کنی و "
    "بعد از تأیید، کانفیگت خودکار ساخته می‌شه. ⚡"
)

NAME_PROMPT = (
    "🧾 <b>سفارش #{order_id}</b>\n"
    "📦 پلن: {plan_title} — {plan_gb} گیگ ({plan_months} ماه)\n"
    "💰 مبلغ دقیق: {price}\n\n"
    "حالا یه <b>اسم برای اشتراکت</b> انتخاب کن:\n\n"
    "• فقط حروف انگلیسی کوچیک، عدد و آندرلاین\n"
    "• بین ۳ تا ۱۶ حرف\n"
    "• این اسم توی لینک اشتراکت میاد:\n"
    "  <code>…/sub/{base}</code>\n\n"
    "اسم رو همین‌جا بفرست 👇"
)

NAME_TAKEN = (
    "❌ اسم <code>{name}</code> قبلاً گرفته شده.\n\n"
    "یه اسم دیگه بفرست یا یکی از اینا رو انتخاب کن:"
)

NAME_INVALID = (
    "❌ اسم نامعتبره.\n"
    "فقط حروف انگلیسی کوچیک، عدد و آندرلاین — بین ۳ تا ۱۶ حرف.\n"
    "مثلاً: <code>farzaneh</code> یا <code>ali_1367</code>"
)

# ---------------------------------------------------------------------------
# پرداخت آنلاین / کیف پول / کد تخفیف / تست رایگان (v3)
# ---------------------------------------------------------------------------
METHOD_PROMPT = (
    "🧾 <b>سفارش #{order_id}</b>\n"
    "📦 پلن: {plan_title} — {plan_gb} گیگ ({plan_days} روز)\n"
    "💰 مبلغ: {price}{discount_line}\n\n"
    "روش پرداخت رو انتخاب کن:"
)

DISCOUNT_PROMPT = (
    "🎟 <b>کد تخفیف</b>\n\n"
    "کد تخفیف رو بفرست (یا /skip برای رد کردن):"
)

DISCOUNT_APPLIED = (
    "✅ کد <code>{code}</code> اعمال شد ({percent}٪ — {discount} تخفیف).\n"
    "💰 مبلغ جدید: <b>{price}</b>"
)

DISCOUNT_REJECT = {
    "not_found": "❌ همچین کد تخفیفی وجود نداره.",
    "inactive": "❌ این کد غیرفعال شده.",
    "expired": "❌ این کد منقضی شده.",
    "exhausted": "❌ ظرفیت استفاده از این کد پر شده.",
    "used_by_user": "❌ قبلاً با این کد خرید کردی — هر کد یک‌بار برای هر نفره.",
    "bad_format": "❌ فرمت کد درست نیست.",
}

ONLINE_PAY_PROMPT = (
    "🌐 <b>پرداخت آنلاین — سفارش #{order_id}</b>\n"
    "💰 مبلغ: <b>{price}</b>\n\n"
    "۱) با دکمهٔ زیر به درگاه برو و پرداخت کن\n"
    "۲) بعد از پرداخت، برگرد و دکمهٔ «پرداخت کردم» رو بزن\n"
    "⚡ بلافاصله بعد از تأیید، کانفیگ خودکار ساخته می‌شه"
)

PAY_CHECK_WAIT = "⏳ در حال بررسی پرداخت..."
PAY_CHECK_OK = "✅ پرداخت تأیید شد! در حال ساخت کانفیگ..."
PAY_CHECK_FAIL = "❌ پرداخت تأیید نشد:\n{reason}\n\nاگر پرداخت کردی چند دقیقه صبر کن و دوباره امتحان کن، یا با پشتیبانی در تماس باش."
PAY_REQUEST_FAIL = "❌ اتصال به درگاه ناموفق بود:\n{reason}\n\nدوباره تلاش کن یا روش کارت‌به‌کارت رو انتخاب کن."

WALLET_PAID = (
    "✅ <b>پرداخت از کیف پول انجام شد!</b>\n"
    "💰 مبلغ کسرشده: {price}\n"
    "💼 موجودی باقی‌مانده: {balance}\n\n"
    "در حال ساخت کانفیگ..."
)

WALLET_INSUFFICIENT = (
    "❌ موجودی کیف پول کافی نیست.\n"
    "💼 موجودی: <b>{balance}</b>\n"
    "💰 مبلغ سفارش: <b>{price}</b>\n\n"
    "می‌تونی از /wallet شارژ کنی یا روش دیگه‌ای رو انتخاب کنی."
)

WALLET_MENU = (
    "💼 <b>کیف پول</b>\n\n"
    "موجودی: <b>{balance}</b>\n\n"
    "با /wallet هر وقت خواستی این‌جا رو ببین. سود زیرمجموعه‌ها هم همین‌جا شارژ می‌شه."
)

DEPOSIT_PROMPT = (
    "➕ <b>شارژ کیف پول</b>\n\n"
    "مبلغ رو به <b>تومان</b> بفرست (حداقل {min}):"
)

DEPOSIT_BAD = "❌ مبلغ نامعتبره. فقط عدد به تومان (حداقل {min}) — مثلاً: <code>200000</code>"

TRIAL_DISABLED = "❌ اکانت تست در حال حاضر غیرفعاله."
TRIAL_USED = "❌ شما قبلاً اکانت تست رایگان گرفته‌اید. هر کاربر فقط یک‌بار می‌تونه تست بگیره."
TRIAL_CREATING = "⏳ در حال ساخت اکانت تست..."
TRIAL_OK = (
    "🎁 <b>اکانت تست رایگان شما آماده‌ست!</b>\n\n"
    "📦 حجم: {gb} گیگ\n"
    "⏳ اعتبار: {days} روز\n"
    "🔤 اسم اشتراک: <code>{name}</code>\n\n"
    "🔗 لینک اشتراک (توی نرم‌افزار Add Subscription بزن):\n<code>{sub_link}</code>\n\n"
    "اگه راضی بودی، از 🛍 خرید سرویس پلن کامل بگیر."
)


def shop_keyboard(is_invited: bool | None = None, plans_store: PlansStore | None = None,
                  user_id: int | None = None) -> InlineKeyboardMarkup:
    """پلن‌ها — پلن‌های نمایندگی فقط برای زیرمجموعه‌ها (is_invited=True)."""
    plans = (plans_store.visible_plans(reseller=bool(is_invited)) if plans_store else [])
    rows = []
    for plan in plans:
        price = fa_toman(plans_store.pricing.price_for_plan(plan, user_id=user_id)) if plans_store else "—"
        label = f"{plan.title} • {fa_num(plan.gb)} گیگ • {fa_num(plan.days)} روز • {price}"
        rows.append([InlineKeyboardButton(label, callback_data=f"shop_buy_{plan.slug}")])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _is_valid_last4(text: str) -> bool:
    """۴ رقم آخر کارت — فقط ۴ عدد."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) == 4


def _name_buttons(suggestions: list[str]) -> InlineKeyboardMarkup:
    """دکمه‌های اسم پیشنهادی (callback_data محدود به 64 بایت — shop_nm_<name>)."""
    rows = [[InlineKeyboardButton(f"↻ {s}", callback_data=f"shop_nm_{s}")] for s in suggestions[:3]]
    rows.append([InlineKeyboardButton("🎲 اسم رندوم بده", callback_data="shop_nm_random")])
    return InlineKeyboardMarkup(rows)


def _wallet_balance(ctx: dict, user_id: int) -> int:
    """موجودی کیف پول کاربر (resellers store — wallet مشترک)."""
    resellers = ctx.get("resellers")
    if resellers is None:
        return 0
    try:
        member = resellers.get(user_id) or resellers.ensure_member(user_id)
        return int(member.get("balance_toman") or 0)
    except Exception:
        logger.exception("wallet balance lookup failed")
        return 0


def payment_method_keyboard(order_id: int, ctx: dict, user_id: int) -> InlineKeyboardMarkup:
    """انتخاب روش پرداخت — کارت‌به‌کارت / آنلاین / کیف پول + کد تخفیف."""
    rows = []
    if ctx.get("card_number"):
        rows.append([InlineKeyboardButton("💳 کارت‌به‌کارت", callback_data=f"shop_pm_card_{order_id}")])
    if ctx.get("zarinpal") is not None:
        rows.append([InlineKeyboardButton("🌐 پرداخت آنلاین (زرین‌پال)", callback_data=f"shop_pm_online_{order_id}")])
    if ctx.get("resellers") is not None:
        balance = _wallet_balance(ctx, user_id)
        rows.append([InlineKeyboardButton(
            f"💼 پرداخت از کیف پول ({format_reseller_toman(balance)})",
            callback_data=f"shop_pm_wallet_{order_id}",
        )])
    rows.append([InlineKeyboardButton("🎟 اعمال کد تخفیف", callback_data=f"shop_dc_{order_id}")])
    rows.append([InlineKeyboardButton("❌ انصراف", callback_data="shop_cancel")])
    return InlineKeyboardMarkup(rows)


def order_summary_text(order: dict, plan, discount_amount: int = 0) -> str:
    """خلاصهٔ سفارش برای صفحهٔ انتخاب روش پرداخت."""
    discount_line = ""
    price = int(order["amount_toman"])
    if discount_amount > 0:
        discount_line = f"\n🎟 تخفیف: {fa_toman(discount_amount)}"
    return METHOD_PROMPT.format(
        order_id=order["id"],
        plan_title=plan.title if plan else "?",
        plan_gb=fa_num(plan.gb) if plan else "?",
        plan_days=fa_num(plan.days) if plan else "?",
        price=fa_toman(price),
        discount_line=discount_line,
    )


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------
def _is_invited_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """این کاربر زیرمجموعهٔ یک نماینده هست؟ (پنل نمایندگی + فروشگاه)"""
    res_store = context.bot_data.get("resellers")
    if res_store is None:
        return False
    try:
        return bool(res_store.inviter_of(user_id))
    except Exception:
        logger.exception("inviter lookup failed")
        return False


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = context.bot_data.get("store")
    await update.effective_message.reply_text(
        SHOP_INTRO,
        reply_markup=shop_keyboard(_is_invited_user(context, update.effective_user.id), _plans(ctx),
                                   user_id=update.effective_user.id),
        parse_mode="HTML",
    )


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""

    ctx = context.bot_data.get("store")
    if ctx is None:
        await query.edit_message_text("❌ فروشگاه فعال نیست.")
        return
    store: OrderStore = ctx["store"]

    if data == "shop_menu_open":
        # دکمهٔ منوی اصلی → نمایش پلن‌ها (هندلر pattern="^shop_" اینجا می‌گیرد)
        await query.edit_message_text(
            SHOP_INTRO,
            reply_markup=shop_keyboard(_is_invited_user(context, user_id), _plans(ctx), user_id=user_id),
            parse_mode="HTML",
        )
        return

    if data.startswith("shop_buy_"):
        slug = data.split("_")[-1]
        plan = _plans(ctx).get(slug)
        if plan is None:
            await query.edit_message_text("❌ پلن نامعتبر.", reply_markup=shop_keyboard(plans_store=_plans(ctx), user_id=user_id))
            return
        # پلن نمایندگی فقط برای زیرمجموعه‌ها
        if plan.reseller_only and not _is_invited_user(context, user_id):
            await query.answer("این پلن مخصوص کاربران دعوت‌شده است.", show_alert=True)
            return

        # مبلغ = قیمت لحظهٔ خرید (با تخفیف/قیمت اختصاصی مشتری)
        final_price = _plans(ctx).pricing.price_for_plan(plan, user_id=user_id)
        order = store.create_order(user_id, slug, client_name=None, amount_toman=final_price)
        context.user_data.pop("shop_pending_last4", None)
        context.user_data.pop("shop_pay_method", None)
        context.user_data.pop("shop_pending_discount", None)

        if ctx.get("zarinpal") is not None or ctx.get("resellers") is not None:
            # انتخاب روش پرداخت (کارت‌به‌کارت / آنلاین / کیف پول)
            context.user_data["shop_pending_method"] = order["id"]
            await query.edit_message_text(
                order_summary_text(order, plan),
                reply_markup=payment_method_keyboard(order["id"], ctx, user_id),
                parse_mode="HTML",
            )
            return

        # درگاه/کیف‌پول فعال نیست → مستقیم فلوی کارت‌به‌کارت قدیمی
        context.user_data["shop_pending_name"] = order["id"]
        await query.edit_message_text(
            NAME_PROMPT.format(
                order_id=order["id"],
                plan_title=plan.title,
                plan_gb=plan.gb,
                plan_months=max(1, round(plan.days / 30)),
                price=fa_toman(_plans(ctx).pricing.price_for_plan(plan, user_id=user_id)),
                base="username",
            ),
            parse_mode="HTML",
        )

    elif data.startswith("shop_pm_card_"):
        order_id = int(data.rsplit("_", 1)[-1])
        context.user_data.pop("shop_pending_method", None)
        context.user_data["shop_pay_method"] = "card"
        context.user_data["shop_pending_name"] = order_id
        await _prompt_name(query, ctx, order_id)

    elif data.startswith("shop_pm_online_"):
        order_id = int(data.rsplit("_", 1)[-1])
        context.user_data.pop("shop_pending_method", None)
        context.user_data["shop_pay_method"] = "online"
        context.user_data["shop_pending_name"] = order_id
        await _prompt_name(query, ctx, order_id)

    elif data.startswith("shop_pm_wallet_"):
        order_id = int(data.rsplit("_", 1)[-1])
        context.user_data.pop("shop_pending_method", None)
        context.user_data["shop_pay_method"] = "wallet"
        context.user_data["shop_pending_name"] = order_id
        await _prompt_name(query, ctx, order_id)

    elif data.startswith("shop_dc_"):
        order_id = int(data.rsplit("_", 1)[-1])
        context.user_data["shop_pending_discount"] = order_id
        await query.answer()
        await query.message.reply_text(DISCOUNT_PROMPT, parse_mode="HTML")

    elif data.startswith("shop_back_"):
        # برگشت به صفحهٔ انتخاب روش پرداخت
        order_id = int(data.rsplit("_", 1)[-1])
        order = store.get_order(order_id)
        plan = _plans(ctx).get(order["plan"]) if order else None
        if order is None or plan is None:
            await query.edit_message_text("❌ این سفارش دیگر معتبر نیست.")
            return
        context.user_data["shop_pending_method"] = order_id
        context.user_data.pop("shop_pay_method", None)
        await query.edit_message_text(
            order_summary_text(order, plan),
            reply_markup=payment_method_keyboard(order_id, ctx, order["telegram_id"]),
            parse_mode="HTML",
        )

    elif data.startswith("shop_pay_check_"):
        payment_id = int(data.rsplit("_", 1)[-1])
        await _check_online_payment(update, context, payment_id)

    elif data.startswith("shop_trial"):
        await _trial_request(update, context)

    elif data.startswith("shop_wallet"):
        await wallet_command(update, context)

    elif data.startswith("shop_nm_"):
        # کلیک روی اسم پیشنهادی
        picked = data[len("shop_nm_"):]
        if picked == "random":
            picked = random_sub_name()
        await _handle_chosen_name(update, context, picked)

    elif data == "shop_cancel":
        for key in ("shop_pending_last4", "shop_pending_name", "shop_pending_method",
                    "shop_pay_method", "shop_pending_discount", "shop_pending_deposit"):
            context.user_data.pop(key, None)
        await query.edit_message_text("❌ پرداخت لغو شد.", reply_markup=shop_keyboard(plans_store=_plans(ctx), user_id=user_id))

    elif data.startswith("shop_approve_"):
        await _admin_approve(update, context, data.split("_")[-1])

    elif data.startswith("shop_reject_"):
        await _admin_reject(update, context, data.split("_")[-1])


# ---------------------------------------------------------------------------
# دریافت اسم دلخواه (متن) — مرحله قبل از واریز
# ---------------------------------------------------------------------------
async def shop_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MessageHandler: انتخاب اسم اشتراک — فقط وقتی سفارش در انتظار اسم است."""
    order_id = context.user_data.get("shop_pending_name")
    if not order_id:
        return  # ربطی به فروشگاه ندارد → بگذار بقیه هندلرها بگیرندش

    ctx = context.bot_data.get("store")
    if ctx is None:
        return
    store: OrderStore = ctx["store"]
    user_id = update.effective_user.id
    order = store.get_order(order_id)
    if order is None or order["telegram_id"] != user_id:
        context.user_data.pop("shop_pending_name", None)
        return

    text = (update.message.text or "").strip()
    # دستور /start و امثالش را نادیده بگیر
    if text.startswith("/"):
        return
    name = normalize_sub_name(text)
    if not sub_name_valid(name):
        await update.message.reply_text(NAME_INVALID, parse_mode="HTML")
        return
    await _handle_chosen_name(update, context, name)


async def _prompt_name(query, ctx: dict, order_id: int) -> None:
    """نمایش مرحلهٔ انتخاب اسم برای سفارش جاری."""
    store: OrderStore = ctx["store"]
    order = store.get_order(order_id)
    plan = _plans(ctx).get(order["plan"]) if order else None
    if order is None or plan is None:
        await query.edit_message_text("❌ این سفارش دیگر معتبر نیست.")
        return
    await query.edit_message_text(
        NAME_PROMPT.format(
            order_id=order["id"],
            plan_title=plan.title,
            plan_gb=plan.gb,
            plan_months=max(1, round(plan.days / 30)),
            price=fa_toman(int(order["amount_toman"])),
            base="username",
        ),
        parse_mode="HTML",
    )


async def _handle_chosen_name(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    """اعتبارسنجی نهایی اسم (پنل + سفارش‌ها) و رفتن به مرحله واریز."""
    ctx = context.bot_data.get("store")
    store: OrderStore = ctx["store"]
    order_id = context.user_data.get("shop_pending_name")
    if not order_id:
        return
    order = store.get_order(order_id)
    if order is None:
        context.user_data.pop("shop_pending_name", None)
        return
    plan = _plans(ctx).get(order["plan"])

    # ۱) چک پنل (کلاینت‌های موجود) — async
    taken_panel = False
    try:
        taken_panel = await ctx["name_exists"](name)
    except Exception:
        logger.exception("name_exists check failed; assuming free")
    # ۲) چک سفارش‌های موفق فروشگاه
    taken_store = store.name_taken(name)

    if taken_panel or taken_store:
        suggestions = suggest_names(name, lambda n: store.name_taken(n))
        context.user_data["shop_suggestions"] = suggestions
        await update.effective_message.reply_text(
            NAME_TAKEN.format(name=name),
            parse_mode="HTML",
            reply_markup=_name_buttons(suggestions),
        )
        return

    # اسم آزاد است → ثبت و رفتن به مرحله واریز
    store.update_order(order_id, client_name=name)
    context.user_data.pop("shop_pending_name", None)
    method = context.user_data.get("shop_pay_method", "card")

    if method == "online" and ctx.get("zarinpal") is not None:
        await _start_online_payment(update, context, ctx, order_id)
        return
    if method == "wallet" and ctx.get("resellers") is not None:
        await _pay_with_wallet(update, context, ctx, order_id)
        return

    context.user_data["shop_pending_last4"] = order_id

    card = ctx.get("card_number") or "نامشخص"
    holder = ctx.get("card_holder") or ""
    holder_line = f"\n👤 به نام: {holder}" if holder else ""
    base_url = ctx.get("sub_base_url") or "https://…"
    sub_link = f"{base_url.rstrip('/')}/sub/{name}"

    await update.effective_message.reply_text(
        f"✅ اسم <code>{name}</code> برات رزرو شد!\n\n"
        f"🧾 <b>سفارش #{order_id}</b>\n"
        f"📦 پلن: {plan.title} — {fa_num(plan.gb)} گیگ ({fa_num(plan.days)} روز)\n"
        f"💰 مبلغ دقیق: {fa_toman(int(order['amount_toman']))}\n\n"
        f"💳 شماره کارت:\n<code>{card}</code>{holder_line}\n\n"
        f"🔗 لینک اشتراک بعد از تأیید:\n<code>{sub_link}</code>\n\n"
        "۱) دقیقاً همین مبلغ رو واریز کن\n"
        "۲) بعد از واریز، <b>۴ رقم آخر کارتی که باهاش پرداخت کردی</b> رو بفرست\n"
        "۳) بعد از تأیید پشتیبانی، اشتراکت فعال می‌شه ⚡",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# دریافت ۴ رقم آخر کارت از کاربر (متن)
# ---------------------------------------------------------------------------
async def shop_last4_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MessageHandler: ۴ رقم آخر کارت — بعد از انتخاب پلن و اسم."""
    order_id = context.user_data.get("shop_pending_last4")
    if not order_id:
        return  # ربطی به فروشگاه ندارد → بگذار بقیه هندلرها بگیرندش

    ctx = context.bot_data.get("store")
    if ctx is None:
        return
    store: OrderStore = ctx["store"]
    user_id = update.effective_user.id
    order = store.get_order(order_id)
    if order is None or order["telegram_id"] != user_id:
        context.user_data.pop("shop_pending_last4", None)
        return

    text = (update.message.text or "").strip()
    if not _is_valid_last4(text):
        await update.message.reply_text(
            "❌ لطفاً فقط <b>۴ رقم آخر کارت</b> رو بفرست (مثلاً <code>5859</code>).",
            parse_mode="HTML",
        )
        return

    # ثبت + ارسال برای ادمین
    request_id = secrets.token_hex(4)
    store.update_order(
        order_id,
        status=OrderStatus.SUBMITTED,
        request_id=request_id,
        card_last4=text,
        paid_at=datetime.now(timezone.utc).isoformat(),
    )

    plan = _plans(ctx).get(order["plan"])
    client_name = order.get("client_name") or "—"
    caption = (
        f"🛒 <b>سفارش فروشگاه — نیاز به تأیید</b>\n\n"
        f"🧾 سفارش #{order['id']}\n"
        f"👤 تلگرام: <code>{user_id}</code>\n"
        f"🔤 اسم اشتراک: <code>{client_name}</code>\n"
        f"📦 پلن: {plan.title if plan else '?'} ({plan.gb if plan else '?'} گیگ)\n"
        f"💰 مبلغ: {fa_toman(order['amount_toman'])}\n"
        f"💳 ۴ رقم آخر کارت پرداخت‌کننده: <code>{text}</code>\n"
        f"🕒 زمان: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        "واریز را در بانک چک کن. اگر تأیید شد دکمه ✅ را بزن — کانفیگ خودکار ساخته می‌شود."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید و ساخت کانفیگ", callback_data=f"shop_approve_{request_id}")],
        [InlineKeyboardButton("❌ رد", callback_data=f"shop_reject_{request_id}")],
    ])

    sent_to = 0
    for chat_id in ctx.get("admins") or [ctx["admin_id"]]:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            sent_to += 1
        except Exception:
            logger.exception("failed to send shop order to admin %s", chat_id)
    if sent_to == 0:
        store.update_order(order_id, status=OrderStatus.FAILED)
        await update.message.reply_text(
            "❌ ارسال سفارش به پشتیبانی ناموفق بود. لطفاً دوباره تلاش کن."
        )
        return

    context.user_data.pop("shop_pending_last4", None)
    await update.message.reply_text(
        "✅ ۴ رقم آخر ثبت شد و برای تأیید ارسال شد!\n"
        "بعد از تأیید پشتیبانی، کانفیگت همین‌جا ساخته و ارسال می‌شه. ⏳"
    )


# ---------------------------------------------------------------------------
# تأیید/رد توسط ادمین
# ---------------------------------------------------------------------------
async def _admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: str) -> None:
    query = update.callback_query
    await query.answer()
    ctx = context.bot_data.get("store")
    if ctx is None:
        return
    if not ctx.get("is_admin", lambda _uid: False)(update.effective_user.id):
        await query.answer("❌ فقط ادمین اجازه دارد.", show_alert=True)
        return
    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return
    if order.get("status") != OrderStatus.SUBMITTED:
        await query.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return

    store.update_order(order["id"], status=OrderStatus.APPROVED)
    await query.edit_message_text(
        "✅ <b>تأیید شد — در حال ساخت کانفیگ...</b>",
        parse_mode="HTML",
    )

    ok = await finalize_successful_payment(context, ctx, order, owner_admin_id=update.effective_user.id)
    if not ok:
        await context.bot.send_message(
            chat_id=ctx["admin_id"],
            text=f"❌ ساخت کانفیگ سفارش #{order['id']} ناموفق بود. وضعیت کاربر را بررسی کن.",
        )


async def _credit_reseller_commission(context: ContextTypes.DEFAULT_TYPE, ctx: dict, order: dict) -> None:
    """سود نمایندگی: خریدِ زیرمجموعه → درصد به کیف پول دعوت‌کننده."""
    res_store = ctx.get("resellers") or context.application.bot_data.get("resellers")
    if res_store is None:
        return
    try:
        credited = res_store.credit_commission(
            order["telegram_id"], order["amount_toman"], order["id"]
        )
        if credited:
            inviter_id, commission = credited
            await context.bot.send_message(
                chat_id=inviter_id,
                text=(
                    f"💰 سود جدید!\n"
                    f"خرید زیرمجموعه‌ات → {format_reseller_toman(commission)} به کیف پولت اضافه شد.\n"
                    f"جمع سودت رو با /myresellers ببین."
                ),
            )
            await context.bot.send_message(
                chat_id=ctx["admin_id"],
                text=f"💼 سود نمایندگی: {commission:,} تومان → نمایندهٔ {inviter_id}",
            )
    except Exception:
        logger.exception("reseller commission failed (order ok)")


async def finalize_successful_payment(
    context: ContextTypes.DEFAULT_TYPE,
    ctx: dict,
    order: dict,
    *,
    owner_admin_id: int | None = None,
) -> bool:
    """پرداخت قطعی شده → مصرف کد تخفیف + ساخت کانفیگ + سود نمایندگی.

    از هر سه مسیر صدا زده می‌شود: کارت‌به‌کارت (تأیید ادمین)، درگاه آنلاین، کیف پول.
    ضدتکرار: اگر سفارش قبلاً PROVISIONED شده دوباره کانفیگ نمی‌سازد.
    """
    store: OrderStore = ctx["store"]
    fresh = store.get_order(order["id"])
    if fresh is None:
        return False
    if fresh.get("status") == OrderStatus.PROVISIONED:
        return True  # قبلاً انجام شده
    if fresh.get("status") not in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.APPROVED):
        return False

    # مصرف کد تخفیف (فقط بار اول)
    promo_store = ctx.get("promo")
    discount_code = (fresh.get("discount_code") or "").strip()
    if promo_store is not None and discount_code:
        try:
            promo_store.consume(discount_code, fresh["telegram_id"], fresh["id"])
        except Exception:
            logger.exception("promo consume failed (order %s)", fresh["id"])

    ok = await provision_order(ctx, fresh["telegram_id"], fresh, owner_admin_id=owner_admin_id)
    if ok:
        try:
            await context.bot.send_message(
                chat_id=ctx["admin_id"],
                text=f"✅ کانفیگ سفارش #{fresh['id']} ساخته و برای کاربر ارسال شد.",
            )
        except Exception:
            logger.exception("admin notify failed (order %s)", fresh["id"])
        await _credit_reseller_commission(context, ctx, fresh)
    return ok


async def _admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: str) -> None:
    query = update.callback_query
    await query.answer()
    ctx = context.bot_data.get("store")
    if ctx is None:
        return
    if not ctx.get("is_admin", lambda _uid: False)(update.effective_user.id):
        await query.answer("❌ فقط ادمین اجازه دارد.", show_alert=True)
        return
    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return
    if order.get("status") != OrderStatus.SUBMITTED:
        await query.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return

    store.update_order(order["id"], status=OrderStatus.REJECTED)
    await query.edit_message_text("❌ <b>رد شد.</b>", parse_mode="HTML")
    try:
        await context.bot.send_message(
            chat_id=order["telegram_id"],
            text=(
                "❌ متأسفانه پرداخت شما تأیید نشد.\n"
                "اگر واریز انجام داده‌اید لطفاً با پشتیبانی در تماس باشید."
            ),
        )
    except Exception:
        logger.exception("failed to notify user about rejection")


# ---------------------------------------------------------------------------
# ساخت خودکار کلاینت (پس از تأیید) — با اسم انتخابی کاربر
# ---------------------------------------------------------------------------
async def provision_order(ctx: dict, user_id: int, order: dict, *, owner_admin_id: int | None = None) -> bool:
    """ساخت کلاینت در پنل S-UI + ثبت اتصال + ارسال لینک اشتراک به کاربر."""
    plan = _plans(ctx).get(order["plan"])
    if plan is None:
        return False
    store: OrderStore = ctx["store"]
    try:
        client_name = (order.get("client_name") or "").strip()
        if not client_name:
            # اسم به هر دلیلی ثبت نشده — fallback یکتا
            client_name = random_sub_name()
            store.update_order(order["id"], client_name=client_name)
        expiry = make_expiry_days(plan.days)
        volume = gb_to_bytes(plan.gb)

        client_data = ctx["builder"](
            name=client_name,
            volume_bytes=volume,
            expiry_timestamp=expiry,
            desc=f"فروشگاه — سفارش #{order['id']} — tg:{order['telegram_id']}",
            group="shop",
            inbounds=ctx["inbound_ids"],
        )
        result = await ctx["create_client"]("new", client_data)
        if not result:
            # شاید کلاینت واقعاً ساخته شده ولی پاسخ پنل ناقص رسیده — چک کن
            client_id_probe = await ctx["find_client_id"](client_name)
            if not client_id_probe:
                raise RuntimeError("S-UI did not accept client creation")
        else:
            client_id_probe = await ctx["find_client_id"](client_name)
        # S-UI save معمولاً id برنمی‌گرداند → از فهرست کلاینت‌ها بر اساس نام پیدا می‌کنیم
        client_id = client_id_probe
        if not client_id:
            raise RuntimeError("created client not found by name")

        # ثبت اتصال تلگرام → کلاینت
        ctx["assign"](user_id, client_id, owner_admin_id=owner_admin_id)

        # کش لیست کلاینت‌ها را خالی کن — وگرنه منوی اشتراک‌ها ۵ دقیقه خرید جدید را بدون نام/انقضا نشان میدهد
        import sui_bot.bot as _botmod
        _botmod.clients_cache = None
        _botmod.clients_cache_time = 0.0

        # به‌روزرسانی سفارش
        store.update_order(
            order["id"],
            status=OrderStatus.PROVISIONED,
            client_id=client_id,
            provisioned_at=datetime.now(timezone.utc).isoformat(),
        )

        # ارسال پیام موفقیت + لینک ساب
        base_url = ctx.get("sub_base_url") or ""
        sub_link = f"{base_url.rstrip('/')}/sub/{client_name}" if base_url else ""
        text = (
            f"🎉 <b>پرداخت تأیید شد — اشتراک شما فعال است!</b>\n\n"
            f"🧾 سفارش #{order['id']}\n"
            f"🔤 اسم اشتراک: <code>{client_name}</code>\n"
            f"📦 پلن: {plan.title} ({plan.gb} گیگ)\n"
            f"⏳ انقضا: {datetime.fromtimestamp(expiry, tz=timezone.utc).strftime('%Y-%m-%d')}\n\n"
        )
        if sub_link:
            text += f"🔗 لینک اشتراک (توی نرم‌افزار Add Subscription بزن):\n<code>{sub_link}</code>\n\n"
        text += "برای دیدن حجم باقیمانده، همین لینک رو توی مرورگر باز کن 📊"

        await ctx["bot"].send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔗 مشاهده اشتراک", callback_data=f"select_sub_{client_id}")]]
            ),
        )
        return True
    except Exception:
        logger.exception("provision failed for order %s", order["id"])
        store.update_order(order["id"], status=OrderStatus.FAILED)
        try:
            await ctx["bot"].send_message(
                chat_id=user_id,
                text=(
                    "❌ خطا در ساخت کانفیگ. پرداخت شما ثبت شده ولی ساخت خودکار انجام نشد.\n"
                    "لطفاً با پشتیبانی در تماس باشید."
                ),
            )
        except Exception:  # noqa: S110 - intentional fallback
            pass
        return False


# ---------------------------------------------------------------------------
# پرداخت آنلاین (زرین‌پال) — v3
# ---------------------------------------------------------------------------
def _callback_url(ctx: dict) -> str:
    """آدرس callback برای زرین‌پال؛ اگر سرور webhook نداشته باشد لینک ربات."""
    base = (ctx.get("callback_base") or "").strip().rstrip("/")
    if base:
        return f"{base}/pay/callback"
    return f"https://t.me/{ctx.get('bot_username') or 'telegram'}"


async def _start_online_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: dict, order_id: int
) -> None:
    store: OrderStore = ctx["store"]
    payments: PaymentsStore = ctx["payments"]
    zarinpal: ZarinPalClient = ctx["zarinpal"]
    order = store.get_order(order_id)
    user_id = order["telegram_id"]
    amount = int(order["amount_toman"])

    payment = payments.create(user_id, amount, kind="order", order_id=order_id)
    try:
        authority = await zarinpal.request_payment(
            amount, _callback_url(ctx), f"Order #{order_id}"
        )
    except ZarinPalError as exc:
        payments.mark_failed(payment["id"], str(exc))
        await update.effective_message.reply_text(
            PAY_REQUEST_FAIL.format(reason=exc),
            parse_mode="HTML",
        )
        return

    payments.update(payment["id"], authority=authority)
    pay_link = zarinpal.start_pay_url(authority)
    await update.effective_message.reply_text(
        ONLINE_PAY_PROMPT.format(order_id=order_id, price=fa_toman(amount)),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 رفتن به درگاه پرداخت", url=pay_link)],
            [InlineKeyboardButton("✅ پرداخت کردم — بررسی کن", callback_data=f"shop_pay_check_{payment['id']}")],
            [InlineKeyboardButton("↻ روش پرداخت دیگر", callback_data=f"shop_back_{order_id}")],
        ]),
    )


async def _check_online_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payment_id: int
) -> None:
    """دکمهٔ «پرداخت کردم» — تأیید تراکنش در زرین‌پال و ساخت کانفیگ."""
    query = update.callback_query
    ctx = context.bot_data.get("store")
    if ctx is None:
        await query.answer("❌ فروشگاه فعال نیست.", show_alert=True)
        return
    payments: PaymentsStore = ctx["payments"]
    zarinpal: ZarinPalClient = ctx["zarinpal"]
    store: OrderStore = ctx["store"]
    payment = payments.get(payment_id)
    if payment is None:
        await query.answer("❌ این تراکنش پیدا نشد.", show_alert=True)
        return
    user_id = update.effective_user.id
    if payment["tg_id"] != user_id and not ctx.get("is_admin", lambda _u: False)(user_id):
        await query.answer("❌ این تراکنش مال شما نیست.", show_alert=True)
        return
    if payment["status"] == PaymentStatus.PAID:
        await query.answer("✅ این پرداخت قبلاً تأیید و تحویل شده است.", show_alert=True)
        return
    if payment["status"] == PaymentStatus.FAILED:
        await query.answer("❌ این تراکنش ناموفق ثبت شده؛ لطفاً دوباره خرید را شروع کنید.", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(PAY_CHECK_WAIT)
    result = await zarinpal.verify_payment(payment["amount_toman"], payment.get("authority") or "")
    if not result.ok:
        await query.message.reply_text(PAY_CHECK_FAIL.format(reason=result.message))
        return

    payments.mark_paid(payment["id"], result.ref_id)
    await query.message.reply_text(PAY_CHECK_OK)

    if payment["kind"] == "order":
        order = store.get_order(payment["order_id"]) if payment.get("order_id") else None
        if order is not None:
            store.update_order(order["id"], status=OrderStatus.APPROVED, paid_at=datetime.now(timezone.utc).isoformat())
            order = store.get_order(order["id"])
            await finalize_successful_payment(context, ctx, order)
    else:  # deposit
        _credit_wallet(ctx, payment["tg_id"], payment["amount_toman"])
        balance = _wallet_balance(ctx, payment["tg_id"])
        try:
            await ctx["bot"].send_message(
                chat_id=payment["tg_id"],
                text=(
                    f"✅ شارژ کیف پول انجام شد!\n"
                    f"💰 مبلغ: {format_reseller_toman(payment['amount_toman'])}"
                    + (f"\n🧾 کد پیگیری: <code>{result.ref_id}</code>" if result.ref_id else "")
                    + f"\n💼 موجودی جدید: <b>{format_reseller_toman(balance)}</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("deposit notify failed (payment %s)", payment["id"])


def _credit_wallet(ctx: dict, tg_id: int, amount_toman: int) -> int | None:
    """شارژ کیف پول — مقدار جدید موجودی یا None."""
    resellers = ctx.get("resellers")
    if resellers is None:
        return None
    try:
        resellers.ensure_member(tg_id)
        return resellers.adjust_balance(tg_id, int(amount_toman))
    except Exception:
        logger.exception("wallet credit failed")
        return None


async def _pay_with_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: dict, order_id: int
) -> None:
    store: OrderStore = ctx["store"]
    order = store.get_order(order_id)
    if order is None:
        await update.effective_message.reply_text("❌ این سفارش دیگر معتبر نیست.")
        return
    user_id = order["telegram_id"]
    amount = int(order["amount_toman"])
    balance = _wallet_balance(ctx, user_id)
    if balance < amount:
        await update.effective_message.reply_text(
            WALLET_INSUFFICIENT.format(balance=format_reseller_toman(balance), price=fa_toman(amount)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↻ روش پرداخت دیگر", callback_data=f"shop_back_{order_id}")
            ]]),
        )
        return

    new_balance = _credit_wallet(ctx, user_id, -amount)
    store.update_order(order_id, status=OrderStatus.APPROVED, paid_at=datetime.now(timezone.utc).isoformat())
    order = store.get_order(order_id)
    await update.effective_message.reply_text(
        WALLET_PAID.format(price=fa_toman(amount), balance=format_reseller_toman(new_balance or 0)),
        parse_mode="HTML",
    )
    ok = await finalize_successful_payment(context, ctx, order)
    if not ok:
        # ساخت کانفیگ شکست خورد → برگشت پول به کیف پول
        _credit_wallet(ctx, user_id, amount)
        try:
            await ctx["bot"].send_message(
                chat_id=ctx["admin_id"],
                text=f"❌ ساخت کانفیگ سفارش #{order_id} ناموفق بود؛ مبلغ به کیف پول کاربر برگشت داده شد.",
            )
        except Exception:
            logger.exception("wallet refund admin notify failed")


# ---------------------------------------------------------------------------
# اکانت تست رایگان — v3
# ---------------------------------------------------------------------------
async def _trial_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    ctx = context.bot_data.get("store")
    trial_store: TrialStore | None = ctx.get("trial") if ctx else None
    if trial_store is None:
        if query:
            await query.answer(TRIAL_DISABLED, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(TRIAL_DISABLED)
        return
    user_id = update.effective_user.id
    if trial_store.has_used(user_id):
        if query:
            await query.answer(TRIAL_USED, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(TRIAL_USED)
        return

    if query:
        await query.answer()
        target = query.message
    else:
        target = update.effective_message
    await target.reply_text(TRIAL_CREATING)

    # اسم یکتا برای کلاینت تست
    for _ in range(5):
        name = "t" + secrets.token_hex(4)
        try:
            if not await ctx["name_exists"](name):
                break
        except Exception:
            logger.exception("trial name_exists check failed")
            break
    inbounds = list(ctx.get("inbound_ids") or [])
    trial_inbound = int(ctx.get("trial_inbound_id") or 0)
    if trial_inbound:
        inbounds = [trial_inbound]
    if not inbounds:
        await target.reply_text("❌ اینباندی برای ساخت اکانت تست پیدا نشد؛ با پشتیبانی در تماس باش.")
        return

    client_data = ctx["builder"](
        name=name,
        volume_bytes=trial_volume_bytes(),
        expiry_timestamp=trial_expiry_ts(),
        desc=f"trial:tg:{user_id}",
        group="trial",
        inbounds=inbounds,
    )
    result = await ctx["create_client"]("new", client_data)
    client_id = await ctx["find_client_id"](name) if result else None
    if not client_id:
        await target.reply_text("❌ ساخت اکانت تست ناموفق بود؛ لطفاً دوباره تلاش کن یا با پشتیبانی در تماس باش.")
        return
    ctx["assign"](user_id, client_id)
    try:
        import sui_bot.bot as _botmod
        _botmod.clients_cache = None
        _botmod.clients_cache_time = 0.0
    except Exception:
        logger.exception("clients cache reset failed (trial)")

    trial_store.record(user_id, name)
    base_url = ctx.get("sub_base_url") or ""
    sub_link = f"{base_url.rstrip('/')}/sub/{name}" if base_url else ""
    await target.reply_text(
        TRIAL_OK.format(
            gb=str(trial_volume_bytes() / (1024 * 1024 * 1024)).rstrip("0").rstrip("."),
            days=1,
            name=name,
            sub_link=sub_link or "—",
        ),
        parse_mode="HTML",
    )


async def trial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /trial — اکانت تست رایگان."""
    await _trial_request(update, context)


# ---------------------------------------------------------------------------
# کیف پول — منو + شارژ آنلاین
# ---------------------------------------------------------------------------
async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wallet یا دکمهٔ کیف پول → موجودی + شارژ."""
    query = update.callback_query
    if query:
        await query.answer()
    ctx = context.bot_data.get("store")
    if ctx is None or ctx.get("resellers") is None:
        text = "❌ کیف پول فعال نیست."
        if query:
            await query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return
    user_id = update.effective_user.id
    balance = _wallet_balance(ctx, user_id)
    rows = []
    if ctx.get("zarinpal") is not None:
        rows.append([InlineKeyboardButton("➕ شارژ کیف پول (درگاه آنلاین)", callback_data="wallet_dep")])
    rows.append([InlineKeyboardButton("🛍 فروشگاه", callback_data="shop_menu_open")])
    text = WALLET_MENU.format(balance=format_reseller_toman(balance))
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML"
        )


async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌های wallet_ — شارژ کیف پول و بررسی واریز."""
    query = update.callback_query
    await query.answer()
    ctx = context.bot_data.get("store")
    if ctx is None:
        await query.edit_message_text("❌ فروشگاه فعال نیست.")
        return
    data = query.data or ""
    if data == "wallet_dep":
        context.user_data["shop_pending_deposit"] = True
        min_amount = int(ctx.get("zarinpal_min_toman") or 0)
        await query.edit_message_text(DEPOSIT_PROMPT.format(min=fa_toman(min_amount)), parse_mode="HTML")
    elif data.startswith("wallet_chk_"):
        payment_id = int(data.rsplit("_", 1)[-1])
        await _check_online_payment(update, context, payment_id)


# ---------------------------------------------------------------------------
# ورودی متنی مشترک (گروه ۶): کد تخفیف + مبلغ واریز + ساخت کد توسط ادمین
# ---------------------------------------------------------------------------
DISCOUNT_STEPS = ("code", "percent", "max_uses", "days")


def _parse_int(text: str) -> int | None:
    cleaned = (text or "").strip().replace("٬", "").replace(",", "")
    try:
        return int(cleaned)
    except ValueError:
        return None


async def shop_extra_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ورودی متنی فلوی تخفیف/واریز/پنل تخفیف — بدون فلگ، no-op (گروه ۶)."""
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    # ۱) ورود کد تخفیف در سفارش
    order_id = context.user_data.get("shop_pending_discount")
    if order_id:
        ctx = context.bot_data.get("store")
        store: OrderStore = ctx["store"]
        promo = ctx.get("promo")
        order = store.get_order(order_id)
        if ctx is None or promo is None or order is None or order["telegram_id"] != update.effective_user.id:
            context.user_data.pop("shop_pending_discount", None)
            return
        result: PromoResult = promo.discount_amount(text, update.effective_user.id, int(order["amount_toman"]))
        if not result.ok:
            await update.message.reply_text(DISCOUNT_REJECT.get(result.reason, "❌ کد نامعتبر است."))
            return
        new_total = int(order["amount_toman"]) - result.amount
        clean_code = text.strip().upper()
        store.update_order(
            order_id,
            amount_toman=new_total,
            discount_code=clean_code,
            discount_amount=result.amount,
        )
        context.user_data.pop("shop_pending_discount", None)
        plan = _plans(ctx).get(order["plan"])
        await update.message.reply_text(
            DISCOUNT_APPLIED.format(code=clean_code, percent=result.percent,
                                    discount=fa_toman(result.amount), price=fa_toman(new_total))
            + "\n\n" + order_summary_text(store.get_order(order_id), plan),
            parse_mode="HTML",
            reply_markup=payment_method_keyboard(order_id, ctx, update.effective_user.id),
        )
        return

    # ۲) مبلغ واریز کیف پول
    if context.user_data.get("shop_pending_deposit"):
        ctx = context.bot_data.get("store")
        if ctx is None or ctx.get("zarinpal") is None:
            context.user_data.pop("shop_pending_deposit", None)
            return
        min_amount = int(ctx.get("zarinpal_min_toman") or 0)
        amount = _parse_int(text)
        if amount is None or amount < min_amount:
            await update.message.reply_text(DEPOSIT_BAD.format(min=fa_toman(min_amount)), parse_mode="HTML")
            return
        context.user_data.pop("shop_pending_deposit", None)
        payments: PaymentsStore = ctx["payments"]
        zarinpal: ZarinPalClient = ctx["zarinpal"]
        payment = payments.create(update.effective_user.id, amount, kind="deposit")
        try:
            authority = await zarinpal.request_payment(amount, _callback_url(ctx), "Wallet deposit")
        except ZarinPalError as exc:
            payments.mark_failed(payment["id"], str(exc))
            await update.message.reply_text(PAY_REQUEST_FAIL.format(reason=exc), parse_mode="HTML")
            return
        payments.update(payment["id"], authority=authority)
        await update.message.reply_text(
            f"🌐 مبلغ <b>{fa_toman(amount)}</b> برای شارژ کیف پول ثبت شد.\nبه درگاه برو و بعد از پرداخت دکمهٔ بررسی رو بزن:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 رفتن به درگاه پرداخت", url=zarinpal.start_pay_url(authority))],
                [InlineKeyboardButton("✅ پرداخت کردم — بررسی کن", callback_data=f"wallet_chk_{payment['id']}")],
            ]),
        )
        return

    # ۳) ساخت کد تخفیف توسط ادمین (مرحله‌به‌مرحله)
    promo_state = context.user_data.get("promo_create")
    if promo_state:
        _promo_admin_text(update, context, promo_state, text)


# ---------------------------------------------------------------------------
# پنل ادمین — مدیریت کدهای تخفیف (/discounts)
# ---------------------------------------------------------------------------
def _promo_line(item: dict) -> str:
    uses = f"{item.get('used_count', 0)}/{item.get('max_uses') or '∞'}"
    expires = (item.get("expires_at") or "")[:10] or "بدون انقضا"
    status = "✅ فعال" if item.get("active", True) else "⛔ غیرفعال"
    return f"• <code>{item['code']}</code> — {fa_num(item['percent'])}٪ — مصرف {uses} — {status} — انقضا: {expires}"


def _promo_list_markup(discounts) -> InlineKeyboardMarkup:
    rows = []
    for item in discounts.all():
        code = item["code"]
        rows.append([
            InlineKeyboardButton(f"🔁 {code}", callback_data=f"promo_t_{code}"),
            InlineKeyboardButton(f"🗑 {code}", callback_data=f"promo_d_{code}"),
        ])
    rows.append([InlineKeyboardButton("➕ ساخت کد جدید", callback_data="promo_new")])
    return InlineKeyboardMarkup(rows)


async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/discounts — مدیریت کدهای تخفیف (فقط ادمین)."""
    if not is_store_admin(update, context):
        return
    ctx = context.bot_data.get("store")
    promo = ctx.get("promo") if ctx else None
    if promo is None:
        await update.effective_message.reply_text("❌ سیستم کد تخفیف ثبت نشده است.")
        return
    codes = promo.all()
    text = "🎟 <b>کدهای تخفیف</b>\n\n" + ("\n".join(_promo_line(c) for c in codes) if codes else "کدی ساخته نشده.")
    await update.effective_message.reply_text(text, reply_markup=_promo_list_markup(promo), parse_mode="HTML")


def is_store_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ctx = context.bot_data.get("store")
    if ctx is None:
        return False
    return bool(ctx.get("is_admin", lambda _u: False)(update.effective_user.id))


async def promo_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌های promo_ — ساخت/فعال‌سازی/حذف کد تخفیف."""
    query = update.callback_query
    await query.answer()
    if not is_store_admin(update, context):
        await query.answer("❌ فقط ادمین اجازه دارد.", show_alert=True)
        return
    ctx = context.bot_data.get("store")
    promo = ctx.get("promo")
    if promo is None:
        await query.edit_message_text("❌ سیستم کد تخفیف ثبت نشده است.")
        return
    data = query.data or ""
    if data == "promo_new":
        context.user_data["promo_create"] = {"step": "code", "data": {}}
        await query.message.reply_text(
            "➕ <b>ساخت کد تخفیف</b>\n\n۱) کد رو بفرست (۳ تا ۳۲ کاراکتر لاتین/عدد/خط‌تیره):\n\n/cancel برای انصراف",
            parse_mode="HTML",
        )
        return
    if data.startswith("promo_t_"):
        code = data[len("promo_t_"):]
        result = promo.toggle(code)
        if result is None:
            await query.answer("کد پیدا نشد.", show_alert=True)
            return
        await query.answer(f"کد {code}: {'فعال' if result else 'غیرفعال'} شد.")
        codes = promo.all()
        await query.edit_message_text(
            "🎟 <b>کدهای تخفیف</b>\n\n" + ("\n".join(_promo_line(c) for c in codes) if codes else "کدی ساخته نشده."),
            reply_markup=_promo_list_markup(promo),
            parse_mode="HTML",
        )
        return
    if data.startswith("promo_d_"):
        code = data[len("promo_d_"):]
        promo.delete(code)
        await query.answer(f"کد {code} حذف شد.")
        codes = promo.all()
        await query.edit_message_text(
            "🎟 <b>کدهای تخفیف</b>\n\n" + ("\n".join(_promo_line(c) for c in codes) if codes else "کدی ساخته نشده."),
            reply_markup=_promo_list_markup(promo),
            parse_mode="HTML",
        )
        return


async def _promo_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, text: str) -> None:
    """مراحل ساخت کد تخفیف: کد → درصد → سقف استفاده → روز اعتبار."""
    step = state.get("step")
    data = state.setdefault("data", {})
    if step == "code":
        from .promo_core import code_valid, normalize_code
        if not code_valid(text):
            await update.message.reply_text("❌ کد نامعتبر: ۳ تا ۳۲ کاراکتر لاتین/عدد/خط‌تیره. دوباره بفرست:")
            return
        data["code"] = normalize_code(text)
        state["step"] = "percent"
        await update.message.reply_text("۲) درصد تخفیف (۱ تا ۹۰):")
        return
    if step == "percent":
        percent = _parse_int(text)
        if percent is None or not 1 <= percent <= 90:
            await update.message.reply_text("❌ عدد بین ۱ تا ۹۰ بفرست:")
            return
        data["percent"] = percent
        state["step"] = "max_uses"
        await update.message.reply_text("۳) سقف تعداد استفاده (۰ = بی‌نهایت):")
        return
    if step == "max_uses":
        max_uses = _parse_int(text)
        if max_uses is None or max_uses < 0:
            await update.message.reply_text("❌ عدد صحیح بفرست (۰ = بی‌نهایت):")
            return
        data["max_uses"] = max_uses
        state["step"] = "days"
        await update.message.reply_text("۴) اعتبار به روز (۰ = بدون انقضا):")
        return
    if step == "days":
        days = _parse_int(text)
        if days is None or days < 0:
            await update.message.reply_text("❌ عدد صحیح بفرست (۰ = بدون انقضا):")
            return
        ctx = context.bot_data.get("store")
        promo = ctx.get("promo")
        try:
            promo.create(data["code"], data["percent"], data["max_uses"], days)
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}\nکد رو دوباره بفرست:")
            state["step"] = "code"
            return
        context.user_data.pop("promo_create", None)
        codes = promo.all()
        await update.message.reply_text(
            f"✅ کد <code>{data['code']}</code> ساخته شد ({data['percent']}٪).\n\n"
            + ("\n".join(_promo_line(c) for c in codes) if codes else ""),
            reply_markup=_promo_list_markup(promo),
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# ثبت در bot.py
# ---------------------------------------------------------------------------
def register_store_handlers(
    app,
    *,
    store: OrderStore,
    admin_id: int,
    card_number: str,
    card_holder: str,
    builder,
    create_client,
    assign,
    find_client_id,
    name_exists,
    inbound_ids: list[int],
    sub_base_url: str = "",
    is_admin=None,
    admins: list[int] | None = None,
    plans_store: PlansStore | None = None,
    zarinpal: ZarinPalClient | None = None,
    payments: PaymentsStore | None = None,
    promo=None,
    trial: TrialStore | None = None,
    resellers=None,
    trial_inbound_id: int = 0,
    callback_base: str = "",
    bot_username: str = "",
    zarinpal_min_toman: int = 50_000,
) -> None:
    """هندلرهای فروشگاه را به اپلیکیشن اضافه کن.

    builder        = build_client_data_new        (از bot.py)
    create_client  = create_or_edit_client        (از bot.py)
    assign         = add_client_assignment        (از bot.py)
    find_client_id = async (name) -> client_id    (از bot.py)
    name_exists    = async (name) -> bool         (از bot.py — چک تکراری بودن اسم)
    inbound_ids    = [id, ...] برای ساخت کلاینت (از get_inbounds_list)
    sub_base_url   = https://tnt.traviann.ir:2096 (برای نمایش لینک)
    zarinpal       = ZarinPalClient یا None (درگاه غیرفعال)
    payments       = PaymentsStore (لازم برای درگاه/شارژ کیف پول)
    promo          = DiscountStore (کد تخفیف)
    trial          = TrialStore (اکانت تست رایگان)
    resellers      = ResellerStore (کیف پول + نمایندگی)
    """
    app.bot_data["store"] = {
        "store": store,
        "admin_id": admin_id,
        "is_admin": is_admin or (lambda uid: uid == admin_id),
        "admins": list(admins or [admin_id]),
        "plans_store": plans_store,
        "card_number": card_number,
        "card_holder": card_holder,
        "builder": builder,
        "create_client": create_client,
        "assign": assign,
        "find_client_id": find_client_id,
        "name_exists": name_exists,
        "inbound_ids": list(inbound_ids or []),
        "sub_base_url": sub_base_url,
        "bot": app.bot,
        "zarinpal": zarinpal,
        "payments": payments,
        "promo": promo,
        "trial": trial,
        "resellers": resellers,
        "trial_inbound_id": int(trial_inbound_id or 0),
        "callback_base": callback_base,
        "bot_username": bot_username,
        "zarinpal_min_toman": int(zarinpal_min_toman),
    }
    if resellers is not None:
        app.bot_data["resellers"] = resellers
    app.add_handler(CommandHandler("shop", shop_command))
    app.add_handler(CommandHandler("trial", trial_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("discounts", promo_command))
    app.add_handler(CallbackQueryHandler(shop_callback, pattern="^shop_"))
    app.add_handler(CallbackQueryHandler(wallet_callback, pattern="^wallet_"))
    app.add_handler(CallbackQueryHandler(promo_admin_callback, pattern="^promo_"))
    # ترتیب مهم است: اول اسم، بعد ۴ رقم آخر — هر دو فقط با user_data فعال می‌شوند
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, shop_name_handler), group=4)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, shop_last4_handler), group=5)
    # تخفیف / واریز / پنل تخفیف — گروه ۶ (بعد از هندلرهای اصلی)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, shop_extra_text_handler), group=6)
