#!/usr/bin/env bash
# ============================================================================
#  SUI-BOT STACK — one-line installer from GitHub
#
#  Run:
#    REPO_URL=https://github.com/<USER>/<REPO>.git bash <(curl -fsSL https://raw.githubusercontent.com/<USER>/<REPO>/master/install.sh)
#
#  Installs: sui-bot (Telegram bot) + sui-subpage (sub UI + menu) + nginx (2096)
#  Auto-detects the local s-ui panel (port, domain, cert, API token).
# ============================================================================
set -euo pipefail

LOG=/var/log/sui-stack-install.log
exec > >(tee -a "$LOG") 2>&1

say() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo -e "\033[1;31m!! $*\033[0m"; exit 1; }
warn(){ echo -e "\033[1;33m   ⚠ $*\033[0m"; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo -i)"

# ------------------------------------------------------------ 0. cleanup old installs
say "Cleaning up old installs (v1/v2)"
for old_svc in $(systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -E '^(sui-bot|sui-subpage)\.service' || true); do
  systemctl stop "$old_svc" 2>/dev/null || true
  systemctl disable "$old_svc" 2>/dev/null || true
  echo "   stopped old service: $old_svc"
done
for old_venv in /opt/sui-bot/.venv /opt/sui-bot-v2/.venv; do
  if [[ -x $old_venv/bin/pip ]]; then
    "$old_venv/bin/pip" uninstall -y -q sui-bot 2>/dev/null || true
    echo "   removed old package: $old_venv"
  fi
done
# NOTE: data (/var/lib/sui-bot) and config (/etc/sui-bot) are preserved
rm -rf /opt/sui-bot /opt/sui-bot-v2 /opt/sui-bot-v2-src.old
rm -f /etc/systemd/system/sui-bot.service /etc/systemd/system/sui-subpage.service
systemctl daemon-reload
echo "   ✔ ready for clean install (data & tokens preserved)"

# ------------------------------------------------------------ 0.1 repo source
REPO_URL="${REPO_URL:-}"
if [[ -z $REPO_URL ]]; then
  SCRIPT_SRC="${BASH_SOURCE[0]}"
  if [[ -f $SCRIPT_SRC ]] && grep -q "DEFAULT_REPO_URL=" "$SCRIPT_SRC" 2>/dev/null; then
    REPO_URL="$(grep -m1 'DEFAULT_REPO_URL=' "$SCRIPT_SRC" | cut -d'"' -f2)"
  fi
fi
REPO_URL="${REPO_URL:-__DEFAULT_REPO_URL__}"
[[ $REPO_URL == *"__DEFAULT_REPO_URL__"* ]] && die "Set REPO_URL: REPO_URL=https://github.com/user/repo bash install.sh"

say "Fetching source from ${REPO_URL}"
rm -rf /opt/sui-bot-v2-src
mkdir -p /opt/sui-bot-v2-src
if ! command -v git >/dev/null; then
  apt-get update -qq && apt-get install -y -qq git ca-certificates
fi
# no -b → clones the repo's default branch (master/main both fine)
git clone --depth 1 "$REPO_URL" /opt/sui-bot-v2-src 2>/dev/null \
  || die "git clone failed — check REPO_URL and access"
HERE=/opt/sui-bot-v2-src
cd "$HERE"

# ------------------------------------------------------------ 1. find s-ui panel
say "Detecting s-ui panel on this server"
SUI_DB=""
for cand in /usr/local/s-ui/db/s-ui.db /opt/s-ui/db/s-ui.db /usr/local/s-ui/s-ui.db; do
  [[ -f $cand ]] && SUI_DB=$cand && break
done
[[ -n $SUI_DB ]] || die "s-ui database not found — install/start s-ui first (systemctl status s-ui)"
echo "   panel DB: $SUI_DB"

# pipe-delimited parse so empty fields do NOT shift values
IFS='|' read -r WEB_PORT WEB_DOMAIN CERT CERTKEY SUB_PORT < <(python3 - "$SUI_DB" << 'PY'
import sqlite3, sys
n = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
g = lambda k: (n.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or ('',))[0] or ''
print("|".join([
    g('webPort') or '2095',
    g('webDomain'),
    g('webCertFile'),
    g('webKeyFile'),
    g('subPort') or '2097',
]))
PY
)
WEB_PORT=${WEB_PORT:-2095}
SUB_PORT=${SUB_PORT:-2097}

