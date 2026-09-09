#!/usr/bin/env bash
# ============================================================================
#  SUI-BOT STACK — نصب یک‌خطی از گیت‌هاب
#
#  نصب کامل: sui-bot (بات تلگرام) + sui-subpage (UI ساب + منو) + nginx (2096)
#  با تشخیص خودکار s-ui روی همین سرور (پورت، دامنه، گواهی، توکن API)
#
#  اجرا:
#    bash <(curl -fsSL https://raw.githubusercontent.com/<USER>/<REPO>/main/install.sh)
#
#  گزینه‌ها (env):
#    REPO_RAW      آدرس raw ریپو (پیش‌فرض: همین اسکریپت خودش را تشخیص می‌دهد)
#    SUI_BOT_ENV   مسیر فایل env آماده (اگر نباشد تعاملی می‌پرسد)
#    SKIP_NGINX=1  اگر nginx/2096 را جداگانه می‌چینی
# ============================================================================
set -euo pipefail

LOG=/var/log/sui-stack-install.log
exec > >(tee -a "$LOG") 2>&1

say() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo -e "\033[1;31m!! $*\033[0m"; exit 1; }

[[ $EUID -eq 0 ]] || die "با root اجرا کن (sudo -i)"

# ------------------------------------------------------------ 0. منبع کد
REPO_URL="${REPO_URL:-}"
if [[ -z $REPO_URL ]]; then
  # از خود اسکریپت آدرس ریپو را استخراج کن (پشتیبانی از curl-pipe)
  SCRIPT_SRC="${BASH_SOURCE[0]}"
  if [[ -f $SCRIPT_SRC ]] && grep -q "DEFAULT_REPO_URL=" "$SCRIPT_SRC" 2>/dev/null; then
    REPO_URL="$(grep -m1 'DEFAULT_REPO_URL=' "$SCRIPT_SRC" | cut -d'"' -f2)"
  fi
fi
REPO_URL="${REPO_URL:-__DEFAULT_REPO_URL__}"
[[ $REPO_URL == *"__DEFAULT_REPO_URL__"* ]] && die "REPO_URL را ست کن: REPO_URL=https://github.com/user/repo bash install.sh"

say "دریافت سورس از ${REPO_URL} (branch: ${BRANCH:-main})"
rm -rf /opt/sui-bot-v2-src
mkdir -p /opt/sui-bot-v2-src
if command -v git >/dev/null; then
  git clone --depth 1 -b "${BRANCH:-main}" "$REPO_URL" /opt/sui-bot-v2-src 2>/dev/null \
    || die "clone ناموفق — REPO_URL و دسترسی را چک کن"
else
  apt-get update -qq && apt-get install -y -qq git ca-certificates
  git clone --depth 1 -b "${BRANCH:-main}" "$REPO_URL" /opt/sui-bot-v2-src \
    || die "clone ناموفق"
fi
HERE=/opt/sui-bot-v2-src
cd "$HERE"

# ------------------------------------------------------------ 1. s-ui
say "پیدا کردن s-ui روی این سرور"
SUI_DB=""
for cand in /usr/local/s-ui/db/s-ui.db /opt/s-ui/db/s-ui.db /usr/local/s-ui/s-ui.db; do
  [[ -f $cand ]] && SUI_DB=$cand && break
done
[[ -n $SUI_DB ]] || die "دیتابیس s-ui پیدا نشد — اول s-ui را نصب/بالا بیاور"
echo "   دیتابیس پنل: $SUI_DB"

read -r WEB_PORT WEB_DOMAIN CERT CERTKEY SUB_PORT < <(python3 - "$SUI_DB" << 'PY'
import sqlite3, sys
n = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
g = lambda k: (n.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or ('',))[0]
print(g('webPort') or 2095, g('webDomain') or '', g('webCertFile') or '', g('webKeyFile') or '', g('subPort') or 2097)
PY
)
[[ -n $WEB_DOMAIN ]] || die "webDomain در پنل خالی است — اول در تنظیمات پنل دامنه را ست کن"
echo "   پنل: https://${WEB_DOMAIN}:${WEB_PORT} | ساب: ${SUB_PORT} | گواهی: ${CERT}"

curl -sk -o /dev/null --max-time 8 --resolve "${WEB_DOMAIN}:${WEB_PORT}:127.0.0.1" \
  "https://${WEB_DOMAIN}:${WEB_PORT}/" || die "پنل s-ui روی ${WEB_PORT} جواب نمی‌دهد"
echo "   ✔ پنل زنده است"

