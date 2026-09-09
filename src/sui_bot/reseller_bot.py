"""نمایندگی فروش — هندلرهای تلگرام.

- هر کاربر با /start یه لینک دعوت شخصی داره (همه نماینده‌ان)
- خرید زیرمجموعه → سود درصدی خودکار به کیف پول دعوت‌کننده
- پلن‌های ارزان‌تر فقط برای زیرمجموعه‌ها (slugهای r1/r3/r6/r12)
- /myresellers پنل نماینده | /settle تسویه ادمین
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from .reseller_core import (
    DEFAULT_COMMISSION_PCT,
    RESELLER_PLANS,
    ResellerStore,
    format_reseller_toman,
)
from .store_core import OrderStore

logger = logging.getLogger("sui_bot.reseller")


async def _safe_query_answer(query, text=None, show_alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception:  # noqa: S110 - intentional fallback
        pass

RES_PREF = "res_"

LINK_TMPL = (
    "💼 <b>پنل نمایندگی Vpnfiy</b>\n\n"
    "💰 <b>کیف پول:</b> {balance}\n"
    "📈 <b>درآمد کل:</b> {earned}\n"
    "🎯 درصد سود تو: {pct}٪\n\n"
    "👥 <b>زیرمجموعه‌ها:</b> {n_invited} نفر\n"
    "🛒 فروش زیرمجموعه‌ها: {n_sold} سفارش\n\n"
    "🔗 <b>لینک دعوت</b> (هر کی با این بیاد زیرمجموعه‌ات میشه):\n"
    "<code>{link}</code>\n\n"
    "💵 برای برداشت، دکمهٔ «درخواست تسویه» رو بزن — پیامت مستقیم میره برای پشتیبانی."
)

SETTLE_REQUEST_TMPL = (
    "💵 <b>درخواست تسویه</b>\n\n"
    "👤 نماینده: <a href=\"{origin}\">{first_name}</a> (<code>{tg_id}</code>)\n"
    "👛 موجودی درخواستی: <b>{balance}</b>\n"
    "📈 کل درآمدش تا حالا: {earned}\n\n"
    "پس از واریز، /settle {tg_id} بزن."
)


def reseller_deep_link(bot_username: str, telegram_id: int) -> str:
    return f"https://t.me/{bot_username}?start=r{telegram_id}"


def _parse_start_ref(args: list[str]) -> int | None:
    """«r5612345678» → 5612345678."""
    if not args:
        return None
    raw = (args[0] or "").strip()
    if raw.startswith("r") and raw[1:].isdigit():
        val = int(raw[1:])
        if val > 0:
            return val
    return None


def reseller_plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for p in RESELLER_PLANS:
        label = f"{p['title']} — {p['gb']} گیگ — {format_reseller_toman(p['price_toman'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{RES_PREF}buy_{p['slug']}")])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def register_reseller_handlers(app) -> None:
    """از bot.py صدا زده می‌شود؛ ctx در bot_data['store'] توسط register_store_handlers ست شده."""
    app.add_handler(CommandHandler("myresellers", reseller_panel))
    app.add_handler(CommandHandler("settle", admin_settle))
    app.add_handler(CommandHandler("setpct", admin_setpct))
    app.add_handler(CommandHandler("finance", admin_finance))
    # ترتیب مهم: ادمین اول — res_manage_/res_m_ قبل از پترن کلی res_
    app.add_handler(CallbackQueryHandler(admin_finance_callback, pattern=r"^(admin_finance_refresh|res_edit_|res_manage_|res_m_)"))
    app.add_handler(CallbackQueryHandler(reseller_callback, pattern=f"^{RES_PREF}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_finance_amount), group=2)


async def reseller_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_reseller_panel(update, context, edit=False)


async def reseller_panel_update(update: Update, context: ContextTypes.DEFAULT_TYPE, update_obj=None) -> None:
    """نمای پنل از طریق دکمهٔ منو (edit_message_text)."""
    await _render_reseller_panel(update, context, edit=True, update_obj=update_obj)


async def _render_reseller_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False, update_obj=None) -> None:
    ctx_store: ResellerStore | None = context.bot_data.get("resellers")
    if ctx_store is None:
        target = update_obj or update.effective_message
        await target.reply_text("❌ نمایندگی فعال نیست.")
        return
    user_id = update.effective_user.id
    me = ctx_store.ensure_member(user_id)
    bot_username = (await context.bot.get_me()).username
    pct = me.get("pct") if me.get("pct") is not None else DEFAULT_COMMISSION_PCT
    invited = ctx_store.invited_by_member(user_id)

    sold = 0
    per_invitee: dict[int, int] = {}
    store = context.bot_data.get("store", {}).get("store")
    if store is not None:
        inv_ids = [m["telegram_id"] for m in invited]
        for o in store.all_orders():
            if o.get("telegram_id") in inv_ids and o.get("status") in ("approved", "provisioned"):
                sold += 1
                per_invitee[o["telegram_id"]] = per_invitee.get(o["telegram_id"], 0) + 1

    text = LINK_TMPL.format(
        link=reseller_deep_link(bot_username, user_id),
        pct=pct,
        n_invited=len(invited),
        n_sold=sold,
        earned=format_reseller_toman(me.get("earned_total", 0)),
        balance=format_reseller_toman(me.get("balance_toman", 0)),
    )

    # لیست بصری زیرمجموعه‌ها
    if invited:
        text += "\n👥 <b>زیرمجموعه‌های تو:</b>\n"
        for m in invited[:15]:
            n = per_invitee.get(m["telegram_id"], 0)
            try:
                chat = await context.bot.get_chat(m["telegram_id"])
                who = f"@{chat.username}" if chat.username else (chat.first_name or str(m["telegram_id"]))
            except Exception:
                who = str(m["telegram_id"])
            text += f"  • {who} — {n} خرید\n"
    else:
        text += "\n👥 هنوز زیرمجموعه‌ای نداری — لینک دعوت رو بفرست 👆"

    balance = me.get("balance_toman", 0)
    kb_rows = [[InlineKeyboardButton("🛍️ پلن‌های مخصوص زیرمجموعه", callback_data=f"{RES_PREF}plans")]]
    if balance > 0:
        kb_rows.append([InlineKeyboardButton(f"💵 درخواست تسویه ({format_reseller_toman(balance)})", callback_data="res_settle_request")])
    kb_rows.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="res_panel_open")])
    kb_rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    target = update_obj or update.effective_message
    try:
        if edit and hasattr(target, "edit_text"):
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _res_settle_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نماینده دکمهٔ درخواست تسویه زد → پیام به ادمین + تأیید به نماینده."""
    res_store: ResellerStore | None = context.bot_data.get("resellers")
    if res_store is None:
        return
    user = update.effective_user
    me = res_store.get(user.id)
    balance = me.get("balance_toman", 0) if me else 0
    if balance <= 0:
        await _safe_query_answer(update.callback_query, "موجودی برای تسویه نیست.", show_alert=True)
        return
    origin = f"t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    text = SETTLE_REQUEST_TMPL.format(
        origin=origin,
        first_name=user.first_name or "نماینده",
        tg_id=user.id,
        balance=format_reseller_toman(balance),
        earned=format_reseller_toman(me.get("earned_total", 0)),
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تسویه شد (واریز کردم)", callback_data=f"res_admin_settle_{user.id}"),
        InlineKeyboardButton("❌ رد", callback_data="res_settle_reject_info"),
    ]])
    try:
        admin_id = context.bot_data.get("store", {}).get("admin_id")
        await context.bot.send_message(admin_id, text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("settle request to admin failed")
    await _safe_query_answer(update.callback_query, "✅ درخواستت ارسال شد — بعد از واریز خبرت می‌کنیم.", show_alert=True)


async def reseller_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == f"{RES_PREF}plans":
        await query.edit_message_text(
            "🛍️ <b>پلن‌های مخصوص زیرمجموعه‌ها</b>\n"
            "فقط کاربرانی که با لینک دعوت تو میان این قیمت‌ها رو می‌بینن:",
            reply_markup=reseller_plans_keyboard(),
            parse_mode="HTML",
        )
    elif data.startswith("res_admin_settle_"):
        # ادمین روی «تسویه شد» زد
        if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):
            await query.answer("❌ فقط ادمین.", show_alert=True)
            return
        target = int(data.split("_")[-1])
        paid = context.bot_data["resellers"].settle(target)
        if paid <= 0:
            await query.answer("⚠️ موجودی نبود.", show_alert=True)
            return
        await query.edit_message_text(query.message.text_html.split("\n\nپس از واریز")[0] + "\n\n✅ <b>تسویه انجام شد.</b>", parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=target,
                text=f"💵 تسویه انجام شد — {format_reseller_toman(paid)} واریز گردید.",
            )
        except Exception:
            logger.exception("settle notify failed")
    elif data == "res_settle_reject_info":
        await query.answer("درخواست رد شد.", show_alert=True)


