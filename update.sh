#!/usr/bin/env bash
# ============================================================================
#  SUI-BOT — آپدیت از گیت‌هاب
#
#  اجرا:  sui-bot-update          (یا: bash update.sh)
#
#  کارها:
#    1) clone تازهٔ ریپو (نسخهٔ جدید) کنار نسخهٔ فعلی
#    2) pip uninstall قدیمی → نصب پکیج جدید (فایل‌های حذف‌شده واقعاً پاک می‌شوند)
#    3) یونیت‌های systemd جدید → daemon-reload → ری‌استارت سرویس‌ها
#    4) راستی‌آزمایی
#
#  داده‌ها (کیف پول، سفارش‌ها، تخفیف‌ها، تنظیمات) در /var/lib/sui-bot
#  و /etc/sui-bot دست‌نخورده می‌مانند.
# ============================================================================
set -euo pipefail

say() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo -e "\033[1;31m!! $*\033[0m"; exit 1; }

_upsert_env() {  # _upsert_env KEY VALUE — در /etc/sui-bot/sui-bot.env
  grep -q "^$1=" /etc/sui-bot/sui-bot.env \
    && sed -i "s#^$1=.*#$1=\"$2\"#" /etc/sui-bot/sui-bot.env \
    || echo "$1=\"$2\"" >> /etc/sui-bot/sui-bot.env
}

[[ $EUID -eq 0 ]] || die "با root اجرا کن (sudo -i)"

APP_DIR=/opt/sui-bot-v2
SRC_DIR=/opt/sui-bot-v2-src
VENV_PY="$APP_DIR/.venv/bin/python"
DATA_DIR=/var/lib/sui-bot

# --- پیدا کردن آدرس ریپو (از clone قبلی یا env) ---
REPO_URL="${REPO_URL:-}"
if [[ -z $REPO_URL && -d $SRC_DIR/.git ]]; then
  REPO_URL=$(git -C "$SRC_DIR" config --get remote.origin.url || true)
fi
if [[ -z $REPO_URL ]]; then
  REPO_URL=$(grep -E '^(REPO_URL|SUI_BOT_REPO)=' /etc/sui-bot/sui-bot.env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)
fi
[[ -n $REPO_URL ]] || die "آدرس ریپو پیدا نشد — این‌طوری اجرا کن: REPO_URL=https://github.com/user/repo.git sui-bot-update"
BRANCH="${BRANCH:-$(git ls-remote --symref "$REPO_URL" HEAD 2>/dev/null | awk '/^ref:/{sub("refs/heads/","",$2); print $2}')}"
BRANCH="${BRANCH:-master}"
echo "   ریپو: ${REPO_URL}  (برنچ: ${BRANCH})"

# --- نسخهٔ فعلی برای گزارش ---
OLD_VER=$("$VENV_PY" -c 'import sui_bot; print(getattr(sui_bot,"__version__","?"))' 2>/dev/null || echo "?")

# ------------------------------------------------------------ 1. clone تازه
say "دریافت نسخهٔ جدید"
rm -rf "${SRC_DIR}.new"
git clone --depth 1 -b "$BRANCH" "$REPO_URL" "${SRC_DIR}.new" 2>/dev/null \
  || die "clone ناموفق — آدرس ریپو/برنچ را چک کن"

# چک سالم بودن سورس قبل از تعویض
python3 -c "import sys; sys.path.insert(0,'${SRC_DIR}.new/src'); import sui_bot" \
  || die "سورس جدید import نمی‌شود — آپدیت لغو شد (نسخهٔ فعلی دست‌نخورده ماند)"

# ------------------------------------------------------------ 2. تعویض سورس + نصب تمیز
say "تعویض سورس و نصب پکیج جدید (فایل‌های حذف‌شده پاک می‌شوند)"
rm -rf "$SRC_DIR.old"
if [[ -d $SRC_DIR ]]; then
  mv "$SRC_DIR" "${SRC_DIR}.old"
fi
mv "${SRC_DIR}.new" "$SRC_DIR"

