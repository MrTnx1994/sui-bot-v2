"""Final small patch: fan-out shop order to all admins (exact local text)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sui_bot"
TARGET = ROOT / "store_bot.py"

OLD = '''    try:
        await context.bot.send_message(
            chat_id=ctx["admin_id"],
            text=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("failed to send shop order to admin")
        store.update_order(order_id, status=OrderStatus.FAILED)
        await update.message.reply_text(
            "❌ ارسال سفارش به پشتیبانی ناموفق بود. لطفاً دوباره تلاش کن."
        )
        return'''

NEW = '''    sent_to = 0
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
        return'''


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if OLD not in text:
        raise SystemExit("pattern not found")
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()
