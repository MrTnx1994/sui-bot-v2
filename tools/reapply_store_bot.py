"""Re-apply store_bot.py changes to the clean extracted file."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sui_bot"
TARGET = ROOT / "store_bot.py"


def patch(old: str, new: str, count: int = 1) -> None:
    text = TARGET.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences != count:
        raise SystemExit(f"store_bot.py: expected {count}, found {occurrences}:\n{old[:90]}")
    TARGET.write_text(text.replace(old, new, count), encoding="utf-8")
    print(f"ok {old[:48]!r}")


# 1) imports: PLANS/get_plan/make_expiry حذف و PlansStore/fa_* اضافه
patch(
    """from .store_core import (
    PLANS,
    OrderStatus,
    OrderStore,
    format_toman,
    gb_to_bytes,
    get_plan,
    make_expiry,
    normalize_sub_name,
    random_sub_name,
    sub_name_valid,
    suggest_names,
)
from .reseller_core import format_reseller_toman""",
    """from .store_core import (
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
from .reseller_core import format_reseller_toman""",
)

# 2) فروشگاه:PlansStore + قیمت اختصاصی + اعداد فارسی
patch(
    'def shop_keyboard(is_invited: bool | None = None) -> InlineKeyboardMarkup:',
    'def shop_keyboard(is_invited: bool | None = None, plans_store: PlansStore | None = None,\n'
    '                  user_id: int | None = None) -> InlineKeyboardMarkup:',
)
patch(
    '''    rows = []
    for plan in PLANS:
        if plan.reseller_only and is_invited is not True:
            continue
        label = f"{plan.title} — {plan.gb} گیگ — {format_toman(plan.price_toman)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"shop_buy_{plan.slug}")])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)''',
    '''    plans = (plans_store.visible_plans(reseller=bool(is_invited)) if plans_store else [])
    rows = []
    for plan in plans:
        price = fa_toman(plans_store.pricing.price_for_plan(plan, user_id=user_id)) if plans_store else "—"
        label = f"{plan.title} • {fa_num(plan.gb)} گیگ • {fa_num(plan.days)} روز • {price}"
        rows.append([InlineKeyboardButton(label, callback_data=f"shop_buy_{plan.slug}")])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)''',
)

# 3) helper _plans + ctx routing
patch(
    'logger = logging.getLogger("sui_bot.store_bot")',
    '''logger = logging.getLogger("sui_bot.store_bot")


def _plans(ctx: dict | None) -> PlansStore:
    """PlansStore از bot_data (ثبت‌شده در register_store_handlers)."""
    if ctx and ctx.get("plans_store") is not None:
        return ctx["plans_store"]
    raise RuntimeError("plans store is not registered")''',
)

# 4) shop_command: ctx + plans_store
patch(
    '''async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        SHOP_INTRO,
        reply_markup=shop_keyboard(_is_invited_user(context, update.effective_user.id)),
    )''',
    '''async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = context.bot_data.get("store")
    await update.effective_message.reply_text(
        SHOP_INTRO,
        reply_markup=shop_keyboard(_is_invited_user(context, update.effective_user.id), _plans(ctx),
                                   user_id=update.effective_user.id),
    )''',
)

# 5) shop_callback menu open
patch(
    '''        await query.edit_message_text(
            SHOP_INTRO,
            reply_markup=shop_keyboard(_is_invited_user(context, user_id)),
        )''',
    '''        await query.edit_message_text(
            SHOP_INTRO,
            reply_markup=shop_keyboard(_is_invited_user(context, user_id), _plans(ctx), user_id=user_id),
        )''',
)

# 6) shop_buy_ flow: plan lookup + قیمت لحظهٔ خرید
patch(
    "        plan = get_plan(slug)",
    "        plan = _plans(ctx).get(slug)",
)
patch(
    '''        # سفارش بدون اسم ساخته می‌شود؛ اسم بعداً انتخاب می‌شود
        order = store.create_order(user_id, slug, client_name=None)''',
    '''        # مبلغ = قیمت لحظهٔ خرید (با تخفیف/قیمت اختصاصی مشتری)
        final_price = _plans(ctx).pricing.price_for_plan(plan, user_id=user_id)
        order = store.create_order(user_id, slug, client_name=None, amount_toman=final_price)''',
)
patch(
    '''                plan_title=plan.title,
                plan_gb=plan.gb,
                plan_months=plan.months,
                price=format_toman(plan.price_toman),''',
    '''                plan_title=plan.title,
                plan_gb=plan.gb,
                plan_months=max(1, round(plan.days / 30)),
                price=fa_toman(_plans(ctx).pricing.price_for_plan(plan, user_id=user_id)),''',
)
patch(
    "reply_markup=shop_keyboard())",
    "reply_markup=shop_keyboard(plans_store=_plans(ctx), user_id=user_id))",
    count=2,
)

# 7) نام‌ها/پیام واریز: قیمت اختصاصی
patch(
    '    plan = get_plan(order["plan"])',
    '    plan = _plans(ctx).get(order["plan"])',
    count=3,
)
patch(
    "        f\"💰 مبلغ دقیق: {format_toman(plan.price_toman)}\\n\\n\"",
    "        f\"💰 مبلغ دقیق: {fa_toman(_plans(ctx).pricing.price_for_plan(plan, user_id=user_id))}\\n\\n\"",
)
patch(
    'f"💰 مبلغ: {format_toman(order[\'amount_toman\'])}\\n"',
    'f"💰 مبلغ: {fa_toman(order[\'amount_toman\'])}\\n"',
)

# 8) approve flow: owner admin + expiry by days
patch(
    "    ok = await provision_order(ctx, order[\"telegram_id\"], order)",
    "        ok = await provision_order(ctx, order[\"telegram_id\"], order, owner_admin_id=update.effective_user.id)",
)
patch(
    "async def provision_order(ctx: dict, user_id: int, order: dict) -> bool:",
    "async def provision_order(ctx: dict, user_id: int, order: dict, *, owner_admin_id: int | None = None) -> bool:",
)
patch(
    "        ctx[\"assign\"](user_id, client_id)",
    "        ctx[\"assign\"](user_id, client_id, owner_admin_id=owner_admin_id)",
)
patch(
    "        expiry = make_expiry(plan.months)",
    "        expiry = make_expiry_days(plan.days)",
)

# 9) admin approve/reject guards (multi-admin)
patch(
    '    if update.effective_user.id != ctx["admin_id"]:',
    '    if not ctx.get("is_admin", lambda _uid: False)(update.effective_user.id):',
    count=2,
)

# 10) double-processing guard (چند-ادمینی: اولین تایید برنده است)
patch(
    '''    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return

    store.update_order(order["id"], status=OrderStatus.APPROVED)''',
    '''    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return
    if order.get("status") != OrderStatus.SUBMITTED:
        await query.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return

    store.update_order(order["id"], status=OrderStatus.APPROVED)''',
)
patch(
    '''    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return

    store.update_order(order["id"], status=OrderStatus.REJECTED)''',
    '''    store: OrderStore = ctx["store"]
    order = store.find_by_request_id(request_id)
    if order is None:
        await query.edit_message_text("❌ این درخواست دیگر معتبر نیست.")
        return
    if order.get("status") != OrderStatus.SUBMITTED:
        await query.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return

    store.update_order(order["id"], status=OrderStatus.REJECTED)''',
)

# 11) send order card to every admin (اولین تایید برنده)
patch(
    '''    try:
        await context.bot.send_message(
            chat_id=ctx["admin_id"],
            text=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to send shop order to admin")
        store.update_order(order_id, status=OrderStatus.FAILED)
        await update.message.reply_text(
            "❌ ارسال سفارش به پشتیبانی ناموفق بود. لطفاً دوباره تلاش کن."
        )
        return''',
    '''    sent_to = 0
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
        return''',
)

# 12) register: is_admin/admins/plans_store
patch(
    "    is_admin=None,\n    admins: list[int] | None = None,\n) -> None:",
    "    is_admin=None,\n    admins: list[int] | None = None,\n    plans_store: PlansStore | None = None,\n) -> None:",
)
patch(
    '''        "admin_id": admin_id,
        "card_number": card_number,''',
    '''        "admin_id": admin_id,
        "is_admin": is_admin or (lambda uid: uid == admin_id),
        "admins": list(admins or [admin_id]),
        "plans_store": plans_store,
        "card_number": card_number,''',
)

print("store_bot.py: all patches applied")