async def admin_settle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx_store: ResellerStore | None = context.bot_data.get("resellers")
    if ctx_store is None:
        return
    if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):
        await update.effective_message.reply_text("❌ فقط ادمین.")
        return
    if not context.args:
        await update.effective_message.reply_text("استفاده: /settle «آیدی‌عددی تلگرام»")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ آیدی عددی نامعتبر.")
        return
    paid = ctx_store.settle(target)
    if paid <= 0:
        await update.effective_message.reply_text("⚠️ موجودی برای تسویه نبود.")
        return
    await update.effective_message.reply_text(
        f"✅ تسویه شد: {format_reseller_toman(paid)} → کاربر <code>{target}</code>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=target,
            text=f"💵 تسویه انجام شد — {format_reseller_toman(paid)} واریز گردید.",
        )
    except Exception:
        logger.exception("settle notify failed")


async def admin_setpct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx_store: ResellerStore | None = context.bot_data.get("resellers")
    if ctx_store is None:
        return
    if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):
        await update.effective_message.reply_text("❌ فقط ادمین.")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("استفاده: /setpct «آیدی» «درصد»")
        return
    try:
        target, pct = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ ورودی نامعتبر.")
        return
    ok = ctx_store.set_pct(target, pct)
    await update.effective_message.reply_text(
        f"✅ درصد سود کاربر <code>{target}</code> → {pct}٪" if ok
        else "⚠️ کاربر پیدا نشد (باید حداقل یک‌بار /myresellers زده باشه).",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# پنل مالی ادمین — نمای کلی درآمد فروشگاه + ویرایش کیف پول نماینده‌ها
# ---------------------------------------------------------------------------
async def admin_finance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: OrderStore | None = context.bot_data.get("store", {}).get("store")
    res_store: ResellerStore | None = context.bot_data.get("resellers")
    if store is None:
        await update.effective_message.reply_text("❌ فروشگاه فعال نیست.")
        return
    orders = store.all_orders()
    paid = [o for o in orders if o.get("status") in ("approved", "provisioned")]
    gross = sum(int(o.get("amount_toman") or 0) for o in paid)
    failed = [o for o in orders if o.get("status") == "failed"]
    pending = [o for o in orders if o.get("status") == "submitted"]

    # فروش به تفکیک خریدار (ادمین همه را می‌بیند)
    per_buyer: dict[int, dict] = {}
    for o in paid:
        b = per_buyer.setdefault(int(o["telegram_id"]), {"n": 0, "sum": 0})
        b["n"] += 1
        b["sum"] += int(o.get("amount_toman") or 0)

    lines = [
        "📊 <b>پنل مالی فروشگاه</b>\n",
        f"🧾 سفارش‌های پرداخت‌شده: {len(paid)}",
        f"💰 درآمد کل: <b>{format_reseller_toman(gross)}</b>",
        f"⏳ منتظر تأیید: {len(pending)} | ❌ ناموفق: {len(failed)}",
    ]
    kb_rows: list[list[InlineKeyboardButton]] = []
    if per_buyer:
        lines.append("\n🛍️ <b>خریدارها (تعداد × مبلغ):</b>")
        top = sorted(per_buyer.items(), key=lambda kv: kv[1]["sum"], reverse=True)[:12]
        for uid, v in top:
            lines.append(f"  • <code>{uid}</code> — {v['n']} خرید — {format_reseller_toman(v['sum'])}")
        if len(per_buyer) > 12:
            lines.append(f"  … و {len(per_buyer) - 12} نفر دیگر")

    if res_store is not None:
        members = res_store.all()
        with_inviter = [m for m in members if m.get("invited_by")]
        balances = sum(m.get("balance_toman", 0) for m in members)
        totals = res_store.totals()
        lines += [
            "\n💼 <b>نماینده‌ها</b>",
            f"👥 عضو برنامه: {len(members)} | زیرمجموعه‌دار: {len(with_inviter)}",
            f"👛 موجودی پرداخت‌نشدهٔ همه: <b>{format_reseller_toman(balances)}</b>",
            f"📈 کل سود تولیدشده: {format_reseller_toman(totals.get('paid_out', 0))}",
        ]
        for m in sorted(members, key=lambda x: -x.get("balance_toman", 0))[:12]:
            label = f"{m['telegram_id']} — کیف {format_reseller_toman(m.get('balance_toman', 0))} — {res_store.commission_label(m['telegram_id'])}"
            kb_rows.append([InlineKeyboardButton(label, callback_data=f"{RES_PREF}manage_{m['telegram_id']}")])

    kb_rows.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin_finance_refresh")])
    kb = InlineKeyboardMarkup(kb_rows)
    await update.effective_message.reply_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


async def reseller_manage_view(update: Update, context: ContextTypes.DEFAULT_TYPE, target: int, edit_mode: str | None = None) -> None:
    """صفحهٔ مدیریت یک نماینده — همه‌چیز قابل مشاهده و تنظیم."""
    res_store: ResellerStore | None = context.bot_data.get("resellers")
    store: OrderStore | None = context.bot_data.get("store", {}).get("store")
    if res_store is None:
        return
    me = res_store.get(target)
    if me is None:
        me = res_store.ensure_member(target)

    # اسم تلگرام
    who = str(target)
    try:
        chat = await context.bot.get_chat(target)
        if chat.username:
            who = f"@{chat.username}"
        elif chat.first_name:
            who = chat.first_name
    except Exception:  # noqa: S110 - intentional fallback
        pass

    invited = res_store.invited_by_member(target)
    # خریدهای هر زیرمجموعه
    per_invitee: dict[int, dict] = {}
    store_orders = store.all_orders() if store else []
    for o in store_orders:
        if o.get("status") in ("approved", "provisioned") and int(o.get("telegram_id", 0)) in [m["telegram_id"] for m in invited]:
            b = per_invitee.setdefault(int(o["telegram_id"]), {"n": 0, "sum": 0})
            b["n"] += 1
            b["sum"] += int(o.get("amount_toman") or 0)
    total_sold = sum(v["n"] for v in per_invitee.values())
    total_sales = sum(v["sum"] for v in per_invitee.values())

    bal = me.get("balance_toman", 0)
    text = [
        f"👤 <b>مدیریت نماینده: {who}</b>",
        f"🆔 <code>{target}</code>\n",
        f"🤝 دعوت‌کننده: {me.get('invited_by') or '—'}",
        f"🧮 نوع سود: <b>{res_store.commission_label(target)}</b>\n",
        f"💰 کیف پول: <b>{format_reseller_toman(bal)}</b>",
        f"📈 درآمد کل: <b>{format_reseller_toman(me.get('earned_total', 0))}</b>",
        f"👥 زیرمجموعه‌ها: {len(invited)} نفر | فروش: {total_sold} سفارش ({format_reseller_toman(total_sales)})\n",
    ]

    if invited:
        text.append("👤 <b>زیرمجموعه‌ها:</b>")
        for m in invited[:12]:
            v = per_invitee.get(m["telegram_id"], {"n": 0, "sum": 0})
            try:
                c = await context.bot.get_chat(m["telegram_id"])
                nm = f"@{c.username}" if c.username else (c.first_name or str(m["telegram_id"]))
            except Exception:
                nm = str(m["telegram_id"])
            text.append(f"  • {nm} (<code>{m['telegram_id']}</code>) — {v['n']} خرید — {format_reseller_toman(v['sum'])}")

    payouts = res_store.payouts_for(target, 8)
    if payouts:
        text.append("\n🧾 <b>آخرین سودها:</b>")
        for p in payouts:
            c = int(p.get("commission_toman") or 0)
            text.append(f"  • {'+' if c > 0 else '−'} {format_reseller_toman(abs(c))} — سفارش {p.get('order_id') or 'تسویه'}")

    kb_rows: list[list[InlineKeyboardButton]] = []
    if edit_mode == "pct":
        text.append("\n✏️ <b>درصد جدید بفرست (مثلاً 25)</b>")
    elif edit_mode == "fixed":
        text.append("\n✏️ <b>مبلغ ثابت از هر خرید بفرست (تومان، مثلاً 15000)</b>")
    elif edit_mode == "bal":
        text.append("\n✏️ <b>مبلغ بفرست — مثبت اضافه، منفی کم (تسویه دستی)</b>")

    kb_rows.append([InlineKeyboardButton("🧮 تغییر درصد", callback_data=f"{RES_PREF}m_{target}_pct"),
                    InlineKeyboardButton("💵 مبلغ ثابت", callback_data=f"{RES_PREF}m_{target}_fixed")])
    kb_rows.append([InlineKeyboardButton("👛 ویرایش کیف پول", callback_data=f"{RES_PREF}m_{target}_bal"),
                    InlineKeyboardButton("✅ تسویه کامل", callback_data=f"{RES_PREF}m_{target}_settle")])
    kb_rows.append([InlineKeyboardButton("↩️ پنل مالی", callback_data="admin_finance_refresh")])
    kb = InlineKeyboardMarkup(kb_rows)

    q = update.callback_query
    if q is not None and q.message is not None:
        try:
            await q.edit_message_text("\n".join(text), reply_markup=kb, parse_mode="HTML")
            return
        except Exception:  # noqa: S110 - intentional fallback
            pass
    await update.effective_message.reply_text("\n".join(text), reply_markup=kb, parse_mode="HTML")


async def admin_finance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):
        await query.answer("❌ فقط ادمین.", show_alert=True)
        return
    data = query.data or ""
    if data == "admin_finance_refresh":
        context.application.create_task(admin_finance_refresh(update, context))
    elif data.startswith(f"{RES_PREF}manage_"):
        target = int(data.split("_")[2])
        await reseller_manage_view(update, context, target)
    elif data.startswith(f"{RES_PREF}m_"):
        # res_m_<id>_<mode>
        parts = data.split("_")
        target = int(parts[2])
        mode = parts[3] if len(parts) > 3 else "bal"
        if mode == "settle":
            paid = context.bot_data["resellers"].settle(target)
            if paid <= 0:
                await query.answer("⚠️ موجودی نبود.", show_alert=True)
            else:
                try:
                    await context.bot.send_message(chat_id=target, text=f"💵 تسویه انجام شد — {format_reseller_toman(paid)} واریز گردید.")
                except Exception:
                    logger.exception("settle notify failed")
            await reseller_manage_view(update, context, target)
            return
        context.user_data["fin_edit_target"] = target
        context.user_data["fin_edit_mode"] = mode
        await reseller_manage_view(update, context, target, edit_mode=mode)
    elif data.startswith(f"{RES_PREF}edit_"):
        target = int(data.split("_")[-1])
        context.user_data["fin_edit_target"] = target
        context.user_data["fin_edit_mode"] = "bal"
        m = context.bot_data["resellers"].get(target)
        bal = m.get("balance_toman", 0) if m else 0
        await query.edit_message_text(
            f"✏️ ویرایش کیف پول <code>{target}</code>\n"
            f"موجودی فعلی: <b>{format_reseller_toman(bal)}</b>\n\n"
            "مبلغ به تومان بفرست (مثلاً <code>50000</code>) —\n"
            "منفی = کم کردن از کیف پول (مثل تسویه دستی):\n"
            "<code>-50000</code>",
            parse_mode="HTML",
        )