# ------------------------------------------------------------ 2. env
say "تنظیمات بات (sui-bot.env)"
mkdir -p /etc/sui-bot
if [[ -n ${SUI_BOT_ENV:-} && -f $SUI_BOT_ENV ]]; then
  cp "$SUI_BOT_ENV" /etc/sui-bot/sui-bot.env
elif [[ -f /etc/sui-bot/sui-bot.env ]]; then
  echo "   env موجود پیدا شد (بکاپ گرفته شد) — فقط مقادیر خالی را ازت می‌پرسم"
  cp /etc/sui-bot/sui-bot.env "/etc/sui-bot/sui-bot.env.bak-$(date +%s)"
elif [[ -f $HERE/sui-bot.env.sample ]]; then
  cp "$HERE/sui-bot.env.sample" /etc/sui-bot/sui-bot.env
  echo "   نمونهٔ تنظیمات ساخته شد → /etc/sui-bot/sui-bot.env"
else
  touch /etc/sui-bot/sui-bot.env
fi

# set_env_value KEY "سؤال" [پیش‌فرض]
#  → اگر مقدار موجود معتبر باشد دست نمی‌زند؛ وگرنه از کاربر می‌پرسد و درج می‌کند
set_env_value() {
  local key=$1 question=$2 default=${3:-} current="" value="" hint=""
  current=$(grep -E "^${key}=" /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs || true)
  if [[ -n $current && $current != "replace-me" && $current != "0000-0000-0000-0000" ]]; then
    echo "   ${key} ← موجود است (دست نخورد)"
    return
  fi
  [[ -n $default ]] && hint=" [${default}]"
  read -r -p "❓ ${question}${hint}: " value || die "ورودی خوانده نشد (نیاز به ترمینال تعاملی)"
  [[ -z $value && -n $default ]] && value="$default"
  sed -i "/^${key}=/d" /etc/sui-bot/sui-bot.env
  echo "${key}=\"${value}\"" >> /etc/sui-bot/sui-bot.env
}