# اگر venv وجود نداشت یا خراب بود، از نو بساز
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  say "venv وجود ندارد — از نو ساخته می‌شود"
  rm -rf "$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
if ! "$VENV_PY" -c 'import sui_bot' 2>/dev/null; then
  "$APP_DIR/.venv/bin/pip" uninstall -y -q sui-bot 2>/dev/null || true
  "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -q "$SRC_DIR"
else
  "$APP_DIR/.venv/bin/pip" uninstall -y -q sui-bot 2>/dev/null || true
  "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -q "$SRC_DIR"
fi

# ------------------------------------------------------------ 3. systemd
say "به‌روزرسانی سرویس‌ها"
cp "$SRC_DIR/units/sui-bot.service"    /etc/systemd/system/
cp "$SRC_DIR/units/sui-subpage.service" /etc/systemd/system/
# اسکریپت‌های مدیریتی هم تازه شوند
if [[ -f $SRC_DIR/update.sh ]]; then
  install -m 755 "$SRC_DIR/update.sh" /usr/local/bin/sui-bot-update
fi
if [[ -f $SRC_DIR/uninstall.sh ]]; then
  install -m 755 "$SRC_DIR/uninstall.sh" /usr/local/bin/sui-bot-uninstall
fi
systemctl daemon-reload

# ------------------------------------------------------------ 3.1 nginx هم‌گام با نسخهٔ جدید
# اگر قالب nginx تغییر کرده باشد، کانفیگ فعال دوباره از روی آن ساخته می‌شود
# (دامنه/گواهی/پورت از کانفیگ فعلی استخراج می‌شود — پنل دست نمی‌خورد)
say "هم‌گام‌سازی nginx با نسخهٔ جدید"
CUR_DOMAIN=$(grep -oPm1 'server_name\s+\K[^;]+' /etc/nginx/sites-available/sub-ui 2>/dev/null | head -1 | xargs || true)
CUR_CERT=$(grep -oPm1 'ssl_certificate\s+\K[^;]+' /etc/nginx/sites-available/sub-ui 2>/dev/null | head -1 | xargs || true)
CUR_KEY=$(grep -oPm1 'ssl_certificate_key\s+\K[^;]+' /etc/nginx/sites-available/sub-ui 2>/dev/null | head -1 | xargs || true)
CUR_PORT=$(grep -oPm1 'listen\s+\K[0-9]+' /etc/nginx/sites-available/sub-ui 2>/dev/null | head -1 | xargs || true)
CUR_PORT=${CUR_PORT:-88}
# مهاجرت: تلگرام مینی‌اپ را فقط روی 443/80/88 باز می‌کند (8443 روی این سرور
# در اشغال پنل s-ui است). هر پورت دیگر → اولین پورت مجازِ آزاد: 88
if [[ ! $CUR_PORT =~ ^(443|80|88)$ ]]; then
  for cand in 88 443 80; do
    if ! ss -ltn 2>/dev/null | grep -q ":${cand} "; then
      CUR_PORT=$cand; break
    fi
  done