# domain: panel settings → existing nginx vhost → cert dir → ask
if [[ -z $WEB_DOMAIN ]]; then
  WEB_DOMAIN=$(grep -rhoPm1 'server_name\s+\K[^;]+' /etc/nginx/sites-enabled/ 2>/dev/null | head -1 | xargs || true)
  WEB_DOMAIN=${WEB_DOMAIN%% *}
fi
if [[ -z $WEB_DOMAIN && -d /root/cert ]]; then
  guess=$(ls -1 /root/cert 2>/dev/null | head -1)
  [[ $guess == *.* ]] && WEB_DOMAIN=$guess
fi
read -r -p "❓ Server/panel domain [${WEB_DOMAIN:-required}]: " ans
[[ -n $ans ]] && WEB_DOMAIN=$ans
[[ -n $WEB_DOMAIN ]] || die "Domain is required"

# cert: panel settings → standard path next to domain
if [[ -z $CERT || -z $CERTKEY ]]; then
  if [[ -f /root/cert/${WEB_DOMAIN}/fullchain.pem ]]; then
    CERT="/root/cert/${WEB_DOMAIN}/fullchain.pem"
    CERTKEY="/root/cert/${WEB_DOMAIN}/privkey.pem"
    echo "   cert found at standard path: $CERT"
  fi
fi
echo "   panel: https://${WEB_DOMAIN}:${WEB_PORT} | sub: ${SUB_PORT} | cert: ${CERT:-will be issued}"

if curl -sk -o /dev/null --max-time 8 --resolve "${WEB_DOMAIN}:${WEB_PORT}:127.0.0.1" \
  "https://${WEB_DOMAIN}:${WEB_PORT}/"; then
  echo "   ✔ panel is alive"
else
  warn "panel did not answer on ${WEB_PORT} — continuing anyway (check later: systemctl status s-ui)"
fi

# ------------------------------------------------------------ 2. bot env (interactive)
say "Bot configuration (/etc/sui-bot/sui-bot.env)"
mkdir -p /etc/sui-bot
if [[ -n ${SUI_BOT_ENV:-} && -f $SUI_BOT_ENV ]]; then
  cp "$SUI_BOT_ENV" /etc/sui-bot/sui-bot.env
elif [[ -f /etc/sui-bot/sui-bot.env ]]; then
  echo "   existing env found (backup taken) — press Enter to keep current values"
  cp /etc/sui-bot/sui-bot.env "/etc/sui-bot/sui-bot.env.bak-$(date +%s)"
elif [[ -f $HERE/sui-bot.env.sample ]]; then
  cp "$HERE/sui-bot.env.sample" /etc/sui-bot/sui-bot.env
  echo "   created from sample → /etc/sui-bot/sui-bot.env"
else
  touch /etc/sui-bot/sui-bot.env
fi