echo "────────────────────────────────────────────────────────"
echo "  چند مقدار لازم داریم  (Enter خالی = رد شدنِ موارد اختیاری)"
echo "────────────────────────────────────────────────────────"
set_env_value "BOT_TOKEN"            "توکن ربات تلگرام (از @BotFather)"
set_env_value "SUI_TOKEN"            "توکن API پنل s-ui (پنل → تنظیمات → API Token)"
set_env_value "ADMIN_TELEGRAM_ID"    "آیدی عددی ادمین اصلی (از @userinfobot)"
PRIMARY_ID=$(grep -E '^ADMIN_TELEGRAM_ID=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs)
set_env_value "ADMIN_IDS"            "آیدی ادمین‌های دیگر با کاما" "${PRIMARY_ID}"
set_env_value "BOT_DISPLAY_NAME"     "اسم نمایشی ربات" "Vpnfiy"
set_env_value "PAYMENT_CARD_NUMBER"  "شماره کارت برای کارت‌به‌کارت"
set_env_value "PAYMENT_CARD_HOLDER"  "به نامِ صاحب کارت"
set_env_value "ZARINPAL_MERCHANT_ID" "مرچنت‌کد زرین‌پال (اختیاری — Enter = فقط کارت‌به‌کارت)"
chmod 600 /etc/sui-bot/sui-bot.env

BOT_TOKEN_VAL=$(grep -E '^BOT_TOKEN=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"')
[[ -n $BOT_TOKEN_VAL && $BOT_TOKEN_VAL != "replace-me" ]] \
  || die "BOT_TOKEN خالی ماند — بدون توکن ربات بالا نمی‌آید؛ دوباره اجرا کن."

# SUI_HOST و SUB_BASE و منو را با مقادیر همین سرور بازنویسی کن
SUI_TOKEN_VAL=$(grep -E '^SUI_TOKEN=' /etc/sui-bot/sui-bot.env | cut -d= -f2- | tr -d '"')
sed -i "s#^SUI_HOST=.*#SUI_HOST=\"https://${WEB_DOMAIN}:${WEB_PORT}/app\"#" /etc/sui-bot/sui-bot.env
grep -q '^SUB_BASE_URL_OVERRIDE=' /etc/sui-bot/sui-bot.env \
  && sed -i "s#^SUB_BASE_URL_OVERRIDE=.*#SUB_BASE_URL_OVERRIDE=\"https://${WEB_DOMAIN}:2096/sub\"#" /etc/sui-bot/sui-bot.env \
  || echo "SUB_BASE_URL_OVERRIDE=\"https://${WEB_DOMAIN}:2096/sub\"" >> /etc/sui-bot/sui-bot.env
# دکمهٔ منو (مینی‌اپ) — روی همین دامنهٔ 2096
grep -q '^MENU_WEBAPP_URL=' /etc/sui-bot/sui-bot.env \
  && sed -i "s#^MENU_WEBAPP_URL=.*#MENU_WEBAPP_URL=\"https://${WEB_DOMAIN}:2096/sub/menu\"#" /etc/sui-bot/sui-bot.env \
  || echo "MENU_WEBAPP_URL=\"https://${WEB_DOMAIN}:2096/sub/menu\"" >> /etc/sui-bot/sui-bot.env
echo "   SUI_HOST → https://${WEB_DOMAIN}:${WEB_PORT}/app"
echo "   MENU     → https://${WEB_DOMAIN}:2096/sub/menu"

# ------------------------------------------------------------ 3. token
say "ساخت/سازگاری توکن API پنل"
python3 - "$SUI_DB" "$SUI_TOKEN_VAL" << 'PY'
import sqlite3, sys, time
db, tok = sys.argv[1], sys.argv[2]
n = sqlite3.connect(db, timeout=15)
row = n.execute("SELECT id, expiry FROM tokens WHERE token=?", (tok,)).fetchone()
if row:
    if row[1] and row[1] < time.time():
        n.execute("UPDATE tokens SET expiry=? WHERE id=?", (int(time.time()) + 315360000, row[0]))
        n.commit(); print("   توکن موجود — انقضا تمدید شد (۱۰ سال)")
    else:
        print("   توکن موجود در پنل هست — مشکلی نیست")
else:
    if tok in ("", "replace-me"):
        print("   ⚠ توکن خالی/نمونه است — بعد از پر کردن env دوباره اجرا کن")
    else:
        n.execute("INSERT INTO tokens(desc, token, expiry, user_id) VALUES (?,?,?,1)",
                  ("telegram-bot", tok, int(time.time()) + 315360000))
        n.commit(); print("   توکن داخل پنل ساخته شد")
PY
# ------------------------------------------------------------ 4. cert
say "گواهی SSL"
if [[ -f $CERT && -f $CERTKEY ]]; then
  echo "   گواهی موجود: $CERT"
else
  say "گواهی نیست — صدور با acme.sh (standalone روی 80)"
  apt-get install -y -qq socat >/dev/null 2>&1 || true
  curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL:-admin@${WEB_DOMAIN}}" >/dev/null 2>&1 \
    || die "acme.sh نصب نشد — یا ACME_EMAIL=you@mail.com ست کن یا گواهی را دستی بیاور"
  ~/.acme.sh/acme.sh --issue -d "$WEB_DOMAIN" --standalone \
    && ~/.acme.sh/acme.sh --install-cert -d "$WEB_DOMAIN" \
        --fullchain-file "/root/cert/${WEB_DOMAIN}/fullchain.pem" \
        --key-file       "/root/cert/${WEB_DOMAIN}/privkey.pem" \
    || die "صدور گواهی ناموفق — DNS دامنه را به این سرور نشان بده"
  CERT="/root/cert/${WEB_DOMAIN}/fullchain.pem"
  CERTKEY="/root/cert/${WEB_DOMAIN}/privkey.pem"
  # مسیر گواهی را در پنل هم ثبت کن
  python3 - "$SUI_DB" "$CERT" "$CERTKEY" << 'PY'
import sqlite3, sys
db, cert, key = sys.argv[1], sys.argv[2], sys.argv[3]
n = sqlite3.connect(db, timeout=15)
for k, v in (("webCertFile", cert), ("webKeyFile", key)):
    if n.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone():
        n.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
    else:
        n.execute("INSERT INTO settings(key,value) VALUES (?,?)", (k, v))
n.commit()
PY
  echo "   ✔ گواهی صادر و در پنل ثبت شد"
fi

# ------------------------------------------------------------ 5. user
say "کاربر سرویس sui-bot"
getent passwd sui-bot >/dev/null || useradd --system --home-dir /var/lib/sui-bot --create-home --shell /usr/sbin/nologin sui-bot
mkdir -p /var/lib/sui-bot
chown sui-bot:sui-bot /var/lib/sui-bot

# ------------------------------------------------------------ 6. app
say "نصب بات (venv + پکیج از سورس clone شده)"
command -v python3 >/dev/null || { apt-get update -qq; apt-get install -y -qq python3 python3-venv; }
rm -rf /opt/sui-bot-v2
python3 -m venv /opt/sui-bot-v2/.venv
/opt/sui-bot-v2/.venv/bin/pip install -q --upgrade pip
/opt/sui-bot-v2/.venv/bin/pip install -q "$HERE"
echo "   نسخه: $(/opt/sui-bot-v2/.venv/bin/python -c 'import sui_bot; print(getattr(sui_bot,"__version__","ok"))' 2>/dev/null || echo ok)"