async def admin_finance_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # بازسازی همان نمای پنل مالی — ساده: دوباره admin_finance با پیام جدید
    class _FakeMsg:
        def __init__(self, chat): self._chat = chat
        async def reply_text(self, *a, **k):
            return await context.bot.send_message(self._chat, *a, **k)
    update_eff = update
    update_eff._effective_message = _FakeMsg(update.effective_user.id)
    await admin_finance(update_eff, context)


async def admin_finance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ورودی ادمین در حالت ویرایش (کیف پول / درصد / مبلغ ثابت)."""
    target = context.user_data.get("fin_edit_target")
    if not target:
        return
    if not context.bot_data.get("store", {}).get("is_admin", lambda _uid: False)(update.effective_user.id):
        return
    raw = (update.message.text or "").strip().replace(",", "")
    try:
        amount = int(raw)
    except ValueError:
        await update.message.reply_text("❌ عدد نامعتبر — فقط عدد بفرست.")
        return
    res_store: ResellerStore | None = context.bot_data.get("resellers")
    if res_store is None:
        return
    mode = context.user_data.get("fin_edit_mode", "bal")
    context.user_data.pop("fin_edit_target", None)
    context.user_data.pop("fin_edit_mode", None)

    if mode == "pct":
        if not (0 <= amount <= 100):
            await update.message.reply_text("❌ درصد باید بین ۰ تا ۱۰۰ باشد.")
            return
        res_store.set_pct(target, amount)
        await update.message.reply_text(
            f"✅ سود <code>{target}</code> → <b>{amount}٪ از هر خرید</b> (مبلغ ثابت حذف شد)",
            parse_mode="HTML",
        )
        try:
            await context.bot.send_message(chat_id=target, text=f"🧮 درصد سود تو {amount}٪ شد.")
        except Exception:  # noqa: S110 - intentional fallback
            pass
        return
    if mode == "fixed":
        if amount < 0:
            await update.message.reply_text("❌ مبلغ ثابت نمی‌تواند منفی باشد.")
            return
        res_store.set_fixed(target, amount)
        await update.message.reply_text(
            f"✅ سود <code>{target}</code> → <b>{format_reseller_toman(amount)} ثابت از هر خرید</b>",
            parse_mode="HTML",
        )
        try:
            await context.bot.send_message(chat_id=target, text=f"💵 سود تو {format_reseller_toman(amount)} ثابت از هر خرید شد.")
        except Exception:  # noqa: S110 - intentional fallback
            pass
        return

    new_bal = res_store.adjust_balance(target, amount)
    if new_bal is None:
        await update.message.reply_text("⚠️ کاربر پیدا نشد.")
        return
    verb = "اضافه شد" if amount > 0 else "کم شد"
    await update.message.reply_text(
        f"✅ {format_reseller_toman(abs(amount))} {verb} → کیف <code>{target}</code>\n"
        f"موجودی جدید: <b>{format_reseller_toman(new_bal)}</b>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=target,
            text=f"💼 کیف پولت بروزرسانی شد — موجودی فعلی: {format_reseller_toman(new_bal)}",
        )
    except Exception:
        logger.exception("finance notify failed")
