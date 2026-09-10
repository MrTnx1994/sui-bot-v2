#!/usr/bin/env bash
# ============================================================================
#  SUI-BOT — حذف کامل (آن‌اینستال)
#
#  اجرا:  sui-bot-uninstall          (یا: bash uninstall.sh)
#
#  چیزهایی که پاک می‌شوند:
#    - سرویس‌های sui-bot و sui-subpage (stop + disable)
#    - کد نصب‌شده: /opt/sui-bot-v2 و /opt/sui-bot-v2-src
#    - اسکریپت‌های مدیریتی /usr/local/bin/sui-bot-*
#
#  چیزهایی که پیش‌فرض **نگه** داشته می‌شوند (با سؤال):
#    - داده‌ها: /var/lib/sui-bot  → بکاپ خودکار در /root/sui-bot-data-<date>.tar.gz
#    - تنظیمات و توکن‌ها: /etc/sui-bot/sui-bot.env
#    - کانفیگ nginx (2096)  → با KEEP_NGINX=0 پاک می‌شود
# ============================================================================
set -euo pipefail

say() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo -e "\033[1;31m!! $*\033[0m"; exit 1; }
ok()  { echo -e "   \033[1;32m✔\033[0m $*"; }

[[ $EUID -eq 0 ]] || die "با root اجرا کن (sudo -i)"

APP_DIR=/opt/sui-bot-v2
SRC_DIR=/opt/sui-bot-v2-src
DATA_DIR=/var/lib/sui-bot
ENV_DIR=/etc/sui-bot

echo "────────────────────────────────────────────"
echo "  حذف SUI-BOT — داده‌ها بکاپ می‌گیرند"
echo "────────────────────────────────────────────"
read -r -p "مطمئنی؟ (برای تأیید بنویس: حذف) " confirm
[[ $confirm == "حذف" ]] || die "لغو شد — هیچ چیزی پاک نشد."

# ------------------------------------------------------------ 1. سرویس‌ها
say "متوقف و غیرفعال کردن سرویس‌ها"
systemctl stop sui-bot sui-subpage 2>/dev/null || true
systemctl disable sui-bot sui-subpage 2>/dev/null || true
rm -f /etc/systemd/system/sui-bot.service /etc/systemd/system/sui-subpage.service
systemctl daemon-reload
ok "سرویس‌ها حذف شدند"

# ------------------------------------------------------------ 2. بکاپ داده‌ها
say "بکاپ داده‌ها (کیف پول / سفارش‌ها / تخفیف‌ها / تنظیمات)"
BACKUP="/root/sui-bot-data-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
if [[ -d $DATA_DIR && -n $(ls -A "$DATA_DIR" 2>/dev/null) ]]; then
  tar -czf "$BACKUP" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"
  chmod 600 "$BACKUP"
  ok "بکاپ: $BACKUP"
else
  echo "   داده‌ای نبود — بکاپی ساخته نشد"
fi
if [[ -d $ENV_DIR ]]; then
  tar -czf "${BACKUP%.tar.gz}-etc.tar.gz" -C "$(dirname "$ENV_DIR")" "$(basename "$ENV_DIR")"
  chmod 600 "${BACKUP%.tar.gz}-etc.tar.gz"
  ok "بکاپ تنظیمات/توکن‌ها: ${BACKUP%.tar.gz}-etc.tar.gz"
fi

# ------------------------------------------------------------ 3. کد و venv
say "حذف کد نصب‌شده"
rm -rf "$APP_DIR" "$SRC_DIR" "$SRC_DIR.old"
rm -f /usr/local/bin/sui-bot-update /usr/local/bin/sui-bot-uninstall /usr/local/bin/sui-bot
ok "/opt/sui-bot-v2* و دستورهای مدیریتی پاک شدند"

# ------------------------------------------------------------ 4. nginx (اختیاری)
if [[ ${KEEP_NGINX:-1} == 1 ]]; then
  echo "   nginx دست‌نخورده ماند (KEEP_NGINX=0 برای حذف)"
else
  say "حذف کانفیگ nginx 2096"
  rm -f /etc/nginx/sites-enabled/sub-ui /etc/nginx/sites-available/sub-ui
  if nginx -t; then
    systemctl reload nginx
  else
    echo "   ⚠ nginx config test failed — not reloading"
  fi
  ok "کانفیگ sub-ui از nginx حذف شد"
fi

# ------------------------------------------------------------ 5. داده و env (سؤال)
read -r -p "❓ پوشهٔ دادهٔ /var/lib/sui-bot هم پاک شود؟ (y/N) " ans
if [[ $ans == y || $ans == Y ]]; then
  rm -rf "$DATA_DIR"
  ok "داده‌ها پاک شدند (بکاپ در /root هست)"
else
  echo "   داده‌ها ماندند: $DATA_DIR"
fi
read -r -p "❓ فایل تنظیمات و توکن‌ها (/etc/sui-bot) هم پاک شود؟ (y/N) " ans
if [[ $ans == y || $ans == Y ]]; then
  rm -rf "$ENV_DIR"
  ok "تنظیمات پاک شدند (بکاپ در /root هست)"
else
  echo "   تنظیمات ماندند: $ENV_DIR"
fi
read -r -p "❓ کاربر سیستمی sui-bot هم حذف شود؟ (y/N) " ans
if [[ $ans == y || $ans == Y ]]; then
  userdel sui-bot 2>/dev/null || true
  ok "کاربر حذف شد"
fi

echo
echo -e "\033[1;32m★★★ آن‌اینستال کامل شد ★★★\033[0m"
echo "   برای نصب دوباره: REPO_URL=<ریپو> bash <(curl -fsSL .../install.sh)"
echo "   برای بازگردانی داده‌ها: tar -xzf ${BACKUP} -C /var/lib/"