# ------------------------------------------------------------ 7. systemd + ابزارهای مدیریتی
say "سرویس‌های systemd + دستورهای مدیریتی"
cp "$HERE/units/sui-bot.service" /etc/systemd/system/
cp "$HERE/units/sui-subpage.service" /etc/systemd/system/
# sui-bot-update / sui-bot-uninstall — در دسترس همه‌جا
install -m 755 "$HERE/update.sh"    /usr/local/bin/sui-bot-update
install -m 755 "$HERE/uninstall.sh" /usr/local/bin/sui-bot-uninstall
# آدرس ریپو را ثبت کن تا sui-bot-update بدون آرگومان کار کند
grep -q '^SUI_BOT_REPO=' /etc/sui-bot/sui-bot.env \
  && sed -i "s#^SUI_BOT_REPO=.*#SUI_BOT_REPO=\"${REPO_URL}\"#" /etc/sui-bot/sui-bot.env \
  || echo "SUI_BOT_REPO=\"${REPO_URL}\"" >> /etc/sui-bot/sui-bot.env
systemctl daemon-reload
systemctl enable --now sui-bot sui-subpage

# ------------------------------------------------------------ 8. nginx
if [[ ${SKIP_NGINX:-0} != 1 ]]; then
  say "nginx — پورت 2096 (مرورگر→UI+منو / اپ→فید خام)"
  command -v nginx >/dev/null || { apt-get update -qq; apt-get install -y -qq nginx; }
  sed -e "s/__DOMAIN__/${WEB_DOMAIN}/g" \
      -e "s#__CERT__#${CERT}#g" \
      -e "s#__CERTKEY__#${CERTKEY}#g" \
      "$HERE/nginx/sub-ui.conf.tmpl" > /etc/nginx/sites-available/sub-ui
  ln -sf /etc/nginx/sites-available/sub-ui /etc/nginx/sites-enabled/sub-ui
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
fi

# ------------------------------------------------------------ 9. verify
say "راستی‌آزمایی نهایی"
sleep 6
fail=0
chk() { if eval "$2"; then echo "   ✔ $1"; else echo "   ✗ $1"; fail=1; fi; }
chk "sui-bot فعال"        "[[ $(systemctl is-active sui-bot) == active ]]"
chk "sui-subpage فعال"    "[[ $(systemctl is-active sui-subpage) == active ]]"
chk "استارت خودکار بعد ریبوت (sui-bot)"     "systemctl is-enabled sui-bot | grep -q enabled"
chk "استارت خودکار بعد ریبوت (sui-subpage)" "systemctl is-enabled sui-subpage | grep -q enabled"
chk "nginx فعال"          "systemctl is-active nginx"
chk "API پنل با توکن"     "curl -sk --max-time 8 --resolve '${WEB_DOMAIN}:${WEB_PORT}:127.0.0.1' -H 'Token: ${SUI_TOKEN_VAL}' 'https://${WEB_DOMAIN}:${WEB_PORT}/app/apiv2/clients' | grep -q '\"success\":true'"
chk "منوی وب 2096"        "curl -sk -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' 'https://127.0.0.1:2096/sub/menu' | grep -q 200"
chk "UI مرورگر 2096"      "curl -sk -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' 'https://127.0.0.1:2096/sub/test' | grep -q 200"
chk "فید اپ 2096"         "curl -sk -o /dev/null -w '%{http_code}' -A 'v2rayNG' 'https://127.0.0.1:2096/sub/test' | grep -qE '200|404'"
echo
if [[ $fail -eq 0 ]]; then
  echo -e "\033[1;32m★★★ همه‌چیز نصب و سالم است ★★★\033[0m"
  echo "   دکمهٔ مربع تلگرام الان منوی اصلی را باز می‌کند."
  echo "   دستورهای مدیریتی:"
  echo "     sui-bot-update      ← آپدیت از گیت‌هاب (فایل‌های اضافی پاک، داده‌ها می‌مانند)"
  echo "     sui-bot-uninstall   ← حذف کامل (با بکاپ خودکار داده‌ها)"
  echo "   گزارش کامل: $LOG"
else
  echo -e "\033[1;33mنصب تمام شد ولی بعضی تست‌ها پاس نشد — لاگ: $LOG\033[0m"
fi