# set_env_value KEY "question" [default] [numeric|csv|secret]
mask() { local v=$1; if [[ ${#v} -gt 10 ]]; then echo "${v:0:4}...${v: -4}"; else echo "$v"; fi; }
set_env_value() {
  local key=$1 question=$2 default=${3:-} mode=${4:-} current="" value="" hint=""
  current=$(grep -E "^${key}=" /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs || true)
  if [[ -n $current && $current != "replace-me" && $current != "0000-0000-0000-0000" ]]; then
    read -r -p "   ${key} current: $(mask "$current")  [Enter=keep | type new]: " value
    [[ -z $value ]] && { echo "      (kept)"; return; }
  else
    [[ -n $default ]] && hint=" [${default}]"
    read -r -p "❓ ${question}${hint}: " value || die "stdin not readable (interactive terminal required)"
    [[ -z $value && -n $default ]] && value="$default"
    [[ -z $value ]] && { echo "      (left empty — fill later: nano /etc/sui-bot/sui-bot.env)"; return; }
  fi
  # validation loop
  while true; do
    case $mode in
      numeric) [[ $value =~ ^[0-9]{4,20}$ ]] && break ;;
      csv)     [[ $value =~ ^[0-9]+(,[0-9]+)*$ ]] && break ;;
      *)       break ;;
    esac
    read -r -p "   ✗ invalid format (numbers only${mode:+, comma-separated}). Retry: " value
  done
  sed -i "/^${key}=/d" /etc/sui-bot/sui-bot.env
  echo "${key}=\"${value}\"" >> /etc/sui-bot/sui-bot.env
  echo "      ← saved"
}

echo "────────────────────────────────────────────────────────"
echo "  Bot connection info  (Enter = keep current value)"
echo "────────────────────────────────────────────────────────"
set_env_value "BOT_TOKEN"            "Telegram bot token (from @BotFather)"
set_env_value "SUI_TOKEN"            "s-ui panel API token (panel → Settings → API Token)"
set_env_value "ADMIN_TELEGRAM_ID"    "Primary admin NUMERIC Telegram ID (get it from @userinfobot)" "" numeric
PRIMARY_ID=$(grep -E '^ADMIN_TELEGRAM_ID=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs)
set_env_value "ADMIN_IDS"            "Other admin numeric IDs, comma-separated" "${PRIMARY_ID}" csv
set_env_value "BOT_DISPLAY_NAME"     "Bot display name" "Vpnfiy"
set_env_value "PAYMENT_CARD_NUMBER"  "Card number for card-to-card payments"
set_env_value "PAYMENT_CARD_HOLDER"  "Card holder name"
set_env_value "ZARINPAL_MERCHANT_ID" "ZarinPal merchant code (optional — Enter = card-to-card only)"
chmod 600 /etc/sui-bot/sui-bot.env

BOT_TOKEN_VAL=$(grep -E '^BOT_TOKEN=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs)
[[ -n $BOT_TOKEN_VAL && $BOT_TOKEN_VAL != "replace-me" ]] \
  || die "BOT_TOKEN is empty — the bot cannot start; run the installer again."

# --- rewrite server-derived values (sample placeholders must not survive) ---
sed -i "s#^SUI_HOST=.*#SUI_HOST=\"https://${WEB_DOMAIN}:${WEB_PORT}/app\"#" /etc/sui-bot/sui-bot.env
_upsert() {  # _upsert KEY VALUE
  grep -q "^$1=" /etc/sui-bot/sui-bot.env \
    && sed -i "s#^$1=.*#$1=\"$2\"#" /etc/sui-bot/sui-bot.env \
    || echo "$1=\"$2\"" >> /etc/sui-bot/sui-bot.env
}
_upsert "SUB_BASE_URL_OVERRIDE" "https://${WEB_DOMAIN}:2096/sub"
_upsert "MENU_WEBAPP_URL"       "https://${WEB_DOMAIN}:2096/sub/menu"
_upsert "STORE_ENABLED"         "true"
_upsert "DATA_DIR"              "/var/lib/sui-bot"
_upsert "SUBPAGE_PORT"          "8099"
_upsert "SUBPAGE_BIND"          "127.0.0.1"
SUI_TOKEN_VAL=$(grep -E '^SUI_TOKEN=' /etc/sui-bot/sui-bot.env | tail -1 | cut -d= -f2- | tr -d '"' | xargs || true)
echo "   SUI_HOST → https://${WEB_DOMAIN}:${WEB_PORT}/app"
echo "   MENU     → https://${WEB_DOMAIN}:2096/sub/menu"

# ------------------------------------------------------------ 3. panel API token compatibility
say "Ensuring API token exists inside the panel DB"
python3 - "$SUI_DB" "$SUI_TOKEN_VAL" << 'PY'
import sqlite3, sys, time
db, tok = sys.argv[1], sys.argv[2]
n = sqlite3.connect(db, timeout=15)
row = n.execute("SELECT id, expiry FROM tokens WHERE token=?", (tok,)).fetchone()
if row:
    if row[1] and row[1] < time.time():
        n.execute("UPDATE tokens SET expiry=? WHERE id=?", (int(time.time()) + 315360000, row[0]))
        n.commit(); print("   existing token — expiry extended (10 years)")
    else:
        print("   existing token is valid — nothing to do")
else:
    if tok in ("", "replace-me"):
        print("   ⚠ token empty/sample — fill env and re-run installer")
    else:
        n.execute("INSERT INTO tokens(desc, token, expiry, user_id) VALUES (?,?,?,1)",
                  ("telegram-bot", tok, int(time.time()) + 315360000))
        n.commit(); print("   token created inside panel DB")
PY

# ------------------------------------------------------------ 4. TLS certificate
say "TLS certificate"
if [[ -n $CERT && -f $CERT && -n $CERTKEY && -f $CERTKEY ]]; then
  echo "   using existing cert: $CERT"
else
  say "No cert found — issuing with acme.sh (standalone, port 80)"
  apt-get install -y -qq socat >/dev/null 2>&1 || true
  curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL:-admin@${WEB_DOMAIN}}" >/dev/null 2>&1 \
    || die "acme.sh install failed — or provide cert manually"
  ~/.acme.sh/acme.sh --issue -d "$WEB_DOMAIN" --standalone \
    && ~/.acme.sh/acme.sh --install-cert -d "$WEB_DOMAIN" \
        --fullchain-file "/root/cert/${WEB_DOMAIN}/fullchain.pem" \
        --key-file       "/root/cert/${WEB_DOMAIN}/privkey.pem" \
    || die "certificate issuance failed — point DNS of ${WEB_DOMAIN} to this server first"
  CERT="/root/cert/${WEB_DOMAIN}/fullchain.pem"
  CERTKEY="/root/cert/${WEB_DOMAIN}/privkey.pem"
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
  echo "   ✔ certificate issued and registered in panel"
fi

# ------------------------------------------------------------ 5. service user
say "Service user sui-bot"
getent passwd sui-bot >/dev/null || useradd --system --home-dir /var/lib/sui-bot --create-home --shell /usr/sbin/nologin sui-bot
mkdir -p /var/lib/sui-bot
chown sui-bot:sui-bot /var/lib/sui-bot

# ------------------------------------------------------------ 6. app install
say "Installing bot (venv + package from cloned source)"
if ! command -v python3 >/dev/null || ! dpkg -s python3-venv >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv
fi
rm -rf /opt/sui-bot-v2
python3 -m venv /opt/sui-bot-v2/.venv \
  || die "venv creation failed — run: apt install python3-venv"
/opt/sui-bot-v2/.venv/bin/pip install -q --upgrade pip
/opt/sui-bot-v2/.venv/bin/pip install -q "$HERE"
echo "   version: $(/opt/sui-bot-v2/.venv/bin/python -c 'import sui_bot; print(getattr(sui_bot,"__version__","ok"))' 2>/dev/null || echo ok)"

# ------------------------------------------------------------ 7. systemd + management tools
say "systemd services + management commands"
cp "$HERE/units/sui-bot.service" /etc/systemd/system/
cp "$HERE/units/sui-subpage.service" /etc/systemd/system/
install -m 755 "$HERE/sui-bot"       /usr/local/bin/sui-bot
install -m 755 "$HERE/update.sh"     /usr/local/bin/sui-bot-update
install -m 755 "$HERE/uninstall.sh"  /usr/local/bin/sui-bot-uninstall
grep -q '^SUI_BOT_REPO=' /etc/sui-bot/sui-bot.env \
  && sed -i "s#^SUI_BOT_REPO=.*#SUI_BOT_REPO=\"${REPO_URL}\"#" /etc/sui-bot/sui-bot.env \
  || echo "SUI_BOT_REPO=\"${REPO_URL}\"" >> /etc/sui-bot/sui-bot.env
systemctl daemon-reload
systemctl enable --now sui-bot sui-subpage

# ------------------------------------------------------------ 8. nginx
if [[ ${SKIP_NGINX:-0} != 1 ]]; then
  say "nginx — port 2096 (browser→UI+menu / VPN apps→raw feed)"

  # s-ui's own subscription service must NOT own 2096 — nginx needs it.
  # If s-ui sub is bound to 2096, move the panel's subPort to 2097.
  if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ':2096 '; then
    owner=$(ss -ltnp 2>/dev/null | grep ':2096 ' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    echo "   port 2096 is used by: ${owner:-unknown}"
    if [[ $owner == s-ui* ]]; then
      say "s-ui sub occupies 2096 → moving panel subPort to 2097 (nginx takes 2096)"
      systemctl stop s-ui 2>/dev/null || true
      python3 - "$SUI_DB" << 'PY'
import sqlite3, sys
n = sqlite3.connect(sys.argv[1], timeout=15)
if n.execute("SELECT 1 FROM settings WHERE key='subPort'").fetchone():
    n.execute("UPDATE settings SET value='2097' WHERE key='subPort'")
else:
    n.execute("INSERT INTO settings(key,value) VALUES ('subPort','2097')")
n.commit()
print("   panel subPort → 2097")
PY
      SUB_PORT=2097
      systemctl start s-ui 2>/dev/null || true
      sleep 3
    fi
  fi

  # sub backend scheme: https only if the panel sub service has its own cert+key
  SUB_SCHEME=$(python3 - "$SUI_DB" << 'PY'
import sqlite3, sys, os
n = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
g = lambda k: (n.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or ('',))[0] or ''
key, cert = g('subKeyFile'), g('subCertFile')
print('https' if key and cert and os.path.exists(key) and os.path.exists(cert) else 'http')
PY
)
  echo "   sub backend: ${SUB_SCHEME}://127.0.0.1:${SUB_PORT}"

  command -v nginx >/dev/null || { apt-get update -qq; apt-get install -y -qq nginx; }
  sed -e "s/__DOMAIN__/${WEB_DOMAIN}/g" \
      -e "s#__CERT__#${CERT}#g" \
      -e "s#__CERTKEY__#${CERTKEY}#g" \
      -e "s#https://127.0.0.1:2097#${SUB_SCHEME}://127.0.0.1:${SUB_PORT}#g" \
      "$HERE/nginx/sub-ui.conf.tmpl" > /etc/nginx/sites-available/sub-ui
  ln -sf /etc/nginx/sites-available/sub-ui /etc/nginx/sites-enabled/sub-ui
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
  sleep 1
  ss -ltn 2>/dev/null | grep -q ':2096 ' && echo "   ✔ nginx is listening on 2096" || warn "nothing is listening on 2096 yet"
fi

# ------------------------------------------------------------ 9. verify
say "Final verification"
sleep 6
fail=0
chk() { if eval "$2"; then echo "   ✔ $1"; else echo "   ✗ $1"; fail=1; fi; }
chk "sui-bot active"        "[[ $(systemctl is-active sui-bot) == active ]]"
chk "sui-subpage active"    "[[ $(systemctl is-active sui-subpage) == active ]]"
chk "auto-start on reboot (sui-bot)"     "systemctl is-enabled sui-bot | grep -q enabled"
chk "auto-start on reboot (sui-subpage)" "systemctl is-enabled sui-subpage | grep -q enabled"
chk "nginx active"          "systemctl is-active nginx"
chk "panel API via token"   "curl -sk --max-time 8 --resolve '${WEB_DOMAIN}:${WEB_PORT}:127.0.0.1' -H 'Token: ${SUI_TOKEN_VAL}' 'https://${WEB_DOMAIN}:${WEB_PORT}/app/apiv2/clients' | grep -q '\"success\":true'"
chk "web menu 2096"         "curl -sk -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' 'https://127.0.0.1:2096/sub/menu' | grep -q 200"
chk "browser UI 2096"       "curl -sk -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' 'https://127.0.0.1:2096/sub/test' | grep -q 200"
chk "app feed 2096"         "curl -sk -o /dev/null -w '%{http_code}' -A 'v2rayNG' 'https://127.0.0.1:2096/sub/test' | grep -qE '200|404'"

if [[ $fail -eq 0 ]]; then
  echo -e "\033[1;32m★★★ ALL CHECKS PASSED — INSTALL COMPLETE ★★★\033[0m"
else
  echo -e "\033[1;33mInstall finished but some checks failed — see log: $LOG\033[0m"
fi

# ------------------------------------------------------------ guide
echo
echo "════════════════════════ 📖 HOW TO RUN ════════════════════════"
echo
echo "  ▶  Nothing to run manually — the bot is already running as a"
echo "     systemd service and auto-starts on every server reboot."
echo
echo "  🤖 Inside the Telegram bot (as admin):"
echo "     /panel       admin dashboard (users, server, backups)"
echo "     /shop        shop (plans: admin settings → Plans/Pricing)"
echo "     /discounts   create promo/discount codes"
echo "     /wallet      user wallet"
echo "     /trial       free trial 500MB / 1 day"
echo "     /diag        panel health check"
echo
echo "  🖥️  On the server (management commands):"
echo "     sui-bot            full status (tokens masked)"
echo "     sui-bot logs       live bot logs"
echo "     sui-bot restart    restart services"
echo "     sui-bot menu       test the web menu page"
echo "     sui-bot config     edit tokens/settings (nano) + restart"
echo "     sui-bot update     ⬆ update from GitHub (keeps all data)"
echo "     sui-bot uninstall  full removal (auto-backup included)"
echo
echo "  📁 Important paths:"
echo "     config & tokens : /etc/sui-bot/sui-bot.env"
echo "     data            : /var/lib/sui-bot  (orders, wallet, promos, backups)"
echo "     code            : /opt/sui-bot-v2 (+ source in /opt/sui-bot-v2-src)"
echo "     system logs     : journalctl -u sui-bot -n 50"
echo
echo "  📱 The square menu button next to the message box in Telegram"
echo "     opens the graphical main menu."
echo "═════════════════════════════════════════════════════════════════"