fi
if [[ -z $CUR_DOMAIN ]]; then
  CUR_DOMAIN=$(grep -E '^SUI_HOST=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | sed 's#https://##; s#/app.*##')
fi
if [[ -n $CUR_DOMAIN && -f $CUR_CERT && -f $CUR_KEY && -f $SRC_DIR/nginx/sub-ui.conf.tmpl ]]; then
  sed -e "s/__DOMAIN__/${CUR_DOMAIN}/g" \
      -e "s/__UI_PORT__/${CUR_PORT}/g" \
      -e "s#__CERT__#${CUR_CERT}#g" \
      -e "s#__CERTKEY__#${CUR_KEY}#g" \
      "$SRC_DIR/nginx/sub-ui.conf.tmpl" > /etc/nginx/sites-available/sub-ui
  nginx -t && systemctl reload nginx
  _upsert_env "MENU_WEBAPP_URL"      "https://${CUR_DOMAIN}:${CUR_PORT}/sub/menu"
  _upsert_env "SUB_BASE_URL_OVERRIDE" "https://${CUR_DOMAIN}:${CUR_PORT}/sub"
  echo "   ✔ nginx + MENU هم‌گام شد → https://${CUR_DOMAIN}:${CUR_PORT}/sub/menu"
else
  echo "   (کانفیگ nginx فعلی کامل نیست — بدون تغییر ماند؛ install.sh را دوباره اجرا کن)"
fi

# ------------------------------------------------------------ 3.3 BOT_USERNAME برای کارت‌های منو
say "ساخت BOT_USERNAME (لینک deep-link کارت‌های منو)"
BOT_TOKEN_VAL=$(grep -E '^BOT_TOKEN=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs || true)
if [[ -n $BOT_TOKEN_VAL && $BOT_TOKEN_VAL != "replace-me" ]]; then
  BOT_UNAME=$(curl -fsS --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN_VAL}/getMe" 2>/dev/null \
              | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("username",""))' 2>/dev/null || true)
  if [[ -n $BOT_UNAME ]]; then
    _upsert_env "BOT_USERNAME" "$BOT_UNAME"
    echo "   ✔ BOT_USERNAME → ${BOT_UNAME}"
  else
    echo "   ⚠ getMe جواب نداد — کارت‌های منو روی sendData می‌افتند (کار می‌کنند ولی با تاخیر)"
  fi
fi

# ------------------------------------------------------------ 3.2 یکدست‌سازی زبان
# همهٔ کاربران → فارسی (زبان انتخابی قبلی پاک می‌شود؛ کاربر می‌تواند از 🌐 عوض کند)
say "یکدست‌سازی زبان ربات (فارسی پیش‌فرض)"
LANG_FILE="${DATA_DIR}/user_languages.json"
if [[ -f $LANG_FILE ]]; then
  cp "$LANG_FILE" "${LANG_FILE}.bak-$(date +%s)"
  echo '{}' > "$LANG_FILE"
  chown sui-bot:sui-bot "$LANG_FILE" 2>/dev/null || true
  echo "   ✔ همهٔ کاربران به فارسی برگشتند"
else
  echo "   (فایل زبان نبود — پیش‌فرض فارسی اعمال می‌شود)"
fi

# همه‌چیز آماده شد → حالا ری‌استارت
systemctl enable --now sui-bot sui-subpage >/dev/null 2>&1 || true
systemctl restart sui-bot sui-subpage

# ------------------------------------------------------------ 4. verify
say "راستی‌آزمایی"
sleep 6
fail=0
chk() { if eval "$2"; then echo "   ✔ $1"; else echo "   ✗ $1"; fail=1; fi; }
chk "sui-bot فعال"     "[[ $(systemctl is-active sui-bot) == active ]]"
chk "sui-subpage فعال" "[[ $(systemctl is-active sui-subpage) == active ]]"
MENU_URL_VAL=$(grep -E '^MENU_WEBAPP_URL=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' || true)
if [[ -n $MENU_URL_VAL ]]; then
  chk "صفحهٔ منوی وب" "curl -fsk --max-time 8 -A 'Mozilla/5.0' '$MENU_URL_VAL' | grep -q telegram-web-app.js"
fi

NEW_VER=$("$VENV_PY" -c 'import sui_bot; print(getattr(sui_bot,"__version__","?"))' 2>/dev/null || echo "?")
echo
echo "   نسخه: ${OLD_VER}  →  ${NEW_VER}"
if [[ $fail -eq 0 ]]; then
  echo -e "\033[1;32m★★★ آپدیت موفق — داده‌ها دست‌نخورده ★★★\033[0m"
  echo "   نسخهٔ قبلی در ${SRC_DIR}.old نگه داشته شد (بعد از اطمینان پاکش کن)"
else
  echo -e "\033[1;33mآپدیت اعمال شد ولی بعضی چک‌ها پاس نشد: journalctl -u sui-bot -n 30\033[0m"
  echo "   برگشت به نسخهٔ قبلی:  rm -rf $SRC_DIR && mv ${SRC_DIR}.old $SRC_DIR && $APP_DIR/.venv/bin/pip install -q $SRC_DIR && systemctl restart sui-bot sui-subpage"
fi
