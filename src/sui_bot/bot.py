import asyncio
import json
import os
import tempfile
import glob
import shutil
import threading
import base64
import secrets
import signal
import string
import uuid
import html
import warnings
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from typing import Optional, Any, List
import logging
from logging.handlers import RotatingFileHandler
from collections import defaultdict
from pathlib import Path

import aiohttp
import psutil
from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, ExtBot, MessageHandler, filters
from telegram.error import BadRequest, InvalidToken, NetworkError, TelegramError, TimedOut
from telegram.helpers import escape_markdown
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning

from .backup import BackupTooLargeError, stream_response_to_file, validate_sqlite_database
from .backup_bundle import MAX_BUNDLE_BYTES, build_bundle, load_bundle, restore_bundle, write_bundle
from .config import Settings, validate_display_name
from .admin_registry import AdminRegistry, parse_admin_ids
from .diagnostics import format_diag_report, probe_endpoints
from .display import jalali_date_long, usage_bar
from .plans_store import PlansStore, fa_num
from .connection_guides import (
    ConnectionGuideStore,
    MAX_MESSAGES_PER_GUIDE,
    MAX_TITLE_LENGTH,
    split_guide_text,
    validate_guide_message,
)
from .reporting import (
    expiring_clients_with_assignments,
    load_expired_notification_ids,
    save_expired_notification_ids,
)
from .runtime_settings import load_runtime_settings, save_runtime_setting
from .security import can_access_client, is_public_callback, validate_service_url
from .store_bot import register_store_handlers
from .reseller_core import ResellerStore
from .reseller_bot import register_reseller_handlers, _parse_start_ref, _res_settle_request, reseller_panel_update, admin_finance
from .store_core import OrderStore
from .localization import LanguageStore, SUPPORTED_LANGUAGES, translate
from .navigation import has_multiple_subscriptions
from .outgoing_localization import (
    copyable_ltr_code,
    ltr_isolate,
    localize_inline_markup,
    localize_outgoing_text,
    preserve_dynamic_text,
)
from .sui_metadata import (
    build_subscription_urls,
    build_web_panel_url,
    extract_load_metadata,
    extract_partial_metadata,
    replace_url_origin,
)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
bot_log_file = os.getenv("BOT_LOG_FILE", "bot.log").strip()
if bot_log_file:
    try:
        file_handler = RotatingFileHandler(bot_log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
    except OSError as exc:
        # Journald still receives stdout/stderr under systemd; a logfile must
        # never prevent the bot from starting.
        logger.warning("File logging disabled because %s could not be opened: %s", bot_log_file, exc)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


DATA_DIR = os.getenv("DATA_DIR", "").strip()


def managed_data_path(value: str) -> str:
    path = Path(value)
    if not DATA_DIR:
        return str(path)
    state_root = Path(DATA_DIR).resolve()
    resolved = path.resolve() if path.is_absolute() else (state_root / path).resolve()
    if resolved != state_root and state_root not in resolved.parents:
        raise RuntimeError(f"Managed data path must stay inside DATA_DIR: {value}")
    return str(resolved)


SETTINGS = Settings.from_env()
RUNTIME_SETTINGS_FILE = managed_data_path("runtime_settings.json")
RUNTIME_SETTINGS = load_runtime_settings(RUNTIME_SETTINGS_FILE)
SUI_HOST = validate_service_url(SETTINGS.sui_host, allow_insecure_http=SETTINGS.allow_insecure_http)
SUI_TOKEN = SETTINGS.sui_token
BOT_TOKEN = SETTINGS.bot_token
ADMIN_TELEGRAM_ID = SETTINGS.admin_telegram_id
# --- چند-ادمینی: همه‌ی گاردها از admin_registry استفاده می‌کنند ---
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""), ADMIN_TELEGRAM_ID)
_admin_registry = AdminRegistry(
    primary_id=ADMIN_TELEGRAM_ID,
    admin_ids=ADMIN_IDS,
    owners_file=None,  # بعد از شناختن DATA_DIR در پایین ست می‌شود
)


def is_admin(user_id: int) -> bool:
    """Single source of truth for admin checks."""
    return _admin_registry.is_admin(user_id)


def runtime_add_admin(user_id: int) -> str:
    """Add admin at runtime (primary only, via bot UI); keeps ADMIN_IDS synced."""
    result = _admin_registry.add_admin(user_id)
    if result == "added":
        global ADMIN_IDS
        ADMIN_IDS = _admin_registry.all_admins
    return result


def runtime_remove_admin(user_id: int) -> str:
    """Remove admin at runtime (primary only, via bot UI); keeps ADMIN_IDS synced."""
    result = _admin_registry.remove_admin(user_id)
    if result == "removed":
        global ADMIN_IDS
        ADMIN_IDS = _admin_registry.all_admins
    return result


def admin_group_scope(user_id: int) -> set[str] | None:
    """گروه‌های مجاز یک ادمین؛ None یعنی بدون محدودیت."""
    if user_id == ADMIN_TELEGRAM_ID:
        return None
    groups = _admin_registry.groups_of(user_id)
    return None if groups is None else {str(g).strip().casefold() for g in groups}


def client_in_admin_scope(client: dict, scope: set[str] | None) -> bool:
    if scope is None:
        return True
    group = str(client.get("group") or "").strip().casefold()
    return bool(group) and group in scope


def admin_recipients(kind: str, customer_id: int | None = None) -> list[int]:
    """Where should a notification go? support/finance → owner, system → primary."""
    return _admin_registry.recipients_for(kind, customer_id)
ADMIN_CLIENT_ID = SETTINGS.admin_client_id
BACKUP_DIR = managed_data_path(SETTINGS.backup_dir)
DB_NAME = SETTINGS.db_name
BACKUP_MAX_BYTES = SETTINGS.backup_max_bytes
RATE_LIMIT_WINDOW = SETTINGS.rate_limit_window
MAX_REQUESTS_PER_WINDOW = SETTINGS.max_requests_per_window
RATE_LIMIT_SECONDS = SETTINGS.rate_limit_seconds
BLOCK_DURATION = SETTINGS.block_duration
REDIS_ENABLED = SETTINGS.redis_enabled
REDIS_HOST = SETTINGS.redis_host
REDIS_PORT = SETTINGS.redis_port
REDIS_DB = SETTINGS.redis_db
ITEMS_PER_PAGE = SETTINGS.items_per_page
SUB_CACHE_FILE = managed_data_path(SETTINGS.sub_cache_file)
SUB_CACHE_DURATION = SETTINGS.sub_cache_duration
ASSIGNMENTS_FILE = managed_data_path(SETTINGS.assignments_file)
METRICS_FILE = managed_data_path(SETTINGS.metrics_file)
REMINDER_DAYS = [1, 3, 5]
REMINDER_COOLDOWN = SETTINGS.reminder_cooldown
RENEWAL_MONTHLY_PRICE = int(RUNTIME_SETTINGS.get("RENEWAL_MONTHLY_PRICE", SETTINGS.renewal_monthly_price))
RENEWAL_MONTH_OPTIONS = str(RUNTIME_SETTINGS.get("RENEWAL_MONTH_OPTIONS", SETTINGS.renewal_month_options))
PAYMENT_CARD_NUMBER = str(RUNTIME_SETTINGS.get("PAYMENT_CARD_NUMBER", SETTINGS.payment_card_number))
PAYMENT_CARD_HOLDER = str(RUNTIME_SETTINGS.get("PAYMENT_CARD_HOLDER", SETTINGS.payment_card_holder))
BOT_DISPLAY_NAME = validate_display_name(RUNTIME_SETTINGS.get("BOT_DISPLAY_NAME", SETTINGS.bot_display_name))
ADMIN_TIMEZONE = str(RUNTIME_SETTINGS.get("ADMIN_TIMEZONE", "UTC")).upper()
PAYMENT_CURRENCY = str(RUNTIME_SETTINGS.get("PAYMENT_CURRENCY", "TOMAN")).upper()
WEB_PANEL_BASE_URL = SETTINGS.web_panel_base_url
WEB_PANEL_ENABLED = str(RUNTIME_SETTINGS.get("WEB_PANEL_ENABLED", "false")).strip().lower() in {
    "1", "true", "yes", "on"
}
SUBSCRIPTION_PUBLIC_ORIGIN = SETTINGS.subscription_public_origin
HIDE_SUBSCRIPTION_PORT = str(
    RUNTIME_SETTINGS.get("HIDE_SUBSCRIPTION_PORT", SETTINGS.hide_subscription_port)
).strip().lower() in {"1", "true", "yes", "on"}
LANGUAGE_STORE_FILE = managed_data_path("user_languages.json")
language_store = LanguageStore(LANGUAGE_STORE_FILE)
# پلن‌ها و قیمت‌گذاری فروشگاه (قابل مدیریت از ربات)
_plans_store = PlansStore(managed_data_path("plans_config.json"))
# فایل مالکیت مشتری‌ها (هر مشتری متعلق به کدام ادمین است)
_admin_registry.owners_file = managed_data_path("admin_owners.json")
_admin_registry._load()
# ادمین‌های اضافه‌شده از داخل ربات (ماندگار بین ری‌استارت‌ها)
_admin_registry.set_admins_file(managed_data_path("extra_admins.json"))
ADMIN_IDS = _admin_registry.all_admins
EXPIRED_NOTIFICATIONS_FILE = managed_data_path("expired_notifications.json")
CONNECTION_GUIDES_FILE = managed_data_path("connection_guides.json")
connection_guide_store = ConnectionGuideStore(CONNECTION_GUIDES_FILE)

# حذف یک‌بارهٔ کیبورد ثابت قدیمی «🏠» که روی کلاینت کاربران چسبیده است
KEYBOARD_CLEANUP_FILE = managed_data_path("keyboard_cleanup.json")


def _keyboard_cleanup_done(user_id: int) -> bool:
    try:
        data = json.loads(Path(KEYBOARD_CLEANUP_FILE).read_text(encoding="utf-8"))
        return int(user_id) in {int(v) for v in data} if isinstance(data, list) else False
    except (OSError, ValueError):
        return False


def _mark_keyboard_cleanup_done(user_id: int) -> None:
    try:
        try:
            data = json.loads(Path(KEYBOARD_CLEANUP_FILE).read_text(encoding="utf-8"))
            done = {int(v) for v in data} if isinstance(data, list) else set()
        except (OSError, ValueError):
            done = set()
        done.add(int(user_id))
        Path(KEYBOARD_CLEANUP_FILE).write_text(json.dumps(sorted(done)), encoding="utf-8")
    except OSError:
        logger.debug("keyboard cleanup flag save failed", exc_info=True)


async def _remove_stale_home_keyboard(update: Update, user_id: int) -> None:
    """حذف کیبورد ثابت قدیمی از پایین چت — فقط یک‌بار برای هر کاربر."""
    if _keyboard_cleanup_done(user_id):
        return
    try:
        await update.message.reply_text(
            "⌨️ دکمهٔ قدیمی پایین چت حذف شد.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except TelegramError:
        logger.debug("stale keyboard removal failed", exc_info=True)
    _mark_keyboard_cleanup_done(user_id)

# Inbounds cache constants
INBOUNDS_CACHE_FILE = managed_data_path("inbounds_cache.json")
INBOUNDS_CACHE_DURATION = 24 * 60 * 60  # 24 hours

# Alert system constants
CPU_ALERT_THRESHOLD = 90
RAM_ALERT_THRESHOLD = 85
ALERT_COOLDOWN = 120  # 2 minutes between alerts
MONITOR_INTERVAL = 30  # Check every 30 seconds
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32
MAX_DESC_LEN = 120
MAX_GROUP_LEN = 64
MAX_REMARK_LEN = 120
MAX_BROADCAST_LEN = 2000
MAX_CALLBACK_DATA_LEN = 64
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
API_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
API_GET_ATTEMPTS = 4

# Alert State tracking
alert_state = {
    'cpu_alert_sent': 0,
    'ram_alert_sent': 0,
    'cpu_recovered': True,
    'ram_recovered': True
}

reminder_last_sent = {}
clients_cache = None
clients_cache_time = 0
CLIENTS_CACHE_DURATION = 300

# Inbounds cache variables
inbounds_cache = None
inbounds_cache_time = 0

CREATE_USER_NAME, CREATE_USER_INBOUNDS, CREATE_USER_VOLUME, CREATE_USER_EXPIRY, CREATE_USER_DESC, CREATE_USER_GROUP = range(6)
EDIT_USER_GET_ID, EDIT_USER_NAME, EDIT_USER_INBOUNDS, EDIT_USER_VOLUME, EDIT_USER_EXPIRY, EDIT_USER_DESC, EDIT_USER_GROUP, EDIT_USER_ENABLE, EDIT_USER_REGEN = range(6, 15)
DELETE_USER_GET_ID, DELETE_USER_CONFIRM = range(15, 17)
BROADCAST_MESSAGE, BROADCAST_CONFIRM = range(17, 19)
SETTINGS_CARD_NUMBER, SETTINGS_CARD_HOLDER, SETTINGS_DISPLAY_NAME = range(19, 22)
RESTORE_BACKUP_FILE = 22
CONNECTION_GUIDE_TITLE, CONNECTION_GUIDE_CONTENT = range(23, 25)
CONNECTION_GUIDE_EDIT_TITLE, CONNECTION_GUIDE_EDIT_ITEM, CONNECTION_GUIDE_APPEND_ITEM = range(25, 28)
CREATE_USER_REMARK, CREATE_USER_LIFECYCLE, CREATE_USER_RESET_DAYS = range(28, 31)
EDIT_USER_REMARK, EDIT_USER_LIFECYCLE, EDIT_USER_RESET_DAYS = range(31, 34)
SETTINGS_MONTHLY_PRICE = 34
# --- مدیریت پلن‌ها/قیمت‌گذاری ---
PLAN_TITLE, PLAN_GB, PLAN_DAYS, PLAN_PRICE, PLAN_RESELLER = range(35, 40)
PRICING_VALUE = 40
UPRICE_USER, UPRICE_VALUE = 41, 42

ADMIN_TIMEZONES = {
    "UTC": ("UTC", "UTC / Global"),
    "IRAN": ("Asia/Tehran", "Iran"),
    "CHINA": ("Asia/Shanghai", "China"),
    "RUSSIA": ("Europe/Moscow", "Russia (Moscow)"),
}
PAYMENT_CURRENCIES = {
    "TOMAN": "Iranian toman (TOMAN)",
    "USD": "US dollar (USD)",
    "CNY": "Chinese yuan (CNY)",
    "RUB": "Russian ruble (RUB)",
}
if ADMIN_TIMEZONE not in ADMIN_TIMEZONES:
    ADMIN_TIMEZONE = "UTC"
if PAYMENT_CURRENCY not in PAYMENT_CURRENCIES:
    PAYMENT_CURRENCY = "TOMAN"

_user_requests = {}
_blocked_users = {}
sub_base_url = None
sub_cache_time = 0
telegram_clients = {}  # Format: {telegram_id: [client_id1, client_id2, ...]}
redis_client = None
pending_renew_requests = {}
bot_backup_lock = asyncio.Lock()

def parse_renewal_month_options(raw: Any) -> List[int]:
    items = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            month = int(part)
        except ValueError:
            continue
        if 1 <= month <= 24:
            items.append(month)
    unique_sorted = sorted(set(items))
    return unique_sorted if unique_sorted else [1, 2, 3]

renewal_month_options = parse_renewal_month_options(RENEWAL_MONTH_OPTIONS)

RENEW_REQUEST_TTL_SECONDS = 48 * 60 * 60

class MetricsTracker:
    def __init__(self):
        self.metrics = {
            'commands': defaultdict(lambda: defaultdict(int)),
            'errors': defaultdict(int),
            'response_times': defaultdict(list),
            'last_activity': {},
            'total_commands': 0,
            'start_time': datetime.now().isoformat()
        }

    def load_metrics(self):
        if os.path.exists(METRICS_FILE):
            try:
                with open(METRICS_FILE, 'r') as f:
                    data = json.load(f)
                    if 'commands' in data:
                        commands_dict = defaultdict(lambda: defaultdict(int))
                        for k, v in data['commands'].items():
                            user_commands = defaultdict(int)
                            user_commands.update(v)
                            commands_dict[int(k)] = user_commands
                        self.metrics['commands'] = commands_dict
                    if 'errors' in data:
                        errors_dict = defaultdict(int)
                        errors_dict.update({int(k): v for k, v in data['errors'].items()})
                        self.metrics['errors'] = errors_dict
                    if 'response_times' in data:
                        response_dict = defaultdict(list)
                        response_dict.update(data['response_times'])
                        self.metrics['response_times'] = response_dict
                    if 'last_activity' in data:
                        self.metrics['last_activity'] = {int(k): v for k, v in data['last_activity'].items()}
                    if 'total_commands' in data:
                        self.metrics['total_commands'] = data['total_commands']
                    if 'start_time' in data:
                        self.metrics['start_time'] = data['start_time']
                    logger.info("Metrics loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load metrics: {e}")

    def save_metrics(self):
        try:
            data = {
                'commands': dict(self.metrics['commands']),
                'errors': dict(self.metrics['errors']),
                'response_times': dict(self.metrics['response_times']),
                'last_activity': dict(self.metrics['last_activity']),
                'total_commands': self.metrics['total_commands'],
                'start_time': self.metrics['start_time']
            }
            with io_lock:
                with tempfile.NamedTemporaryFile('w', delete=False, dir='.') as tmp:
                    json.dump(data, tmp, indent=2)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    tmp_name = tmp.name
                shutil.move(tmp_name, METRICS_FILE)
        except Exception as e:
            logger.error(f"Failed to save metrics: {e}")

    def record_command(self, user_id: int, command: str, response_time: float = None):
        if user_id not in self.metrics['commands']:
            self.metrics['commands'][user_id] = defaultdict(int)
        self.metrics['commands'][user_id][command] += 1
        self.metrics['last_activity'][user_id] = datetime.now().isoformat()
        self.metrics['total_commands'] += 1
        if response_time:
            self.metrics['response_times'][command].append(response_time)
            if len(self.metrics['response_times'][command]) > 100:
                self.metrics['response_times'][command] = self.metrics['response_times'][command][-100:]
        if self.metrics['total_commands'] % 50 == 0:
            self.save_metrics()

    def record_error(self, user_id: int):
        if user_id not in self.metrics['errors']:
            self.metrics['errors'][user_id] = 0
        self.metrics['errors'][user_id] += 1
        if self.metrics['errors'][user_id] % 5 == 0:
            self.save_metrics()

    def get_user_stats(self, user_id: int) -> dict:
        commands = self.metrics['commands'].get(user_id, {})
        total = sum(commands.values())
        last_activity = self.metrics['last_activity'].get(user_id)
        errors = self.metrics['errors'].get(user_id, 0)
        return {
            'total_commands': total,
            'commands': dict(commands),
            'last_activity': last_activity,
            'errors': errors
        }

    def get_global_stats(self) -> dict:
        user_totals = {user_id: sum(cmds.values()) for user_id, cmds in self.metrics['commands'].items()}
        most_active = sorted(user_totals.items(), key=lambda x: x[1], reverse=True)[:10]
        command_totals = defaultdict(int)
        for user_cmds in self.metrics['commands'].values():
            for cmd, count in user_cmds.items():
                command_totals[cmd] += count
        most_used = sorted(command_totals.items(), key=lambda x: x[1], reverse=True)[:10]
        avg_response_times = {cmd: sum(times) / len(times) if times else 0 for cmd, times in self.metrics['response_times'].items()}
        return {
            'total_commands': self.metrics['total_commands'],
            'total_users': len(self.metrics['commands']),
            'most_active_users': most_active,
            'most_used_commands': most_used,
            'avg_response_times': avg_response_times,
            'total_errors': sum(self.metrics['errors'].values()),
            'start_time': self.metrics['start_time']
        }

metrics = MetricsTracker()
metrics.load_metrics()
io_lock = threading.Lock()

class RateLimiter:
    def __init__(self, redis_client = None):
        self.redis = redis_client
        self.use_redis = redis_client is not None
        self._memory_requests = {}
        self._memory_blocks = {}
        self._last_cleanup = 0

    async def check_rate_limit(self, user_id: int) -> bool:
        if is_admin(user_id):
            return True
        if self.use_redis:
            return await self._check_redis(user_id)
        else:
            return self._check_memory(user_id)

    async def _check_redis(self, user_id: int) -> bool:
        current_time = datetime.now().timestamp()
        block_key = f"blocked:{user_id}"
        blocked_until = await self.redis.get(block_key)
        if blocked_until:
            if float(blocked_until) > current_time:
                return False
            else:
                await self.redis.delete(block_key)
        requests_key = f"requests:{user_id}"
        await self.redis.zremrangebyscore(requests_key, 0, current_time - RATE_LIMIT_WINDOW)
        recent = await self.redis.zrange(requests_key, 0, -1, withscores=True)
        if recent and current_time - recent[-1][1] < RATE_LIMIT_SECONDS:
            return False
        if len(recent) >= MAX_REQUESTS_PER_WINDOW:
            await self.redis.setex(block_key, BLOCK_DURATION, str(current_time + BLOCK_DURATION))
            logger.warning(f"User {user_id} blocked for {BLOCK_DURATION}s")
            return False
        await self.redis.zadd(requests_key, {str(current_time): current_time})
        await self.redis.expire(requests_key, RATE_LIMIT_WINDOW)
        return True

    def _check_memory(self, user_id: int) -> bool:
        current_time = datetime.now().timestamp()
        self._cleanup_memory(current_time)
        if user_id in self._memory_blocks:
            if current_time < self._memory_blocks[user_id]:
                return False
            else:
                del self._memory_blocks[user_id]
        if user_id not in self._memory_requests:
            self._memory_requests[user_id] = []
        user_requests = self._memory_requests[user_id]
        user_requests[:] = [t for t in user_requests if current_time - t < RATE_LIMIT_WINDOW]
        if user_requests and current_time - user_requests[-1] < RATE_LIMIT_SECONDS:
            return False
        if len(user_requests) >= MAX_REQUESTS_PER_WINDOW:
            self._memory_blocks[user_id] = current_time + BLOCK_DURATION
            logger.warning(f"User {user_id} blocked for {BLOCK_DURATION}s")
            return False
        user_requests.append(current_time)
        return True

    def _cleanup_memory(self, current_time: float):
        # Periodic cleanup to prevent unbounded growth for inactive users.
        if current_time - self._last_cleanup < 300:
            return
        stale_users = []
        for uid, reqs in self._memory_requests.items():
            if not reqs:
                stale_users.append(uid)
                continue
            if current_time - reqs[-1] > (RATE_LIMIT_WINDOW + BLOCK_DURATION):
                stale_users.append(uid)
        for uid in stale_users:
            self._memory_requests.pop(uid, None)
            self._memory_blocks.pop(uid, None)
        self._last_cleanup = current_time

    async def get_block_status(self, user_id: int):
        if self.use_redis:
            block_key = f"blocked:{user_id}"
            blocked_until = await self.redis.get(block_key)
            if blocked_until:
                remaining = float(blocked_until) - datetime.now().timestamp()
                return remaining if remaining > 0 else None
        else:
            if user_id in self._memory_blocks:
                remaining = self._memory_blocks[user_id] - datetime.now().timestamp()
                return remaining if remaining > 0 else None
        return None

    async def reset_user(self, user_id: int):
        if self.use_redis:
            await self.redis.delete(f"requests:{user_id}", f"blocked:{user_id}")
        else:
            if user_id in self._memory_requests:
                self._memory_requests[user_id].clear()
            if user_id in self._memory_blocks:
                del self._memory_blocks[user_id]
        logger.info(f"Rate limit reset for user {user_id}")

rate_limiter = None

def md_escape(value: Any) -> str:
    return escape_markdown(str(value), version=1)


def user_language(user_id: int) -> str:
    # پیش‌فرض فارسی — کاربر می‌تواند از دکمهٔ 🌐 زبان عوض کند
    return language_store.get(user_id) or "fa"


def tr(recipient_id: int, key: str, **values: Any) -> str:
    """Translate for a recipient without reserving template field names."""
    return translate(user_language(recipient_id), key, **values)

async def localized_query_answer(query, text=None, *args, **kwargs):
    localized = localize_outgoing_text(
        user_language(query.from_user.id), text, display_name=BOT_DISPLAY_NAME
    )
    try:
        return await query.answer(localized, *args, **kwargs)
    except NetworkError as exc:
        # Answering a callback only dismisses Telegram's loading indicator. A
        # temporary Telegram/API timeout must not cancel the actual button
        # operation that follows.
        logger.warning(
            "Telegram callback acknowledgement was interrupted (%s); continuing button action",
            exc,
        )
        return None

def localized_remaining_time(expiry_timestamp: int, user_id: int) -> str:
    if expiry_timestamp == 0:
        return tr(user_id, "unlimited")
    expiry = datetime.fromtimestamp(expiry_timestamp, timezone.utc)
    remaining_seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
    if remaining_seconds <= 0:
        return tr(user_id, "disabled")
    days = int(remaining_seconds // 86400) + (1 if remaining_seconds % 86400 else 0)
    return tr(user_id, "days_remaining", days=days).removeprefix("⏳ ")


# [reply-keyboard removed per Hossein — inline menu only]


def language_keyboard():
    rows = [
        [InlineKeyboardButton(label, callback_data=f"lang_set_{code}")]
        for code, label in SUPPORTED_LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(rows)

class LocalizedExtBot(ExtBot):
    """Translate all outgoing Telegram content for its destination chat."""

    @staticmethod
    def _language(chat_id) -> str:
        try:
            return user_language(int(chat_id))
        except (TypeError, ValueError):
            return "en"

    async def send_message(self, chat_id, text, *args, **kwargs):
        language = self._language(chat_id)
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().send_message(
            chat_id, localize_outgoing_text(language, text, display_name=BOT_DISPLAY_NAME), *args, **kwargs
        )

    async def edit_message_text(self, text, chat_id=None, *args, **kwargs):
        effective_chat_id = chat_id
        language = self._language(effective_chat_id)
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().edit_message_text(
            localize_outgoing_text(language, text, display_name=BOT_DISPLAY_NAME), chat_id, *args, **kwargs
        )

    async def send_document(self, chat_id, document, *args, **kwargs):
        language = self._language(chat_id)
        kwargs["caption"] = localize_outgoing_text(
            language, kwargs.get("caption"), display_name=BOT_DISPLAY_NAME
        )
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().send_document(chat_id, document, *args, **kwargs)

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        language = self._language(chat_id)
        kwargs["caption"] = localize_outgoing_text(
            language, kwargs.get("caption"), display_name=BOT_DISPLAY_NAME
        )
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().send_photo(chat_id, photo, *args, **kwargs)

    async def send_video(self, chat_id, video, *args, **kwargs):
        language = self._language(chat_id)
        kwargs["caption"] = localize_outgoing_text(
            language, kwargs.get("caption"), display_name=BOT_DISPLAY_NAME
        )
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().send_video(chat_id, video, *args, **kwargs)

    async def edit_message_caption(self, *args, chat_id=None, caption=None, **kwargs):
        language = self._language(chat_id)
        localized_caption = localize_outgoing_text(language, caption, display_name=BOT_DISPLAY_NAME)
        kwargs["reply_markup"] = localize_inline_markup(
            kwargs.get("reply_markup"), language, self, display_name=BOT_DISPLAY_NAME
        )
        return await super().edit_message_caption(
            *args, chat_id=chat_id, caption=localized_caption, **kwargs
        )

def is_safe_callback_data(data: str) -> bool:
    if not data or len(data) > MAX_CALLBACK_DATA_LEN:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-")
    return all(ch in allowed for ch in data)

def sui_response_object(response: object) -> dict | None:
    """Return a successful S-UI response object, rejecting error envelopes."""
    if not isinstance(response, dict) or response.get("success") is not True:
        return None
    obj = response.get("obj")
    return obj if isinstance(obj, dict) else None


def sui_payload(response: object) -> object | None:
    """Return a successful S-UI obj of any shape (dict OR list).

    Several S-UI endpoints (logs, changes, keypairs, users, tokens) return a
    JSON *list* inside ``obj``; treating those as failures made the bot show
    nothing or a generic error even when the panel answered correctly.
    """
    if not isinstance(response, dict) or response.get("success") is not True:
        return None
    return response.get("obj")


def sui_clients(response: object) -> list[dict] | None:
    """Return a fully validated clients collection from an S-UI envelope."""
    obj = sui_response_object(response)
    clients = obj.get("clients") if obj is not None else None
    if not isinstance(clients, list) or any(not isinstance(client, dict) for client in clients):
        return None
    return clients


async def find_shop_client_id(client_name: str) -> int | None:
    """کلاینت تازه‌ساخته‌شده را از فهرست پنل بر اساس نام پیدا کن (id برمی‌گرداند).

    عمداً کش ۵ دقیقه‌ای را دور می‌زند — بلافاصله بعد از save صدا زده می‌شود
    و کش قدیمی کلاینت جدید را ندارد (باگ «created client not found»).
    """
    data = await api_client.get('apiv2/clients')
    clients = sui_clients(data)
    if not clients:
        # fallback به کش اگر پنل جواب نداد
        clients = await get_all_clients_list()
    for client in clients or []:
        if client.get("name") == client_name:
            return client.get("id")
    return None


async def shop_name_exists(client_name: str) -> bool:
    """آیا کلاینتی با این نام در پنل هست؟ (برای جلوگیری از اسم تکراری)"""
    clients = await get_all_clients_list()
    for client in clients or []:
        if (client.get("name") or "").lower() == client_name.lower():
            return True
    return False


class APIClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip('/')
        self.headers = {'Token': token}
        self.session = None
        # آخرین دلیل شکست هر endpoint؛ برای نمایش علت به ادمین (/diag و پیام‌های خطا)
        self.last_errors: dict[str, str] = {}

    def record_error(self, endpoint: str, detail: str) -> None:
        normalized = endpoint.lstrip('/').split('?')[0]
        self.last_errors[normalized] = str(detail)[:200]

    def clear_error(self, endpoint: str) -> None:
        self.last_errors.pop(endpoint.lstrip('/').split('?')[0], None)

    def error_reason(self, endpoint: str | None = None) -> str:
        """دلیل آخرین شکست برای نمایش در پیام؛ بدون جزئیات حساس."""
        if endpoint:
            reason = self.last_errors.get(endpoint.lstrip('/').split('?')[0])
        elif self.last_errors:
            reason = next(iter(self.last_errors.values()))
        else:
            reason = None
        return f" (Reason: {reason})" if reason else ""

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
            self.session = aiohttp.ClientSession(headers=self.headers, connector=connector, timeout=API_TIMEOUT)

    @staticmethod
    async def decode_json_response(response: aiohttp.ClientResponse) -> dict:
        content_length = response.content_length
        if content_length is not None and content_length > MAX_API_RESPONSE_BYTES:
            raise ValueError("S-UI response exceeds the configured safety limit")
        payload = await response.content.read(MAX_API_RESPONSE_BYTES + 1)
        if len(payload) > MAX_API_RESPONSE_BYTES:
            raise ValueError("S-UI response exceeds the configured safety limit")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("S-UI response must be a JSON object")
        return decoded

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(self, endpoint: str, params=None, *, attempts: int | None = None, log_failure: bool = True):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        attempt_limit = API_GET_ATTEMPTS if attempts is None else max(1, min(attempts, API_GET_ATTEMPTS))
        log = logger.warning if log_failure else logger.debug
        for attempt in range(1, attempt_limit + 1):
            await self.ensure_session()
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                ) as response:
                    response.raise_for_status()
                    decoded = await self.decode_json_response(response)
                    if sui_response_object(decoded) is None:
                        reason = str(decoded.get("msg") or "unsuccessful API response")[:200]
                        self.record_error(endpoint, reason)
                        log("S-UI GET %s returned an unsuccessful API response: %s", endpoint, reason)
                        return None
                    self.clear_error(endpoint)
                    return decoded
            except asyncio.CancelledError:
                raise
            except aiohttp.ClientResponseError as exc:
                retryable = exc.status in {408, 425, 429} or exc.status >= 500
                self.record_error(endpoint, f"HTTP {exc.status}")
                if not retryable or attempt == attempt_limit:
                    log(
                        "S-UI GET %s failed with HTTP %s%s",
                        endpoint, exc.status,
                        f" after {attempt} attempts" if retryable else "",
                    )
                    return None
                delay = 0.5 * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self.record_error(endpoint, f"{type(exc).__name__}: {exc}"[:200])
                if attempt == attempt_limit:
                    log(
                        "S-UI GET %s failed after %s attempts (%s): %s",
                        endpoint, attempt_limit, type(exc).__name__, exc,
                    )
                    return None
                delay = 0.5 * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
        return None

api_client = APIClient(SUI_HOST, SUI_TOKEN)

async def create_or_edit_client(action: str, client_data: dict) -> dict:
    await api_client.ensure_session()
    url = f"{api_client.base_url}/apiv2/save"
    # POST گاهی پاسخ ناقص برمی‌گرداند (JSON وسط قطع می‌شود) — تا ۳ بار تلاش کن
    last_err = None
    for attempt in range(1, 4):
        try:
            data_payload = {"object": "clients", "action": action, "data": json.dumps(client_data)}
            async with api_client.session.post(url, data=data_payload) as response:
                response.raise_for_status()
                decoded = await api_client.decode_json_response(response)
                if isinstance(decoded, dict) and decoded.get("success") is False:
                    api_client.record_error("apiv2/save", str(decoded.get("msg") or "save rejected")[:200])
                else:
                    api_client.clear_error("apiv2/save")
                return decoded
        except Exception as e:
            last_err = e
            api_client.record_error("apiv2/save", f"{type(e).__name__}: {e}"[:200])
            logger.warning(f"S-UI save attempt {attempt}/3 failed: {type(e).__name__}: {e}")
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)
    logger.error(f"Failed to {action} client after 3 attempts: {last_err}")
    return None

def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def random_alnum(n: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))

def rand_b64(nbytes: int) -> str:
    return base64.b64encode(os.urandom(nbytes)).decode("ascii")

def random_seq(n: int = 10) -> str:
    # Equivalent to RandomUtil.randomSeq(n)
    return random_alnum(n)

def random_shadowsocks_password(length: int) -> str:
    # S-UI's RandomUtil.randomShadowsocksPassword(n) generates n random
    # bytes and returns their standard Base64 representation.
    return rand_b64(length)

def random_uuid() -> str:
    return str(uuid.uuid4())

def update_configs(configs: dict, new_user_name: str) -> dict:
    for _, config in configs.items():
        if not isinstance(config, dict):
            continue
        if "name" in config:
            config["name"] = new_user_name
        elif "username" in config:
            config["username"] = new_user_name
    return configs

def shuffle_configs(configs: dict, key: Optional[str] = None):
    keys = [key] if key else list(configs.keys())
    for k in keys:
        if k not in configs or not isinstance(configs[k], dict):
            continue
        if k in ("mixed", "socks", "http", "anytls", "trojan", "naive", "hysteria2"):
            configs[k]["password"] = random_seq(10)
        elif k == "shadowsocks":
            configs[k]["password"] = random_shadowsocks_password(32)
        elif k == "shadowsocks16":
            configs[k]["password"] = random_shadowsocks_password(16)
        elif k == "shadowtls":
            configs[k]["password"] = random_shadowsocks_password(32)
        elif k == "hysteria":
            configs[k]["auth_str"] = random_seq(10)
        elif k == "tuic":
            configs[k]["password"] = random_seq(10)
            configs[k]["uuid"] = random_uuid()
        elif k in ("vmess", "vless"):
            configs[k]["uuid"] = random_uuid()

def random_configs(user: str) -> dict:
    mixed_password = random_seq(10)
    ss_password_16 = random_shadowsocks_password(16)
    ss_password_32 = random_shadowsocks_password(32)
    uid = random_uuid()
    return {
        "mixed": {"username": user, "password": mixed_password},
        "socks": {"username": user, "password": mixed_password},
        "http": {"username": user, "password": mixed_password},
        "shadowsocks": {"name": user, "password": ss_password_32},
        "shadowsocks16": {"name": user, "password": ss_password_16},
        "shadowtls": {"name": user, "password": ss_password_32},
        "vmess": {"name": user, "uuid": uid, "alterId": 0},
        "vless": {"name": user, "uuid": uid, "flow": "xtls-rprx-vision"},
        "anytls": {"name": user, "password": mixed_password},
        "trojan": {"name": user, "password": mixed_password},
        "naive": {"username": user, "password": mixed_password},
        "hysteria": {"name": user, "auth_str": mixed_password},
        "tuic": {"name": user, "uuid": uid, "password": mixed_password},
        "hysteria2": {"name": user, "password": mixed_password},
    }

def build_config_for_name(name: str) -> dict:
    return random_configs(name)

def build_client_data_new(
    name: str,
    volume_bytes: int,
    expiry_timestamp: int,
    desc: str,
    group: str,
    inbounds: List[int],
    enable: bool = True,
    remark: str = "",
    delay_start: bool = False,
    auto_reset: bool = False,
    reset_days: int = 0,
    next_reset: int = 0,
) -> dict:
    return {
        "enable": enable,
        "name": name,
        "config": build_config_for_name(name),
        "inbounds": inbounds,
        "links": [],
        "volume": volume_bytes if volume_bytes > 0 else 0,
        "expiry": expiry_timestamp if expiry_timestamp > 0 else 0,
        "up": 0,
        "down": 0,
        "desc": desc,
        "group": group,
        "remark": remark,
        "delayStart": delay_start,
        "autoReset": auto_reset,
        "resetDays": reset_days if reset_days > 0 else 0,
        "nextReset": next_reset if next_reset > 0 else 0,
        "totalUp": 0,
        "totalDown": 0,
        "createdAt": 0,
        "onlineAt": 0,
    }

def build_client_data_edit(
    client_id: int,
    name: str,
    volume_bytes: int,
    expiry_timestamp: int,
    desc: str,
    group: str,
    inbounds: List[int],
    enable: bool = True,
    regenerate_secrets: bool = False,
    original_client: Optional[dict] = None,
    remark: Optional[str] = None,
    delay_start: Optional[bool] = None,
    auto_reset: Optional[bool] = None,
    reset_days: Optional[int] = None,
    next_reset: Optional[int] = None,
) -> dict:
    edited = {
        "id": client_id,
        "enable": enable,
        "name": name,
        "inbounds": inbounds,
        "volume": volume_bytes if volume_bytes > 0 else 0,
        "expiry": expiry_timestamp if expiry_timestamp > 0 else 0,
        "up": 0,
        "down": 0,
        "desc": desc,
        "group": group,
        "links": [],
        "remark": "",
        "delayStart": False,
        "autoReset": False,
        "resetDays": 0,
        "nextReset": 0,
        "totalUp": 0,
        "totalDown": 0,
        "createdAt": 0,
        "onlineAt": 0,
    }
    if original_client:
        edited["up"] = original_client.get("up", 0)
        edited["down"] = original_client.get("down", 0)
        edited["links"] = original_client.get("links", [])
        for field, default in (
            ("remark", ""),
            ("delayStart", False),
            ("autoReset", False),
            ("resetDays", 0),
            ("nextReset", 0),
            ("totalUp", 0),
            ("totalDown", 0),
            ("createdAt", 0),
            ("onlineAt", 0),
        ):
            edited[field] = original_client.get(field, default)
    if regenerate_secrets:
        if original_client and isinstance(original_client.get("config"), dict):
            cfg = json.loads(json.dumps(original_client.get("config", {})))
            update_configs(cfg, name)
            shuffle_configs(cfg)
            edited["config"] = cfg
        else:
            edited["config"] = build_config_for_name(name)
    elif original_client:
        cfg = original_client.get("config", {})
        if isinstance(cfg, dict):
            cfg = json.loads(json.dumps(cfg))
            update_configs(cfg, name)
        edited["config"] = cfg
    else:
        edited["config"] = {}
    if remark is not None:
        edited["remark"] = remark
    if delay_start is not None:
        edited["delayStart"] = delay_start
    if auto_reset is not None:
        edited["autoReset"] = auto_reset
    if reset_days is not None:
        edited["resetDays"] = max(0, reset_days)
    if next_reset is not None:
        edited["nextReset"] = max(0, next_reset)
    return edited


def build_client_renewal_data(original_client: dict, client_id: int, new_expiry: int) -> dict:
    """Build a renewal edit while resetting current and accumulated traffic."""
    renewed = json.loads(json.dumps(original_client))
    renewed.update({
        "id": client_id,
        "expiry": new_expiry,
        "enable": True,
        "up": 0,
        "down": 0,
        "totalUp": 0,
        "totalDown": 0,
    })
    return renewed

# Backward-compatible alias for existing call sites.
def build_client_data(
    name: str,
    volume_bytes: int,
    expiry_timestamp: int,
    desc: str,
    group: str,
    inbounds: List[int],
    enable: bool = True,
) -> dict:
    return build_client_data_new(
        name=name,
        volume_bytes=volume_bytes,
        expiry_timestamp=expiry_timestamp,
        desc=desc,
        group=group,
        inbounds=inbounds,
        enable=enable,
    )

def calculate_remaining_time(expiry_timestamp: int) -> str:
    if expiry_timestamp == 0:
        return "♾️ Unlimited"
    now = datetime.now(timezone.utc)
    expiry = datetime.fromtimestamp(expiry_timestamp, timezone.utc)
    if expiry <= now:
        return "0 Days (Expired)"
    remaining_seconds = (expiry - now).total_seconds()
    remaining_days = int(remaining_seconds / 86400)
    if remaining_seconds % 86400 > 0:
        remaining_days += 1
    return f"{remaining_days} Days"

def atomic_json_write(filepath: str, data: dict):
    try:
        with io_lock:
            with tempfile.NamedTemporaryFile('w', delete=False, dir=os.path.dirname(filepath) or '.') as tmp:
                json.dump(data, tmp, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_name = tmp.name
            shutil.move(tmp_name, filepath)
    except Exception as e:
        logger.error(f"Failed to write {filepath}: {e}")
        if 'tmp_name' in locals() and os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise

def load_assignments():
    global telegram_clients
    if os.path.exists(ASSIGNMENTS_FILE):
        try:
            with io_lock:
                with open(ASSIGNMENTS_FILE, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("telegram_clients"), dict):
                    # Accept an older wrapped export format as well as the
                    # current plain Telegram-ID mapping.
                    data = data["telegram_clients"]
                if not isinstance(data, dict):
                    raise ValueError("assignments file must contain a JSON object")
                telegram_clients = {}
                for k, v in data.items():
                    tg_id = int(k)
                    raw_client_ids = v if isinstance(v, list) else [v]
                    client_ids = []
                    for raw_client_id in raw_client_ids:
                        client_id = int(raw_client_id)
                        if client_id > 0 and client_id not in client_ids:
                            client_ids.append(client_id)
                    if tg_id > 0 and client_ids:
                        telegram_clients[tg_id] = client_ids
                total_links = sum(len(client_ids) for client_ids in telegram_clients.values())
                logger.info(
                    "Loaded %s Telegram assignment(s) and %s client link(s) from %s",
                    len(telegram_clients),
                    total_links,
                    ASSIGNMENTS_FILE,
                )
        except Exception as e:
            logger.error("Failed to load assignments from %s: %s", ASSIGNMENTS_FILE, e)
            telegram_clients = {ADMIN_TELEGRAM_ID: [ADMIN_CLIENT_ID]}
        if not telegram_clients:
            # فایل موجود اما خالی (مثلاً بعد از یک خاموشی ناگهانی) — همان پیش‌فرض ادمین
            logger.warning("Assignments file was empty; restoring admin default assignment")
            telegram_clients = {ADMIN_TELEGRAM_ID: [ADMIN_CLIENT_ID]}
            save_assignments()
    else:
        logger.warning("Assignments file not found at %s; using admin default", ASSIGNMENTS_FILE)
        telegram_clients = {ADMIN_TELEGRAM_ID: [ADMIN_CLIENT_ID]}

def save_assignments():
    # Ensure all values are lists before saving
    clean_data = {}
    for tg_id, value in telegram_clients.items():
        if isinstance(value, list):
            clean_data[tg_id] = value
        else:
            clean_data[tg_id] = [value]  # Convert single ID to list
    atomic_json_write(ASSIGNMENTS_FILE, clean_data)

def load_cached_sub_uri():
    global sub_base_url, sub_cache_time
    if os.path.exists(SUB_CACHE_FILE):
        try:
            with io_lock:
                with open(SUB_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    sub_base_url = cache.get("subURI")
                    sub_cache_time = cache.get("timestamp", 0)
                    logger.info("Loaded cached subURI")
        except Exception as e:
            logger.error(f"Failed to load subscription cache: {e}")

def save_cached_sub_uri(sub_uri: str):
    global sub_base_url, sub_cache_time
    sub_base_url = sub_uri.rstrip('/')
    sub_cache_time = datetime.now().timestamp()
    try:
        atomic_json_write(SUB_CACHE_FILE, {"subURI": sub_base_url, "timestamp": sub_cache_time})
    except Exception as e:
        logger.error(f"Failed to save subscription cache: {e}")

# Inbounds cache functions
def load_cached_inbounds():
    global inbounds_cache_time
    if os.path.exists(INBOUNDS_CACHE_FILE):
        try:
            with io_lock:
                with open(INBOUNDS_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    inbounds_cache_time = cache.get("timestamp", 0)
                    logger.info("Loaded cached inbounds")
                    return cache.get("inbounds", [])
        except Exception as e:
            logger.error(f"Failed to load inbounds cache: {e}")
    return []

def save_cached_inbounds(inbounds):
    try:
        atomic_json_write(INBOUNDS_CACHE_FILE, {
            "inbounds": inbounds,
            "timestamp": datetime.now().timestamp()
        })
    except Exception as e:
        logger.error(f"Failed to save inbounds cache: {e}")

async def refresh_server_metadata() -> bool:
    """Refresh subscription URI and inbounds together from ``/apiv2/load``."""
    global inbounds_cache, inbounds_cache_time
    data = await api_client.get('apiv2/load', attempts=1, log_failure=False)
    load_error: ValueError | None = None
    if data:
        try:
            sub_uri, inbounds = extract_load_metadata(data)
            source = "/apiv2/load"
        except ValueError as exc:
            load_error = exc
            data = None
    try:
        if not data:
            settings_data, inbounds_data = await asyncio.gather(
                api_client.get('apiv2/settings'),
                api_client.get('apiv2/inbounds'),
            )
            sub_uri, inbounds = extract_partial_metadata(settings_data, inbounds_data, api_client.base_url)
            source = "/apiv2/settings + /apiv2/inbounds fallback"
    except ValueError as exc:
        if load_error:
            logger.error("S-UI metadata refresh failed (load: %s; fallback: %s)", load_error, exc)
        else:
            logger.error("S-UI metadata refresh failed: %s", exc)
        return False
    save_cached_sub_uri(sub_uri)
    inbounds_cache = inbounds
    inbounds_cache_time = datetime.now().timestamp()
    save_cached_inbounds(inbounds)
    logger.info("Refreshed subURI and %s inbound(s) from %s", len(inbounds), source)
    return True


async def get_inbounds_list(force_refresh=False):
    global inbounds_cache
    now = datetime.now().timestamp()

    if not force_refresh and inbounds_cache is not None and (now - inbounds_cache_time) < INBOUNDS_CACHE_DURATION:
        return inbounds_cache

    try:
        if await refresh_server_metadata():
            return inbounds_cache or []
    except Exception as e:
        logger.error(f"Failed to fetch S-UI metadata: {e}")

    # Fallback to cache file if API fails
    if inbounds_cache is None:
        inbounds_cache = load_cached_inbounds()

    return inbounds_cache if inbounds_cache else []

def get_current_inbounds():
    global inbounds_cache
    return inbounds_cache if inbounds_cache else []

def get_inbound_display_name(inbound_id):
    inbounds = get_current_inbounds()
    for inbound in inbounds:
        if inbound.get("id") == inbound_id:
            tag = inbound.get("tag", f"Inbound {inbound_id}")
            port = inbound.get("listen_port", "")
            if port:
                return f"{tag} ({port})"
            return tag
    return f"Inbound {inbound_id}"

def create_inbounds_keyboard(selected_inbounds=None, prefix="inbound"):
    if selected_inbounds is None:
        selected_inbounds = []

    inbounds = get_current_inbounds()
    keyboard = []

    for inbound in inbounds:
        inbound_id = inbound.get("id")
        tag = inbound.get("tag", f"Inbound {inbound_id}")
        port = inbound.get("listen_port", "")

        # Create display text with selection indicator
        display_text = preserve_dynamic_text(tag)
        if port:
            display_text += f" ({port})"

        if inbound_id in selected_inbounds:
            display_text = "✅ " + display_text

        keyboard.append([InlineKeyboardButton(display_text, callback_data=f'{prefix}_{inbound_id}')])

    # Add control buttons
    keyboard.extend([
        [InlineKeyboardButton("✅ All Inbounds", callback_data=f'{prefix}_all')],
        [InlineKeyboardButton("✔️ Confirm Selection", callback_data=f'{prefix}_done')],
        [InlineKeyboardButton("❌ Abort", callback_data=f'{prefix}_cancel')]
    ])

    return keyboard

async def get_subscription_base_url(force_refresh=False) -> str:
    global sub_base_url
    # UI لینک ساب باید همیشه از پورت 2096 برود (sub-UI با حالت مرورگر) نه 2097 خام.
    override = os.environ.get("SUB_BASE_URL_OVERRIDE", "").strip().rstrip('/')
    if override:
        return override
    now = datetime.now().timestamp()
    if not force_refresh and sub_base_url and (now - sub_cache_time) < SUB_CACHE_DURATION:
        return sub_base_url
    if await refresh_server_metadata() and sub_base_url:
        return sub_base_url
    if sub_base_url:
        return sub_base_url
    raise RuntimeError("S-UI did not provide a subscription URI and no cached URI is available")

async def refresh_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)

    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return

    # Show loading message
    await query.edit_message_text("⏳ Updating SUB Link & Inbounds...")

    # Refresh subscription URL
    new_uri = await get_subscription_base_url(force_refresh=True)

    # Refresh inbounds list
    inbounds = await get_inbounds_list(force_refresh=True)

    if inbounds:
        inbound_count = len(inbounds)
        inbound_names = ", ".join([get_inbound_display_name(inbound.get("id")) for inbound in inbounds[:3]])
        if inbound_count > 3:
            inbound_names += f" & {inbound_count - 3} Other Inbounds"

        msg = (f"✅ Successful Update\n\n"
               f"🔗 SUB Link: {new_uri}\n\n"
               f"📡 Inbounds: {inbound_count} Inbounds found\n"
               f"📋 Include: {inbound_names}")
    else:
        msg = (f"⚠️ Update Error occurred\n\n"
               f"🔗 Sub Link: {new_uri}\n\n"
               f"❌ Couldn't Update Inbounds.\n"
               f"Using Saved Cache.")

    keyboard = [[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

def rate_limited(admin_only=False, track_metrics=True):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            start_time = datetime.now().timestamp()
            command = func.__name__
            if admin_only and not is_admin(user_id):
                if update.message:
                    await update.message.reply_text("❌ Admin Only")
                elif update.callback_query:
                    await localized_query_answer(update.callback_query, "❌ Admin Only", show_alert=True)
                return
            if not await rate_limiter.check_rate_limit(user_id):
                remaining_block = await rate_limiter.get_block_status(user_id)
                if remaining_block:
                    minutes_left = int(remaining_block / 60) + 1
                    reply_text = f"⏰ You've Been Blocked For Spamming The Bot\nPlease wait {minutes_left} Minutes."
                else:
                    reply_text = "❌ Do not spam the bot."
                if update.message:
                    await update.message.reply_text(reply_text)
                elif update.callback_query:
                    await localized_query_answer(update.callback_query, reply_text, show_alert=True)
                return
            try:
                result = await func(update, context)
                if track_metrics:
                    response_time = datetime.now().timestamp() - start_time
                    metrics.record_command(user_id, command, response_time)
                return result
            except Exception:
                logger.exception(f"Error in {func.__name__}")
                metrics.record_error(user_id)
                if update.message:
                    await update.message.reply_text("❌ Unexpected error occurred. Please try again.")
                elif update.callback_query:
                    await localized_query_answer(update.callback_query, "❌ Unexpected error occurred. Please try again.", show_alert=True)
        return wrapper
    return decorator


def format_client_timestamp(value: object) -> str | None:
    """Format an S-UI Unix timestamp in the administrator's selected zone."""
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    try:
        zone_name, _ = ADMIN_TIMEZONES[ADMIN_TIMEZONE]
        local_time = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(ZoneInfo(zone_name))
        offset = local_time.strftime("%z")
        offset_text = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
        return f"{local_time:%Y-%m-%d %H:%M:%S} {offset_text}"
    except (OSError, OverflowError, ValueError):
        return None


def format_client(client: dict, is_admin: bool = False, user_id: int | None = None) -> str:
    uid = user_id if user_id is not None else ADMIN_TELEGRAM_ID
    name = preserve_dynamic_text(client["name"]) if client.get("name") else "Unknown"
    volume = client.get("volume", 0)
    up = client.get("up", 0)
    down = client.get("down", 0)
    expiry = client.get("expiry", 0)
    enable = tr(uid, "enabled") if client.get("enable", False) else tr(uid, "disabled")

    lines = []
    if is_admin:
        lines.append(f"{tr(uid, 'user')}: {name} (ID: {client.get('id', 'N/A')})")
    else:
        lines.append(f"{tr(uid, 'user')}: {name}")

    lines.append(f"{tr(uid, 'status')}: {enable}")

    # نوار مصرف بصری — قلب کارت
    total_used = up + down
    if volume > 0:
        lines.append(f"📊 {usage_bar(total_used, volume)}")
        lines.append(f"{tr(uid, 'total_usage')}: {format_bytes(total_used)} از {format_bytes(volume)}")
    else:
        lines.append(f"{tr(uid, 'total_usage')}: {format_bytes(total_used)} ({tr(uid, 'unlimited')})")
    lines.append(f"⬆️ {format_bytes(up)} | ⬇️ {format_bytes(down)}")

    if expiry == 0 and client.get("delayStart") and not client.get("autoReset"):
        expiry_text = tr(uid, "delayed_expiry_value", days=max(1, int(client.get("resetDays", 0) or 0)))
    else:
        expiry_text = localized_remaining_time(expiry, uid)
        jalali = jalali_date_long(expiry)
        if jalali:
            expiry_text += f" ({jalali})"
    lines.append(f"{tr(uid, 'expiry')}: {expiry_text}")

    if is_admin:
        description = preserve_dynamic_text(client["desc"]) if client.get("desc") else "N/A"
        group = preserve_dynamic_text(client["group"]) if client.get("group") else "N/A"
        remark = preserve_dynamic_text(client["remark"]) if client.get("remark") else "N/A"
        created_at = format_client_timestamp(client.get("createdAt")) or "N/A"
        last_online = format_client_timestamp(client.get("onlineAt")) or tr(uid, "never")
        lines.append(f"{tr(uid, 'description')}: {description}")
        lines.append(f"{tr(uid, 'group')}: {group}")
        lines.append(f"{tr(uid, 'remark')}: {remark}")
        lines.append(f"{tr(uid, 'created_at')}: {preserve_dynamic_text(created_at)}")
        lines.append(f"{tr(uid, 'last_online')}: {preserve_dynamic_text(last_online)}")

    return "\n".join(lines)

async def get_client_usage(client_id: int) -> str:
    try:
        data = await api_client.get('apiv2/clients', {'id': client_id})
        if not data:
            return f"❌ Server unresponsive. Try Again Later.{api_client.error_reason('apiv2/clients')}"
        clients = sui_clients(data)
        if clients is None:
            return "❌ Invalid response from server. Try Again Later."
        if not clients:
            return "❌ User Not Found"
        client = clients[0]
        return format_client(client)
    except Exception as e:
        logger.exception("Error in get_client_usage")
        return f"❌ Error: {e}"

async def get_all_clients_list():
    global clients_cache, clients_cache_time
    now = datetime.now().timestamp()
    if clients_cache is not None and (now - clients_cache_time) < CLIENTS_CACHE_DURATION:
        return clients_cache
    try:
        data = await api_client.get('apiv2/clients')
        if not data:
            return clients_cache if clients_cache else []
        clients = sui_clients(data)
        if clients is None:
            logger.warning("S-UI clients response was malformed; retaining the previous cache")
            return clients_cache if clients_cache else []
        clients_cache = clients
        clients_cache_time = now
        return clients_cache
    except Exception:
        logger.exception("Error in get_all_clients_list")
        return clients_cache if clients_cache else []

async def get_client_map():
    clients = await get_all_clients_list()
    return {client.get("id"): client for client in clients if client.get("id") is not None}

def build_client_to_tg_index():
    client_to_tg = defaultdict(list)
    for tg_id, assigned in telegram_clients.items():
        if isinstance(assigned, list):
            for client_id in assigned:
                client_to_tg[client_id].append(tg_id)
        elif assigned is not None:
            client_to_tg[assigned].append(tg_id)
    return client_to_tg

def user_has_client_access(tg_id: int, client_id: int) -> bool:
    return can_access_client(tg_id, client_id, telegram_clients, ADMIN_TELEGRAM_ID)

def get_renewal_month_options() -> List[int]:
    return list(renewal_month_options)

def set_renewal_month_options(new_options: List[int]) -> List[int]:
    global renewal_month_options
    renewal_month_options = parse_renewal_month_options(",".join(map(str, new_options)))
    save_runtime_setting("RENEWAL_MONTH_OPTIONS", ",".join(map(str, renewal_month_options)), RUNTIME_SETTINGS_FILE)
    return renewal_month_options

def cleanup_pending_renew_requests():
    now_ts = datetime.now(timezone.utc).timestamp()
    stale_keys = []
    for req_id, req in pending_renew_requests.items():
        created_at_raw = req.get("created_at")
        created_ts = None
        if isinstance(created_at_raw, (int, float)):
            created_ts = float(created_at_raw)
        elif isinstance(created_at_raw, str):
            try:
                created_ts = datetime.fromisoformat(created_at_raw).timestamp()
            except Exception:
                created_ts = None
        if created_ts is None or (now_ts - created_ts) > RENEW_REQUEST_TTL_SECONDS:
            stale_keys.append(req_id)
    for req_id in stale_keys:
        pending_renew_requests.pop(req_id, None)
    if stale_keys:
        logger.info(f"Cleaned {len(stale_keys)} stale renewal request(s)")

def renewal_amount(months: int, user_id: int | None = None) -> int:
    """قیمت تمدید: جدول قیمت ماهانه (اگر فعال) → قیمت پایه → تخفیف/اضافهٔ اختصاصی مشتری."""
    pricing_price = _plans_store.pricing.price_for_renewal(months)
    amount = pricing_price if pricing_price is not None else RENEWAL_MONTHLY_PRICE * months
    if user_id is not None:
        amount = _plans_store.pricing.adjust_for_user(user_id, amount)
    return amount


def format_money(amount: int) -> str:
    return f"{amount:,} {PAYMENT_CURRENCY}"


def payment_price_steps() -> tuple[int, int]:
    return (10_000, 50_000) if PAYMENT_CURRENCY == "TOMAN" else (1, 10)


def localized_currency_name(user_id: int, currency: str | None = None) -> str:
    selected = (currency or PAYMENT_CURRENCY).lower()
    return tr(user_id, f"currency_{selected}")

def build_settings_menu_text() -> str:
    return (
        "⚙️ Admin Settings\n\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'settings_choose_category')}"
    )


def build_payment_settings_text() -> str:
    months_text = ", ".join(str(m) for m in get_renewal_month_options())
    holder_line = (
        f"\n{tr(ADMIN_TELEGRAM_ID, 'payment_card_holder', holder=preserve_dynamic_text(PAYMENT_CARD_HOLDER))}"
        if PAYMENT_CARD_HOLDER else ""
    )
    card_number = preserve_dynamic_text(ltr_isolate(PAYMENT_CARD_NUMBER))
    return (
        f"{tr(ADMIN_TELEGRAM_ID, 'payments_and_renewal')}\n\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'price_per_month_value', amount=preserve_dynamic_text(format_money(RENEWAL_MONTHLY_PRICE)))}\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'payment_currency_value', currency=localized_currency_name(ADMIN_TELEGRAM_ID))}\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'enabled_renewal_options', options=preserve_dynamic_text(months_text))}\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'payment_card_number', number=card_number)}{holder_line}"
    )


def build_admin_tools_settings_text() -> str:
    display_name = preserve_dynamic_text(BOT_DISPLAY_NAME)
    port_status = tr(
        ADMIN_TELEGRAM_ID,
        "subscription_port_hidden" if HIDE_SUBSCRIPTION_PORT else "subscription_port_kept",
    )
    web_panel_status_key = (
        "web_panel_enabled"
        if WEB_PANEL_ENABLED and WEB_PANEL_BASE_URL
        else "web_panel_pending"
        if WEB_PANEL_ENABLED
        else "web_panel_disabled"
    )
    return (
        f"{tr(ADMIN_TELEGRAM_ID, 'administration')}\n\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'display_name_label')}: {display_name}\n"
        f"🕐 Administrative Timezone: {ADMIN_TIMEZONES[ADMIN_TIMEZONE][1]}\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'subscription_link_mode')}: {port_status}\n\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'web_panel_setting')}: {tr(ADMIN_TELEGRAM_ID, web_panel_status_key)}\n\n"
        f"{tr(ADMIN_TELEGRAM_ID, 'guide_admin_count')}: {len(connection_guide_store.list_guides())}"
    )

def build_settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "payments_and_renewal"), callback_data='settings_payments')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "administration"), callback_data='settings_admin_tools')],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')],
    ])


def build_payment_settings_keyboard():
    small_step, large_step = payment_price_steps()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "renewal_plans"), callback_data='settings_plans')],
        [InlineKeyboardButton(f"➖ {small_step:,}", callback_data=f'settings_price_minus_{small_step}'), InlineKeyboardButton(f"➕ {small_step:,}", callback_data=f'settings_price_plus_{small_step}')],
        [InlineKeyboardButton(f"➖ {large_step:,}", callback_data=f'settings_price_minus_{large_step}'), InlineKeyboardButton(f"➕ {large_step:,}", callback_data=f'settings_price_plus_{large_step}')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "set_currency"), callback_data='settings_currency')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "set_exact_monthly_price"), callback_data='settings_set_monthly_price')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "set_card_number"), callback_data='settings_set_card_number')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "set_card_holder"), callback_data='settings_set_card_holder')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "reset_plans"), callback_data='settings_plans_reset')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "back_to_settings"), callback_data='admin_settings')],
    ])


def build_admin_tools_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "set_display_name"), callback_data='settings_set_display_name')],
        [InlineKeyboardButton("🕐 Set Administrative Timezone", callback_data='settings_timezone')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "connection_guides_title"), callback_data='settings_connection_guides')],
        [InlineKeyboardButton(
            tr(
                ADMIN_TELEGRAM_ID,
                "keep_subscription_port" if HIDE_SUBSCRIPTION_PORT else "remove_subscription_port",
            ),
            callback_data='settings_subscription_port',
        )],
        [InlineKeyboardButton(
            tr(ADMIN_TELEGRAM_ID, "disable_web_panel" if WEB_PANEL_ENABLED else "enable_web_panel"),
            callback_data='settings_web_panel',
        )],
        [InlineKeyboardButton("💾 Backup & Restore", callback_data='settings_backup_restore')],
        [InlineKeyboardButton("👥 مدیران", callback_data='settings_admins')],
        [InlineKeyboardButton("🔙 Back To Settings", callback_data='admin_settings')],
    ])

def build_admins_text() -> str:
    """متن صفحه مدیریت ادمین‌ها — HTML (با parse_mode فرستاده می‌شود)."""
    lines = ["👥 <b>مدیران ربات</b>\n"]
    for aid in _admin_registry.all_admins:
        if aid == ADMIN_TELEGRAM_ID:
            lines.append(f"• <code>{aid}</code> ⭐ (اصلی — بدون محدودیت)")
            continue
        groups = _admin_registry.groups_of(aid)
        if groups is None:
            scope = "بدون محدودیت"
        elif groups:
            scope = "گروه‌ها: " + ", ".join(preserve_dynamic_text(g) for g in groups)
        else:
            scope = "بدون دسترسی (قفل)"
        lines.append(f"• <code>{aid}</code> — {scope}")
    lines.append("\nفقط ادمین اصلی می‌تواند ادمین اضافه/حذف کند یا دسترسی گروهی بدهد.")
    return "\n".join(lines)


def build_admins_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ افزودن ادمین", callback_data="settings_admin_add")]]
    if user_id == ADMIN_TELEGRAM_ID:
        for aid in _admin_registry.all_admins:
            if aid != ADMIN_TELEGRAM_ID:
                rows.append([
                    InlineKeyboardButton(f"🗂 گروه‌ها {aid}", callback_data=f"settings_admin_groups_{aid}"),
                    InlineKeyboardButton(f"🗑 حذف {aid}", callback_data=f"settings_admin_del_{aid}"),
                ])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="settings_admin_tools")])
    return InlineKeyboardMarkup(rows)


def build_admin_groups_text(state: dict) -> str:
    label = "بدون محدودیت" if state.get("unrestricted") else ("، ".join(state["sel"]) or "—")
    return (
        "🗂 <b>دسترسی گروهی ادمین</b>\n\n"
        f"• ادمین: <code>{state['admin']}</code>\n"
        f"• وضعیت فعلی: {preserve_dynamic_text(label)}\n\n"
        "با ✅/⬜ گروه‌ها را انتخاب کنید؛ «💾 ذخیره» ادمین را فقط به انتخاب‌ها "
        "محدود می‌کند. «♾ بدون محدودیت» دسترسی کامل را برمی‌گرداند."
    )


def build_admin_groups_keyboard(state: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, g in enumerate(state["all"]):
        mark = "✅" if g in state["sel"] else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {preserve_dynamic_text(g)}", callback_data=f"settings_admin_groups_toggle_{i}")])
    if not state["all"]:
        rows.append([InlineKeyboardButton("⚠️ پنل هیچ گروه‌ای ندارد", callback_data="settings_admin_groups_none")])
    rows.append([
        InlineKeyboardButton("💾 ذخیره دسترسی", callback_data="settings_admin_groups_save"),
        InlineKeyboardButton("♾ بدون محدودیت", callback_data="settings_admin_groups_unlimited"),
    ])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="settings_admins")])
    return InlineKeyboardMarkup(rows)


def build_admin_add_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو", callback_data="settings_admins")],
    ])

def build_backup_restore_keyboard():    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 ساخت و ارسال بکاپ", callback_data='settings_backup_create')],
        [InlineKeyboardButton("📥 راهنمای بازگردانی", callback_data='settings_backup_help')],
        [InlineKeyboardButton("🔙 Back", callback_data='settings_admin_tools')],
    ])


def build_connection_guides_admin_text(user_id: int) -> str:
    status = tr(user_id, "guide_enabled" if connection_guide_store.enabled else "guide_disabled")
    return (
        f"{tr(user_id, 'guide_admin_title')}\n\n"
        f"{tr(user_id, 'guide_admin_status')}: {status}\n"
        f"{tr(user_id, 'guide_admin_count')}: {len(connection_guide_store.list_guides())}\n\n"
        f"{tr(user_id, 'guide_admin_help')}"
    )


def build_connection_guides_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    toggle_key = "guide_disable" if connection_guide_store.enabled else "guide_enable"
    rows = [
        [InlineKeyboardButton(tr(user_id, toggle_key), callback_data="settings_guides_toggle")],
        [InlineKeyboardButton(tr(user_id, "guide_add"), callback_data="settings_guides_add")],
    ]
    if connection_guide_store.list_guides():
        rows.append([InlineKeyboardButton(tr(user_id, "guide_edit"), callback_data="settings_guides_edit")])
        rows.append([InlineKeyboardButton(tr(user_id, "guide_delete"), callback_data="settings_guides_delete")])
    rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="settings_admin_tools")])
    return InlineKeyboardMarkup(rows)


def build_connection_guides_user_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(preserve_dynamic_text(guide["title"]), callback_data=f"connection_guide_{guide['id']}")]
        for guide in connection_guide_store.list_guides()
    ]
    rows.append([InlineKeyboardButton(tr(user_id, "main_menu"), callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def guide_item_label(index: int, item: dict[str, str]) -> str:
    kind = item["type"].capitalize()
    content = item.get("text") or item.get("caption") or ""
    preview = " ".join(content.split())[:28]
    suffix = f" — {preview}" if preview else ""
    return preserve_dynamic_text(f"{index + 1}. {kind}{suffix}")


def build_connection_guide_edit_text(user_id: int, guide: dict[str, Any]) -> str:
    return (
        f"{tr(user_id, 'guide_edit')}: {preserve_dynamic_text(guide['title'])}\n\n"
        f"{tr(user_id, 'guide_items')}: {len(guide['messages'])}"
    )


def build_connection_guide_edit_keyboard(user_id: int, guide: dict[str, Any]) -> InlineKeyboardMarkup:
    guide_id = guide["id"]
    rows = [[InlineKeyboardButton(tr(user_id, "guide_edit_title"), callback_data=f"settings_guides_title_{guide_id}")]]
    rows.extend([
        [InlineKeyboardButton(guide_item_label(index, item), callback_data=f"settings_guides_item_{guide_id}_{index}")]
        for index, item in enumerate(guide["messages"])
    ])
    if len(guide["messages"]) < MAX_MESSAGES_PER_GUIDE:
        rows.append([InlineKeyboardButton(tr(user_id, "guide_add_item"), callback_data=f"settings_guides_append_{guide_id}")])
    rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="settings_guides_edit")])
    return InlineKeyboardMarkup(rows)


def build_connection_guide_item_keyboard(user_id: int, guide_id: str, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(user_id, "guide_replace_item"), callback_data=f"settings_guides_replace_{guide_id}_{index}")],
        [InlineKeyboardButton(tr(user_id, "guide_delete_item"), callback_data=f"settings_guides_item_delete_{guide_id}_{index}")],
        [InlineKeyboardButton(tr(user_id, "back"), callback_data=f"settings_guides_edit_{guide_id}")],
    ])

def bot_state_paths() -> dict[str, str]:
    return {
        "assignments": ASSIGNMENTS_FILE,
        "metrics": METRICS_FILE,
        "languages": LANGUAGE_STORE_FILE,
        "runtime_settings": RUNTIME_SETTINGS_FILE,
        "subscription_cache": SUB_CACHE_FILE,
        "inbounds_cache": INBOUNDS_CACHE_FILE,
        "expired_notifications": EXPIRED_NOTIFICATIONS_FILE,
        "connection_guides": CONNECTION_GUIDES_FILE,
    }

def backup_configuration_summary() -> dict[str, Any]:
    return {
        "admin_telegram_id": ADMIN_TELEGRAM_ID,
        "admin_client_id": ADMIN_CLIENT_ID,
        "sui_host": SUI_HOST,
        "database_name": DB_NAME,
        "backup_max_bytes": BACKUP_MAX_BYTES,
        "items_per_page": ITEMS_PER_PAGE,
        "rate_limit_window": RATE_LIMIT_WINDOW,
        "max_requests_per_window": MAX_REQUESTS_PER_WINDOW,
        "bot_display_name": BOT_DISPLAY_NAME,
        "admin_timezone": ADMIN_TIMEZONE,
        "payment_currency": PAYMENT_CURRENCY,
        "secrets_included": False,
        "secret_notice": "BOT_TOKEN and SUI_TOKEN are intentionally excluded",
    }

async def send_state_backup(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    async with bot_backup_lock:
        await asyncio.to_thread(save_assignments)
        await asyncio.to_thread(metrics.save_metrics)
        with tempfile.TemporaryDirectory(prefix="sui-bot-backup-") as temporary_dir:
            filename = f"sui-bot-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sui-backup.json"
            destination = Path(temporary_dir) / filename
            bundle = await asyncio.to_thread(build_bundle, bot_state_paths(), backup_configuration_summary())
            await asyncio.to_thread(write_bundle, bundle, destination)
            with destination.open("rb") as backup_file:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=backup_file,
                    filename=filename,
                    caption=(
                        "✅ SUI Bot backup created.\n\n"
                        "Keep this file private. Restore it with /restore.\n"
                        "Bot and S-UI tokens are not included."
                    ),
                )

def reload_restored_state() -> None:
    global inbounds_cache, RENEWAL_MONTHLY_PRICE, RENEWAL_MONTH_OPTIONS
    global PAYMENT_CARD_NUMBER, PAYMENT_CARD_HOLDER, BOT_DISPLAY_NAME, HIDE_SUBSCRIPTION_PORT, WEB_PANEL_ENABLED
    global ADMIN_TIMEZONE, PAYMENT_CURRENCY
    global renewal_month_options
    load_assignments()
    language_store.load()
    metrics.load_metrics()
    connection_guide_store.load()
    load_cached_sub_uri()
    inbounds_cache = load_cached_inbounds()
    restored_settings = load_runtime_settings(RUNTIME_SETTINGS_FILE)
    RUNTIME_SETTINGS.clear()
    RUNTIME_SETTINGS.update(restored_settings)
    RENEWAL_MONTHLY_PRICE = int(restored_settings.get("RENEWAL_MONTHLY_PRICE", SETTINGS.renewal_monthly_price))
    RENEWAL_MONTH_OPTIONS = str(restored_settings.get("RENEWAL_MONTH_OPTIONS", SETTINGS.renewal_month_options))
    PAYMENT_CARD_NUMBER = str(restored_settings.get("PAYMENT_CARD_NUMBER", SETTINGS.payment_card_number))
    PAYMENT_CARD_HOLDER = str(restored_settings.get("PAYMENT_CARD_HOLDER", SETTINGS.payment_card_holder))
    BOT_DISPLAY_NAME = validate_display_name(restored_settings.get("BOT_DISPLAY_NAME", SETTINGS.bot_display_name))
    HIDE_SUBSCRIPTION_PORT = str(
        restored_settings.get("HIDE_SUBSCRIPTION_PORT", SETTINGS.hide_subscription_port)
    ).strip().lower() in {"1", "true", "yes", "on"}
    WEB_PANEL_ENABLED = str(restored_settings.get("WEB_PANEL_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    restored_timezone = str(restored_settings.get("ADMIN_TIMEZONE", "UTC")).upper()
    ADMIN_TIMEZONE = restored_timezone if restored_timezone in ADMIN_TIMEZONES else "UTC"
    restored_currency = str(restored_settings.get("PAYMENT_CURRENCY", "TOMAN")).upper()
    PAYMENT_CURRENCY = restored_currency if restored_currency in PAYMENT_CURRENCIES else "TOMAN"
    renewal_month_options = parse_renewal_month_options(RENEWAL_MONTH_OPTIONS)

@rate_limited(admin_only=True)
async def restore_backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only")
        return ConversationHandler.END
    await update.message.reply_text(
        "📥 Send the `.sui-backup.json` file now.\n\n"
        "The file will be validated before any state is replaced.\n"
        "Cancel: /cancel"
    )
    return RESTORE_BACKUP_FILE

async def restore_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    document = update.message.document
    if document is None:
        await update.message.reply_text("❌ Send the backup as a document, or use /cancel.")
        return RESTORE_BACKUP_FILE
    if document.file_size and document.file_size > MAX_BUNDLE_BYTES:
        await update.message.reply_text("❌ Backup file is too large.")
        return RESTORE_BACKUP_FILE
    status = await update.message.reply_text("⏳ Validating and restoring backup...")
    try:
        async with bot_backup_lock:
            with tempfile.TemporaryDirectory(prefix="sui-bot-restore-") as temporary_dir:
                source = Path(temporary_dir) / "uploaded.sui-backup.json"
                telegram_file = await context.bot.get_file(document.file_id)
                await telegram_file.download_to_drive(custom_path=str(source))
                bundle = await asyncio.to_thread(load_bundle, source)
                restored = await asyncio.to_thread(restore_bundle, bundle, bot_state_paths())
            await asyncio.to_thread(reload_restored_state)
        await status.edit_text(
            "✅ Backup restored successfully.\n\n"
            f"Restored sections: {', '.join(restored) or 'none'}\n"
            "The restored assignments and settings are active."
        )
        context.user_data.clear()
        return ConversationHandler.END
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, TelegramError) as exc:
        logger.warning("Rejected backup restore from admin %s: %s", update.effective_user.id, exc)
        await status.edit_text(f"❌ Restore failed: {str(exc)[:500]}")
        return RESTORE_BACKUP_FILE

async def restore_backup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Backup restore canceled.", reply_markup=build_settings_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

def build_settings_plans_keyboard():
    enabled = set(get_renewal_month_options())
    rows = []
    for m in range(1, 13):
        icon = "✅" if m in enabled else "⬜"
        rows.append([InlineKeyboardButton(f"{icon} {m} Month", callback_data=f'settings_plan_toggle_{m}')])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data='settings_payments')])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# مدیریت پلن‌ها و قیمت‌گذاری (فروشگاه)
# ---------------------------------------------------------------------------
def build_plans_admin_text() -> str:
    store = _plans_store
    p = store.pricing
    mode_names = {"manual": "دستی هر پلن", "per_gb": "هر گیگ", "monthly": "ماهانه"}
    pricing_line = {
        "manual": "قیمت هر پلن دستی ثبت شده است",
        "per_gb": f"هر گیگ: {format_money(p.per_gb_toman)}",
        "monthly": f"هر ماه: {format_money(p.monthly_toman)}" + (
            f" | اختصاصی: {', '.join(f'{m}م={format_money(int(v))}' for m, v in sorted(p.month_prices.items(), key=lambda x: int(x[0])))}"
            if p.month_prices else ""
        ),
    }[p.mode]
    lines = [
        "📦 مدیریت پلن‌های فروشگاه",
        "",
        f"💰 حالت قیمت‌گذاری: {mode_names[p.mode]} — {pricing_line}",
        f"تعداد پلن‌ها: {len(store.plans)}",
        "",
    ]
    for plan in store.plans:
        status = "🟢" if plan.active else "⚪️"
        tag = " (نمایندگی)" if plan.reseller_only else ""
        price = format_money(store.pricing.price_for_plan(plan)) if plan.price_toman == 0 else format_money(plan.price_toman)
        lines.append(f"{status} {plan.title} — {plan.gb} گیگ / {plan.days} روز — {price}{tag}")
    return "\n".join(lines)


def build_plans_admin_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for plan in _plans_store.plans:
        rows.append([
            InlineKeyboardButton(f"✏️ {plan.title}", callback_data=f'plan_edit_{plan.slug}'),
            InlineKeyboardButton("🗑", callback_data=f'plan_del_{plan.slug}'),
            InlineKeyboardButton("▶️" if plan.active else "⏸", callback_data=f'plan_toggle_{plan.slug}'),
        ])
    rows.append([InlineKeyboardButton("➕ پلن جدید", callback_data='plan_add')])
    rows.append([InlineKeyboardButton("💰 تنظیم قیمت‌گذاری", callback_data='pricing_menu')])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data='admin_settings')])
    return InlineKeyboardMarkup(rows)


def build_pricing_menu() -> tuple[str, InlineKeyboardMarkup]:
    p = _plans_store.pricing
    icons = {"manual": "🧾", "per_gb": "📏", "monthly": "📅"}
    text = (
        "💰 قیمت‌گذاری پلن‌ها\n\n"
        f"حالت فعلی: {p.mode}\n\n"
        "• manual → قیمت هر پلن جداگانه ثبت می‌شود\n"
        "• per_gb → قیمت = حجم × قیمت هر گیگ\n"
        "• monthly → قیمت ماهانه؛ برای هر مدت می‌توانی قیمت جدا بگذاری\n"
    )
    rows = [
        [InlineKeyboardButton(f"{icons['manual']} دستی هر پلن", callback_data='pricing_mode_manual'),
         InlineKeyboardButton(f"{icons['per_gb']} هر گیگ", callback_data='pricing_mode_per_gb')],
        [InlineKeyboardButton(f"{icons['monthly']} ماهانه", callback_data='pricing_mode_monthly')],
        [InlineKeyboardButton("👤 قیمت اختصاصی مشتری", callback_data='pricing_user_menu')],
    ]
    if p.mode == "per_gb":
        rows.append([InlineKeyboardButton(f"✏️ قیمت هر گیگ: {fa_num(p.per_gb_toman)}", callback_data='pricing_set_per_gb')])
    if p.mode == "monthly":
        rows.append([InlineKeyboardButton(f"✏️ قیمت پایه هر ماه: {fa_num(p.monthly_toman)}", callback_data='pricing_set_monthly')])
        if p.month_prices:
            for m, v in sorted(p.month_prices.items(), key=lambda x: int(x[0])):
                rows.append([InlineKeyboardButton(
                    f"🗑 قیمت {fa_num(m)} ماه: {fa_num(v)} (حذف)",
                    callback_data=f'pricing_del_month_{m}',
                )])
        for m in range(1, 13):
            if str(m) not in p.month_prices:
                rows.append([InlineKeyboardButton(f"➕ قیمت اختصاصی {fa_num(m)} ماه", callback_data=f'pricing_set_month_{m}')])
    rows.append([InlineKeyboardButton("🔙 بازگشت به پلن‌ها", callback_data='settings_plans')])
    return text, InlineKeyboardMarkup(rows)


def build_user_pricing_menu() -> tuple[str, InlineKeyboardMarkup]:
    """فهرست مشتری‌هایی که قیمت اختصاصی دارند."""
    p = _plans_store.pricing
    entries = sorted(set(list(p.user_discounts.keys()) + list(p.user_prices.keys())))
    text = (
        "👤 قیمت اختصاصی مشتری‌ها\n\n"
        "برای تنظیم، آیدی عددی تلگرام مشتری را انتخاب/وارد کن.\n"
        "می‌توانی درصد تخفیف/اضافه بدهی یا قیمت مطلق یک پلن را ثابت کنی.\n"
    )
    rows = []
    for uid in entries:
        summary = _plans_store.user_pricing_summary(int(uid)) or ""
        rows.append([
            InlineKeyboardButton(f"✏️ {uid} — {summary[:32]}", callback_data=f'uprice_edit_{uid}'),
            InlineKeyboardButton("🗑", callback_data=f'uprice_clear_{uid}'),
        ])
    rows.append([InlineKeyboardButton("➕ مشتری جدید", callback_data='uprice_new')])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data='pricing_menu')])
    return text, InlineKeyboardMarkup(rows)

def normalize_card_number(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 12 or len(digits) > 19:
        return None
    groups = [digits[i:i+4] for i in range(0, len(digits), 4)]
    return "-".join(groups)

async def settings_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Only admin", show_alert=True)
        return ConversationHandler.END

    if query.data == "settings_set_card_number":
        await query.edit_message_text(
            "💳 Enter new card number.\n"
            "You can send digits with or without dashes.\n\n"
            "Cancel: /cancel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='settings_payments')]])
        )
        return SETTINGS_CARD_NUMBER

    if query.data == "settings_set_monthly_price":
        await query.edit_message_text(
            tr(
                query.from_user.id,
                "monthly_price_prompt",
                currency=preserve_dynamic_text(PAYMENT_CURRENCY),
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(tr(query.from_user.id, "back"), callback_data='settings_payments')
            ]]),
        )
        return SETTINGS_MONTHLY_PRICE

    if query.data == "settings_set_card_holder":
        await query.edit_message_text(
            "👤 Enter new card holder name.\n"
            "Use `-` to clear holder name.\n\n"
            "Cancel: /cancel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='settings_payments')]])
        )
        return SETTINGS_CARD_HOLDER

    if query.data == "settings_set_display_name":
        await query.edit_message_text(
            tr(query.from_user.id, "enter_display_name"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr(query.from_user.id, "back"), callback_data='settings_admin_tools')]])
        )
        return SETTINGS_DISPLAY_NAME

    return ConversationHandler.END

async def settings_card_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAYMENT_CARD_NUMBER
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    value = normalize_card_number(update.message.text.strip())
    if not value:
        await update.message.reply_text("❌ Invalid card number. Enter 12-19 digits.")
        return SETTINGS_CARD_NUMBER

    PAYMENT_CARD_NUMBER = value
    save_runtime_setting("PAYMENT_CARD_NUMBER", PAYMENT_CARD_NUMBER, RUNTIME_SETTINGS_FILE)
    await update.message.reply_text(
        f"✅ Card number updated to:\n`{PAYMENT_CARD_NUMBER}`",
        parse_mode='Markdown',
        reply_markup=build_payment_settings_keyboard()
    )
    return ConversationHandler.END

async def settings_card_holder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAYMENT_CARD_HOLDER
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    value = update.message.text.strip()
    if value == "-":
        value = ""
    if len(value) > 80:
        await update.message.reply_text("❌ Holder name too long (max 80 chars).")
        return SETTINGS_CARD_HOLDER

    PAYMENT_CARD_HOLDER = value
    save_runtime_setting("PAYMENT_CARD_HOLDER", PAYMENT_CARD_HOLDER, RUNTIME_SETTINGS_FILE)
    holder_text = PAYMENT_CARD_HOLDER if PAYMENT_CARD_HOLDER else "(empty)"
    await update.message.reply_text(
        f"✅ Card holder updated:\n`{md_escape(holder_text)}`",
        parse_mode='Markdown',
        reply_markup=build_payment_settings_keyboard()
    )
    return ConversationHandler.END


async def settings_display_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISPLAY_NAME
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        value = validate_display_name(update.message.text)
    except RuntimeError:
        await update.message.reply_text(tr(update.effective_user.id, "display_name_invalid"))
        return SETTINGS_DISPLAY_NAME

    BOT_DISPLAY_NAME = value
    save_runtime_setting("BOT_DISPLAY_NAME", BOT_DISPLAY_NAME, RUNTIME_SETTINGS_FILE)
    await update.message.reply_text(
        tr(update.effective_user.id, "display_name_updated", name=preserve_dynamic_text(BOT_DISPLAY_NAME)),
        reply_markup=build_admin_tools_settings_keyboard(),
    )
    return ConversationHandler.END

async def settings_card_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Settings edit canceled.", reply_markup=build_settings_menu_keyboard())
    return ConversationHandler.END


async def settings_navigation_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """End an abandoned settings editor before navigating elsewhere."""
    query = update.callback_query
    await localized_query_answer(query)
    if query.data == 'settings_payments':
        await query.edit_message_text(build_payment_settings_text(), reply_markup=build_payment_settings_keyboard())
    elif query.data == 'settings_admin_tools':
        await query.edit_message_text(
            build_admin_tools_settings_text(), reply_markup=build_admin_tools_settings_keyboard()
        )
    elif query.data == 'admin_settings':
        await query.edit_message_text(build_settings_menu_text(), reply_markup=build_settings_menu_keyboard())
    else:
        await query.edit_message_text(
            tr(query.from_user.id, "welcome"),
            reply_markup=get_main_menu_keyboard(True, query.from_user.id),
        )
    return ConversationHandler.END


async def connection_guide_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data.pop("new_connection_guide", None)
    await query.edit_message_text(tr(query.from_user.id, "guide_enter_title"))
    return CONNECTION_GUIDE_TITLE


async def connection_guide_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    title = " ".join((update.message.text or "").split())
    if not title or len(title) > MAX_TITLE_LENGTH:
        await update.message.reply_text(tr(update.effective_user.id, "guide_enter_title"))
        return CONNECTION_GUIDE_TITLE
    context.user_data["new_connection_guide"] = {"title": title, "messages": []}
    await update.message.reply_text(tr(update.effective_user.id, "guide_send_content"))
    return CONNECTION_GUIDE_CONTENT


def connection_guide_message_payload(message) -> dict[str, str] | None:
    if message.text:
        return {"type": "text", "text": message.text}
    caption = message.caption or ""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "caption": caption}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id, "caption": caption}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id, "caption": caption}
    return None


async def connection_guide_content_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    draft = context.user_data.get("new_connection_guide")
    if not isinstance(draft, dict):
        return ConversationHandler.END
    messages = draft.get("messages", [])
    if len(messages) >= MAX_MESSAGES_PER_GUIDE:
        await update.message.reply_text(tr(update.effective_user.id, "guide_limit"))
        return CONNECTION_GUIDE_CONTENT
    payload = connection_guide_message_payload(update.message)
    if payload is None:
        await update.message.reply_text(tr(update.effective_user.id, "guide_unsupported"))
        return CONNECTION_GUIDE_CONTENT
    try:
        messages.append(validate_guide_message(payload))
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return CONNECTION_GUIDE_CONTENT
    await update.message.reply_text(tr(update.effective_user.id, "guide_item_saved", count=len(messages)))
    return CONNECTION_GUIDE_CONTENT


async def connection_guide_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    draft = context.user_data.get("new_connection_guide")
    messages = draft.get("messages", []) if isinstance(draft, dict) else []
    if not messages:
        await update.message.reply_text(tr(update.effective_user.id, "guide_empty"))
        return CONNECTION_GUIDE_CONTENT
    title = draft["title"]
    try:
        await asyncio.to_thread(connection_guide_store.add, title, messages)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return CONNECTION_GUIDE_CONTENT
    context.user_data.pop("new_connection_guide", None)
    await update.message.reply_text(
        tr(update.effective_user.id, "guide_saved", title=preserve_dynamic_text(title)),
        reply_markup=build_connection_guides_admin_keyboard(update.effective_user.id),
    )
    return ConversationHandler.END


async def connection_guide_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_connection_guide", None)
    context.user_data.pop("connection_guide_edit", None)
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            tr(update.effective_user.id, "guide_cancelled"),
            reply_markup=build_connection_guides_admin_keyboard(update.effective_user.id),
        )
    return ConversationHandler.END


async def send_connection_guide(context: ContextTypes.DEFAULT_TYPE, chat_id: int, guide: dict[str, Any]) -> None:
    for item in guide["messages"]:
        if item["type"] == "text":
            for chunk in split_guide_text(item["text"]):
                await context.bot.send_message(chat_id=chat_id, text=preserve_dynamic_text(chunk))
        elif item["type"] == "photo":
            caption = preserve_dynamic_text(item["caption"]) if item.get("caption") else None
            await context.bot.send_photo(chat_id=chat_id, photo=item["file_id"], caption=caption)
        elif item["type"] == "video":
            caption = preserve_dynamic_text(item["caption"]) if item.get("caption") else None
            await context.bot.send_video(chat_id=chat_id, video=item["file_id"], caption=caption)
        elif item["type"] == "document":
            caption = preserve_dynamic_text(item["caption"]) if item.get("caption") else None
            await context.bot.send_document(chat_id=chat_id, document=item["file_id"], caption=caption)
        await asyncio.sleep(0.15)


async def connection_guide_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    data = query.data
    if data.startswith("settings_guides_title_"):
        guide_id = data.removeprefix("settings_guides_title_")
        mode, state, prompt = "title", CONNECTION_GUIDE_EDIT_TITLE, "guide_send_new_title"
        index = None
    elif data.startswith("settings_guides_replace_"):
        payload = data.removeprefix("settings_guides_replace_")
        guide_id, raw_index = payload.rsplit("_", 1)
        mode, state, prompt, index = "replace", CONNECTION_GUIDE_EDIT_ITEM, "guide_send_replacement", int(raw_index)
    else:
        guide_id = data.removeprefix("settings_guides_append_")
        mode, state, prompt, index = "append", CONNECTION_GUIDE_APPEND_ITEM, "guide_send_append", None
    if connection_guide_store.get(guide_id) is None:
        await localized_query_answer(query, tr(query.from_user.id, "guide_unavailable"), show_alert=True)
        return ConversationHandler.END
    context.user_data["connection_guide_edit"] = {"guide_id": guide_id, "mode": mode, "index": index}
    await query.edit_message_text(tr(query.from_user.id, prompt))
    return state


async def connection_guide_edit_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit = context.user_data.get("connection_guide_edit", {})
    title = " ".join((update.message.text or "").split())
    if not title or len(title) > MAX_TITLE_LENGTH:
        await update.message.reply_text(tr(update.effective_user.id, "guide_send_new_title"))
        return CONNECTION_GUIDE_EDIT_TITLE
    updated = await asyncio.to_thread(connection_guide_store.update_title, edit.get("guide_id", ""), title)
    return await finish_connection_guide_edit(update, context, updated, "guide_title_updated")


async def connection_guide_edit_item_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit = context.user_data.get("connection_guide_edit", {})
    payload = connection_guide_message_payload(update.message)
    if payload is None:
        await update.message.reply_text(tr(update.effective_user.id, "guide_unsupported"))
        return CONNECTION_GUIDE_EDIT_ITEM if edit.get("mode") == "replace" else CONNECTION_GUIDE_APPEND_ITEM
    try:
        payload = validate_guide_message(payload)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return CONNECTION_GUIDE_EDIT_ITEM if edit.get("mode") == "replace" else CONNECTION_GUIDE_APPEND_ITEM
    if edit.get("mode") == "replace":
        updated = await asyncio.to_thread(
            connection_guide_store.replace_message, edit.get("guide_id", ""), edit.get("index", -1), payload
        )
        notice = "guide_item_updated"
    else:
        try:
            updated = await asyncio.to_thread(connection_guide_store.append_message, edit.get("guide_id", ""), payload)
        except ValueError:
            await update.message.reply_text(tr(update.effective_user.id, "guide_limit"))
            return ConversationHandler.END
        notice = "guide_item_added"
    return await finish_connection_guide_edit(update, context, updated, notice)


async def finish_connection_guide_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, updated: bool, notice: str):
    edit = context.user_data.pop("connection_guide_edit", {})
    guide = connection_guide_store.get(edit.get("guide_id", ""))
    if not updated or guide is None:
        await update.message.reply_text(tr(update.effective_user.id, "guide_unavailable"))
    else:
        await update.message.reply_text(
            tr(update.effective_user.id, notice),
            reply_markup=build_connection_guide_edit_keyboard(update.effective_user.id, guide),
        )
    return ConversationHandler.END


async def settings_monthly_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RENEWAL_MONTHLY_PRICE
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        value = int(update.message.text.strip().replace(',', ''))
    except ValueError:
        value = 0
    if not 1 <= value <= 10**15:
        await update.message.reply_text(tr(update.effective_user.id, "monthly_price_invalid"))
        return SETTINGS_MONTHLY_PRICE
    RENEWAL_MONTHLY_PRICE = value
    save_runtime_setting("RENEWAL_MONTHLY_PRICE", str(value), RUNTIME_SETTINGS_FILE)
    await update.message.reply_text(
        tr(
            update.effective_user.id,
            "monthly_price_updated",
            amount=preserve_dynamic_text(format_money(value)),
        ),
        reply_markup=build_payment_settings_keyboard(),
    )
    return ConversationHandler.END

def get_main_menu_keyboard(is_admin=False, user_id: int | None = None):
    uid = user_id if user_id is not None else ADMIN_TELEGRAM_ID
    keyboard = []
    if is_admin:
        # ادمین: داشبورد فشرده — همه‌چیز با دو کلیک در دسترس
        keyboard.append([
            InlineKeyboardButton(tr(uid, "all_users"), callback_data='all_clients_page_1'),
            InlineKeyboardButton(tr(uid, "online_users"), callback_data='online_users'),
        ])
        keyboard.append([
            InlineKeyboardButton("🩺 عیب‌یابی", callback_data='diag_rerun'),
            InlineKeyboardButton(tr(uid, "server_status"), callback_data='server_status'),
        ])
        keyboard.append([
            InlineKeyboardButton(tr(uid, "create_user"), callback_data='create_user_prompt'),
            InlineKeyboardButton(tr(uid, "edit_user"), callback_data='edit_user_prompt'),
        ])
        keyboard.append([
            InlineKeyboardButton(tr(uid, "links"), callback_data='manage_links'),
            InlineKeyboardButton(tr(uid, "broadcast"), callback_data='broadcast_message'),
        ])
        keyboard.append([
            InlineKeyboardButton(tr(uid, "bot_stats"), callback_data='bot_stats'),
            InlineKeyboardButton(tr(uid, "settings"), callback_data='admin_settings'),
        ])
        keyboard.append([InlineKeyboardButton("👥 مدیران", callback_data='settings_admins')])
        if SETTINGS.store_enabled:
            keyboard.append([InlineKeyboardButton(tr(uid, "shop_menu"), callback_data='shop_menu_open')])
    else:
        # مشتری: چیدمان عکس با نوشته‌های قبلی — خرید بالا، بعد اشتراک | پشتیبانی
        subscription_key = "my_subscriptions" if has_multiple_subscriptions(telegram_clients, uid) else "my_subscription"
        if SETTINGS.store_enabled:
            keyboard.append([InlineKeyboardButton(tr(uid, "shop_menu"), callback_data='shop_menu_open')])
            if getattr(SETTINGS, "trial_enabled", True):
                keyboard.append([InlineKeyboardButton("🎁 اکانت تست رایگان", callback_data='shop_trial')])
            keyboard.append([InlineKeyboardButton("💼 کیف پول", callback_data='shop_wallet')])
        keyboard.append([
            InlineKeyboardButton(tr(uid, subscription_key), callback_data='my_usage'),
            InlineKeyboardButton(tr(uid, "support"), callback_data='support_contact'),
        ])
    if connection_guide_store.enabled and connection_guide_store.list_guides():
        keyboard.append([InlineKeyboardButton(tr(uid, "connection_guide"), callback_data='connection_guides')])
    keyboard.append([InlineKeyboardButton(tr(uid, "language"), callback_data='language_settings')])
    return InlineKeyboardMarkup(keyboard)


def subscription_keyboard(user_id: int, client_id: int, web_panel_url: str | None):
    keyboard = [
        [InlineKeyboardButton(tr(user_id, "my_links"), callback_data=f'get_sub_links_{client_id}'),
         InlineKeyboardButton(tr(user_id, "renew"), callback_data=f'renew_start_{client_id}')],
        [InlineKeyboardButton(tr(user_id, "refresh"), callback_data=f'my_usage_{client_id}'),
         InlineKeyboardButton(tr(user_id, "support"), callback_data='support_contact')],
    ]
    if web_panel_url:
        keyboard.append([InlineKeyboardButton(tr(user_id, "web_panel"), url=web_panel_url)])
    if has_multiple_subscriptions(telegram_clients, user_id):
        keyboard.append([InlineKeyboardButton(tr(user_id, "back_subscriptions"), callback_data='my_usage')])
    keyboard.append([InlineKeyboardButton(tr(user_id, "main_menu"), callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)


def web_panel_url_for(username: str) -> str | None:
    if not WEB_PANEL_ENABLED or not WEB_PANEL_BASE_URL:
        return None
    return build_web_panel_url(WEB_PANEL_BASE_URL, username, BOT_DISPLAY_NAME)


def renewal_reminder_keyboard(user_id: int, reminders: list[dict]) -> InlineKeyboardMarkup:
    if len(reminders) == 1:
        reminder = reminders[0]
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(tr(user_id, "renew"), callback_data=f"renew_start_{reminder['client_id']}")
        ]])

    rows = []
    for reminder in reminders:
        description = " ".join(str(reminder.get("desc") or reminder.get("name") or reminder["client_id"]).split())
        short_description = description if len(description) <= 28 else f"{description[:27]}…"
        label = f"{tr(user_id, 'renew')} — {preserve_dynamic_text(short_description)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"renew_start_{reminder['client_id']}")])
    return InlineKeyboardMarkup(rows)


def reminder_remaining_text(locale: str, days: int, *, short: bool = False) -> str:
    if days == 1:
        return translate(locale, "hours_short_24" if short else "hours_remaining_24")
    return translate(locale, "days_short" if short else "days_remaining", days=days)


def get_pagination_keyboard(current_page: int, total_pages: int, prefix: str):
    keyboard = []
    nav_row = []
    if current_page > 1:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f'{prefix}_page_{current_page-1}'))
    nav_row.append(InlineKeyboardButton(f"📄 {current_page}/{total_pages}", callback_data='current_page'))
    if current_page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f'{prefix}_page_{current_page+1}'))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)


# --- دکمه چهارگوش جمع‌وجور «⬜» (بدون نوشته) — منو را در چت باز/بسته می‌کند ---
MENU_TOGGLE_TEXT = "⬜"


def menu_toggle_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(MENU_TOGGLE_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def open_menu_message(update, context, text: str, reply_markup) -> None:
    """یک پیام منو در چت نگه‌دار: اگر باز است ویرایشش کن، نه پیام‌های جدید."""
    chat_id = update.effective_chat.id
    mid = context.chat_data.get("menu_message_id")
    if mid:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=mid, text=text, reply_markup=reply_markup
            )
            return
        except TelegramError:
            context.chat_data.pop("menu_message_id", None)
    if not context.chat_data.get("menu_keyboard_shown"):
        # فقط یک‌بار: کیبوردِ دکمه چهارگوش؛ بعد از آن پایدار می‌ماند
        await context.bot.send_message(
            chat_id=chat_id,
            text="⬜ با همین دکمهٔ کوچک، منو همین‌جا باز/بسته می‌شود.",
            reply_markup=menu_toggle_keyboard(),
        )
        context.chat_data["menu_keyboard_shown"] = True
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    context.chat_data["menu_message_id"] = msg.message_id


async def close_menu_message(update, context) -> None:
    mid = context.chat_data.pop("menu_message_id", None)
    if mid:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
        except TelegramError:
            pass


@rate_limited(admin_only=False)
async def menu_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ضربه روی «⬜» — باز/بسته کردن پیام منو بدون شلوغ‌کردن چت."""
    if (update.message.text or "").strip() != MENU_TOGGLE_TEXT:
        return
    user_id = update.effective_user.id
    await _remove_stale_home_keyboard(update, user_id)
    if context.chat_data.get("menu_message_id"):
        await close_menu_message(update, context)
        return
    await open_menu_message(
        update, context, tr(user_id, "welcome"),
        get_main_menu_keyboard(is_admin(user_id), user_id),
    )


async def setup_bot_commands(app) -> None:
    """/ منوی تلگرام (دکمهٔ آبی کنار فیلد تایپ) + کامندهای ادمین در چت ادمین."""
    logger.info("Registering Telegram command menu (default + %d admin scope(s))", len(ADMIN_IDS))
    try:
        _user_commands = [
            BotCommand("start", "منو اصلی"),
            BotCommand("topup", "شارژ حساب"),
            BotCommand("deploy", "ایجاد سرور"),
            BotCommand("usage", "اشتراک‌های من"),
            BotCommand("shop", "خرید اشتراک"),
            BotCommand("support", "پشتیبانی"),
        ]
        if SETTINGS.store_enabled:
            _user_commands.append(BotCommand("wallet", "کیف پول"))
            if getattr(SETTINGS, "trial_enabled", True):
                _user_commands.append(BotCommand("trial", "اکانت تست رایگان"))
        await app.bot.set_my_commands(_user_commands)
        # کامندهای ادمین فقط داخل چت ادمین‌ها ظاهر می‌شوند
        for admin_id in ADMIN_IDS:
            _admin_commands = list(_user_commands) + [
                BotCommand("diag", "تست سلامت پنل"),
                BotCommand("panel", "داشبورد مدیریت"),
                BotCommand("metrics", "آمار ربات"),
                BotCommand("checkinactive", "بررسی غیرفعال‌ها"),
            ]
            if SETTINGS.store_enabled:
                _admin_commands.append(BotCommand("discounts", "مدیریت کدهای تخفیف"))
            await app.bot.set_my_commands(
                _admin_commands,
                scope=BotCommandScopeChat(admin_id),
            )
        # دکمهٔ مربعی کنار کادر تایپ: اگر MENU_WEBAPP_URL تنظیم شده باشد
        # مینی‌اپ «منوی اصلی» باز می‌شود؛ وگرنه لیست کامندها (رفتار قبلی).
        menu_url = getattr(SETTINGS, "menu_webapp_url", "") or ""
        if menu_url:
            try:
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="منو", web_app=WebAppInfo(url=menu_url))
                )
                logger.info("Telegram menu button set to WebApp: %s", menu_url)
            except TelegramError as exc:
                logger.warning("WebApp menu button rejected (%s); falling back to commands", exc)
                await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        else:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Telegram command menu registered")
    except TelegramError as exc:
        logger.warning("Could not register bot commands: %s", exc)


@rate_limited(admin_only=False)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # نمایندگی: رفرال از deep-link (r<tg_id>) — فقط بار اول عضویت ثبت می‌شود
    if context.args:
        ref = _parse_start_ref(context.args)
        res_store = context.bot_data.get("resellers")
        if ref and res_store is not None and ref != user_id:
            try:
                res_store.ensure_member(user_id, invited_by=ref)
            except Exception:
                logging.getLogger("sui_bot.reseller").exception("referral capture failed")
    if language_store.get(user_id) is None:
        # تشخیص خودکار زبان از کلاینت تلگرام؛ fa/ru/zh شناخته می‌شوند، بقیه انگلیسی
        tg_lang = (update.effective_user.language_code or "en")[:2]
        auto = tg_lang if tg_lang in SUPPORTED_LANGUAGES else "en"
        if auto != "en":
            await asyncio.to_thread(language_store.set, user_id, auto)
        else:
            await update.message.reply_text(translate("en", "choose_language"), reply_markup=language_keyboard())
            return
    admin_flag = is_admin(user_id)
    await _remove_stale_home_keyboard(update, user_id)
    welcome_msg = tr(user_id, "welcome")
    keyboard = get_main_menu_keyboard(admin_flag, user_id)
    await open_menu_message(update, context, welcome_msg, keyboard)

@rate_limited(admin_only=False)
async def stale_home_tap_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ضربه به دکمهٔ قدیمی چسبیدهٔ «🏠» — حذفش کن و منو را نشان بده."""
    await _remove_stale_home_keyboard(update, update.effective_user.id)
    await start(update, context)

async def _refresh_admin_scopes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """ثبت دوبارهٔ اسکوپ دستورات + لیست اطلاع‌رسانی فروشگاه پس از تغییر ادمین‌ها."""
    try:
        await setup_bot_commands(context.application)
    except Exception:
        logger.exception("admin scope refresh failed")
    try:
        store_ctx = context.application.bot_data.get("store")
        if isinstance(store_ctx, dict) and "admins" in store_ctx:
            store_ctx["admins"] = list(ADMIN_IDS)
    except Exception:
        logger.debug("store admins refresh failed", exc_info=True)


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


@rate_limited(admin_only=False)
async def admin_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler (group=1): گرفتن آیدی عددی ادمین جدید — تنها هندلر این گروه."""
    if not context.user_data.get("admin_add_pending"):
        return
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        context.user_data.pop("admin_add_pending", None)
        return
    text = (update.message.text or "").strip()
    if text == MENU_TOGGLE_TEXT:
        return  # مصرفش نکن؛ هندلر گروه ۰ منو را باز/بسته می‌کند
    if text.lower() in {"لغو", "cancel", "انصراف"}:
        context.user_data.pop("admin_add_pending", None)
        await update.message.reply_text(
            build_admins_text(),
            reply_markup=build_admins_keyboard(update.effective_user.id),
            parse_mode="HTML",
        )
        return
    digits = text.translate(_FA_DIGITS).strip()
    if not digits.isdigit():
        await update.message.reply_text("❌ آیدی عددی نامعتبر. فقط عدد بفرست یا «لغو».")
        return
    result = runtime_add_admin(int(digits))
    if result == "added":
        context.user_data.pop("admin_add_pending", None)
        await _refresh_admin_scopes(context)
        await update.message.reply_text(
            f"✅ ادمین <code>{digits}</code> اضافه شد.",
            reply_markup=build_admins_keyboard(update.effective_user.id),
            parse_mode="HTML",
        )
    elif result == "exists":
        context.user_data.pop("admin_add_pending", None)
        await update.message.reply_text(
            f"⚠️ <code>{digits}</code> قبلاً ادمین است.",
            reply_markup=build_admins_keyboard(update.effective_user.id),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ آیدی نامعتبر. دوباره بفرست یا «لغو».")


@rate_limited(admin_only=False)
async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پنل ارتباط با پشتیبانی — راهنما + دکمهٔ ارسال پیام به ادمین."""
    user_id = update.effective_user.id
    text = (
        f"{tr(user_id, 'support_title')}\n\n"
        "اگر مشکل داری، همین‌جا بنویس — پیامت مستقیم می‌ره برای پشتیبانی و جواب رو همین‌جا می‌گیری.\n\n"
        "چند نکتهٔ سریع:\n"
        "• اتصال وصل نمیشه؟ اول اپ رو آپدیت و لینک اشتراک رو رفرش کن\n"
        "• حجم/انقضا اشتباهه؟ شمارهٔ سفارش رو بنویس\n"
        "• برای خرید جدید از دکمهٔ خرید اشتراک استفاده کن"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ ارسال پیام به پشتیبانی", callback_data='support_send'),
    ], [
        InlineKeyboardButton(tr(user_id, "main_menu"), callback_data='main_menu'),
    ]])
    await update.message.reply_text(text, reply_markup=kb)


async def support_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    text = (
        f"{tr(user_id, 'support_title')}\n\n"
        "مشکلت رو همین‌جا بنویس (متنی بفرست) — مستقیم برای پشتیبانی ارسال میشه."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ ارسال پیام به پشتیبانی", callback_data='support_send'),
    ], [
        InlineKeyboardButton(tr(user_id, "back"), callback_data='my_usage'),
    ]])
    await query.edit_message_text(text, reply_markup=kb)


@rate_limited(admin_only=False)
async def deploy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /deploy — ایجاد سرور (باز کردن فروشگاه)."""
    if not SETTINGS.store_enabled:
        await usage(update, context)
        return
    from .store_bot import shop_command as _shop_command
    await _shop_command(update, context)


@rate_limited(admin_only=False)
async def webapp_menu_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دیتای ارسالی از مینی‌اپ منو (دکمهٔ مربعی کنار کادر تایپ).

    کاربر روی کارت‌های صفحهٔ /menu می‌زند → این‌جا action مربوطه در خودِ ربات باز می‌شود.
    """
    message = update.effective_message
    web_app_data = getattr(message, "web_app_data", None)
    if web_app_data is None:
        return
    raw = (web_app_data.data or "").strip()
    try:
        import json as _json
        payload = _json.loads(raw)
        action = str(payload.get("act") or "").strip().lower() if isinstance(payload, dict) else raw
    except Exception:
        action = raw

    action_map = {
        "shop": "shop",
        "buy": "shop",
        "plans": "shop",
        "usage": "usage",
        "my_usage": "usage",
        "subscriptions": "usage",
        "wallet": "wallet",
        "balance": "wallet",
        "trial": "trial",
        "support": "support",
    }
    action = action_map.get(action, "")

    if action == "shop" and SETTINGS.store_enabled:
        from .store_bot import shop_command as _shop_command
        await _shop_command(update, context)
        return
    if action == "wallet" and SETTINGS.store_enabled:
        from .store_bot import wallet_command as _wallet_command
        await _wallet_command(update, context)
        return
    if action == "trial" and SETTINGS.store_enabled:
        from .store_bot import trial_command as _trial_command
        await _trial_command(update, context)
        return
    if action == "usage":
        await usage(update, context)
        return
    if action == "support":
        context.user_data["support_pending"] = True
        await message.reply_text(
            "✍️ پیامت رو بنویس و بفرست (یا /cancel بزن برای انصراف):"
        )
        return
    await message.reply_text("📍 از منوی اصلی ربات استفاده کن: /start")


@rate_limited(admin_only=False)
async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /topup — شارژ حساب (باز کردن فروشگاه)."""
    if not SETTINGS.store_enabled:
        await usage(update, context)
        return
    from .store_bot import shop_command as _shop_command
    await _shop_command(update, context)


@rate_limited(admin_only=False)
async def support_send_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["support_pending"] = True
    await query.edit_message_text(
        "✍️ پیامت رو بنویس و بفرست (یا /cancel بزن برای انصراف):"
    )


async def support_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام متنی کاربر وقتی در حالت پشتیبانیه → فورارد به ادمینِ مالکِ مشتری."""
    if not context.user_data.get("support_pending"):
        return
    if (update.message.text or "").strip() == MENU_TOGGLE_TEXT:
        return  # دکمهٔ منو را به‌عنوان پیام پشتیبانی نفرست
    user = update.effective_user
    context.user_data.pop("support_pending", None)
    origin = f"t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    header = (
        f"🆘 <b>پیام پشتیبانی از کاربر</b>\n"
        f"👤 <a href=\"{origin}\">{user.first_name or 'کاربر'}</a> (<code>{user.id}</code>)\n"
        f"{'─' * 20}\n"
    )
    # مسیریابی: مالک مشتری (اگر چند ادمین باشد)؛ بدون مشتری → ادمین اصلی
    recipients = admin_recipients("support", customer_id=user.id)
    try:
        for chat_id in recipients:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=user.id,
                message_id=update.effective_message.message_id,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=header,
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("support forward failed")
    await update.message.reply_text(
        "✅ پیامت رسید! پشتیبانی بررسی می‌کنه و جواب رو همین‌جا می‌گیری."
    )


@rate_limited(admin_only=False)
async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    client_ids = telegram_clients.get(tg_id)
    if not client_ids:
        # کاربر بدون ساب = فرصت فروش: راهنمای خرید + پشتیبانی به‌جای بن‌بست
        keyboard = []
        if SETTINGS.store_enabled:
            keyboard.append([InlineKeyboardButton(tr(tg_id, "shop_menu"), callback_data='shop_menu_open')])
        keyboard.append([InlineKeyboardButton(tr(tg_id, "support"), callback_data='support_contact')])
        keyboard.append([InlineKeyboardButton(tr(tg_id, "main_menu"), callback_data='main_menu')])
        await update.message.reply_text(tr(tg_id, "not_active"), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if len(client_ids) == 1:
        # Single subscription - show directly
        client_id = client_ids[0]
        is_admin_user = (is_admin(tg_id))
        usage_msg, client = await get_client_usage_record(client_id, is_admin_user, tg_id)
        username = client.get("name", "Unknown") if client else "Unknown"
        web_panel_url = web_panel_url_for(username)

        await update.message.reply_text(usage_msg, reply_markup=subscription_keyboard(tg_id, client_id, web_panel_url))
    else:
        # Multiple subscriptions - show selection menu
        keyboard = []
        client_map = await get_client_map()
        for client_id in client_ids:
            client = client_map.get(client_id)
            if client:
                name = preserve_dynamic_text(client["name"]) if client.get("name") else "Unknown"
                desc = preserve_dynamic_text(client["desc"]) if client.get("desc") else "No description"
                expiry = client.get("expiry", 0)
                expiry_str = localized_remaining_time(expiry, tg_id)
                button_text = f"📱 {desc} ({name}) - {expiry_str}"
            else:
                button_text = f"📱 Subscription #{client_id}"

            keyboard.append([InlineKeyboardButton(button_text, callback_data=f'select_sub_{client_id}')])

        keyboard.append([InlineKeyboardButton(tr(tg_id, "main_menu"), callback_data='main_menu')])

        await update.message.reply_text(
            tr(tg_id, "select_subscription", count=len(client_ids)),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def get_client_usage_record(
    client_id: int,
    is_admin: bool,
    user_id: int | None = None,
) -> tuple[str, dict | None]:
    try:
        data = await api_client.get('apiv2/clients', {'id': client_id})
        if not data:
            return f"❌ Server unresponsive. Try Again Later.{api_client.error_reason('apiv2/clients')}", None
        clients = sui_clients(data)
        if clients is None:
            return "❌ Invalid response from server. Try Again Later.", None
        if not clients:
            return "❌ User Not Found.", None
        client = clients[0]
        if not isinstance(client, dict):
            return "❌ Invalid response from server. Try Again Later.", None
        return format_client(client, is_admin=is_admin, user_id=user_id), client
    except Exception as e:
        logger.exception("Error in get_client_usage_record")
        return f"❌ Error: {e}", None


async def get_client_usage_for_display(client_id: int, is_admin: bool, user_id: int | None = None) -> str:
    message, _ = await get_client_usage_record(client_id, is_admin, user_id)
    return message

@rate_limited(admin_only=True)
async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = metrics.get_global_stats()
    start_time = datetime.fromisoformat(stats['start_time'])
    uptime = datetime.now() - start_time
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    msg = f"📊 Bot Stats\n\n⏱️ Running Time: {days} Days, {hours} Hour, {minutes} Minute\n📨 All Commands: {stats['total_commands']}\n👥 All Users: {stats['total_users']}\n❌ All Errors: {stats['total_errors']}\n\n🔥 Active Users:\n"
    for i, (uid, count) in enumerate(stats['most_active_users'][:5], 1):
        msg += f"{i}. User {uid}: {count} Command\n"
    msg += "\n📈 Most Used Commands:\n"
    for i, (cmd, count) in enumerate(stats['most_used_commands'][:5], 1):
        clean_cmd = cmd.replace('button_', '')
        msg += f"{i}. {clean_cmd}: {count} Times\n"
    keyboard = [[InlineKeyboardButton("🔄 بروزرسانی", callback_data='bot_stats')], [InlineKeyboardButton("👥 جزئیات کاربران", callback_data='user_details_page_1')], [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


@rate_limited(admin_only=True)
async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """داشبورد یک‌صفحه‌ای ادمین: سرور + آنلاین‌ها + انقضاها + فروشگاه."""
    user_id = update.effective_user.id
    status_msg = "⏳ در حال جمع‌آوری…"
    await update.message.reply_text(status_msg)

    # ۱) وضعیت سرور
    status_data = await api_client.get('apiv2/status', {'r': 'cpu,mem,sys,sbd'})
    obj = sui_response_object(status_data)
    lines = ["🖥 <b>داشبورد مدیریت</b>\n"]
    if obj:
        cpu = round(obj.get("cpu", 0) or 0)
        mem = obj.get("mem", {}) or {}
        mem_pct = round((mem.get("current", 0) / mem.get("total", 1) * 100) if mem.get("total") else 0)
        sbd = obj.get("sbd", {}) or {}
        running = sbd.get("running", False)
        lines.append(f"💻 CPU: {usage_bar(cpu, 100, 8)}")
        lines.append(f"🧠 RAM: {usage_bar(mem_pct, 100, 8)}")
        lines.append(f"📦 هسته: {'🟢 فعال' if running else '🔴 متوقف'}")
    else:
        lines.append(f"⚠️ وضعیت سرور: نامشخص{api_client.error_reason('apiv2/status')}")

    # ۲) آنلاین‌ها
    onlines = await api_client.get('apiv2/onlines')
    online_obj = sui_response_object(onlines)
    online_count = 0
    if online_obj and isinstance(online_obj.get("user"), list):
        online_count = len(online_obj["user"])
    lines.append(f"🌐 آنلاین: {fa_num(online_count)}")

    # ۳) انقضاها و فروشگاه
    clients = await get_all_clients_list()
    panel_scope = admin_group_scope(user_id)
    if panel_scope is not None:
        clients = [c for c in clients or [] if client_in_admin_scope(c, panel_scope)]
    now = datetime.now(timezone.utc).timestamp()
    expiring = []
    expired = 0
    for client in clients or []:
        expiry = client.get("expiry", 0) or 0
        if not expiry:
            continue
        if expiry < now:
            expired += 1
        elif expiry - now < 3 * 86400:
            expiring.append(client)
    lines.append(f"👥 کلاینت‌ها: {fa_num(len(clients or []))}")
    lines.append(f"⏳ منقضی: {fa_num(expired)} | ⚠️ نزدیک انقضا (۳ روز): {fa_num(len(expiring))}")

    if SETTINGS.store_enabled:
        try:
            orders_store = context.bot_data.get("store", {}).get("store") if context.bot_data.get("store") else None
            pending = 0
            if orders_store is not None:
                data = orders_store._load()
                pending = sum(1 for o in data.get("orders", []) if o.get("status") == "submitted")
            lines.append(f"🛒 سفارش در انتظار تأیید: {fa_num(pending)}")
        except Exception:
            logger.debug("orders summary failed", exc_info=True)

    # ۴) دکمه‌های اقدام
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 کاربران", callback_data='all_clients_page_1'),
         InlineKeyboardButton("🌐 آنلاین‌ها", callback_data='online_users')],
        [InlineKeyboardButton("🩺 دیاگ", callback_data='diag_rerun'),
         InlineKeyboardButton("📊 آمار", callback_data='bot_stats')],
        [InlineKeyboardButton("⚙️ تنظیمات", callback_data='admin_settings')],
    ])
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")


@rate_limited(admin_only=True)
async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Probe every read-only panel endpoint and show a green/red report."""
    await update.message.reply_text("🩺 Probing panel endpoints…")
    rows = await probe_endpoints(api_client.get)
    report = format_diag_report(rows)
    if api_client.last_errors:
        report += f"\n\n⚠️ Recorded failures:{api_client.error_reason()}"
    keyboard = [[InlineKeyboardButton("🔄 Re-run", callback_data='diag_rerun')]]
    await update.message.reply_text(report, reply_markup=InlineKeyboardMarkup(keyboard))


async def diag_rerun_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return
    await localized_query_answer(query)
    try:
        rows = await probe_endpoints(api_client.get)
        report = format_diag_report(rows)
        if api_client.last_errors:
            report += f"\n\n⚠️ Recorded failures:{api_client.error_reason()}"
        await query.edit_message_text(
            report,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Re-run", callback_data='diag_rerun')]]),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# ---------------------------------------------------------------------------
# هندلرهای مدیریت پلن‌ها و قیمت‌گذاری فروشگاه
# ---------------------------------------------------------------------------
def _plans_summary_text() -> str:
    return build_plans_admin_text()


async def plan_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["plan_editing"] = None
    await query.edit_message_text(
        "➕ پلن جدید\n\nعنوان پلن رو بفرست (مثلاً: «۳ ماهه ۱۰۰ گیگ»)\nلغو: /cancel"
    )
    return PLAN_TITLE


async def plan_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return ConversationHandler.END
    slug = query.data.removeprefix("plan_edit_")
    plan = _plans_store.get(slug)
    if plan is None:
        await query.edit_message_text("❌ پلن پیدا نشد.", reply_markup=build_plans_admin_keyboard())
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["plan_editing"] = slug
    await query.edit_message_text(
        f"✏️ ویرایش «{plan.title}»\n"
        f"فعلی: {plan.gb} گیگ / {plan.days} روز / "
        f"{'قیمت دستی ' + format_money(plan.price_toman) if plan.price_toman else 'قیمت خودکار'}\n\n"
        "عنوان جدید رو بفرست (یا /skip برای حفظ عنوان)\nلغو: /cancel"
    )
    return PLAN_TITLE


async def plan_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plan_title"] = update.message.text.strip()[:48]
    context.user_data["plan_stage"] = "gb"
    editing = context.user_data.get("plan_editing")
    prompt = "حجم به گیگ بفرست (مثلاً: 50):" if not editing else "حجم جدید به گیگ (/skip = حفظ):"
    await update.message.reply_text(f"✅ عنوان: {context.user_data['plan_title']}\n\n{prompt}")
    return PLAN_GB


async def plan_gb_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("/"):
        try:
            gb = int(text.replace("گیگ", "").strip())
            if not 1 <= gb <= 100_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ عدد گیگ نامعتبر. مثلاً: 50")
            return PLAN_GB
        context.user_data["plan_gb"] = gb
    elif text != "/skip":
        await update.message.reply_text("❌ عدد بفرست یا /skip")
        return PLAN_GB
    context.user_data["plan_stage"] = "days"
    editing = context.user_data.get("plan_editing")
    prompt = "مدت به روز بفرست (مثلاً: 90 یا «3 ماه»):" if not editing else "مدت جدید به روز (/skip = حفظ):"
    await update.message.reply_text(f"✅ حجم: {context.user_data.get('plan_gb', '-')} گیگ\n\n{prompt}")
    return PLAN_DAYS


async def plan_days_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("/"):
        raw = text.lower().replace("ماهه", " ماه").replace("ماه", " ماه")
        months_hint = "ماه" in raw
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            await update.message.reply_text("❌ مدت نامعتبر. مثلاً: 90 یا «3 ماه»")
            return PLAN_DAYS
        value = int(digits)
        days = value * 30 if months_hint else value
        if not 1 <= days <= 3650:
            await update.message.reply_text("❌ مدت باید ۱ تا ۳۶۵۰ روز باشد.")
            return PLAN_DAYS
        context.user_data["plan_days"] = days
    elif text != "/skip":
        await update.message.reply_text("❌ عدد بفرست یا /skip")
        return PLAN_DAYS
    context.user_data["plan_stage"] = "price"
    await update.message.reply_text(
        "💰 قیمت دستی به تومان بفرست (0 = خودکار طبق حالت قیمت‌گذاری"
        + ("؛ /skip = حفظ قیمت فعلی)" if context.user_data.get("plan_editing") else ")"
        )
    )
    return PLAN_PRICE


async def plan_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("/"):
        digits = "".join(ch for ch in text if ch.isdigit())
        price = int(digits or 0)
        context.user_data["plan_price"] = price
    elif text != "/skip":
        await update.message.reply_text("❌ عدد به تومان بفرست یا /skip")
        return PLAN_PRICE
    editing = context.user_data.get("plan_editing")
    if editing:
        await _save_plan_from_context(update, context)
        return ConversationHandler.END
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 عادی", callback_data='plan_res_no'),
         InlineKeyboardButton("🔒 فقط نمایندگی", callback_data='plan_res_yes')],
    ])
    await update.message.reply_text("این پلن فقط برای زیرمجموعه‌های نمایندگی باشه؟", reply_markup=keyboard)
    return PLAN_RESELLER


async def plan_reseller_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    context.user_data["plan_reseller"] = query.data.endswith("yes")
    await _save_plan_from_context(update, context, query=query)
    return ConversationHandler.END


async def _save_plan_from_context(update: Update, context: ContextTypes.DEFAULT_TYPE, *, query=None) -> None:
    data = context.user_data
    editing = data.get("plan_editing")
    try:
        if editing:
            kwargs = {}
            if data.get("plan_title") is not None:
                kwargs["title"] = data["plan_title"]
            if data.get("plan_gb") is not None:
                kwargs["gb"] = data["plan_gb"]
            if data.get("plan_days") is not None:
                kwargs["days"] = data["plan_days"]
            if data.get("plan_price") is not None:
                kwargs["price_toman"] = data["plan_price"]
            plan = _plans_store.update_plan(editing, **kwargs)
            text = f"✅ پلن «{plan.title}» بروزرسانی شد."
        else:
            plan = _plans_store.add_plan(
                title=data.get("plan_title", ""),
                gb=data.get("plan_gb", 0),
                days=data.get("plan_days", 0),
                price_toman=data.get("plan_price", 0),
                reseller_only=bool(data.get("plan_reseller")),
            )
            text = f"✅ پلن «{plan.title}» ساخته شد (شناسه: {plan.slug})."
    except (ValueError, KeyError) as exc:
        text = f"❌ خطا: {exc}"
    context.user_data.clear()
    text += "\n\n" + _plans_summary_text()
    if query is not None:
        await query.edit_message_text(text, reply_markup=build_plans_admin_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=build_plans_admin_keyboard())


async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ لغو شد.", reply_markup=build_plans_admin_keyboard())
    return ConversationHandler.END


async def plan_field_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/skip داخل مکالمهٔ پلن → مرحلهٔ بعد با حفظ مقدار فعلی."""
    stage = context.user_data.get("plan_stage")
    if stage == "gb":
        context.user_data["plan_stage"] = "days"
        editing = context.user_data.get("plan_editing")
        prompt = "مدت به روز بفرست (مثلاً: 90 یا «3 ماه»):" if not editing else "مدت جدید به روز (/skip = حفظ):"
        await update.message.reply_text(prompt)
        return PLAN_DAYS
    if stage == "days":
        context.user_data["plan_stage"] = "price"
        await update.message.reply_text("💰 قیمت دستی به تومان (0 = خودکار؛ /skip = حفظ):")
        return PLAN_PRICE
    if stage == "price":
        await _save_plan_from_context(update, context)
        return ConversationHandler.END
    await update.message.reply_text("برای شروع از منوی پلن‌ها استفاده کن.")
    return ConversationHandler.END


async def pricing_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return ConversationHandler.END
    data = query.data or ""
    context.user_data.clear()
    if data == "pricing_set_per_gb":
        context.user_data["pricing_target"] = "per_gb"
        prompt = f"📏 قیمت هر گیگ به تومان بفرست (فعلی: {format_money(_plans_store.pricing.per_gb_toman)})\nلغو: /cancel"
    elif data == "pricing_set_monthly":
        context.user_data["pricing_target"] = "monthly"
        prompt = f"📅 قیمت پایه هر ماه به تومان بفرست (فعلی: {format_money(_plans_store.pricing.monthly_toman)})\nلغو: /cancel"
    else:
        months = data.removeprefix("pricing_set_month_")
        context.user_data["pricing_target"] = "month"
        context.user_data["pricing_month"] = months
        prompt = f"💰 قیمت اختصاصی برای {months} ماه به تومان بفرست (0 = حذف قیمت اختصاصی)\nلغو: /cancel"
    await query.edit_message_text(prompt)
    return PRICING_VALUE


async def pricing_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    value = int(digits or 0)
    target = context.user_data.pop("pricing_target", None)
    month_key = context.user_data.pop("pricing_month", None)
    try:
        if target == "per_gb":
            _plans_store.set_per_gb(value)
            text_out = f"✅ قیمت هر گیگ: {format_money(value)}"
        elif target == "monthly":
            _plans_store.set_monthly(value)
            text_out = f"✅ قیمت پایه هر ماه: {format_money(value)}"
        elif target == "month" and month_key:
            _plans_store.set_month_price(int(month_key), value or None)
            text_out = (f"✅ قیمت {month_key} ماه: {format_money(value)}"
                        if value else f"🗑 قیمت اختصاصی {month_key} ماه حذف شد (fallback پایه).")
        else:
            text_out = "❌ درخواست نامعتبر."
    except ValueError as exc:
        text_out = f"❌ {exc}"
    pricing_text, pricing_kb = build_pricing_menu()
    await update.message.reply_text(f"{text_out}\n\n{pricing_text}", reply_markup=pricing_kb)
    return ConversationHandler.END


async def pricing_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    pricing_text, pricing_kb = build_pricing_menu()
    await update.message.reply_text("❌ لغو شد.\n\n" + pricing_text, reply_markup=pricing_kb)
    return ConversationHandler.END


# --- قیمت اختصاصی مشتری -------------------------------------------------------
async def uprice_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ورود آیدی عددی مشتری (از منو یا فرم دستی)."""
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return ConversationHandler.END
    if query.data.startswith("uprice_edit_"):
        uid = query.data.removeprefix("uprice_edit_")
        context.user_data["uprice_user"] = uid
        return await _show_uprice_actions(update, context, uid)
    context.user_data.clear()
    context.user_data["uprice_user"] = None
    await query.edit_message_text(
        "👤 آیدی عددی تلگرام مشتری رو بفرست (مثلاً: 520031285)\nلغو: /cancel"
    )
    return UPRICE_USER


async def uprice_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = "".join(ch for ch in update.message.text if ch.isdigit())
    if not digits:
        await update.message.reply_text("❌ آیدی عددی نامعتبر. دوباره بفرست یا /cancel")
        return UPRICE_USER
    uid = str(int(digits))
    context.user_data["uprice_user"] = uid
    return await _show_uprice_actions(update, context, uid)


async def _show_uprice_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: str):
    summary = _plans_store.user_pricing_summary(int(uid))
    current = summary or "قیمت عادی (بدون تنظیم خاص)"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("٪ درصد تخفیف/اضافه", callback_data=f'uprice_set_disc_{uid}'),
         InlineKeyboardButton("💵 قیمت ثابت یک پلن", callback_data=f'uprice_set_price_{uid}')],
        [InlineKeyboardButton("🗑 پاک کردن همه", callback_data=f'uprice_clear_{uid}'),
         InlineKeyboardButton("🔙 بازگشت", callback_data='pricing_user_menu')],
    ])
    text = f"👤 مشتری: {uid}\nوضعیت فعلی: {current}\n\nچه کاری انجام بدم؟"
    message = update.callback_query.message if update.callback_query else update.message
    await message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def uprice_value_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("uprice_mode") == "disc":
        return await uprice_discount_input(update, context)
    return await uprice_plan_price_input(update, context)


async def uprice_discount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("uprice_user")
    if not uid:
        await update.message.reply_text("❌ اول مشتری رو انتخاب کن.")
        return ConversationHandler.END
    text = update.message.text.strip().replace("٪", "").replace("%", "")
    digits = "".join(ch for ch in text if ch.isdigit() or ch == "-")
    try:
        value = int(digits)
    except ValueError:
        await update.message.reply_text("❌ عدد بفرست؛ مثلاً -20 برای ۲۰٪ تخفیف یا 10 برای ۱۰٪ اضافه.")
        return UPRICE_VALUE
    try:
        _plans_store.set_user_discount(int(uid), value)
        text_out = f"✅ مشتری {uid}: {'تخفیف' if value < 0 else 'اضافه'} {fa_num(abs(value))}٪ ثبت شد."
    except ValueError as exc:
        text_out = f"❌ {exc}"
    context.user_data.clear()
    await update.message.reply_text(text_out, reply_markup=build_plans_admin_keyboard())
    return ConversationHandler.END


async def uprice_plan_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("uprice_user")
    slug = context.user_data.get("uprice_plan")
    if not uid or not slug:
        await update.message.reply_text("❌ درخواست نامعتبر؛ دوباره از منو شروع کن.")
        return ConversationHandler.END
    digits = "".join(ch for ch in update.message.text if ch.isdigit())
    value = int(digits or 0)
    _plans_store.set_user_plan_price(int(uid), slug, value or None)
    plan = _plans_store.get(slug)
    title = plan.title if plan else slug
    text_out = (f"✅ قیمت ثابت «{title}» برای {uid}: {fa_num(value)} تومان"
                if value else f"🗑 قیمت ثابت «{title}» برای {uid} حذف شد.")
    context.user_data.clear()
    await update.message.reply_text(text_out, reply_markup=build_plans_admin_keyboard())
    return ConversationHandler.END


async def uprice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ لغو شد.", reply_markup=build_plans_admin_keyboard())
    return ConversationHandler.END


async def uprice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Admin Only", show_alert=True)
        return
    data = query.data or ""
    if data == 'pricing_user_menu':
        text, kb = build_user_pricing_menu()
        await query.edit_message_text(text, reply_markup=kb)
    elif data.startswith('uprice_set_disc_'):
        uid = data.removeprefix('uprice_set_disc_')
        context.user_data["uprice_user"] = uid
        context.user_data["uprice_mode"] = "disc"
        await query.message.reply_text(
            "٪ درصد رو بفرست؛ منفی = تخفیف، مثبت = اضافه.\nمثلاً: -20 (۲۰٪ تخفیف) یا 10 (۱۰٪ اضافه)\nلغو: /cancel"
        )
    elif data.startswith('uprice_set_price_'):
        uid = data.removeprefix('uprice_set_price_')
        context.user_data["uprice_user"] = uid
        context.user_data["uprice_mode"] = "price"
        rows = []
        for plan in _plans_store.plans:
            rows.append([InlineKeyboardButton(
                f"{plan.title} ({fa_num(plan.gb)} گیگ)", callback_data=f'uprice_pick_{uid}_{plan.slug}',
            )])
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data='pricing_user_menu')])
        await query.message.reply_text("کدوم پلن قیمت ثابت بگیره؟", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('uprice_pick_'):
        rest = data.removeprefix('uprice_pick_')
        uid, slug = rest.split("_", 1)
        context.user_data["uprice_user"] = uid
        context.user_data["uprice_plan"] = slug
        plan = _plans_store.get(slug)
        normal = _plans_store.pricing.price_for_plan(plan) if plan else 0
        await query.message.reply_text(
            f"قیمت ثابت «{plan.title if plan else slug}» برای مشتری {uid} به تومان بفرست\n"
            f"(قیمت عادی فعلی: {fa_num(normal)} — 0 = حذف)\nلغو: /cancel"
        )
    elif data.startswith('uprice_clear_'):
        uid = int(data.removeprefix('uprice_clear_'))
        _plans_store.set_user_discount(uid, None)
        prices = dict(_plans_store.pricing.user_prices.get(str(uid), {}))
        for slug in prices:
            _plans_store.set_user_plan_price(uid, slug, None)
        await localized_query_answer(query, f"🗑 قیمت‌های اختصاصی {uid} پاک شد.", show_alert=False)
        text, kb = build_user_pricing_menu()
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except BadRequest:
            pass

@rate_limited(admin_only=True)
async def assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /assign <TelegramID> <ClientID>")
            return
        tg_id = int(context.args[0])
        client_id = int(context.args[1])
        if tg_id <= 0 or client_id <= 0:
            await update.message.reply_text("❌ IDs Must Be a Positive Number.")
            return

        added, count = add_client_assignment(tg_id, client_id, owner_admin_id=update.effective_user.id)
        if added:
            await update.message.reply_text(f"✅ Client ID {client_id} added to Telegram ID {tg_id}. Total subscriptions: {count}")
        else:
            await update.message.reply_text(f"⚠️ Client ID {client_id} already assigned to Telegram ID {tg_id}")

        keyboard = [[InlineKeyboardButton("🔗 View Links", callback_data='manage_links')],
                   [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
        await update.message.reply_text("Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))
    except ValueError:
        await update.message.reply_text("❌ Wrong Format , Use Integer Numbers.")
    except Exception as e:
        logger.exception("Error in assign")
        await update.message.reply_text(f"❌ Error: {e}")


def add_client_assignment(telegram_id: int, client_id: int, *, owner_admin_id: int | None = None) -> tuple[bool, int]:
    """Add one normalized assignment and persist it; return (added, total).

    owner_admin_id binds the customer to that admin (multi-admin support
    routing). Defaults to the acting admin / primary admin.
    """
    if telegram_id <= 0 or client_id <= 0:
        raise ValueError("Telegram and client IDs must be positive")
    existing = telegram_clients.get(telegram_id, [])
    current = list(existing) if isinstance(existing, list) else [existing]
    current = [int(value) for value in current if value is not None]
    if client_id in current:
        return False, len(current)
    current.append(client_id)
    telegram_clients[telegram_id] = current
    _admin_registry.set_owner(telegram_id, owner_admin_id or ADMIN_TELEGRAM_ID)
    save_assignments()
    return True, len(current)


def find_client_by_id(clients: List[dict], client_id: int) -> dict | None:
    for client in clients:
        try:
            if int(client.get('id')) == client_id:
                return client
        except (TypeError, ValueError):
            continue
    return None


async def show_interactive_assignment_clients(query, page: int = 1) -> None:
    clients = await get_all_clients_list()
    assigned_ids = {
        int(client_id)
        for assigned in telegram_clients.values()
        for client_id in (assigned if isinstance(assigned, list) else [assigned])
    }
    available = []
    for client in clients:
        try:
            client_id = int(client.get('id'))
        except (TypeError, ValueError):
            continue
        if client_id > 0 and client_id not in assigned_ids:
            available.append((client_id, client))
    available.sort(key=lambda item: (str(item[1].get('name', '')).casefold(), item[0]))
    total_pages = max(1, (len(available) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * ITEMS_PER_PAGE
    rows = []
    for client_id, client in available[start:start + ITEMS_PER_PAGE]:
        name = str(client.get('name') or 'Unknown')
        desc = str(client.get('desc') or '')
        label = f"{name} — {desc}" if desc else name
        if len(label) > 48:
            label = f"{label[:45]}..."
        rows.append([InlineKeyboardButton(preserve_dynamic_text(label), callback_data=f'assign_pick_{client_id}')])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f'assign_page_{page - 1}'))
    navigation.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data='current_page'))
    if page < total_pages:
        navigation.append(InlineKeyboardButton("▶️", callback_data=f'assign_page_{page + 1}'))
    rows.append(navigation)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data='add_link_help')])
    message = (
        "👤 Interactive Assignment\n\nChoose an unassigned S-UI client, then Telegram will open its native user picker."
        if available else
        "✅ Every S-UI client is already linked. Use /assign if you intentionally need to link one client to another account."
    )
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(rows))


async def interactive_assign_user_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    pending = context.user_data.get('pending_interactive_assignment')
    shared = update.message.users_shared
    if not isinstance(pending, dict) or shared is None or shared.request_id != pending.get('request_id'):
        await update.message.reply_text("❌ This user selection is no longer active.", reply_markup=ReplyKeyboardRemove())
        return
    if len(shared.users) != 1:
        await update.message.reply_text("❌ Select exactly one Telegram account.", reply_markup=ReplyKeyboardRemove())
        return
    selected = shared.users[0]
    pending['telegram_id'] = int(selected.user_id)
    full_name = " ".join(part for part in (selected.first_name, selected.last_name) if part)
    pending['telegram_label'] = f"@{selected.username}" if selected.username else full_name or str(selected.user_id)
    await update.message.reply_text("✅ Telegram account selected.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(
        "🔗 Confirm Assignment\n\n"
        f"S-UI client: {preserve_dynamic_text(pending['client_name'])} (ID: {pending['client_id']})\n"
        f"Telegram: {preserve_dynamic_text(pending['telegram_label'])} (ID: {pending['telegram_id']})\n\n"
        "The person must start the bot before it can send them messages.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data='assign_confirm')],
            [InlineKeyboardButton("❌ Cancel", callback_data='assign_abort')],
        ]),
    )


async def interactive_assign_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    context.user_data.pop('pending_interactive_assignment', None)
    await update.message.reply_text("❌ Interactive assignment canceled.", reply_markup=ReplyKeyboardRemove())

@rate_limited(admin_only=True)
async def unlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) == 1:
            # Remove all assignments for this user
            tg_id = int(context.args[0])
            if tg_id in telegram_clients:
                del telegram_clients[tg_id]
                save_assignments()
                await update.message.reply_text(f"✅ All links for Telegram ID {tg_id} deleted.")
            else:
                await update.message.reply_text(f"❌ No links for Telegram ID {tg_id}")
        elif len(context.args) == 2:
            # Remove specific client ID from user
            tg_id = int(context.args[0])
            client_id = int(context.args[1])
            if tg_id in telegram_clients:
                current = telegram_clients[tg_id]
                if client_id in current:
                    current.remove(client_id)
                    if current:
                        telegram_clients[tg_id] = current
                    else:
                        del telegram_clients[tg_id]
                    save_assignments()
                    await update.message.reply_text(f"✅ Client ID {client_id} unlinked from Telegram ID {tg_id}")
                else:
                    await update.message.reply_text(f"❌ Client ID {client_id} not assigned to this user")
            else:
                await update.message.reply_text(f"❌ No links for Telegram ID {tg_id}")
        else:
            await update.message.reply_text("Usage: /unlink <TelegramID> [ClientID]")
    except ValueError:
        await update.message.reply_text("❌ Wrong Format , Use Integer Numbers.")
    except Exception as e:
        logger.exception("Error in unlink")
        await update.message.reply_text(f"❌ Error: {e}")

@rate_limited(admin_only=True)
async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /unblock <TelegramID>")
            return
        target_id = int(context.args[0])
        await rate_limiter.reset_user(target_id)
        await update.message.reply_text(f"✅ User {target_id} Unblocked.")
    except ValueError:
        await update.message.reply_text("❌ Wrong Format , Use Integer Numbers.")
    except Exception as e:
        logger.exception("Error in unblock")
        await update.message.reply_text(f"❌ Error: {e}")

# --- Create User Conversation Handlers ---
@rate_limited(admin_only=True)
async def create_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin Only")
        return ConversationHandler.END
    await update.message.reply_text(
        "👤 Create New User\n\n"
        "Please Enter a Usename:\n"
        "(Use English Alphabet & Numbers Only)\n\n"
        "Abort: /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    return CREATE_USER_NAME

async def create_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) < MIN_USERNAME_LEN:
        await update.message.reply_text(f"❌ Username Must Be At Least {MIN_USERNAME_LEN} Characters:")
        return CREATE_USER_NAME
    if len(name) > MAX_USERNAME_LEN:
        await update.message.reply_text(f"❌ Username Must Be At Most {MAX_USERNAME_LEN} Characters:")
        return CREATE_USER_NAME
    if not all(c.isalnum() or c in ('_', '-') for c in name):
        await update.message.reply_text("❌ Use English Alphabet & Numbers Only:")
        return CREATE_USER_NAME
    clients = await get_all_clients_list()
    if any(client.get('name') == name for client in clients):
        await update.message.reply_text("❌ Username Not Available , Choose Another:")
        return CREATE_USER_NAME
    context.user_data['new_client_name'] = name

    keyboard = create_inbounds_keyboard([], prefix="inbound")
    context.user_data['selected_inbounds'] = []
    await update.message.reply_text(
        f"✅ Username Registered: {name}\n\n"
        "📡 Choose Inbounds:\n"
        "(You Can Choose Multiple Options)",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CREATE_USER_INBOUNDS

async def create_user_inbound_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    data = query.data
    if data == 'inbound_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END
    if data == 'inbound_all':
        inbounds = get_current_inbounds()
        context.user_data['selected_inbounds'] = [inbound.get("id") for inbound in inbounds]
        selected_text = "✅ All Inbounds Selected."
    elif data == 'inbound_done':
        if not context.user_data.get('selected_inbounds'):
            await localized_query_answer(query, "❌ Must At Least Select 1 Inbound.", show_alert=True)
            return CREATE_USER_INBOUNDS
        keyboard = [
            [InlineKeyboardButton("♾️ Unlimited", callback_data='volume_unlimited')],
            [InlineKeyboardButton("❌ Abort", callback_data='create_cancel')]
        ]
        selected_names = [get_inbound_display_name(i) for i in context.user_data['selected_inbounds']]
        await query.edit_message_text(
            f"✅ Selected Inbounds:\n{', '.join(selected_names)}\n\n"
            "💾 Input Volume:\n"
            "(GB Format , Example: 50)\n"
            "Or Choose Unlimited.\n\n"
            "Abort: /cancel",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CREATE_USER_VOLUME
    elif data.startswith('inbound_'):
        inbound_id = int(data.split('_')[1])
        selected = context.user_data.get('selected_inbounds', [])
        if inbound_id in selected:
            selected.remove(inbound_id)
            selected_text = f"❌ Inbound {get_inbound_display_name(inbound_id)} Removed."
        else:
            selected.append(inbound_id)
            selected_text = f"✅ Inbound {get_inbound_display_name(inbound_id)} Selected."
        context.user_data['selected_inbounds'] = selected

    keyboard = create_inbounds_keyboard(context.user_data.get('selected_inbounds', []), prefix="inbound")
    selected_count = len(context.user_data.get('selected_inbounds', []))
    await query.edit_message_text(
        f"📡 Select Inbounds:\n({selected_count} Item Selected)\n\n{selected_text}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CREATE_USER_INBOUNDS

async def create_user_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'create_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data == 'volume_unlimited':
            context.user_data['new_client_volume'] = 0
            keyboard = [
                [InlineKeyboardButton("♾️ Unlimited", callback_data='expiry_unlimited')],
                [InlineKeyboardButton("❌ Abort", callback_data='create_cancel')]
            ]
            await query.edit_message_text(
                "✅ Volume: Unlimited\n\n"
                "⏰ Input Expiry:\n"
                "(Days Format , Example: 30)\n"
                "Or Choose Unlimited.\n\n"
                "Abort: /cancel",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return CREATE_USER_EXPIRY
    else:
        text = update.message.text.strip()
        try:
            volume_gb = float(text)
            if volume_gb <= 0:
                await update.message.reply_text("❌ Volume Should Be a Positive Number:")
                return CREATE_USER_VOLUME
            volume_bytes = int(volume_gb * 1024 * 1024 * 1024)
            context.user_data['new_client_volume'] = volume_bytes
            keyboard = [
                [InlineKeyboardButton("♾️ Unlimited", callback_data='expiry_unlimited')],
                [InlineKeyboardButton("❌ Abort", callback_data='create_cancel')]
            ]
            await update.message.reply_text(
                f"✅ Volume: {volume_gb} GB\n\n"
                "⏰ Input Expiry:\n"
                "(Days Format , Example: 30)\n"
                "Or Choose Unlimited.\n\n"
                "Abort: /cancel",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return CREATE_USER_EXPIRY
        except ValueError:
            await update.message.reply_text("❌ Wrong Format , Input Numbers Only:")
            return CREATE_USER_VOLUME

async def create_user_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'create_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data == 'expiry_unlimited':
            context.user_data['new_client_expiry'] = 0
            await query.edit_message_text(
                "✅ Expiry: Unlimited\n\n"
                "📝 Input Description:\n"
                "(Optional; example: Dad, Uncle, Friend)\n\n"
                "Abort: /cancel",
                reply_markup=create_optional_field_keyboard('desc'),
            )
            return CREATE_USER_DESC
    else:
        text = update.message.text.strip()
        try:
            days = int(text)
            if days <= 0:
                await update.message.reply_text("❌ Days Must Be a Positive Number:")
                return CREATE_USER_EXPIRY
            expiry_timestamp = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
            context.user_data['new_client_expiry'] = expiry_timestamp
            await update.message.reply_text(
                f"✅ Expiry: {days} Days\n\n"
                "📝 Input Description:\n"
                "(Optional; example: Dad, Uncle, Friend)\n\n"
                "Abort: /cancel",
                reply_markup=create_optional_field_keyboard('desc'),
            )
            return CREATE_USER_DESC
        except ValueError:
            await update.message.reply_text("❌ Wrong Format , Input Numbers Only:")
            return CREATE_USER_EXPIRY

def create_optional_field_keyboard(field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "leave_empty"), callback_data=f'create_{field}_empty')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "cancel"), callback_data='create_cancel')],
    ])


def edit_optional_field_keyboard(field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "keep_current"), callback_data=f'edit_{field}_keep')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "clear_value"), callback_data=f'edit_{field}_empty')],
        [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "cancel"), callback_data='edit_cancel')],
    ])


async def create_user_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'create_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data != 'create_desc_empty':
            return CREATE_USER_DESC
        desc = ''
        response = query
    else:
        desc = update.message.text.strip()
        response = update.message
    if len(desc) > MAX_DESC_LEN:
        await update.message.reply_text(f"❌ Description Must Be At Most {MAX_DESC_LEN} Characters.")
        return CREATE_USER_DESC
    context.user_data['new_client_desc'] = desc
    text = (
        f"✅ Description: {desc or 'Empty'}\n\n"
        "👥 Type a group name:\n"
        f"(Optional; maximum {MAX_GROUP_LEN} characters)\n\n"
        "Abort: /cancel"
    )
    if update.callback_query:
        await response.edit_message_text(text, reply_markup=create_optional_field_keyboard('group'))
    else:
        await response.reply_text(text, reply_markup=create_optional_field_keyboard('group'))
    return CREATE_USER_GROUP

async def create_user_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'create_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data != 'create_group_empty':
            return CREATE_USER_GROUP
        group = ''
        response = query
    else:
        group = update.message.text.strip()
        response = update.message
    if len(group) > MAX_GROUP_LEN:
        await update.message.reply_text(f"❌ Group must be at most {MAX_GROUP_LEN} characters.")
        return CREATE_USER_GROUP
    scope = admin_group_scope(update.effective_user.id)
    if scope is not None:
        norm = group.strip().casefold()
        if not norm or norm not in scope:
            allowed = ", ".join(sorted(_admin_registry.groups_of(update.effective_user.id) or []))
            await update.message.reply_text(
                f"❌ فقط می‌توانید کاربر را در گروه‌های مجاز خودتان بسازید:\n{allowed or '—'}"
            )
            return CREATE_USER_GROUP
    context.user_data['new_client_group'] = group
    text = (
        f"✅ Group: {group or 'Empty'}\n\n"
        "🗒️ Enter an administrative remark.\n"
        "This is optional and separate from the user description.\n\n"
        "Abort: /cancel"
    )
    if update.callback_query:
        await response.edit_message_text(text, reply_markup=create_optional_field_keyboard('remark'))
    else:
        await response.reply_text(text, reply_markup=create_optional_field_keyboard('remark'))
    return CREATE_USER_REMARK


def lifecycle_keyboard(prefix: str, include_keep: bool = False, user_id: int | None = None) -> InlineKeyboardMarkup:
    uid = ADMIN_TELEGRAM_ID if user_id is None else user_id
    rows = []
    if include_keep:
        rows.append([InlineKeyboardButton(tr(uid, "lifecycle_keep"), callback_data=f'{prefix}_lifecycle_keep')])
    rows.extend([
        [InlineKeyboardButton(tr(uid, "lifecycle_standard"), callback_data=f'{prefix}_lifecycle_regular')],
        [InlineKeyboardButton(tr(uid, "lifecycle_delayed"), callback_data=f'{prefix}_lifecycle_delayed_expiry')],
        [InlineKeyboardButton(tr(uid, "lifecycle_reset_now"), callback_data=f'{prefix}_lifecycle_reset_now')],
        [InlineKeyboardButton(tr(uid, "lifecycle_reset_first"), callback_data=f'{prefix}_lifecycle_reset_first')],
        [InlineKeyboardButton("❌ Abort", callback_data=f'{prefix}_cancel')],
    ])
    return InlineKeyboardMarkup(rows)


def lifecycle_fields(policy: str, days: int, now_timestamp: int | None = None) -> dict[str, int | bool]:
    """Return S-UI lifecycle fields matching ClientService.ResetClients semantics."""
    if policy == 'regular':
        return {"delayStart": False, "autoReset": False, "resetDays": 0, "nextReset": 0}
    if not 1 <= days <= 3650:
        raise ValueError("lifecycle days must be between 1 and 3650")
    if policy == 'delayed_expiry':
        return {"delayStart": True, "autoReset": False, "resetDays": days, "nextReset": 0}
    if policy == 'reset_first':
        return {"delayStart": True, "autoReset": True, "resetDays": days, "nextReset": 0}
    if policy == 'reset_now':
        base = now_timestamp if now_timestamp is not None else int(datetime.now(timezone.utc).timestamp())
        return {
            "delayStart": False,
            "autoReset": True,
            "resetDays": days,
            "nextReset": base + days * 24 * 60 * 60,
        }
    raise ValueError("unsupported lifecycle policy")


async def create_user_remark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'create_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data != 'create_remark_empty':
            return CREATE_USER_REMARK
        remark = ''
        response = query
    else:
        remark = update.message.text.strip()
        response = update.message
    if len(remark) > MAX_REMARK_LEN:
        await update.message.reply_text(f"❌ Remark must be at most {MAX_REMARK_LEN} characters.")
        return CREATE_USER_REMARK
    context.user_data['new_client_remark'] = remark
    text = tr(update.effective_user.id, "lifecycle_create_help")
    if update.callback_query:
        await response.edit_message_text(text, reply_markup=lifecycle_keyboard('create', user_id=update.effective_user.id))
    else:
        await response.reply_text(text, reply_markup=lifecycle_keyboard('create', user_id=update.effective_user.id))
    return CREATE_USER_LIFECYCLE


async def create_user_lifecycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if query.data == 'create_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END
    policy = query.data.removeprefix('create_lifecycle_')
    if policy == 'regular':
        fields = lifecycle_fields('regular', 0)
        context.user_data.update(
            new_client_delay_start=fields['delayStart'], new_client_auto_reset=fields['autoReset'],
            new_client_reset_days=fields['resetDays'], new_client_next_reset=fields['nextReset'],
        )
        return await finish_create_user(query, context)
    if policy not in {'delayed_expiry', 'reset_now', 'reset_first'}:
        return CREATE_USER_LIFECYCLE
    context.user_data['new_client_policy'] = policy
    prompt_key = "lifecycle_delayed_prompt" if policy == 'delayed_expiry' else "lifecycle_reset_prompt"
    prompt = tr(query.from_user.id, prompt_key)
    await query.edit_message_text(f"⚙️ {prompt}\n\nAbort: /cancel")
    return CREATE_USER_RESET_DAYS


async def create_user_reset_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except ValueError:
        days = 0
    if days <= 0 or days > 3650:
        await update.message.reply_text("❌ Enter a whole number from 1 to 3650:")
        return CREATE_USER_RESET_DAYS
    policy = context.user_data['new_client_policy']
    fields = lifecycle_fields(policy, days)
    if policy == 'delayed_expiry':
        context.user_data['new_client_expiry'] = 0
    context.user_data.update(
        new_client_delay_start=fields['delayStart'], new_client_auto_reset=fields['autoReset'],
        new_client_reset_days=fields['resetDays'], new_client_next_reset=fields['nextReset'],
    )
    return await finish_create_user(update.message, context)


async def finish_create_user(message, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data['new_client_name']
    inbounds = context.user_data['selected_inbounds']
    volume = context.user_data['new_client_volume']
    expiry = context.user_data['new_client_expiry']
    desc = context.user_data['new_client_desc']
    group = context.user_data['new_client_group']
    remark = context.user_data.get('new_client_remark', '')
    delay_start = context.user_data.get('new_client_delay_start', False)
    auto_reset = context.user_data.get('new_client_auto_reset', False)
    reset_days = context.user_data.get('new_client_reset_days', 0)
    next_reset = context.user_data.get('new_client_next_reset', 0)
    volume_str = "♾️ Unlimited" if volume == 0 else format_bytes(volume)
    expiry_str = (
        tr(ADMIN_TELEGRAM_ID, "delayed_expiry_value", days=reset_days)
        if expiry == 0 and delay_start and not auto_reset
        else "♾️ Unlimited" if expiry == 0 else calculate_remaining_time(expiry)
    )
    empty_text = tr(ADMIN_TELEGRAM_ID, "empty_value")
    selected_names = [get_inbound_display_name(i) for i in inbounds]
    policy_text = lifecycle_policy_text(delay_start, auto_reset, reset_days, ADMIN_TELEGRAM_ID)
    progress_text = (
        "⏳ Creating User...\n\n"
        f"👤 Username: {name}\n"
        f"📡 Inbounds: {', '.join(selected_names)}\n"
        f"💾 Volume: {volume_str}\n"
        f"⏰ Expiry: {expiry_str}\n"
        f"📝 Description: {desc or empty_text}\n"
        f"👥 Group: {group or empty_text}\n"
        f"🗒️ Remark: {remark or empty_text}\n"
        f"⚙️ Policy: {policy_text}"
    )
    if hasattr(message, 'edit_message_text'):
        await message.edit_message_text(progress_text)
        reply_target = message.message
    else:
        await message.reply_text(progress_text)
        reply_target = message
    client_data = build_client_data_new(
        name=name,
        volume_bytes=volume,
        expiry_timestamp=expiry,
        desc=desc,
        group=group,
        inbounds=inbounds,
        enable=True,
        remark=remark,
        delay_start=delay_start,
        auto_reset=auto_reset,
        reset_days=reset_days,
        next_reset=next_reset,
    )
    result = await create_or_edit_client("new", client_data)
    if result and result.get('success'):
        global clients_cache, clients_cache_time
        clients_cache = None
        clients_cache_time = 0
        await reply_target.reply_text(
            "✅ User Created Successfully.\n\n"
            f"👤 Username: {name}\n"
            f"📡 Inbounds: {', '.join(selected_names)}\n"
            f"💾 Volume: {volume_str}\n"
            f"⏰ Expiry: {expiry_str}\n"
            f"📝 Description: {desc or empty_text}\n"
            f"👥 Group: {group or empty_text}\n"
            f"🗒️ Remark: {remark or empty_text}\n"
            f"⚙️ Policy: {policy_text}\n\n"
            "Main Menu: /start"
        )
    else:
        error_msg = result.get('msg', 'Unknown Error') if result else f"Server Unresponsive{api_client.error_reason('apiv2/save')}"
        error_msg = preserve_dynamic_text(error_msg)
        await reply_target.reply_text(
            f"❌ Failed To Create User:\n{error_msg}\n\n"
            "Try Again: /createuser"
        )
    context.user_data.clear()
    return ConversationHandler.END

async def create_user_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Creating User Aborted.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


async def workflow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel whichever single stateful workflow is currently active."""
    context.user_data.clear()
    await update.message.reply_text("❌ Operation canceled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def mixed_conversation_handler(**kwargs) -> ConversationHandler:
    """Build a mixed message/callback workflow without PTB's generic advisory."""
    # This workflow intentionally keys state by chat and user, because it accepts
    # both ordinary messages and callback queries. per_message=True cannot be used
    # for mixed handlers. The advisory is understood and does not indicate a fault.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message\..*",
            category=PTBUserWarning,
        )
        return ConversationHandler(**kwargs)

async def delete_client(client_id: int) -> dict:
    await api_client.ensure_session()
    url = f"{api_client.base_url}/apiv2/save"
    try:
        data_payload = {"object": "clients", "action": "del", "data": str(client_id)}
        async with api_client.session.post(url, data=data_payload) as response:
            response.raise_for_status()
            return await api_client.decode_json_response(response)
    except Exception as e:
        logger.error(f"Failed to delete client {client_id}: {e}")
        return None

# --- Edit User Conversation Handlers ---
@rate_limited(admin_only=True)
async def edit_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin Only")
        return ConversationHandler.END
    await update.message.reply_text(
        "📝 Edit User\n\n"
        "Please Input The User's Client ID :\n\n"
        "Abort: /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    return EDIT_USER_GET_ID

async def edit_user_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client_id_str = update.message.text.strip()
    try:
        client_id = int(client_id_str)
        if client_id <= 0:
            await update.message.reply_text("❌ Client ID Must Be a Positive Number:")
            return EDIT_USER_GET_ID
    except ValueError:
        await update.message.reply_text("❌ Wrong Format , Use Integer Numbers. ")
        return EDIT_USER_GET_ID

    data = await api_client.get('apiv2/clients', {'id': client_id})
    clients = sui_clients(data)
    if not clients:
        await update.message.reply_text("❌ User With This Client ID Not Found:")
        return EDIT_USER_GET_ID

    client = clients[0]
    if not client_in_admin_scope(client, admin_group_scope(update.effective_user.id)):
        await update.message.reply_text("❌ این کاربر خارج از گروه‌های مجاز شماست:")
        return EDIT_USER_GET_ID
    context.user_data['editing_client_id'] = client_id
    context.user_data['original_client_data'] = client.copy()

    context.user_data['edited_client_name'] = client.get('name')
    context.user_data['edited_selected_inbounds'] = client.get('inbounds', [])
    context.user_data['edited_client_volume'] = client.get('volume', 0)
    context.user_data['edited_client_expiry'] = client.get('expiry', 0)
    context.user_data['edited_client_desc'] = client.get('desc')
    context.user_data['edited_client_group'] = client.get('group')
    context.user_data['edited_client_enable'] = client.get('enable', True)
    context.user_data['edited_client_remark'] = client.get('remark', '')
    context.user_data['edited_client_delay_start'] = bool(client.get('delayStart', False))
    context.user_data['edited_client_auto_reset'] = bool(client.get('autoReset', False))
    context.user_data['edited_client_reset_days'] = int(client.get('resetDays', 0) or 0)
    context.user_data['edited_client_next_reset'] = int(client.get('nextReset', 0) or 0)

    current_name = context.user_data['edited_client_name']
    await update.message.reply_text(
        f"✅ User {client_id} (Username: {current_name}) Selected.\n\n"
        f"👤 Input New Username . Default:(`{md_escape(current_name)}`)\n"
        "To Keep Current Name Input ' . '\n\n"
        "Abort: /cancel"
    )
    return EDIT_USER_NAME

async def edit_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    original_name = context.user_data['original_client_data'].get('name')

    if name == '.':
        name = original_name
    elif not name or len(name) < MIN_USERNAME_LEN:
        await update.message.reply_text(f"❌ Username Must Be At Least {MIN_USERNAME_LEN} Characters:")
        return EDIT_USER_NAME
    elif len(name) > MAX_USERNAME_LEN:
        await update.message.reply_text(f"❌ Username Must Be At Most {MAX_USERNAME_LEN} Characters:")
        return EDIT_USER_NAME
    elif not all(c.isalnum() or c in ('_', '-') for c in name):
        await update.message.reply_text("❌ Use English Alphabet & Numbers Only:")
        return EDIT_USER_NAME
    else:
        clients = await get_all_clients_list()
        if any(client.get('name') == name and client.get('id') != context.user_data['editing_client_id'] for client in clients):
            await update.message.reply_text("❌ Username Not Available , Choose Another:")
            return EDIT_USER_NAME

    context.user_data['edited_client_name'] = name

    keyboard = create_inbounds_keyboard(context.user_data.get('edited_selected_inbounds', []), prefix="edit_inbound")
    selected_count = len(context.user_data.get('edited_selected_inbounds', []))
    await update.message.reply_text(
        f"✅ New Name Registered. {name}\n\n"
        "📡 Choose Inbounds:\n"
        f"({selected_count} Items Selected)\n"
        "Abort: /cancel",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_USER_INBOUNDS

async def edit_user_inbound_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    data = query.data

    if data == 'edit_inbound_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END
    elif data == 'edit_inbound_all':
        inbounds = get_current_inbounds()
        context.user_data['edited_selected_inbounds'] = [inbound.get("id") for inbound in inbounds]
        selected_text = "✅ All Inbounds Selected."
    elif data == 'edit_inbound_done':
        if not context.user_data.get('edited_selected_inbounds'):
            await localized_query_answer(query, "❌ Choose At Least 1 Inbound.", show_alert=True)
            return EDIT_USER_INBOUNDS

        current_volume_bytes = context.user_data['edited_client_volume']
        current_volume_gb = current_volume_bytes / (1024 * 1024 * 1024) if current_volume_bytes > 0 else "Unlimited"

        keyboard = [
            [InlineKeyboardButton("♾️ Unlimited", callback_data='edit_volume_unlimited')],
            [InlineKeyboardButton("❌ Abort", callback_data='edit_cancel')]
        ]
        selected_names = [get_inbound_display_name(i) for i in context.user_data['edited_selected_inbounds']]
        await query.edit_message_text(
            f"✅ Selected Inbounds:\n{', '.join(selected_names)}\n\n"
            "💾 Input New Volume:\n"
            f"(Default: `{current_volume_gb} GB`)\n"
            "To Keep Current Volume Input ' . '\n"
            "Or Choose Unlimited\n\n"
            "Abort: /cancel",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_USER_VOLUME
    elif data.startswith('edit_inbound_'):
        inbound_id = int(data.split('_')[2])
        selected = context.user_data.get('edited_selected_inbounds', [])
        if inbound_id in selected:
            selected.remove(inbound_id)
            selected_text = f"❌ Inbound {get_inbound_display_name(inbound_id)} Removed."
        else:
            selected.append(inbound_id)
            selected_text = f"✅ Inbound {get_inbound_display_name(inbound_id)} Selected."
        context.user_data['edited_selected_inbounds'] = selected

    keyboard = create_inbounds_keyboard(context.user_data.get('edited_selected_inbounds', []), prefix="edit_inbound")
    selected_count = len(context.user_data.get('edited_selected_inbounds', []))
    await query.edit_message_text(
        f"📡 Choose Inbounds:\n({selected_count} Items Selected)\n\n"
        f"Last Change: {selected_text if 'selected_text' in locals() else 'Unchanged'}\n\n"
        "Abort: /cancel",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_USER_INBOUNDS

async def edit_user_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'edit_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        elif query.data == 'edit_volume_unlimited':
            context.user_data['edited_client_volume'] = 0

            current_expiry_ts = context.user_data['edited_client_expiry']
            current_expiry_str = "Unlimited" if current_expiry_ts == 0 else calculate_remaining_time(current_expiry_ts).replace(' Days', '')

            keyboard = [
                [InlineKeyboardButton("♾️ Unlimited", callback_data='edit_expiry_unlimited')],
                [InlineKeyboardButton("❌ Abort", callback_data='edit_cancel')]
            ]
            await query.edit_message_text(
                "✅ Volume: Unlimited\n\n"
                "⏰ Input New Expiry:\n"
                f"(Default: `{current_expiry_str} Days`)\n"
                "To Keep Current Expiry Input ' . '\n"
                "Or Choose Unlimited\n\n"
                "Abort: /cancel",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return EDIT_USER_EXPIRY
    else:
        text = update.message.text.strip()
        if text == '.':
            pass
        else:
            try:
                volume_gb = float(text)
                if volume_gb <= 0:
                    await update.message.reply_text("❌ Volume Should Be a Positive Number:")
                    return EDIT_USER_VOLUME
                volume_bytes = int(volume_gb * 1024 * 1024 * 1024)
                context.user_data['edited_client_volume'] = volume_bytes
            except ValueError:
                await update.message.reply_text("❌ Wrong Format , Input Only Numbers Or ' . '")
                return EDIT_USER_VOLUME

        current_expiry_ts = context.user_data['edited_client_expiry']
        current_expiry_str = "Unlimited" if current_expiry_ts == 0 else calculate_remaining_time(current_expiry_ts).replace(' Days', '')

        keyboard = [
            [InlineKeyboardButton("♾️ Unlimited", callback_data='edit_expiry_unlimited')],
            [InlineKeyboardButton("❌ Abort", callback_data='edit_cancel')]
        ]
        await update.message.reply_text(
            f"✅ Volume: {format_bytes(context.user_data['edited_client_volume'])}\n\n"
            "⏰ Input New Expiry:\n"
            f"(Default: `{current_expiry_str} Days`)\n"
            "To Keep Current Expiry Input ' . '\n"
            "Or Choose Unlimited\n\n"
            "Abort: /cancel",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_USER_EXPIRY

async def edit_user_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'edit_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        elif query.data == 'edit_expiry_unlimited':
            context.user_data['edited_client_expiry'] = 0

            current_desc = md_escape(context.user_data['edited_client_desc'])
            await query.edit_message_text(
                "✅ Expiry: Unlimited\n\n"
                "📝 Input New Description:\n"
                f"(Default: `{current_desc}`)\n"
                "This field is optional. Type a value or use a button below.\n\n"
                "Abort: /cancel",
                reply_markup=edit_optional_field_keyboard('desc'),
            )
            return EDIT_USER_DESC
    else:
        text = update.message.text.strip()
        if text == '.':
            pass
        else:
            try:
                days = int(text)
                if days <= 0:
                    await update.message.reply_text("❌ Days Must Be a Positive Number:")
                    return EDIT_USER_EXPIRY
                expiry_timestamp = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
                context.user_data['edited_client_expiry'] = expiry_timestamp
            except ValueError:
                await update.message.reply_text("❌ Wrong Format , Input Numbers Only Or ' . ':")
                return EDIT_USER_EXPIRY

        current_desc = md_escape(context.user_data['edited_client_desc'])
        await update.message.reply_text(
            f"✅ Expiry: {calculate_remaining_time(context.user_data['edited_client_expiry'])}\n\n"
            "📝 Input New Description:\n"
            f"(Default: `{current_desc}`)\n"
            "This field is optional. Type a value or use a button below.\n\n"
            "Abort: /cancel",
            reply_markup=edit_optional_field_keyboard('desc'),
        )
        return EDIT_USER_DESC

async def edit_user_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_desc = str(context.user_data.get('edited_client_desc') or '')
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'edit_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data == 'edit_desc_keep':
            desc = current_desc
        elif query.data == 'edit_desc_empty':
            desc = ''
        else:
            return EDIT_USER_DESC
        response = query
    else:
        desc = update.message.text.strip()
        if desc == '.':
            desc = current_desc
        response = update.message
    if len(desc) > MAX_DESC_LEN:
        await update.message.reply_text(f"❌ Description Must Be At Most {MAX_DESC_LEN} Characters.")
        return EDIT_USER_DESC

    context.user_data['edited_client_desc'] = desc

    current_group = context.user_data['edited_client_group']
    text = (
        f"✅ Description: {desc or 'Empty'}\n\n"
        "👥 Type a new group name:\n"
        f"(Current: {current_group or 'empty'})\n"
        f"(Optional; maximum {MAX_GROUP_LEN} characters)\n\n"
        "Abort: /cancel"
    )
    if update.callback_query:
        await response.edit_message_text(text, reply_markup=edit_optional_field_keyboard('group'))
    else:
        await response.reply_text(text, reply_markup=edit_optional_field_keyboard('group'))
    return EDIT_USER_GROUP

async def edit_user_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_group = str(context.user_data.get('edited_client_group') or '')
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'edit_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data == 'edit_group_keep':
            group = current_group
        elif query.data == 'edit_group_empty':
            group = ''
        else:
            return EDIT_USER_GROUP
        response = query
    else:
        group = update.message.text.strip()
        if group == '.':
            group = current_group
        response = update.message
    if len(group) > MAX_GROUP_LEN:
        await update.message.reply_text(f"❌ Group must be at most {MAX_GROUP_LEN} characters.")
        return EDIT_USER_GROUP
    scope = admin_group_scope(update.effective_user.id)
    if scope is not None:
        norm = group.strip().casefold()
        if not norm or norm not in scope:
            allowed = ", ".join(sorted(_admin_registry.groups_of(update.effective_user.id) or []))
            await update.message.reply_text(
                f"❌ فقط می‌توانید کاربر را در گروه‌های مجاز خودتان نگه دارید:\n{allowed or '—'}"
            )
            return EDIT_USER_GROUP
    context.user_data['edited_client_group'] = group

    current_remark = context.user_data.get('edited_client_remark', '')
    text = (
        f"✅ Group: {group or 'Empty'}\n\n"
        "🗒️ Enter a new administrative remark.\n"
        f"Current: {current_remark or 'empty'}\n"
        "This field is optional. Type a value or use a button below.\n\n"
        "Abort: /cancel"
    )
    if update.callback_query:
        await response.edit_message_text(text, reply_markup=edit_optional_field_keyboard('remark'))
    else:
        await response.reply_text(text, reply_markup=edit_optional_field_keyboard('remark'))
    return EDIT_USER_REMARK


async def edit_user_remark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_remark = str(context.user_data.get('edited_client_remark') or '')
    if update.callback_query:
        query = update.callback_query
        await localized_query_answer(query)
        if query.data == 'edit_cancel':
            await query.edit_message_text("❌ Operation Aborted.")
            context.user_data.clear()
            return ConversationHandler.END
        if query.data == 'edit_remark_keep':
            remark = current_remark
        elif query.data == 'edit_remark_empty':
            remark = ''
        else:
            return EDIT_USER_REMARK
        response = query
    else:
        remark = update.message.text.strip()
        if remark == '.':
            remark = current_remark
        elif remark == '-':
            remark = ''
        response = update.message
    if len(remark) > MAX_REMARK_LEN:
        await update.message.reply_text(f"❌ Remark must be at most {MAX_REMARK_LEN} characters.")
        return EDIT_USER_REMARK
    context.user_data['edited_client_remark'] = remark
    current_policy = lifecycle_policy_text(
        context.user_data['edited_client_delay_start'],
        context.user_data['edited_client_auto_reset'],
        context.user_data['edited_client_reset_days'],
        update.effective_user.id,
    )
    text = (
        "⚙️ Choose the expiry/reset policy.\n\n"
        f"Current: {current_policy}\n\n"
        f"{tr(update.effective_user.id, 'lifecycle_edit_help')}"
    )
    if update.callback_query:
        await response.edit_message_text(
            text, reply_markup=lifecycle_keyboard('edit', include_keep=True, user_id=update.effective_user.id)
        )
    else:
        await response.reply_text(
            text, reply_markup=lifecycle_keyboard('edit', include_keep=True, user_id=update.effective_user.id)
        )
    return EDIT_USER_LIFECYCLE


def lifecycle_policy_text(
    delay_start: bool, auto_reset: bool, reset_days: int, user_id: int = ADMIN_TELEGRAM_ID
) -> str:
    if delay_start and not auto_reset:
        return tr(user_id, "lifecycle_policy_delayed", days=reset_days)
    if auto_reset:
        key = "lifecycle_policy_reset_first" if delay_start else "lifecycle_policy_reset_now"
        return tr(user_id, key, days=reset_days)
    return tr(user_id, "lifecycle_policy_standard")


async def prompt_edit_enable(message, context: ContextTypes.DEFAULT_TYPE):
    current_enable_status = context.user_data['edited_client_enable']
    keyboard = [
        [InlineKeyboardButton(f"✅ Enable {'✅' if current_enable_status else ''}", callback_data='edit_enable_true')],
        [InlineKeyboardButton(f"❌ Disable {'✅' if not current_enable_status else ''}", callback_data='edit_enable_false')],
        [InlineKeyboardButton("❌ Abort", callback_data='edit_cancel')]
    ]
    text = "⚡ Choose Active/Deactive State Of The User:"
    if hasattr(message, 'edit_message_text'):
        await message.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_USER_ENABLE


async def edit_user_lifecycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if query.data == 'edit_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END
    policy = query.data.removeprefix('edit_lifecycle_')
    if policy == 'keep':
        return await prompt_edit_enable(query, context)
    if policy == 'regular':
        fields = lifecycle_fields('regular', 0)
        context.user_data.update(
            edited_client_delay_start=fields['delayStart'], edited_client_auto_reset=fields['autoReset'],
            edited_client_reset_days=fields['resetDays'], edited_client_next_reset=fields['nextReset'],
        )
        return await prompt_edit_enable(query, context)
    if policy not in {'delayed_expiry', 'reset_now', 'reset_first'}:
        return EDIT_USER_LIFECYCLE
    context.user_data['edited_client_policy'] = policy
    prompt_key = "lifecycle_delayed_prompt" if policy == 'delayed_expiry' else "lifecycle_reset_prompt"
    prompt = tr(query.from_user.id, prompt_key)
    await query.edit_message_text(f"⚙️ {prompt}\n\nAbort: /cancel")
    return EDIT_USER_RESET_DAYS


async def edit_user_reset_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except ValueError:
        days = 0
    if days <= 0 or days > 3650:
        await update.message.reply_text("❌ Enter a whole number from 1 to 3650:")
        return EDIT_USER_RESET_DAYS
    policy = context.user_data['edited_client_policy']
    fields = lifecycle_fields(policy, days)
    if policy == 'delayed_expiry':
        context.user_data['edited_client_expiry'] = 0
    context.user_data.update(
        edited_client_delay_start=fields['delayStart'], edited_client_auto_reset=fields['autoReset'],
        edited_client_reset_days=fields['resetDays'], edited_client_next_reset=fields['nextReset'],
    )
    return await prompt_edit_enable(update.message, context)

async def edit_user_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if query.data == 'edit_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END

    enable_status = query.data.split('_')[2] == 'true'
    context.user_data['edited_client_enable'] = enable_status
    keyboard = [
        [InlineKeyboardButton("🔄 Regenerate Secrets", callback_data='edit_regen_true')],
        [InlineKeyboardButton("🛡️ Keep Existing Secrets", callback_data='edit_regen_false')],
        [InlineKeyboardButton("❌ Abort", callback_data='edit_cancel')]
    ]
    await query.edit_message_text(
        "🔐 Choose Secrets Policy:\n\n"
        "Regenerate creates new passwords/UUIDs.\n"
        "Keep Existing preserves current config credentials.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_USER_REGEN

async def edit_user_regen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    if query.data == 'edit_cancel':
        await query.edit_message_text("❌ Operation Aborted.")
        context.user_data.clear()
        return ConversationHandler.END
    if query.data not in ('edit_regen_true', 'edit_regen_false'):
        return EDIT_USER_REGEN

    regenerate_secrets = (query.data == 'edit_regen_true')
    context.user_data['edited_regenerate_secrets'] = regenerate_secrets

    client_id = context.user_data['editing_client_id']
    name = context.user_data['edited_client_name']
    inbounds = context.user_data['edited_selected_inbounds']
    volume = context.user_data['edited_client_volume']
    expiry = context.user_data['edited_client_expiry']
    desc = context.user_data['edited_client_desc']
    group = context.user_data['edited_client_group']
    enable = context.user_data['edited_client_enable']
    remark = context.user_data['edited_client_remark']
    delay_start = context.user_data['edited_client_delay_start']
    auto_reset = context.user_data['edited_client_auto_reset']
    reset_days = context.user_data['edited_client_reset_days']
    next_reset = context.user_data['edited_client_next_reset']

    volume_str = "♾️ Unlimited" if volume == 0 else format_bytes(volume)
    expiry_str = (
        tr(query.from_user.id, "delayed_expiry_value", days=reset_days)
        if expiry == 0 and delay_start and not auto_reset
        else "♾️ Unlimited" if expiry == 0 else calculate_remaining_time(expiry)
    )
    empty_text = tr(query.from_user.id, "empty_value")
    selected_names = [get_inbound_display_name(i) for i in inbounds]
    enable_text = "✅ Enable" if enable else "❌ Disable"
    policy_text = lifecycle_policy_text(delay_start, auto_reset, reset_days, query.from_user.id)

    await query.edit_message_text(
        "⏳ Implementing Changes...\n\n"
        f"🆔 Client ID: {client_id}\n"
        f"👤 Username: {name}\n"
        f"📡 Inbounds: {', '.join(selected_names)}\n"
        f"💾 Volume: {volume_str}\n"
        f"⏰ Expiry: {expiry_str}\n"
        f"📝 Description: {desc or empty_text}\n"
        f"👥 Group: {group or empty_text}\n"
        f"🗒️ Remark: {remark or empty_text}\n"
        f"⚙️ Policy: {policy_text}\n"
        f"⚡ Status: {enable_text}\n"
        f"🔐 Secrets: {'Regenerated' if regenerate_secrets else 'Kept'}"
    )

    original_client = context.user_data['original_client_data']
    edited_data_for_api = build_client_data_edit(
        client_id=client_id,
        name=name,
        volume_bytes=volume,
        expiry_timestamp=expiry,
        desc=desc,
        group=group,
        inbounds=inbounds,
        enable=enable,
        regenerate_secrets=regenerate_secrets,
        original_client=original_client,
        remark=remark,
        delay_start=delay_start,
        auto_reset=auto_reset,
        reset_days=reset_days,
        next_reset=next_reset,
    )

    result = await create_or_edit_client("edit", edited_data_for_api)
    if result and result.get('success'):
        global clients_cache, clients_cache_time
        clients_cache = None
        clients_cache_time = 0
        await query.edit_message_text(
            "✅ User Successfully Edited.\n\n"
            f"🆔 Client ID: {client_id}\n"
            f"👤 Username: {name}\n"
            f"📡 Inbounds: {', '.join(selected_names)}\n"
            f"💾 Volume: {volume_str}\n"
            f"⏰ Expiry: {expiry_str}\n"
            f"📝 Description: {desc or empty_text}\n"
            f"👥 Group: {group or empty_text}\n"
            f"🗒️ Remark: {remark or empty_text}\n"
            f"⚙️ Policy: {policy_text}\n"
            f"⚡ status: {enable_text}\n"
            f"🔐 Secrets: {'Regenerated' if regenerate_secrets else 'Kept'}\n\n"
            "Main Menu: /start"
        )
    else:
        error_msg = result.get('msg', 'Unknown Error') if result else f"Server Unresponsive{api_client.error_reason('apiv2/save')}"
        error_msg = preserve_dynamic_text(error_msg)
        await query.edit_message_text(
            f"❌ Error Editing User:\n{error_msg}\n\n"
            "Try Again: /edituser"
        )
    context.user_data.clear()
    return ConversationHandler.END

async def edit_user_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Editing User Aborted.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- Delete User Conversation Handlers ---
@rate_limited(admin_only=True)
async def delete_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin Only")
        return ConversationHandler.END
    await update.message.reply_text(
        "🗑️ Delete User\n\n"
        "Please Input The User's Client ID:\n\n"
        "Abort: /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    return DELETE_USER_GET_ID

async def delete_user_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client_id_str = update.message.text.strip()
    try:
        client_id = int(client_id_str)
        if client_id <= 0:
            await update.message.reply_text("❌ Client ID Must Be a Positive Number:")
            return DELETE_USER_GET_ID
    except ValueError:
        await update.message.reply_text("❌ Wrong Format , Use Integer Numbers:")
        return DELETE_USER_GET_ID

    data = await api_client.get('apiv2/clients', {'id': client_id})
    clients = sui_clients(data)
    if not clients:
        await update.message.reply_text("❌ User With This Client ID Not Found:")
        return DELETE_USER_GET_ID

    client = clients[0]
    if not client_in_admin_scope(client, admin_group_scope(update.effective_user.id)):
        await update.message.reply_text("❌ این کاربر خارج از گروه‌های مجاز شماست:")
        return DELETE_USER_GET_ID
    client_name = client.get('name', 'Unknown')
    context.user_data['client_id_to_delete'] = client_id
    context.user_data['client_name_to_delete'] = client_name

    keyboard = [[InlineKeyboardButton("✅ Yes,Delete It", callback_data='delete_confirm_yes')],
                [InlineKeyboardButton("❌ No,Abort", callback_data='delete_confirm_no')]]

    await update.message.reply_text(
        f"⚠️ Are You Sure You Want To Delete User '{client_name}' (Client ID: {client_id}) ? This Action Can't Be Undone.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DELETE_USER_CONFIRM

async def delete_user_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await localized_query_answer(query)
    data = query.data

    if data == 'delete_confirm_yes':
        client_id = context.user_data.get('client_id_to_delete')
        client_name = context.user_data.get('client_name_to_delete', 'Unknown')
        if client_id is None:
            await query.edit_message_text("❌ Client ID Not Found. Try Again: /deleteuser")
            context.user_data.clear()
            return ConversationHandler.END

        await query.edit_message_text(f"⏳ Deleting User'{client_name}' (ID: {client_id})...")
        result = await delete_client(client_id)

        if result and result.get('success'):
            global clients_cache, clients_cache_time
            clients_cache = None
            clients_cache_time = 0

            unlinked_users = []
            for tg_id, assigned_list in list(telegram_clients.items()):
                if client_id in assigned_list:
                    assigned_list.remove(client_id)
                    if assigned_list:
                        telegram_clients[tg_id] = assigned_list
                    else:
                        del telegram_clients[tg_id]
                    unlinked_users.append(tg_id)

            if unlinked_users:
                save_assignments()
                logger.info(f"Auto-unlinked {len(unlinked_users)} Telegram IDs from deleted Client ID {client_id}")

            await query.edit_message_text(
                f"✅ User '{client_name}' (Client ID: {client_id}) Successfully Deleted.\n"
                f"🔗 Auto-unlinked from {len(unlinked_users)} Telegram user(s).\n"
                "Main Menu: /start"
            )
    else:
        await query.edit_message_text("❌ Operation Deleting User Aborted.")

    context.user_data.clear()
    return ConversationHandler.END

async def delete_user_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operation Deleting User Aborted.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

@rate_limited(admin_only=False)
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global HIDE_SUBSCRIPTION_PORT, WEB_PANEL_ENABLED, ADMIN_TIMEZONE, PAYMENT_CURRENCY
    query = update.callback_query
    await localized_query_answer(query)
    user_id = query.from_user.id
    data = query.data
    if not is_safe_callback_data(data):
        await localized_query_answer(query, "❌ Invalid action.", show_alert=True)
        return
    if not is_admin(user_id) and not is_public_callback(data):
        await localized_query_answer(query, "❌ Admin only", show_alert=True)
        return
    metrics.record_command(user_id, f"button_{data}")
    try:
        if data == 'support_contact':
            await support_contact_callback(update, context)
        elif data == 'support_send':
            await support_send_start(update, context)
        elif data == 'admin_finance_open':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Admin only", show_alert=True)
                return
            class _FinMsg:
                async def reply_text(self, *a, **k):
                    k.pop("reply_markup", None)
                    return await query.edit_message_text(*a, **k)
            _fake = update
            _fake._effective_message = _FinMsg()
            await admin_finance(_fake, context)
        elif data == 'res_panel_open':
            class _ResMsg:
                async def reply_text(self, *a, **k):
                    return await query.edit_message_text(*a, **k)
            _fake2 = update
            _fake2._effective_message = _ResMsg()
            await reseller_panel_update(update, context, _fake2)
        elif data == 'res_settle_request':
            await _res_settle_request(update, context)
        elif data == 'language_settings':
            await query.edit_message_text(tr(user_id, "choose_language"), reply_markup=language_keyboard())
        elif data.startswith('lang_set_'):
            language = data.removeprefix('lang_set_')
            if language not in SUPPORTED_LANGUAGES:
                await localized_query_answer(query, "Invalid language", show_alert=True)
                return
            await asyncio.to_thread(language_store.set, user_id, language)
            admin_flag = is_admin(user_id)
            await query.edit_message_text(
                f"{tr(user_id, 'language_saved')}\n\n{tr(user_id, 'welcome')}",
                reply_markup=get_main_menu_keyboard(admin_flag, user_id),
            )
            context.chat_data["menu_message_id"] = query.message.message_id
        elif data == 'main_menu':
            admin_flag = is_admin(user_id)
            keyboard = get_main_menu_keyboard(admin_flag, user_id)
            await query.edit_message_text(tr(user_id, "welcome"), reply_markup=keyboard)
            context.chat_data["menu_message_id"] = query.message.message_id
        elif data == 'admin_settings':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            await query.edit_message_text(
                build_settings_menu_text(),
                reply_markup=build_settings_menu_keyboard()
            )
        elif data == 'settings_payments':
            await query.edit_message_text(
                build_payment_settings_text(), reply_markup=build_payment_settings_keyboard()
            )
        elif data == 'settings_admin_tools':
            await query.edit_message_text(
                build_admin_tools_settings_text(), reply_markup=build_admin_tools_settings_keyboard()
            )
        elif data == 'settings_admins':
            context.user_data.pop("admin_add_pending", None)
            context.user_data.pop("admin_group_editor", None)
            await query.edit_message_text(
                build_admins_text(), reply_markup=build_admins_keyboard(user_id), parse_mode="HTML"
            )
        elif data == 'settings_admin_add':
            if user_id != ADMIN_TELEGRAM_ID:
                await localized_query_answer(query, "❌ Only primary admin", show_alert=True)
                return
            context.user_data["admin_add_pending"] = True
            await query.edit_message_text(
                "➕ آیدی عددی تلگرام ادمین جدید را بفرستید (مثلاً <code>123456789</code>).",
                reply_markup=build_admin_add_keyboard(), parse_mode="HTML",
            )
        elif data.startswith('settings_admin_del_'):
            if user_id != ADMIN_TELEGRAM_ID:
                await localized_query_answer(query, "❌ Only primary admin", show_alert=True)
                return
            try:
                target = int(data.removeprefix('settings_admin_del_'))
            except ValueError:
                await localized_query_answer(query, "❌ Invalid id", show_alert=True)
                return
            result = runtime_remove_admin(target)
            if result == "removed":
                await _refresh_admin_scopes(context)
                await localized_query_answer(query, f"✅ Admin {target} removed.", show_alert=False)
            elif result == "primary":
                await localized_query_answer(query, "❌ Cannot remove primary admin.", show_alert=True)
                return
            else:
                await localized_query_answer(query, "❌ Admin not found.", show_alert=True)
                return
            await query.edit_message_text(
                build_admins_text(), reply_markup=build_admins_keyboard(user_id), parse_mode="HTML"
            )
        elif data.startswith('settings_admin_groups_'):
            if user_id != ADMIN_TELEGRAM_ID:
                await localized_query_answer(query, "❌ Only primary admin", show_alert=True)
                return
            payload = data.removeprefix('settings_admin_groups_')
            state = context.user_data.get("admin_group_editor")
            if payload.startswith('toggle_'):
                if not state:
                    await query.edit_message_text(
                        build_admins_text(), reply_markup=build_admins_keyboard(user_id), parse_mode="HTML"
                    )
                    return
                try:
                    idx = int(payload.removeprefix('toggle_'))
                except ValueError:
                    return
                if 0 <= idx < len(state["all"]):
                    chosen = state["all"][idx]
                    sel = set(state["sel"])
                    if chosen in sel:
                        sel.discard(chosen)
                    else:
                        sel.add(chosen)
                    state["sel"] = sorted(sel)
                    state["unrestricted"] = False
                await query.edit_message_text(
                    build_admin_groups_text(state), reply_markup=build_admin_groups_keyboard(state), parse_mode="HTML"
                )
                return
            if payload == "none":
                await localized_query_answer(query, "⚠️ ابتدا در پنل S-UI گروه بسازید.", show_alert=True)
                return
            if payload in {"save", "unlimited"}:
                if not state:
                    await query.edit_message_text(
                        build_admins_text(), reply_markup=build_admins_keyboard(user_id), parse_mode="HTML"
                    )
                    return
                if payload == "save":
                    _admin_registry.set_groups(state["admin"], state["sel"])
                    await localized_query_answer(query, "✅ دسترسی گروهی ذخیره شد.", show_alert=False)
                else:
                    _admin_registry.clear_groups(state["admin"])
                    await localized_query_answer(query, "♾ دسترسی کامل برگردانده شد.", show_alert=False)
                context.user_data.pop("admin_group_editor", None)
                await query.edit_message_text(
                    build_admins_text(), reply_markup=build_admins_keyboard(user_id), parse_mode="HTML"
                )
                return
            if payload.isdigit():
                target = int(payload)
                if target == ADMIN_TELEGRAM_ID:
                    await localized_query_answer(query, "⭐ ادمین اصلی همیشه بدون محدودیت است.", show_alert=True)
                    return
                clients = await get_all_clients_list()
                all_groups = sorted({
                    str(c.get("group") or "").strip() for c in clients or [] if str(c.get("group") or "").strip()
                })
                current = _admin_registry.groups_of(target)
                state = {
                    "admin": target,
                    "all": all_groups,
                    "sel": list(current) if current is not None else list(all_groups),
                    "unrestricted": current is None,
                }
                context.user_data["admin_group_editor"] = state
                await query.edit_message_text(
                    build_admin_groups_text(state), reply_markup=build_admin_groups_keyboard(state), parse_mode="HTML"
                )
                return
        elif data == 'settings_timezone':
            rows = [
                [InlineKeyboardButton(
                    f"{'✅ ' if key == ADMIN_TIMEZONE else ''}{label}",
                    callback_data=f"settings_timezone_set_{key.lower()}",
                )]
                for key, (_, label) in ADMIN_TIMEZONES.items()
            ]
            rows.append([InlineKeyboardButton("🔙 Back", callback_data='settings_admin_tools')])
            await query.edit_message_text(
                "🕐 Administrative Timezone\n\nCreated and last-online timestamps will use this timezone.",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif data.startswith('settings_timezone_set_'):
            selected = data.removeprefix('settings_timezone_set_').upper()
            if selected not in ADMIN_TIMEZONES:
                await localized_query_answer(query, "❌ Invalid timezone.", show_alert=True)
                return
            ADMIN_TIMEZONE = selected
            save_runtime_setting("ADMIN_TIMEZONE", selected, RUNTIME_SETTINGS_FILE)
            await query.edit_message_text(
                build_admin_tools_settings_text(), reply_markup=build_admin_tools_settings_keyboard()
            )
            await localized_query_answer(query, "✅ Administrative timezone updated.", show_alert=True)
        elif data == 'settings_currency':
            rows = [
                [InlineKeyboardButton(
                    f"{'✅ ' if key == PAYMENT_CURRENCY else ''}{localized_currency_name(user_id, key)}",
                    callback_data=f"settings_currency_set_{key.lower()}",
                )]
                for key in PAYMENT_CURRENCIES
            ]
            rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data='settings_payments')])
            await query.edit_message_text(
                f"{tr(user_id, 'currency_title')}\n\n{tr(user_id, 'currency_help')}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif data.startswith('settings_currency_set_'):
            selected = data.removeprefix('settings_currency_set_').upper()
            if selected not in PAYMENT_CURRENCIES:
                await localized_query_answer(query, tr(user_id, "currency_invalid"), show_alert=True)
                return
            PAYMENT_CURRENCY = selected
            save_runtime_setting("PAYMENT_CURRENCY", selected, RUNTIME_SETTINGS_FILE)
            await query.edit_message_text(
                build_payment_settings_text(), reply_markup=build_payment_settings_keyboard()
            )
            await localized_query_answer(
                query, tr(user_id, "currency_updated"), show_alert=True
            )
        elif data == 'settings_connection_guides':
            await query.edit_message_text(
                build_connection_guides_admin_text(user_id),
                reply_markup=build_connection_guides_admin_keyboard(user_id),
            )
        elif data == 'settings_guides_toggle':
            try:
                await asyncio.to_thread(connection_guide_store.set_enabled, not connection_guide_store.enabled)
            except ValueError:
                await localized_query_answer(query, tr(user_id, "guide_need_one"), show_alert=True)
                return
            await query.edit_message_text(
                build_connection_guides_admin_text(user_id),
                reply_markup=build_connection_guides_admin_keyboard(user_id),
            )
            notice = "guide_enabled_notice" if connection_guide_store.enabled else "guide_disabled_notice"
            await localized_query_answer(query, tr(user_id, notice), show_alert=True)
        elif data == 'settings_guides_delete':
            rows = [
                [InlineKeyboardButton(
                    preserve_dynamic_text(guide["title"]),
                    callback_data=f"settings_guides_delete_{guide['id']}",
                )]
                for guide in connection_guide_store.list_guides()
            ]
            rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="settings_connection_guides")])
            await query.edit_message_text(
                tr(user_id, "guide_choose_delete"),
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif data == 'settings_guides_edit':
            rows = [[InlineKeyboardButton(
                preserve_dynamic_text(guide["title"]), callback_data=f"settings_guides_edit_{guide['id']}"
            )] for guide in connection_guide_store.list_guides()]
            rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="settings_connection_guides")])
            await query.edit_message_text(tr(user_id, "guide_choose_edit"), reply_markup=InlineKeyboardMarkup(rows))
        elif data.startswith('settings_guides_item_delete_'):
            payload = data.removeprefix('settings_guides_item_delete_')
            guide_id, raw_index = payload.rsplit('_', 1)
            try:
                deleted = await asyncio.to_thread(connection_guide_store.delete_message, guide_id, int(raw_index))
            except ValueError:
                await localized_query_answer(query, tr(user_id, "guide_cannot_delete_last"), show_alert=True)
                return
            guide = connection_guide_store.get(guide_id)
            if not deleted or guide is None:
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                build_connection_guide_edit_text(user_id, guide),
                reply_markup=build_connection_guide_edit_keyboard(user_id, guide),
            )
            await localized_query_answer(query, tr(user_id, "guide_item_deleted"), show_alert=True)
        elif data.startswith('settings_guides_item_'):
            payload = data.removeprefix('settings_guides_item_')
            guide_id, raw_index = payload.rsplit('_', 1)
            guide = connection_guide_store.get(guide_id)
            index = int(raw_index)
            if guide is None or not 0 <= index < len(guide["messages"]):
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                guide_item_label(index, guide["messages"][index]),
                reply_markup=build_connection_guide_item_keyboard(user_id, guide_id, index),
            )
        elif data.startswith('settings_guides_edit_'):
            guide_id = data.removeprefix('settings_guides_edit_')
            guide = connection_guide_store.get(guide_id)
            if guide is None:
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                build_connection_guide_edit_text(user_id, guide),
                reply_markup=build_connection_guide_edit_keyboard(user_id, guide),
            )
        elif data.startswith('settings_guides_delete_confirm_'):
            guide_id = data.removeprefix('settings_guides_delete_confirm_')
            await asyncio.to_thread(connection_guide_store.delete, guide_id)
            await query.edit_message_text(
                build_connection_guides_admin_text(user_id),
                reply_markup=build_connection_guides_admin_keyboard(user_id),
            )
            await localized_query_answer(query, tr(user_id, "guide_deleted"), show_alert=True)
        elif data.startswith('settings_guides_delete_'):
            guide_id = data.removeprefix('settings_guides_delete_')
            guide = connection_guide_store.get(guide_id)
            if guide is None:
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                tr(user_id, "guide_delete_confirm", title=preserve_dynamic_text(guide["title"])),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        tr(user_id, "confirm_delete"),
                        callback_data=f"settings_guides_delete_confirm_{guide_id}",
                    )],
                    [InlineKeyboardButton(tr(user_id, "cancel"), callback_data="settings_connection_guides")],
                ]),
            )
        elif data == 'settings_web_panel':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Admin only", show_alert=True)
                return
            if WEB_PANEL_ENABLED:
                WEB_PANEL_ENABLED = False
                save_runtime_setting("WEB_PANEL_ENABLED", "false", RUNTIME_SETTINGS_FILE)
                await query.edit_message_text(
                    build_admin_tools_settings_text(),
                    reply_markup=build_admin_tools_settings_keyboard(),
                )
                await localized_query_answer(query, tr(user_id, "web_panel_disabled_notice"), show_alert=True)
            else:
                await query.edit_message_text(
                    tr(user_id, "web_panel_warning"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            tr(user_id, "confirm_enable_web_panel"),
                            callback_data='settings_web_panel_confirm',
                        )],
                        [InlineKeyboardButton(tr(user_id, "cancel"), callback_data='settings_admin_tools')],
                    ]),
                )
        elif data == 'settings_web_panel_confirm':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Admin only", show_alert=True)
                return
            WEB_PANEL_ENABLED = True
            save_runtime_setting("WEB_PANEL_ENABLED", "true", RUNTIME_SETTINGS_FILE)
            await query.edit_message_text(
                build_admin_tools_settings_text(),
                reply_markup=build_admin_tools_settings_keyboard(),
            )
            notice_key = "web_panel_enabled_notice" if WEB_PANEL_BASE_URL else "web_panel_pending_notice"
            await localized_query_answer(query, tr(user_id, notice_key), show_alert=True)
        elif data == 'settings_subscription_port':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Admin only", show_alert=True)
                return
            if HIDE_SUBSCRIPTION_PORT:
                HIDE_SUBSCRIPTION_PORT = False
                save_runtime_setting("HIDE_SUBSCRIPTION_PORT", "false", RUNTIME_SETTINGS_FILE)
                await query.edit_message_text(
                    build_admin_tools_settings_text(),
                    reply_markup=build_admin_tools_settings_keyboard(),
                )
                await localized_query_answer(query, tr(user_id, "subscription_port_restored"), show_alert=True)
            else:
                await query.edit_message_text(
                    tr(user_id, "subscription_port_warning"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            tr(user_id, "confirm_remove_subscription_port"),
                            callback_data='settings_subscription_port_confirm',
                        )],
                        [InlineKeyboardButton(tr(user_id, "cancel"), callback_data='settings_admin_tools')],
                    ]),
                )
        elif data == 'settings_subscription_port_confirm':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Admin only", show_alert=True)
                return
            HIDE_SUBSCRIPTION_PORT = True
            save_runtime_setting("HIDE_SUBSCRIPTION_PORT", "true", RUNTIME_SETTINGS_FILE)
            await query.edit_message_text(
                build_admin_tools_settings_text(),
                reply_markup=build_admin_tools_settings_keyboard(),
            )
            await localized_query_answer(query, tr(user_id, "subscription_port_removed"), show_alert=True)
        elif data == 'settings_backup_restore':
            await query.edit_message_text(
                "💾 SUI Bot Backup & Restore\n\n"
                "Create one validated file containing assignments, user language choices, metrics, runtime settings, and cached bot data.\n\n"
                "Live bot and S-UI tokens are intentionally excluded.",
                reply_markup=build_backup_restore_keyboard(),
            )
        elif data == 'settings_backup_create':
            await query.edit_message_text("⏳ Creating SUI Bot backup...")
            try:
                await send_state_backup(context, user_id)
            except (OSError, ValueError, RuntimeError, TelegramError) as exc:
                logger.exception("Failed to create or send SUI Bot state backup")
                await query.edit_message_text(
                    f"❌ Backup failed: {str(exc)[:500]}",
                    reply_markup=build_backup_restore_keyboard(),
                )
            else:
                await query.edit_message_text(
                    "✅ Backup sent to this chat. Keep it private.",
                    reply_markup=build_backup_restore_keyboard(),
                )
        elif data == 'connection_guides':
            if not connection_guide_store.enabled or not connection_guide_store.list_guides():
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                f"{tr(user_id, 'connection_guides_title')}\n\n{tr(user_id, 'connection_guides_choose')}",
                reply_markup=build_connection_guides_user_keyboard(user_id),
            )
        elif data.startswith('connection_guide_'):
            guide_id = data.removeprefix('connection_guide_')
            guide = connection_guide_store.get(guide_id) if connection_guide_store.enabled else None
            if guide is None:
                await localized_query_answer(query, tr(user_id, "guide_unavailable"), show_alert=True)
                return
            await query.edit_message_text(
                tr(user_id, "guide_sending", title=preserve_dynamic_text(guide["title"])),
            )
            await send_connection_guide(context, user_id, guide)
            await query.edit_message_text(
                tr(user_id, "guide_finished", title=preserve_dynamic_text(guide["title"])),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(tr(user_id, "back"), callback_data="connection_guides")],
                    [InlineKeyboardButton(tr(user_id, "main_menu"), callback_data="main_menu")],
                ]),
            )
        elif data == 'settings_backup_help':
            await query.edit_message_text(
                "📥 Restore a SUI Bot Backup\n\n"
                "1. Send /restore\n"
                "2. Send the `.sui-backup.json` document\n"
                "3. The bot validates its format, checksum, size, and allowed data sections before restoring it.",
                reply_markup=build_backup_restore_keyboard(),
            )
        elif data == 'settings_plans':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            await query.edit_message_text(
                _plans_summary_text(),
                reply_markup=build_plans_admin_keyboard()
            )
        elif data == 'settings_plans_reset':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            set_renewal_month_options([1, 2, 3])
            await query.edit_message_text(
                build_payment_settings_text(),
                reply_markup=build_payment_settings_keyboard()
            )
            await localized_query_answer(query, "✅ Renewal plans reset to 1,2,3 months.", show_alert=True)
        elif data == 'pricing_menu':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            pricing_text, pricing_kb = build_pricing_menu()
            await query.edit_message_text(pricing_text, reply_markup=pricing_kb)
        elif data.startswith('pricing_mode_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            mode = data.removeprefix('pricing_mode_')
            try:
                _plans_store.set_mode(mode)
                await localized_query_answer(query, f"✅ حالت قیمت‌گذاری: {mode}", show_alert=False)
            except ValueError as exc:
                await localized_query_answer(query, f"❌ {exc}", show_alert=True)
            pricing_text, pricing_kb = build_pricing_menu()
            await query.edit_message_text(pricing_text, reply_markup=pricing_kb)
        elif data.startswith('pricing_set_per_gb'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            context.user_data.clear()
            context.user_data["pricing_target"] = "per_gb"
            await localized_query_answer(query)
            await query.message.reply_text(
                f"📏 قیمت هر گیگ به تومان بفرست (فعلی: {format_money(_plans_store.pricing.per_gb_toman)})\nلغو: /cancel"
            )
        elif data.startswith('pricing_set_monthly'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            context.user_data.clear()
            context.user_data["pricing_target"] = "monthly"
            await localized_query_answer(query)
            await query.message.reply_text(
                f"📅 قیمت پایه هر ماه به تومان بفرست (فعلی: {format_money(_plans_store.pricing.monthly_toman)})\nلغو: /cancel"
            )
        elif data.startswith('pricing_set_month_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            months = data.removeprefix('pricing_set_month_')
            context.user_data.clear()
            context.user_data["pricing_target"] = "month"
            context.user_data["pricing_month"] = months
            await localized_query_answer(query)
            await query.message.reply_text(
                f"💰 قیمت اختصاصی برای {months} ماه به تومان بفرست (0 = حذف قیمت اختصاصی)\nلغو: /cancel"
            )
        elif data.startswith('pricing_del_month_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            months = data.removeprefix('pricing_del_month_')
            _plans_store.set_month_price(int(months), None)
            await localized_query_answer(query, f"🗑 قیمت اختصاصی {months} ماه حذف شد.", show_alert=False)
            pricing_text, pricing_kb = build_pricing_menu()
            await query.edit_message_text(pricing_text, reply_markup=pricing_kb)
        elif data.startswith('plan_toggle_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            slug = data.removeprefix('plan_toggle_')
            try:
                _plans_store.toggle_plan(slug)
            except KeyError:
                await localized_query_answer(query, "❌ پلن پیدا نشد.", show_alert=True)
                return
            await query.edit_message_text(_plans_summary_text(), reply_markup=build_plans_admin_keyboard())
        elif data.startswith('plan_del_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            slug = data.removeprefix('plan_del_')
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ بله، حذف کن", callback_data=f'plan_delok_{slug}'),
                 InlineKeyboardButton("انصراف", callback_data='settings_plans')],
            ])
            await query.edit_message_text(f"پلن «{slug}» برای همیشه حذف شود؟", reply_markup=keyboard)
        elif data.startswith('plan_delok_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            slug = data.removeprefix('plan_delok_')
            _plans_store.delete_plan(slug)
            await localized_query_answer(query, "🗑 پلن حذف شد.", show_alert=False)
            await query.edit_message_text(_plans_summary_text(), reply_markup=build_plans_admin_keyboard())
        elif data.startswith('settings_plan_toggle_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            month = int(data.split('_')[-1])
            current = set(get_renewal_month_options())
            if month in current:
                if len(current) == 1:
                    await localized_query_answer(query, "❌ At least one plan must stay enabled.", show_alert=True)
                    return
                current.remove(month)
                action_text = f"❌ Disabled {month} month plan."
            else:
                current.add(month)
                action_text = f"✅ Enabled {month} month plan."
            set_renewal_month_options(sorted(current))
            enabled = ", ".join(f"{m}M" for m in get_renewal_month_options())
            await query.edit_message_text(
                "📦 Renewal Plan Options\n\n"
                "Toggle months ON/OFF. Enabled months are shown with ✅.\n\n"
                f"Current: {enabled}",
                reply_markup=build_settings_plans_keyboard()
            )
            await localized_query_answer(query, action_text, show_alert=False)
        elif data.startswith('settings_price_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            global RENEWAL_MONTHLY_PRICE
            parts = data.split('_')
            if len(parts) != 4:
                await localized_query_answer(query, "❌ Invalid price action.", show_alert=True)
                return
            op = parts[2]
            delta = int(parts[3])
            if op == 'plus':
                new_price = RENEWAL_MONTHLY_PRICE + delta
            elif op == 'minus':
                new_price = max(1, RENEWAL_MONTHLY_PRICE - delta)
            else:
                await localized_query_answer(query, "❌ Invalid price action.", show_alert=True)
                return
            RENEWAL_MONTHLY_PRICE = new_price
            save_runtime_setting("RENEWAL_MONTHLY_PRICE", str(RENEWAL_MONTHLY_PRICE), RUNTIME_SETTINGS_FILE)
            await query.edit_message_text(
                build_payment_settings_text(),
                reply_markup=build_payment_settings_keyboard()
            )
            await localized_query_answer(
                query,
                tr(
                    user_id,
                    "new_monthly_price",
                    amount=preserve_dynamic_text(format_money(RENEWAL_MONTHLY_PRICE)),
                ),
                show_alert=False,
            )
        elif data == 'create_user_prompt':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            await query.edit_message_text(
                "➕ To Create a New User Follow The Procedure\n\n"
                "/createuser\n\n"
                "This Command Guides You Through The Process.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]])
            )
        elif data == 'edit_user_prompt':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            await query.edit_message_text(
                "📝 To Edit an Existing User Follow The Procedure\n\n"
                "/edituser\n\n"
                "This Command Guides You Through The Process.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]])
            )
        elif data == 'delete_user_prompt':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            await query.edit_message_text(
                "🗑️ To Delete an Existing User Follow The Procedure\n\n"
                "/deleteuser\n\n"
                "This Command Guides You Through The Process.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]])
            )
        elif data.startswith('my_usage'):
            parts = data.split('_')
            if len(parts) > 2 and parts[2].isdigit():
                # Specific subscription selected
                client_id = int(parts[2])
                if not user_has_client_access(user_id, client_id):
                    await localized_query_answer(query, "❌ You don't have access to this subscription.", show_alert=True)
                    return
                is_admin_user = (is_admin(user_id))
                new_usage_msg, client = await get_client_usage_record(client_id, is_admin_user, user_id)
                username = client.get("name", "Unknown") if client else "Unknown"
                web_panel_url = web_panel_url_for(username)

                new_reply_markup = subscription_keyboard(user_id, client_id, web_panel_url)

                try:
                   await query.edit_message_text(new_usage_msg, reply_markup=new_reply_markup)
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        await localized_query_answer(query, "✅ Data Is Up To Date.", show_alert=False)
                    else:
                        raise
            else:
                    # Main menu clicked - show subscription selection if multiple
                    client_ids = telegram_clients.get(user_id)
                    if not client_ids:
                        kb = None
                        if SETTINGS.store_enabled:
                            kb = InlineKeyboardMarkup([[
                                InlineKeyboardButton("🛍️ خرید اشتراک", callback_data='shop_menu_open')
                            ]])
                        await query.edit_message_text(
                            tr(user_id, "not_active"),
                            reply_markup=kb,
                        )
                        return

                    if len(client_ids) == 1:
                        # Single subscription - show directly
                        client_id = client_ids[0]
                        is_admin_user = (is_admin(user_id))
                        usage_msg, client = await get_client_usage_record(client_id, is_admin_user, user_id)
                        username = client.get("name", "Unknown") if client else "Unknown"
                        web_panel_url = web_panel_url_for(username)

                        await query.edit_message_text(
                            usage_msg,
                            reply_markup=subscription_keyboard(user_id, client_id, web_panel_url),
                        )
                    else:
                        # Multiple subscriptions - show selection menu
                        keyboard = []
                        client_map = await get_client_map()
                        for client_id in client_ids:
                            client = client_map.get(client_id)
                            if client:
                               name = preserve_dynamic_text(client["name"]) if client.get("name") else "Unknown"
                               desc = preserve_dynamic_text(client["desc"]) if client.get("desc") else "No description"
                               expiry = client.get("expiry", 0)
                               expiry_str = localized_remaining_time(expiry, user_id)
                               button_text = f"📱 {desc} ({name}) - {expiry_str}"
                            else:
                               button_text = f"📱 Subscription #{client_id}"

                            keyboard.append([InlineKeyboardButton(button_text, callback_data=f'select_sub_{client_id}')])

                        keyboard.append([InlineKeyboardButton(tr(user_id, "main_menu"), callback_data='main_menu')])

                        await query.edit_message_text(
                            tr(user_id, "select_subscription", count=len(client_ids)),
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

        elif data.startswith('get_sub_links'):
            parts = data.split('_')
            if len(parts) > 3 and parts[2] == 'links':
                client_id = int(parts[3])
            else:
                client_ids = telegram_clients.get(user_id)
                if not client_ids:
                    await query.edit_message_text("❌ Bot Is Not Activated For You.")
                    return
                client_id = client_ids[0] if client_ids else None

            if not client_id:
                await query.edit_message_text("❌ No subscription selected.")
                return
            if not user_has_client_access(user_id, client_id):
                await localized_query_answer(query, "❌ You don't have access to this subscription.", show_alert=True)
                return

            data_obj = await api_client.get('apiv2/clients', {'id': client_id})
            if not data_obj:
                await query.edit_message_text(f"❌ Server Unresponsive{api_client.error_reason('apiv2/clients')}")
                return
            clients = sui_clients(data_obj)
            if clients is None:
                await query.edit_message_text("❌ Invalid response from server")
                return
            client_to_tg = build_client_to_tg_index()
            if not clients:
                await query.edit_message_text("❌ User Not Found")
                return
            name = clients[0].get("name", "Unknown")
            base_url = await get_subscription_base_url()
            if SUBSCRIPTION_PUBLIC_ORIGIN and HIDE_SUBSCRIPTION_PORT:
                base_url = replace_url_origin(base_url, SUBSCRIPTION_PUBLIC_ORIGIN)
            main_url, _json_url, _clash_url = build_subscription_urls(
                base_url,
                name,
                remove_port=HIDE_SUBSCRIPTION_PORT,
            )
            msg = (
                f"{tr(user_id, 'subscription_links')}\n\n"
                f"🌐 <code>{html.escape(main_url)}</code>\n\n"
                f"{tr(user_id, 'use_links')}"
            )
            keyboard = [[InlineKeyboardButton(tr(user_id, "back"), callback_data=f'select_sub_{client_id}')]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        elif data.startswith('select_sub_'):
            client_id = int(data.split('_')[-1])
            if not user_has_client_access(user_id, client_id):
                await localized_query_answer(query, "❌ You don't have access to this subscription.", show_alert=True)
                return
            is_admin_user = (is_admin(user_id))
            usage_msg, client = await get_client_usage_record(client_id, is_admin_user, user_id)
            username = client.get("name", "Unknown") if client else "Unknown"
            web_panel_url = web_panel_url_for(username)

            await query.edit_message_text(
                usage_msg,
                reply_markup=subscription_keyboard(user_id, client_id, web_panel_url),
            )
        elif data.startswith('renew_start_'):
            client_id = int(data.split('_')[-1])
            if not user_has_client_access(user_id, client_id):
                await localized_query_answer(query, "❌ You don't have access to this subscription.", show_alert=True)
                return

            cleanup_pending_renew_requests()
            month_options = get_renewal_month_options()
            keyboard = []
            # In the renew_start_ callback
            for months in month_options:
                amount = renewal_amount(months, user_id=user_id)
                keyboard.append([InlineKeyboardButton(
                    tr(user_id, "renew_plan_button", months=months, amount_text=format_money(amount)),
                    callback_data=f'renew_choose_{client_id}_{months}',
                )])
            keyboard.append([InlineKeyboardButton(tr(user_id, "back"), callback_data=f'select_sub_{client_id}')])
            await query.edit_message_text(
                f"{tr(user_id, 'renew_title')}\n\n{tr(user_id, 'renew_choose_duration')}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif data.startswith('renew_choose_'):
            parts = data.split('_')
            if len(parts) != 4:
                await localized_query_answer(query, "❌ Invalid option.", show_alert=True)
                return
            client_id = int(parts[2])
            months = int(parts[3])
            allowed_months = set(get_renewal_month_options())
            if months not in allowed_months:
                await localized_query_answer(query, "❌ Invalid duration.", show_alert=True)
                return
            if not user_has_client_access(user_id, client_id):
                await localized_query_answer(query, "❌ You don't have access to this subscription.", show_alert=True)
                return

            amount = renewal_amount(months, user_id=user_id)
            context.user_data['pending_renew_submission'] = {
                "client_id": client_id,
                "months": months,
                "amount": amount,
                "created_at": datetime.now().timestamp()
            }

            holder_line = (
                f"\n{tr(user_id, 'card_holder_value', holder=preserve_dynamic_text(html.escape(PAYMENT_CARD_HOLDER)))}"
                if PAYMENT_CARD_HOLDER else ""
            )
            card_number = copyable_ltr_code(PAYMENT_CARD_NUMBER)
            keyboard = [
                [InlineKeyboardButton(tr(user_id, "cancel"), callback_data=f'renew_cancel_{client_id}')],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data=f'renew_start_{client_id}')]
            ]
            await query.edit_message_text(
                f"{tr(user_id, 'renew_payment')}\n\n"
                f"{tr(user_id, 'duration_value', months=months)}\n"
                f"{tr(user_id, 'amount_value', amount_text=format_money(amount))}\n"
                f"{tr(user_id, 'card_number_value', card_number=card_number)}\n"
                f"{tr(user_id, 'tap_card_to_copy')}{holder_line}\n\n"
                f"{tr(user_id, 'payment_instructions')}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        elif data.startswith('renew_cancel_'):
            context.user_data.pop('pending_renew_submission', None)
            client_id = int(data.split('_')[-1])
            await query.edit_message_text(
                tr(user_id, "renew_cancelled"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr(user_id, "back"), callback_data=f'select_sub_{client_id}')]])
            )
        elif data.startswith('renew_appr_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            cleanup_pending_renew_requests()
            request_id = data.split('_')[-1]
            # Claim the request before awaiting network I/O so repeated button
            # presses cannot apply the same renewal concurrently.
            req = pending_renew_requests.pop(request_id, None)
            if not req:
                await localized_query_answer(query, tr(user_id, "renew_request_missing"), show_alert=True)
                return

            client_id = req["client_id"]
            months = req["months"]
            user_tg_id = req["user_tg_id"]
            amount = req["amount"]
            data_obj = await api_client.get('apiv2/clients', {'id': client_id})
            clients = sui_clients(data_obj)
            if clients is None:
                pending_renew_requests[request_id] = req
                await localized_query_answer(query, "❌ Server unavailable. Try again.", show_alert=True)
                return
            if not clients:
                pending_renew_requests[request_id] = req
                await localized_query_answer(query, "❌ Client not found on server.", show_alert=True)
                return

            client = clients[0]
            raw_client_desc = client.get("desc")
            client_desc = preserve_dynamic_text(raw_client_desc) if raw_client_desc else "No description"
            new_expiry = req.get("target_expiry")
            if not isinstance(new_expiry, int) or new_expiry <= 0:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                try:
                    current_expiry = int(client.get("expiry", 0) or 0)
                except (TypeError, ValueError):
                    pending_renew_requests[request_id] = req
                    await localized_query_answer(query, tr(user_id, "invalid_server_expiry"), show_alert=True)
                    return
                base_ts = max(now_ts, current_expiry) if current_expiry > 0 else now_ts
                extended_seconds = months * 30 * 24 * 60 * 60
                new_expiry = base_ts + extended_seconds
                req["target_expiry"] = new_expiry

            # Build edit payload from current server object and only change expiry.
            edited_data_for_api = build_client_renewal_data(client, client_id, new_expiry)
            result = await create_or_edit_client("edit", edited_data_for_api)
            if result and result.get("success"):
                global clients_cache, clients_cache_time
                clients_cache = None
                clients_cache_time = 0
                user_expiry = localized_remaining_time(new_expiry, user_tg_id)
                admin_expiry = localized_remaining_time(new_expiry, user_id)
                try:
                    await context.bot.send_message(
                        chat_id=user_tg_id,
                        text=tr(
                            user_tg_id,
                            "renew_user_approved",
                            description=client_desc,
                            months=months,
                            amount=preserve_dynamic_text(format_money(amount)),
                            client_id=client_id,
                            expiry=user_expiry,
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {user_tg_id} after renewal approval: {e}")

                if query.message and (query.message.photo or query.message.document):
                    await query.edit_message_caption(
                        caption=tr(
                            user_id,
                            "renew_admin_approved",
                            request_id=preserve_dynamic_text(request_id),
                            user_id=user_tg_id,
                            client_id=client_id,
                            months=months,
                            amount=preserve_dynamic_text(format_money(amount)),
                            expiry=admin_expiry,
                        )
                    )
                else:
                    await query.edit_message_text(
                        tr(
                            user_id,
                            "renew_admin_approved",
                            request_id=preserve_dynamic_text(request_id),
                            user_id=user_tg_id,
                            client_id=client_id,
                            months=months,
                            amount=preserve_dynamic_text(format_money(amount)),
                            expiry=admin_expiry,
                        )
                    )
            else:
                pending_renew_requests[request_id] = req
                error_msg = result.get("msg", "Unknown Error") if result else f"Server Unresponsive{api_client.error_reason('apiv2/save')}"
                await localized_query_answer(
                    query, f"❌ Failed: {preserve_dynamic_text(error_msg)}", show_alert=True
                )
        elif data.startswith('renew_rej_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            cleanup_pending_renew_requests()
            request_id = data.split('_')[-1]
            req = pending_renew_requests.pop(request_id, None)
            if not req:
                await localized_query_answer(query, tr(user_id, "renew_request_missing"), show_alert=True)
                return
            user_tg_id = req["user_tg_id"]
            client_id = req["client_id"]
            try:
                await context.bot.send_message(
                    chat_id=user_tg_id,
                    text=tr(user_tg_id, "renew_user_rejected", client_id=client_id)
                )
            except Exception as e:
                logger.error(f"Failed to notify user {user_tg_id} after renewal rejection: {e}")

            if query.message and (query.message.photo or query.message.document):
                await query.edit_message_caption(
                    caption=tr(
                        user_id,
                        "renew_admin_rejected",
                        request_id=preserve_dynamic_text(request_id),
                        user_id=user_tg_id,
                        client_id=client_id,
                    )
                )
            else:
                await query.edit_message_text(
                    tr(
                        user_id,
                        "renew_admin_rejected",
                        request_id=preserve_dynamic_text(request_id),
                        user_id=user_tg_id,
                        client_id=client_id,
                    )
                )
        elif data.startswith('all_clients_page_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            page = int(data.split('_')[-1])
            clients = await get_all_clients_list()
            scope = admin_group_scope(user_id)
            if scope is None:
                scope_label = ""
            else:
                scope_groups = sorted(_admin_registry.groups_of(user_id) or [])
                scope_label = " — " + (", ".join(scope_groups) if scope_groups else "بدون دسترسی")
            if scope is not None:
                clients = [c for c in clients or [] if client_in_admin_scope(c, scope)]
            if not clients:
                await query.edit_message_text("❌ No User Found.")
                return
            total_pages = (len(clients) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            page = max(1, min(page, total_pages))
            start_idx = (page - 1) * ITEMS_PER_PAGE
            end_idx = start_idx + ITEMS_PER_PAGE
            page_clients = clients[start_idx:end_idx]
            msg = f"👥 All Users{scope_label} (Page {page}/{total_pages})\n\n"
            for client in page_clients:
                msg += f"{format_client(client, is_admin=True)}\n\n"
            keyboard = get_pagination_keyboard(page, total_pages, 'all_clients')
            await query.edit_message_text(msg, reply_markup=keyboard)
        elif data == 'online_users':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            data_obj = await api_client.get('apiv2/onlines')
            if not data_obj:
                await query.edit_message_text(f"❌ Failed To Get Online Users List.{api_client.error_reason('apiv2/onlines')}")
                return
            obj = data_obj.get("obj", {})
            users = obj.get("user", [])
            scope = admin_group_scope(user_id)
            if scope is not None:
                group_by_name = {
                    str(c.get("name") or "").strip(): str(c.get("group") or "").strip().casefold()
                    for c in await get_all_clients_list() or []
                }
                users = [u for u in users if group_by_name.get(str(u).strip(), "") in scope]
            if not users:
                msg = "❌ No User Online."
            else:
                msg = f"🌐 Online Users ({len(users)} User)\n\n"
                for i, user in enumerate(users):
                    msg += f"👤 User: {user}\n"
                    if i < len(users) - 1:
                        msg += "\n"

            keyboard = [[InlineKeyboardButton("🔄 بروزرسانی", callback_data='online_users')], [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]

            try:
                await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    await localized_query_answer(query, "✅ List Is Up To Date.", show_alert=False)
                else:
                    raise

        elif data == 'server_status':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            data_obj = await api_client.get('apiv2/status', {'r': 'cpu,mem,net,sys,sbd,dsk,swp,dio,db'})
            obj = sui_response_object(data_obj)
            if obj is None:
                await query.edit_message_text(preserve_dynamic_text(f"❌ Failed To Get Server Stats.{api_client.error_reason('apiv2/status')}"))
                return

            # CPU
            cpu_percent = round(obj.get("cpu", 0))

            # Memory
            mem = obj.get("mem", {})
            mem_current = mem.get("current", 0)
            mem_total = mem.get("total", 0)
            mem_percent = round((mem_current / mem_total * 100) if mem_total > 0 else 0)

            # Network - ALL fields
            net = obj.get("net", {})
            net_recv = net.get("recv", 0)
            net_sent = net.get("sent", 0)
            net_precv = net.get("precv", 0)  # Packets received
            net_psent = net.get("psent", 0)  # Packets sent

            # Sing-box - ALL fields
            sbd = obj.get("sbd", {})
            sbd_running = sbd.get("running", False)
            sbd_stats = sbd.get("stats", {})
            sbd_uptime = sbd_stats.get("Uptime", 0)
            sbd_goroutines = sbd_stats.get("NumGoroutine", 0)
            sbd_alloc = sbd_stats.get("Alloc", 0)

            # System info - ALL fields
            sys_info = obj.get("sys", {})
            hostname = sys_info.get("hostName", "Unknown")
            app_version = sys_info.get("appVersion", "Unknown")
            cpu_count = sys_info.get("cpuCount", 0)
            cpu_type = sys_info.get("cpuType", "Unknown")
            ipv4 = sys_info.get("ipv4", [])
            ipv6 = sys_info.get("ipv6", [])
            app_mem = sys_info.get("appMem", 0)
            app_threads = sys_info.get("appThreads", 0)
            boot_time = sys_info.get("bootTime", 0)

            # Disk
            dsk = obj.get("dsk", {})
            disk_current = dsk.get("current", 0)
            disk_total = dsk.get("total", 0)
            disk_percent = round((disk_current / disk_total * 100) if disk_total > 0 else 0)

            # Swap
            swp = obj.get("swp", {})
            swap_current = swp.get("current", 0)
            swap_total = swp.get("total", 0)
            swap_percent = round((swap_current / swap_total * 100) if swap_total > 0 else 0)

            # Disk IO - ALL fields
            dio = obj.get("dio", {})
            dio_read = dio.get("read", 0)
            dio_write = dio.get("write", 0)

            # Database stats - ALL fields
            db = obj.get("db", {})
            db_clients = db.get("clients", 0)
            db_inbounds = db.get("inbounds", 0)
            db_outbounds = db.get("outbounds", 0)
            db_endpoints = db.get("endpoints", 0)
            db_services = db.get("services", 0)
            db_client_down = db.get("clientDown", 0)
            db_client_up = db.get("clientUp", 0)

            def format_duration(seconds):
                days = seconds // 86400
                hours = (seconds % 86400) // 3600
                minutes = (seconds % 3600) // 60
                seconds_remain = seconds % 60

                parts = []
                if days > 0:
                    parts.append(f"{days}d")
                if hours > 0:
                    parts.append(f"{hours}h")
                if minutes > 0:
                    parts.append(f"{minutes}m")
                if seconds_remain > 0 and days == 0:  # Only show seconds if less than a day
                    parts.append(f"{seconds_remain}s")

                return " ".join(parts) if parts else "0s"

            # Calculate uptime from boot time
            import time
            current_time = int(time.time())
            server_uptime_seconds = current_time - boot_time if boot_time > 0 else 0

            # Build message with ALL fields
            msg = "💻 SERVER STATUS\n"
            msg += "━━━━━━━━━━━━━━━━━━━━━\n\n"

            msg += "📌 SYSTEM INFORMATION\n"
            msg += f"🏷️ Hostname: {hostname}\n"
            msg += f"📦 Version: {app_version}\n"
            msg += f"⏰ Server Uptime: {format_duration(server_uptime_seconds)}\n"
            msg += f"🧠 App Memory: {format_bytes(app_mem)}\n"
            msg += f"🔄 App Threads: {app_threads}\n\n"

            msg += "🖥️ CPU & MEMORY\n"
            msg += f"⚡ CPU Usage: {cpu_percent}%\n"
            msg += f"🎛️ Cores: {cpu_count}\n"
            msg += f"🔧 CPU Model: {cpu_type[:50]}\n"
            msg += f"💾 RAM: {format_bytes(mem_current)} / {format_bytes(mem_total)} ({mem_percent}%)\n"
            msg += f"💿 Disk: {format_bytes(disk_current)} / {format_bytes(disk_total)} ({disk_percent}%)\n"
            msg += f"🔄 Swap: {format_bytes(swap_current)} / {format_bytes(swap_total)} ({swap_percent}%)\n\n"

            msg += "📡 NETWORK TRAFFIC\n"
            msg += f"📥 Received: {format_bytes(net_recv)}\n"
            msg += f"📤 Sent: {format_bytes(net_sent)}\n"
            msg += f"📦 Packets RX: {net_precv:,}\n"
            msg += f"📦 Packets TX: {net_psent:,}\n\n"

            msg += "💽 DISK I/O\n"
            msg += f"📖 Read: {format_bytes(dio_read)}\n"
            msg += f"✍️ Write: {format_bytes(dio_write)}\n\n"

            msg += "⚙️ SING-BOX\n"
            msg += f"Status: {'✅ Running' if sbd_running else '❌ Stopped'}\n"
            msg += f"⏱️ Uptime: {format_duration(sbd_uptime)}\n"
            msg += f"🧵 Goroutines: {sbd_goroutines:,}\n"
            msg += f"💾 Heap: {format_bytes(sbd_alloc)}\n\n"

            msg += "🗄️ DATABASE\n"
            msg += f"👥 Clients: {db_clients:,}\n"
            msg += f"📥 Inbounds: {db_inbounds:,}\n"
            msg += f"📤 Outbounds: {db_outbounds:,}\n"
            msg += f"🔗 Endpoints: {db_endpoints:,}\n"
            msg += f"📊 Services: {db_services:,}\n"
            msg += f"⬇️ Client Down: {format_bytes(db_client_down)}\n"
            msg += f"⬆️ Client Up: {format_bytes(db_client_up)}\n\n"

            msg += "🌐 IP ADDRESSES\n"
            msg += "IPv4:\n"
            for ip in ipv4:
                msg += f"  • {ip}\n"
            msg += "\nIPv6:\n"
            for ip in ipv6:
                msg += f"  • {ip}\n"

            await query.edit_message_text(
                preserve_dynamic_text(msg),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(tr(user_id, "refresh"), callback_data='server_status')],
                    [InlineKeyboardButton(tr(user_id, "main_menu"), callback_data='main_menu')],
                ]),
            )
        elif data == 'bot_stats':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            stats = metrics.get_global_stats()
            start_time = datetime.fromisoformat(stats['start_time'])
            uptime = datetime.now() - start_time
            days = uptime.days
            hours = uptime.seconds // 3600
            minutes = (uptime.seconds % 3600) // 60
            msg = f"📊 Bot Stats\n\n⏱️ Time Running: {days} Days, {hours} Hour, {minutes} Minute\n📨 Total Commands: {stats['total_commands']}\n👥 Total Users: {stats['total_users']}\n❌ Total Errors: {stats['total_errors']}\n\n🔥 Active Users:\n"
            for i, (uid, count) in enumerate(stats['most_active_users'][:5], 1):
                msg += f"{i}. User {uid}: {count} Command\n"
            msg += "\n📈 Most Used Commands:\n"
            for i, (cmd, count) in enumerate(stats['most_used_commands'][:5], 1):
                clean_cmd = cmd.replace('button_', '')
                msg += f"{i}. {clean_cmd}: {count} Times\n"
            if stats['avg_response_times']:
                msg += "\n⚡ Average Response Time:\n"
                for cmd, avg_time in list(stats['avg_response_times'].items())[:5]:
                    clean_cmd = cmd.replace('button_', '')
                    msg += f"• {clean_cmd}: {avg_time:.2f}s\n"
            keyboard = [[InlineKeyboardButton("🔄 بروزرسانی", callback_data='bot_stats')], [InlineKeyboardButton("👥 جزئیات کاربران", callback_data='user_details_page_1')], [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith('user_details_page_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            page = int(data.split('_')[-1])
            user_ids = list(telegram_clients.keys())
            if not user_ids:
                await query.edit_message_text("❌ No User Registered.")
                return
            total_pages = (len(user_ids) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            page = max(1, min(page, total_pages))
            start_idx = (page - 1) * ITEMS_PER_PAGE
            end_idx = start_idx + ITEMS_PER_PAGE
            page_users = user_ids[start_idx:end_idx]
            msg = f"👥 جزئیات کاربران (صفحه {page}/{total_pages})\n\n"
            for uid in page_users:
                client_id = telegram_clients[uid]
                user_stats = metrics.get_user_stats(uid)
                msg += f"👤 Telegram ID: {uid}\n🆔 Client ID: {client_id}\n📊 Total Commands: {user_stats['total_commands']}\n"
                if user_stats['last_activity']:
                    last_active = datetime.fromisoformat(user_stats['last_activity'])
                    time_ago = datetime.now() - last_active
                    if time_ago.days > 0:
                        msg += f"🕐 Last Activity: {time_ago.days} Days Ago\n"
                    else:
                        hours = time_ago.seconds // 3600
                        minutes = (time_ago.seconds % 3600) // 60
                        if hours > 0:
                            msg += f"🕐 Last Activity: {hours}h {minutes}m Ago\n"
                        else:
                            msg += f"🕐 Last Activity: {minutes}m Ago\n"
                if user_stats['commands']:
                    top_cmd = max(user_stats['commands'].items(), key=lambda x: x[1])
                    clean_cmd = top_cmd[0].replace('button_', '')
                    msg += f"⭐ Frequent Command: {clean_cmd} ({top_cmd[1]} Times)\n"
                msg += "\n"
            keyboard = get_pagination_keyboard(page, total_pages, 'user_details')
            await query.edit_message_text(msg, reply_markup=keyboard)
        elif data == 'manage_links':

             await show_links_page(query, page=1)

        elif data.startswith('links_page_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            page = int(data.split('_')[-1])
            await show_links_page(query, page=page)
        elif data == 'assign_interactive':
            context.user_data.pop('pending_interactive_assignment', None)
            await show_interactive_assignment_clients(query, page=1)
        elif data.startswith('assign_page_'):
            await show_interactive_assignment_clients(query, page=int(data.rsplit('_', 1)[1]))
        elif data.startswith('assign_pick_'):
            client_id = int(data.rsplit('_', 1)[1])
            clients = await get_all_clients_list()
            client = find_client_by_id(clients, client_id)
            if client is None:
                await localized_query_answer(query, "❌ Client no longer exists.", show_alert=True)
                return
            request_id = secrets.randbelow(2_000_000_000) + 1
            context.user_data['pending_interactive_assignment'] = {
                'client_id': client_id,
                'client_name': str(client.get('name') or 'Unknown'),
                'request_id': request_id,
            }
            picker = ReplyKeyboardMarkup(
                [[KeyboardButton(
                    "👤 Choose Telegram Account",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )], [KeyboardButton("❌ Cancel Interactive Assignment")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await query.edit_message_text(
                f"✅ S-UI client selected: {preserve_dynamic_text(context.user_data['pending_interactive_assignment']['client_name'])} "
                f"(ID: {client_id})\n\nUse the Telegram account picker sent below."
            )
            await query.message.reply_text(
                "Choose one Telegram account. Telegram only shows accounts it allows you to share with this bot.",
                reply_markup=picker,
            )
        elif data == 'assign_confirm':
            pending = context.user_data.get('pending_interactive_assignment')
            if not isinstance(pending, dict) or not pending.get('telegram_id'):
                await localized_query_answer(query, "❌ Assignment expired. Start again.", show_alert=True)
                return
            client_id = int(pending['client_id'])
            telegram_id = int(pending['telegram_id'])
            clients = await get_all_clients_list()
            if find_client_by_id(clients, client_id) is None:
                context.user_data.pop('pending_interactive_assignment', None)
                await query.edit_message_text("❌ The selected S-UI client no longer exists.")
                return
            added, count = add_client_assignment(telegram_id, client_id, owner_admin_id=user_id)
            context.user_data.pop('pending_interactive_assignment', None)
            if added:
                result_text = (
                    f"✅ Client ID {client_id} assigned to Telegram ID {telegram_id}.\n"
                    f"That Telegram account now has {count} subscription(s)."
                )
            else:
                result_text = f"⚠️ Client ID {client_id} is already assigned to Telegram ID {telegram_id}."
            await query.edit_message_text(
                result_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Assign Another", callback_data='assign_interactive')],
                    [InlineKeyboardButton("🔗 View Links", callback_data='manage_links')],
                ]),
            )
        elif data == 'assign_abort':
            context.user_data.pop('pending_interactive_assignment', None)
            await query.edit_message_text(
                "❌ Interactive assignment canceled.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='add_link_help')]]),
            )
        elif data == 'add_link_help':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            assigned_client_ids = set()
            for ids in telegram_clients.values():
                if isinstance(ids, list):
                    assigned_client_ids.update(ids)
                else:
                    assigned_client_ids.add(ids)

            clients = await get_all_clients_list()
            unassigned_clients = []

            for client in clients:
                client_id = client.get("id")
                if client_id and client_id not in assigned_client_ids:
                    unassigned_clients.append(client)

            msg = "➕ Add New Link\n\n"
            msg += "📋 **Commands:**\n"
            msg += "`/assign <TelegramID> <ClientID>` - Add Link\n"
            msg += "`/unlink <TelegramID>` - Remove Link\n"
            msg += "`/unblock <TelegramID>` - Unblock User\n\n"

            if unassigned_clients:
                msg += "📊 **Users Without Link:**\n"
                for i, client in enumerate(unassigned_clients[:5], 1):
                    client_id = client.get("id")
                    name = client.get("name", "Unknown")
                    desc = client.get("desc", "No description")
                    msg += f"{i}. ID: `{client_id}` - {name} ({desc})\n"

                if len(unassigned_clients) > 5:
                    msg += f"\n& {len(unassigned_clients) - 5} Other User..."
            else:
                msg += "✅ All Users Are Linked."

            keyboard = [
                [InlineKeyboardButton("👤 Interactive Assignment", callback_data='assign_interactive')],
                [InlineKeyboardButton("🔙 Return To List", callback_data='manage_links')],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]
            ]

            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == 'refresh_sub':
            await refresh_sub_callback(update, context)
        elif data.startswith('user_stats_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            target_id = int(data.split('_')[-1])
            user_stats = metrics.get_user_stats(target_id)
            msg = f"📊 User stats {target_id}\n\n📨 Total Commands: {user_stats['total_commands']}\n❌ Total Errors: {user_stats['errors']}\n\n📈 Used Commands:\n"
            for cmd, count in sorted(user_stats['commands'].items(), key=lambda x: x[1], reverse=True):
                clean_cmd = cmd.replace('button_', '')
                msg += f"• {clean_cmd}: {count} Times\n"
            if user_stats['last_activity']:
                last_active = datetime.fromisoformat(user_stats['last_activity'])
                time_ago = datetime.now() - last_active
                if time_ago.days > 0:
                    msg += f"\n🕐 Last Activity: {time_ago.days} Days Ago"
                else:
                    hours = time_ago.seconds // 3600
                    minutes = (time_ago.seconds % 3600) // 60
                    if hours > 0:
                        msg += f"\n🕐 Last Activity: {hours}h {minutes}m Ago"
                    else:
                        msg += f"\n🕐 Last Activity: {minutes}m Ago"
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data='user_details_page_1')], [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith('unblock_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return
            target_id = int(data.split('_')[-1])
            await rate_limiter.reset_user(target_id)
            await localized_query_answer(query, f"✅ User {target_id} Unblocked.", show_alert=True)

        elif data == 'broadcast_message':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            keyboard = [
                [InlineKeyboardButton("📢 All Users", callback_data='broadcast_all')],
                [InlineKeyboardButton("📨 Specific Users", callback_data='broadcast_specific')],
                [InlineKeyboardButton("🔙 بازگشت", callback_data='main_menu')]
            ]

            await query.edit_message_text(
                "📢 **Send Broadcast**\n\n"
                "Which Group Do You Want To Send To?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

        elif data == 'broadcast_all':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            context.user_data['broadcast_type'] = 'all'
            context.user_data['broadcast_users'] = list(telegram_clients.keys())

            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data='broadcast_message')]]

            await query.edit_message_text(
                f"📢 **Send Broadcast To All Users**\n\n"
                f"📊 No. Of Users: {len(context.user_data['broadcast_users'])}\n\n"
                "Please Input Your Message:\n"
                "(You Can Use Markdown)\n\n"
                "Abort: /cancel",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

            return BROADCAST_MESSAGE

        elif data == 'broadcast_specific':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            await show_broadcast_users_page(query, context, page=1)


        elif data.startswith('broadcast_page_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            page = int(data.split('_')[-1])
            await show_broadcast_users_page(query, context, page=page)

        elif data.startswith('broadcast_user_'):
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            parts = data.split('_')
            if len(parts) < 4:
                await localized_query_answer(query, "❌ Invalid selection data.", show_alert=True)
                return
            selected_tg_id = int(parts[2])
            selected_client_id = int(parts[3])
            selected_key = f"{selected_tg_id}:{selected_client_id}"
            selected_user_keys = context.user_data.get('selected_user_keys', [])

            if selected_key in selected_user_keys:
                selected_user_keys.remove(selected_key)
                await localized_query_answer(query,
                    f"❌ User {selected_tg_id} / Client {selected_client_id} Removed.",
                    show_alert=True
                )
            else:
                selected_user_keys.append(selected_key)
                await localized_query_answer(query,
                    f"✅ User {selected_tg_id} / Client {selected_client_id} Added.",
                    show_alert=True
                )

            context.user_data['selected_user_keys'] = selected_user_keys

            # Refresh the current page to show updated selection
            current_page = context.user_data.get('broadcast_page', 1)
            await show_broadcast_users_page(query, context, page=current_page)

        elif data == 'broadcast_selection_summary':
            await broadcast_selection_summary_callback(update, context)

        elif data == 'broadcast_clear_selection':
            await broadcast_clear_selection_callback(update, context)

        elif data == 'broadcast_confirm_selection':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            selected_user_keys = context.user_data.get('selected_user_keys', [])

            if not selected_user_keys:
                await localized_query_answer(query, "❌ Choose At Least 1 User.", show_alert=True)
                return

            broadcast_users = []
            seen_users = set()
            for selection_key in selected_user_keys:
                try:
                    tg_id_str, _ = selection_key.split(":", 1)
                    tg_id = int(tg_id_str)
                except (TypeError, ValueError):
                    continue
                if tg_id not in seen_users:
                    seen_users.add(tg_id)
                    broadcast_users.append(tg_id)

            if not broadcast_users:
                await localized_query_answer(query, "❌ Choose At Least 1 User.", show_alert=True)
                return

            context.user_data['broadcast_users'] = broadcast_users

            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data='broadcast_specific')]]

            await query.edit_message_text(
                f"📨 **Send Broadcast To Specific Users**\n\n"
                f"📊 No. Of Users: {len(broadcast_users)}\n"
                f"📦 Selected Subscriptions: {len(selected_user_keys)}\n\n"
                "Please Input Your Message:\n"
                "(You Can Use Markdown)\n\n"
                "Abort: /cancel",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

            return BROADCAST_MESSAGE

        elif data == 'check_inactive_users':
            if not is_admin(user_id):
                await localized_query_answer(query, "❌ Only admin", show_alert=True)
                return

            data_obj = await api_client.get('apiv2/clients')
            if not data_obj:
                await query.edit_message_text(f"❌ Failed To Get Users List.{api_client.error_reason('apiv2/clients')}")
                return

            clients = sui_clients(data_obj)
            if clients is None:
                await query.edit_message_text(f"❌ Failed To Get Users List.{api_client.error_reason('apiv2/clients')}")
                return
            inactive_scope = admin_group_scope(user_id)
            if inactive_scope is not None:
                clients = [c for c in clients if client_in_admin_scope(c, inactive_scope)]
            client_to_tg = build_client_to_tg_index()

            users_with_expiry = []
            users_without_link = []

            for client in clients:
                client_id = client.get("id")
                expiry = client.get("expiry", 0)
                if expiry == 0:
                    continue

                # Find all Telegram IDs that have this client_id in their list
                tg_ids = client_to_tg.get(client_id, [])

                if tg_ids:
                    # Add each Telegram ID that has this subscription
                    for tg_id in tg_ids:
                        users_with_expiry.append({
                            "tg_id": tg_id,
                            "client_id": client_id,
                            "name": client.get("name", "Unknown"),
                            "desc": client.get("desc", "No description"),
                            "expiry": expiry
                        })
                else:
                    users_without_link.append({
                        "client_id": client_id,
                        "name": client.get("name", "Unknown"),
                        "desc": client.get("desc", "No description"),
                        "expiry": expiry
                    })

            inactive_users = []
            active_users = []

            for user in users_with_expiry:
                try:
                   await context.bot.send_chat_action(chat_id=user["tg_id"], action="typing")
                   await asyncio.sleep(0.1)  # Small delay
                   active_users.append(user)
                except Exception as e:
                    error_msg = str(e).lower()
                    if "bot was blocked by the user" in error_msg or \
                       "user is deactivated" in error_msg or \
                       "chat not found" in error_msg or \
                       "forbidden" in error_msg:
                        inactive_users.append(user)
                    else:
                        logger.warning(f"Error checking user {user['tg_id']}: {e}")
            report_message = f"{tr(user_id, 'inactive_check_title')}\n\n"
            report_message += f"{tr(user_id, 'report_stats')}\n"
            report_message += f"• {tr(user_id, 'total_users')}: {len(clients)}\n"
            report_message += f"• {tr(user_id, 'with_telegram_links', count=len(users_with_expiry))}\n"
            report_message += f"• {tr(user_id, 'without_telegram_links', count=len(users_without_link))}\n"
            report_message += f"• {tr(user_id, 'active_users_count', count=len(active_users))}\n"
            report_message += f"• {tr(user_id, 'inactive_users_count', count=len(inactive_users))}\n\n"

            if inactive_users:
                report_message += f"{tr(user_id, 'inactive_not_started_title')}\n"
                for i, user in enumerate(inactive_users[:15], 1):
                    expiry_date = datetime.fromtimestamp(user["expiry"], timezone.utc)
                    now = datetime.now(timezone.utc)
                    remaining_seconds = (expiry_date - now).total_seconds()
                    remaining_days = int(remaining_seconds / 86400)
                    if remaining_seconds % 86400 > 0:
                        remaining_days += 1

                    report_message += tr(
                        user_id,
                        "inactive_check_item",
                        index=i,
                        description=preserve_dynamic_text(user["desc"]),
                        name=preserve_dynamic_text(user["name"]),
                        client_id=user["client_id"],
                        telegram_id=user["tg_id"],
                        days=remaining_days,
                    ) + "\n"
            else:
                report_message += f"{tr(user_id, 'all_linked_started')}\n\n"

            if users_without_link:
                report_message += f"{tr(user_id, 'without_telegram_title')}\n"
                for i, user in enumerate(users_without_link[:10], 1):
                    expiry_date = datetime.fromtimestamp(user["expiry"], timezone.utc)
                    now = datetime.now(timezone.utc)
                    remaining_seconds = (expiry_date - now).total_seconds()
                    remaining_days = int(remaining_seconds / 86400)
                    if remaining_seconds % 86400 > 0:
                        remaining_days += 1

                    report_message += tr(
                        user_id,
                        "without_telegram_item",
                        index=i,
                        description=preserve_dynamic_text(user["desc"]),
                        client_id=user["client_id"],
                        days=remaining_days,
                    ) + "\n"

            keyboard = [
                [InlineKeyboardButton("🔄 بروزرسانی", callback_data='check_inactive_users')],
                [InlineKeyboardButton("🔗 Links", callback_data='manage_links')],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]
            ]

            await query.edit_message_text(
                report_message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            # دوبار زدن همان دکمه؛ بی‌ضرر است
            await localized_query_answer(query, "✅ Already Up To Date.", show_alert=False)
        else:
            logger.exception(f"Error in button_callback: {e}")
            await localized_query_answer(query, f"❌ Unexpected Error: {preserve_dynamic_text(str(e)[:150])}", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in button_callback: {e}")
        await localized_query_answer(query, f"❌ Unexpected Error: {preserve_dynamic_text(str(e)[:150])}", show_alert=True)

async def broadcast_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ Only admin Can Send Broadcasts.")
        return ConversationHandler.END

    message_text = update.message.text
    if not message_text or not message_text.strip():
        await update.message.reply_text("❌ Message Cannot Be Empty.")
        return BROADCAST_MESSAGE
    if len(message_text) > MAX_BROADCAST_LEN:
        await update.message.reply_text(f"❌ Message Too Long. Max {MAX_BROADCAST_LEN} Characters.")
        return BROADCAST_MESSAGE
    broadcast_users = context.user_data.get('broadcast_users', [])
    broadcast_type = context.user_data.get('broadcast_type', 'all')

    context.user_data['broadcast_message'] = message_text

    keyboard = [
        [InlineKeyboardButton("✅ Yes,Send", callback_data='broadcast_execute')],
        [InlineKeyboardButton("❌ No,Abort", callback_data='broadcast_cancel')]
    ]

    await update.message.reply_text(
        f"📢 Broadcast Send Confirmation\n\n"
        f"📝 Message:\n{message_text}\n\n"
        f"📊 Users: {len(broadcast_users)} User\n"
        f"🎯 Type: {'All Users' if broadcast_type == 'all' else 'Specific Users'}\n\n"
        "Are You Sure You Want To Send This Broadcast?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return BROADCAST_CONFIRM

async def broadcast_execute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await localized_query_answer(query)

    if not is_admin(query.from_user.id):
        await localized_query_answer(query, "❌ Only admin", show_alert=True)
        return

    message_text = context.user_data.get('broadcast_message')
    broadcast_users = context.user_data.get('broadcast_users', [])

    if not message_text or not broadcast_users:
        await query.edit_message_text("❌ Error In Receiving Broadcast Info")
        return ConversationHandler.END

    status_message = await query.edit_message_text(
        f"⏳ Sending Broadcast...\n"
        f"📊 No. Of Receivers: {len(broadcast_users)}\n\n"
        f"📈 status: 0/{len(broadcast_users)}"
    )

    successful = 0
    failed = 0
    failed_users = []

    for i, tg_id in enumerate(broadcast_users, 1):
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=tr(tg_id, "broadcast_delivery", message=message_text)
            )
            successful += 1

            if i % 5 == 0 or i == len(broadcast_users):
                await status_message.edit_text(
                    f"⏳ Sending Broadcast...\n"
                    f"📊 No. Of Receivers: {len(broadcast_users)}\n\n"
                    f"📈 status: {i}/{len(broadcast_users)}\n"
                    f"✅ Successful: {successful}\n"
                    f"❌ Failed: {failed}"
                )

            await asyncio.sleep(0.1)

        except Exception as e:
            failed += 1
            failed_users.append(tg_id)
            logger.error(f"Failed to send broadcast to {tg_id}: {e}")

    result_message = (
        f"✅ **Sending Broadcast Finished.**\n\n"
        f"📊 Send Stats:\n"
        f"• ✅ Successful: {successful}\n"
        f"• ❌ Failed: {failed}\n"
        f"• 📊 No. Of Receivers: {len(broadcast_users)}\n\n"
    )

    if failed > 0:
        result_message += "📋 Failed Users:\n"
        for i, failed_id in enumerate(failed_users[:10], 1):
            result_message += f"{i}. User {failed_id}\n"
        if len(failed_users) > 10:
            result_message += f"& {len(failed_users) - 10} Other User...\n"

    keyboard = [[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]

    await status_message.edit_text(
        result_message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    context.user_data.clear()

    return ConversationHandler.END

async def broadcast_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await localized_query_answer(query)

    await query.edit_message_text("❌ Sending Broadcast Aborted.")


    context.user_data.clear()

    return ConversationHandler.END

async def broadcast_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text("❌ Sending Broadcast Aborted.")
    context.user_data.clear()
    return ConversationHandler.END


async def forward_renewal_receipt(
    bot: ExtBot,
    *,
    media_type: str,
    media_file_id: str,
    caption: str,
    keyboard: InlineKeyboardMarkup,
    customer_id: int | None = None,
) -> None:
    """Forward a receipt to the customer's owner admin (multi-admin aware)."""
    recipients = admin_recipients("finance", customer_id=customer_id)
    last_error: Exception | None = None
    for chat_id in recipients:
        for attempt in range(1, 4):
            try:
                if media_type == "photo":
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=media_file_id,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=media_file_id,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                break
            except NetworkError as exc:
                last_error = exc
                if attempt == 3:
                    raise
                logger.warning(
                    "Telegram interrupted renewal receipt delivery (attempt %s/3: %s); retrying",
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.5 * attempt)
    _ = last_error


async def renew_receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_pending_renew_requests()
    user_id = update.effective_user.id
    pending = context.user_data.get('pending_renew_submission')
    if not isinstance(pending, dict):
        return

    try:
        client_id = int(pending.get("client_id", 0))
        months = int(pending.get("months", 0))
        amount = int(pending.get("amount", 0))
    except (TypeError, ValueError):
        client_id = months = amount = 0
    if not client_id or months not in set(get_renewal_month_options()) or amount <= 0:
        context.user_data.pop('pending_renew_submission', None)
        await update.message.reply_text(tr(user_id, "renew_invalid_state"))
        return

    if not user_has_client_access(user_id, client_id):
        context.user_data.pop('pending_renew_submission', None)
        await update.message.reply_text("❌ You don't have access to this subscription.")
        return

    media_type = None
    media_file_id = None
    if update.message.photo:
        media_type = "photo"
        media_file_id = update.message.photo[-1].file_id
    elif update.message.document and str(update.message.document.mime_type or "").startswith("image/"):
        media_type = "document"
        media_file_id = update.message.document.file_id
    else:
        await update.message.reply_text(tr(user_id, "renew_send_image"))
        return

    request_id = secrets.token_hex(4)
    request_record = {
        "request_id": request_id,
        "user_tg_id": user_id,
        "client_id": client_id,
        "months": months,
        "amount": amount,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "created_at": datetime.now(timezone.utc).timestamp()
    }

    client_desc = "Unknown"
    client_name = "Unknown"
    try:
        data_obj = await api_client.get('apiv2/clients', {'id': client_id})
        clients = sui_clients(data_obj) or []
        if clients:
            raw_desc = clients[0].get("desc")
            raw_name = clients[0].get("name")
            client_desc = preserve_dynamic_text(raw_desc) if raw_desc else "Unknown"
            client_name = preserve_dynamic_text(raw_name) if raw_name else "Unknown"
    except Exception as e:
        logger.error(f"Failed to fetch client details for renewal request: {e}")

    try:
        caption = tr(
            ADMIN_TELEGRAM_ID,
            "renew_admin_request",
            request_id=preserve_dynamic_text(request_id),
            user_id=user_id,
            client_id=client_id,
            name=client_name,
            description=client_desc,
            months=months,
            amount=preserve_dynamic_text(format_money(amount)),
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "approve"), callback_data=f"renew_appr_{request_id}")],
            [InlineKeyboardButton(tr(ADMIN_TELEGRAM_ID, "reject"), callback_data=f"renew_rej_{request_id}")]
        ])
        pending_renew_requests[request_id] = request_record
        await forward_renewal_receipt(
            context.bot,
            media_type=media_type,
            media_file_id=media_file_id,
            caption=caption,
            keyboard=keyboard,
            customer_id=user_id,
        )
    except TelegramError as e:
        pending_renew_requests.pop(request_id, None)
        logger.warning("Failed to forward renewal receipt to admin (%s)", e)
        await update.message.reply_text(tr(user_id, "renew_submit_failed"))
        return
    except Exception:
        pending_renew_requests.pop(request_id, None)
        logger.exception("Failed to prepare or forward renewal receipt to admin")
        await update.message.reply_text(tr(user_id, "renew_submit_failed"))
        return

    context.user_data.pop('pending_renew_submission', None)
    await update.message.reply_text(tr(user_id, "receipt_sent"))

@rate_limited(admin_only=True)
async def check_inactive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual command to check inactive users"""
    keyboard = [[InlineKeyboardButton("🔍 Check Inactive Users", callback_data='check_inactive_users')]]
    await update.message.reply_text(
        "To Check Inactive Users Click The Button Below:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def daily_subscription_reminder(app):
    # Delay first run after bot start/restart.
    await asyncio.sleep(10 * 60)
    while True:
        try:
            data = await api_client.get('apiv2/clients')
            if not data:
                logger.error("Failed to fetch clients for reminder")
                await asyncio.sleep(3600)
                continue

            clients = sui_clients(data)
            if clients is None:
                logger.error("Malformed clients response received by reminder task")
                await asyncio.sleep(3600)
                continue
            client_to_tg = build_client_to_tg_index()
            users_with_expiry, users_without_link = expiring_clients_with_assignments(clients, client_to_tg)

            # Group reminders by user for better message formatting
            user_reminders = {}

            for user_data in users_with_expiry:
                    client_id = user_data["client_id"]
                    tg_id = user_data["tg_id"]
                    expiry = user_data["expiry"]
                    name = user_data["name"]
                    desc = user_data["desc"]
                    now = datetime.now(timezone.utc)
                    expiry_date = datetime.fromtimestamp(expiry, timezone.utc)

                    if expiry_date <= now:
                        continue

                    remaining_seconds = (expiry_date - now).total_seconds()
                    remaining_days = int(remaining_seconds / 86400)
                    if remaining_seconds % 86400 > 0:
                        remaining_days += 1

                    if remaining_days in REMINDER_DAYS:
                        last_reminder = reminder_last_sent.get((tg_id, client_id, remaining_days))
                        now_timestamp = datetime.now().timestamp()
                        if last_reminder is None or (now_timestamp - last_reminder) > REMINDER_COOLDOWN:
                            # Group reminders by user
                            if tg_id not in user_reminders:
                                user_reminders[tg_id] = []

                            user_reminders[tg_id].append({
                                "client_id": client_id,
                                "name": name,
                                "desc": desc,
                                "days_remaining": remaining_days
                            })

            # Send regular reminders - now grouped by user
            successful_reminders = []
            failed_reminders = []
            users_not_started = []

            for tg_id, reminders in user_reminders.items():
                try:
                    locale = user_language(tg_id)
                    # Check if user has multiple subscriptions
                    if len(reminders) > 1:
                        message = f"{translate(locale, 'reminder_multi_title')}\n\n"

                        for reminder in reminders:
                            message += f"📱 {reminder['desc']}\n"
                            message += f"{reminder_remaining_text(locale, reminder['days_remaining'])}\n\n"

                        message += translate(locale, "renew_prompt")
                    else:
                        reminder = reminders[0]
                        message = (f"{translate(locale, 'reminder_single_title')}\n\n"
                                  f"📱 {reminder['desc']}\n"
                                  f"{reminder_remaining_text(locale, reminder['days_remaining'])}\n\n"
                                  f"{translate(locale, 'renew_prompt')}")

                    await app.bot.send_message(
                        chat_id=tg_id,
                        text=message,
                        reply_markup=renewal_reminder_keyboard(tg_id, reminders),
                    )

                    # Add each reminder to successful list for reporting
                    for reminder in reminders:
                        successful_reminders.append({
                            "tg_id": tg_id,
                            **reminder
                        })
                        reminder_last_sent[(tg_id, reminder["client_id"], reminder["days_remaining"])] = datetime.now().timestamp()

                    await asyncio.sleep(0.5)

                except Exception as e:
                    error_msg = str(e).lower()
                    if "bot was blocked by the user" in error_msg or \
                       "user is deactivated" in error_msg or \
                       "chat not found" in error_msg or \
                       "forbidden" in error_msg:
                        for reminder in reminders:
                            users_not_started.append({
                                "tg_id": tg_id,
                                **reminder
                            })
                        logger.info(f"User {tg_id} hasn't started the bot or blocked it (has {len(reminders)} expiring subscriptions)")
                    else:
                        for reminder in reminders:
                            failed_reminders.append({
                                "reminder": {
                                    "tg_id": tg_id,
                                    **reminder
                                },
                                "error": str(e)
                            })
                        logger.error(f"Failed to send reminder to {tg_id}: {e}")

            # Send reminder report to admin
            if failed_reminders or users_not_started or user_reminders or users_without_link:
                await send_reminder_report(
                    app,
                    successful_reminders,
                    failed_reminders,
                    users_not_started,
                    users_with_expiry,
                    users_without_link,
                )

            metrics.save_metrics()
            await asyncio.sleep(24 * 60 * 60)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subscription reminder task failed")
            await asyncio.sleep(3600)

async def monitor_expired_subscriptions(app):
    """Continuously notify admin when subscriptions become expired."""
    await asyncio.sleep(60)
    while True:
        try:
            data = await api_client.get('apiv2/clients')
            if not data:
                logger.error("Failed to fetch clients for expiration monitor")
                await asyncio.sleep(300)
                continue

            clients = sui_clients(data)
            if clients is None:
                logger.error("Malformed clients response received by expiration monitor")
                await asyncio.sleep(300)
                continue
            client_to_tg = build_client_to_tg_index()
            now = datetime.now(timezone.utc)

            notified_expired_ids = set()
            try:
                notified_expired_ids = await asyncio.to_thread(
                    load_expired_notification_ids, EXPIRED_NOTIFICATIONS_FILE
                )
            except Exception as e:
                logger.error(f"Failed to load expiration notifications state: {e}")

            currently_expired_ids = set()
            newly_expired_users = []

            for client in clients:
                client_id = client.get("id")
                expiry = client.get("expiry", 0)
                if not client_id or expiry == 0:
                    continue

                expiry_date = datetime.fromtimestamp(expiry, timezone.utc)
                if expiry_date > now:
                    continue

                currently_expired_ids.add(client_id)
                if client_id in notified_expired_ids:
                    continue

                tg_ids = client_to_tg.get(client_id, [])
                representative_tg = tg_ids[0] if tg_ids else "N/A"
                newly_expired_users.append({
                    "client_id": client_id,
                    "name": client.get("name", "Unknown"),
                    "desc": client.get("desc", "No description"),
                    "expiry": expiry,
                    "enable": client.get("enable", True),
                    "tg_id": representative_tg,
                    "tg_ids": tg_ids,
                    "expiry_date": expiry_date,
                })

            if newly_expired_users:
                await send_expiration_notification(app, newly_expired_users)
                notified_expired_ids.update(user["client_id"] for user in newly_expired_users)

            # Allow re-notification on future expiry after a user is renewed.
            notified_expired_ids.intersection_update(currently_expired_ids)

            try:
                await asyncio.to_thread(
                    save_expired_notification_ids,
                    EXPIRED_NOTIFICATIONS_FILE,
                    notified_expired_ids,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as e:
                logger.error(f"Failed to save expiration notifications state: {e}")

            await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Expiration monitor task failed: {e}")
            await asyncio.sleep(300)

async def send_reminder_report(app, successful_reminders, failed_reminders, users_not_started, users_with_expiry, users_without_link):
    """Send a report of reminder delivery status to admin"""
    try:
        locale = user_language(ADMIN_TELEGRAM_ID)
        def tx(key, **values):
            return translate(locale, key, **values)
        report_message = f"{tx('reminder_report')}\n\n"

        # Summary
        total_clients = len(users_with_expiry)
        total_eligible = len(successful_reminders) + len(failed_reminders) + len(users_not_started)

        report_message += f"{tx('summary')}\n"
        report_message += f"• {tx('total_users')}: {total_clients}\n"
        report_message += f"• {tx('eligible_users')}: {total_eligible}\n"
        report_message += f"• {tx('reminded')}: {len(successful_reminders)}\n"
        report_message += f"• {tx('failed_remind')}: {len(failed_reminders)}\n"
        report_message += f"• {tx('inactive')}: {len(users_not_started)}\n\n"

        # Users who haven't started the bot
        if users_not_started:
            report_message += f"{tx('inactive_title')}\n"
            for i, user in enumerate(users_not_started[:10], 1):
                report_message += f"{i}. User {user['desc']} (TG: {user['tg_id']})\n"
                report_message += f"   {reminder_remaining_text(locale, user['days_remaining'], short=True)}\n"

            if len(users_not_started) > 10:
                report_message += f"\n{tx('more_users', count=len(users_not_started) - 10)}\n"

            report_message += "\n"

        # Failed deliveries (other errors)
        if failed_reminders:
            report_message += f"{tx('failed_sends')}\n"
            for i, failed in enumerate(failed_reminders[:5], 1):
                user = failed["reminder"]
                error = failed["error"]
                report_message += f"{i}. User {user['desc']} (TG: {user['tg_id']})\n"
                report_message += f"   📛 Error: {error[:50]}...\n"

            if len(failed_reminders) > 5:
                report_message += f"\n{tx('more_errors', count=len(failed_reminders) - 5)}\n"

            report_message += "\n"

        # Successful deliveries
        if successful_reminders:
            report_message += f"{tx('successful_sends')}\n"
            for i, user in enumerate(successful_reminders[:5], 1):
                report_message += f"{i}. User {user['desc']} (TG: {user['tg_id']})\n"
                report_message += f"   {reminder_remaining_text(locale, user['days_remaining'], short=True)}\n"

            if len(successful_reminders) > 5:
                report_message += f"\n{tx('more_users', count=len(successful_reminders) - 5)}\n"

        if users_without_link:
            now = datetime.now(timezone.utc)
            unassigned_expiring = []
            for user in users_without_link:
                remaining_seconds = (datetime.fromtimestamp(user["expiry"], timezone.utc) - now).total_seconds()
                days = int(remaining_seconds // 86400) + (1 if remaining_seconds > 0 and remaining_seconds % 86400 else 0)
                if days in REMINDER_DAYS:
                    unassigned_expiring.append((user, days))
            if unassigned_expiring:
                report_message += f"\n{tx('unassigned_title', count=len(unassigned_expiring))}\n"
                for index, (user, days) in enumerate(unassigned_expiring[:20], 1):
                    report_message += tx(
                        "unassigned_item_24" if days == 1 else "unassigned_item",
                        index=index, description=user["desc"], name=user["name"],
                        client_id=user["client_id"], days=days,
                    )
                if len(unassigned_expiring) > 20:
                    report_message += f"{tx('more_users', count=len(unassigned_expiring) - 20)}\n"
                report_message += f"{tx('assign_hint')}\n"

        # Add timestamp
        report_message += f"\n{tx('report_time')}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        # Send report to admin
        await app.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=report_message
        )

        logger.info(f"Reminder report sent to admin: {len(successful_reminders)} successful, {len(failed_reminders)} failed, {len(users_not_started)} not started")

    except Exception as e:
        logger.error(f"Failed to send reminder report: {e}")

async def send_expiration_notification(app, expired_users):
    """Notify assigned users and send a separate administrator report."""
    try:
        expired_by_telegram: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for user in expired_users:
            for tg_id in user.get("tg_ids", []):
                expired_by_telegram[int(tg_id)].append(user)
        for tg_id, subscriptions in expired_by_telegram.items():
            try:
                locale = user_language(tg_id)
                lines = [translate(locale, "subscription_expired_title"), ""]
                lines.extend(
                    translate(
                        locale,
                        "subscription_expired_item",
                        description=preserve_dynamic_text(
                            str(subscription.get("desc") or subscription.get("name") or subscription["client_id"])
                        ),
                    )
                    for subscription in subscriptions
                )
                lines.extend(["", translate(locale, "subscription_expired_prompt")])
                await app.bot.send_message(
                    chat_id=tg_id,
                    text="\n".join(lines),
                    reply_markup=renewal_reminder_keyboard(tg_id, subscriptions),
                )
            except TelegramError as exc:
                logger.warning("Could not notify Telegram user %s about expiration: %s", tg_id, exc)

        message = "🚨 Expired Subscriptions\n\n"

        # Group by status
        disabled_users = [u for u in expired_users if not u.get("enable", True)]
        still_enabled_users = [u for u in expired_users if u.get("enable", True)]

        message += f"📅 Detected {len(expired_users)} expired subscription(s):\n\n"

        if disabled_users:
            message += f"❌ Users ({len(disabled_users)} Disabled):\n"
            for i, user in enumerate(disabled_users, 1):
                message += f"{i}. {user.get('desc', 'No description')}\n"
                message += f"   👤 Username: {user.get('name', 'Unknown')}\n"
                message += f"   🆔 Client ID: {user.get('client_id', 'N/A')}\n"
                message += f"   📱 Telegram ID: {user.get('tg_id', 'N/A')}\n\n"

        if still_enabled_users:
            message += f"⚠️ User ({len(still_enabled_users)} Still Enable):\n"
            message += "(Need To Check Manually To Disable)\n"
            for i, user in enumerate(still_enabled_users, 1):
                message += f"{i}. {user.get('desc', 'No description')} (ID: {user.get('client_id', 'N/A')})\n"

        # Add action buttons
        keyboard = [
            [InlineKeyboardButton("👥 All Clients", callback_data='all_clients_page_1')],
            [InlineKeyboardButton("📝 Edit Users", callback_data='edit_user_prompt')],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]
        ]

        await app.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        logger.info(f"Sent expiration notification for {len(expired_users)} users")

    except Exception as e:
        logger.error(f"Failed to send expiration notification: {e}")

async def daily_backup(app):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    while True:
        try:
            filename = f"{DB_NAME}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
            filepath = os.path.join(BACKUP_DIR, filename)
            await api_client.ensure_session()
            url = f"{api_client.base_url}/apiv2/getdb?exclude=changes,stats"
            async with api_client.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}")
                content_length = response.content_length
                if content_length is not None and content_length > BACKUP_MAX_BYTES:
                    raise BackupTooLargeError(f"Backup exceeds {format_bytes(BACKUP_MAX_BYTES)}")
                file_size = await stream_response_to_file(response, Path(filepath), BACKUP_MAX_BYTES)
            await asyncio.to_thread(validate_sqlite_database, Path(filepath))
            logger.info(f"Backup saved: {filepath} ({format_bytes(file_size)})")
            delivered_to = 0
            for _aid in admin_recipients("all"):
                try:
                    await app.bot.send_document(
                        chat_id=_aid,
                        document=Path(filepath),
                        caption=f"📦 Daily Backup\n{filename} ({format_bytes(file_size)})",
                    )
                    delivered_to += 1
                except TelegramError:
                    logger.warning("daily backup delivery failed for %s", _aid)
            # فقط وقتی حداقل یک ادمین فایل را گرفته، لوکال را پاک کن
            if delivered_to:
                await asyncio.to_thread(Path(filepath).unlink, missing_ok=True)
            MAX_BACKUPS = 7
            try:
                backups = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{DB_NAME}_*.db")))
                if len(backups) > MAX_BACKUPS:
                    for old_backup in backups[:-MAX_BACKUPS]:
                        os.remove(old_backup)
                        logger.info(f"Removed old backup: {old_backup}")
            except Exception as e:
                logger.error(f"Failed to cleanup old backups: {e}")
            await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            raise
        except BackupTooLargeError as e:
            logger.error("Backup rejected: %s", e)
            for _aid in admin_recipients("all"):
                try:
                    await app.bot.send_message(chat_id=_aid, text=f"❌ Backup rejected: {e}")
                except TelegramError:
                    logger.warning("backup notice failed for %s", _aid)
            await asyncio.sleep(3600)
        except Exception:
            logger.exception("Backup task failed")
            await asyncio.sleep(3600)

async def cleanup_deleted_clients(app):
    """Periodically check if all assigned client IDs still exist on server and auto-unlink missing ones"""
    while True:
        try:
            # Wait before first run (5 minutes after bot starts)
            await asyncio.sleep(300)

            logger.info("Starting cleanup of deleted clients...")

            # Fetch all existing clients from server
            data = await api_client.get('apiv2/clients')
            if not data:
                logger.error("Failed to fetch clients for cleanup task")
                await asyncio.sleep(3600)
                continue

            existing_clients = sui_clients(data)
            if existing_clients is None:
                logger.error("Malformed clients response received by cleanup task; no assignments changed")
                await asyncio.sleep(3600)
                continue
            existing_client_ids = {client.get("id") for client in existing_clients if client.get("id")}

            unlinked_count = 0
            unlinked_details = []

            # Check all assigned client IDs
            for tg_id, assigned_list in list(telegram_clients.items()):
                # Make a copy of the list to modify while iterating
                original_list = assigned_list.copy()
                removed_ids = []

                for client_id in original_list:
                    if client_id not in existing_client_ids:
                        # Client no longer exists - remove it
                        assigned_list.remove(client_id)
                        removed_ids.append(client_id)
                        unlinked_count += 1

                if removed_ids:
                    unlinked_details.append({
                        "tg_id": tg_id,
                        "removed_ids": removed_ids
                    })

                # If all clients removed, delete the user entry
                if not assigned_list:
                    del telegram_clients[tg_id]

            # Save changes if any were made
            if unlinked_count > 0:
                save_assignments()
                logger.info(f"Cleanup: Auto-unlinked {unlinked_count} deleted client IDs from {len(unlinked_details)} Telegram users")

                # Send notification to admin
                await send_cleanup_notification(app, unlinked_details, unlinked_count)

            # Run every Week
            await asyncio.sleep(7 * 24 * 60 * 60)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cleanup task failed")
            await asyncio.sleep(3600)

async def send_cleanup_notification(app, unlinked_details, total_count):
    """Send notification to admin about auto-unlinked clients"""
    try:
        message = "🧹 Auto-Cleanup Report\n\n"
        message += f"✅ Removed {total_count} deleted client ID(s) from Telegram assignments.\n\n"

        for detail in unlinked_details[:10]:  # Show first 10
            tg_id = detail["tg_id"]
            removed_ids = detail["removed_ids"]
            message += f"📱 Telegram ID: {tg_id}\n"
            message += f"   Removed IDs: {', '.join(map(str, removed_ids))}\n\n"

        if len(unlinked_details) > 10:
            message += f"... and {len(unlinked_details) - 10} more user(s)\n"

        message += f"\n🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        await app.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=message
        )
    except Exception as e:
        logger.error(f"Failed to send cleanup notification: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    network_error = isinstance(context.error, NetworkError)
    if network_error:
        logger.warning("Telegram operation was interrupted (%s)", context.error)
    else:
        logger.error("Unhandled bot error: %s", context.error, exc_info=context.error)
    if update and update.effective_user:
        metrics.record_error(update.effective_user.id)
    if update and update.effective_message:
        notice = (
            "⚠️ Telegram connection hiccup — your last action may not have gone through. Try again."
            if network_error
            else "❌ Unexpected Error , Please Contact Admin"
        )
        try:
            await update.effective_message.reply_text(notice)
        except TelegramError as exc:
            logger.warning("Could not deliver the error notice to Telegram: %s", exc)

def polling_error_callback(error: TelegramError) -> None:
    """Keep transient Telegram long-poll failures concise; Updater retries them."""
    if isinstance(error, NetworkError):
        logger.warning("Telegram polling connection was interrupted (%s); retrying automatically", error)
    else:
        logger.error("Telegram polling error: %s", error)

async def show_links_page(query, page: int = 1):

    if not telegram_clients:
        msg = "❌ No Link Available"
        keyboard = [[InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        return


    clients = await get_all_clients_list()
    client_info = {}
    for client in clients:
        client_id = client.get("id")
        if client_id:
            client_info[client_id] = {
                "name": client.get("name", "Unknown"),
                "desc": client.get("desc", "No description")
            }

    # Sort by Telegram ID
    sorted_links = sorted(telegram_clients.items(), key=lambda x: x[0])

    # Pagination
    items_per_page = 10
    total_pages = (len(sorted_links) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_links = sorted_links[start_idx:end_idx]

    # Build message
    msg = f"🔗 Links (Page {page}/{total_pages})\n"
    msg += f"📊 Total Links: {len(sorted_links)}\n\n"

    for idx, (tg_id, client_ids) in enumerate(page_links, start=1):  # ← FIX: client_ids is a LIST
        # Handle multiple client IDs per user
        for i, client_id in enumerate(client_ids):
            client_data = client_info.get(client_id, {})
            username = md_escape(client_data.get("name", "Unknown"))
            desc = md_escape(client_data.get("desc", "No description"))

            if len(client_ids) == 1:
                # Single subscription - show normally
                msg += f"{start_idx + idx}. 👤 Telegram ID: `{tg_id}`\n"
                msg += f"   ➡️ Client ID: `{client_id}`\n"
                msg += f"   📛 Username: {username}\n"
                msg += f"   📝 Description: {desc}\n\n"
            else:
                # Multiple subscriptions - show with sub-index
                msg += f"{start_idx + idx}.{i+1} 👤 Telegram ID: `{tg_id}`\n"
                msg += f"   ➡️ Client ID: `{client_id}`\n"
                msg += f"   📛 Username: {username}\n"
                msg += f"   📝 Description: {desc}\n\n"

    # Create keyboard with pagination
    keyboard = []

    # Pagination buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f'links_page_{page-1}'))

    nav_buttons.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data='current_page'))

    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f'links_page_{page+1}'))

    if nav_buttons:
        keyboard.append(nav_buttons)

    # Action buttons
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New Link", callback_data='add_link_help')],
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data='manage_links')],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data='main_menu')]
    ])

    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_broadcast_users_page(query, context, page: int = 1):
    """Show paginated list of users for broadcast selection"""
    items_per_page = 15  # Reduced for better display with checkmarks

    # Get clients list
    clients = await get_all_clients_list()

    # Create a mapping of client_id to client_info
    client_info_map = {}
    for client in clients:
        client_id = client.get('id')
        if client_id:
            client_info_map[client_id] = {
                'name': client.get('name', 'Unknown'),
                'desc': client.get('desc', 'No description')
            }

    # Filter out admin and create list of all users
    all_users = []
    for tg_id, client_ids in telegram_clients.items():  # Changed variable name to client_ids
        if tg_id != ADMIN_TELEGRAM_ID:
            # Since each user can have multiple client IDs, we need to handle them individually
            if isinstance(client_ids, list):
                for client_id in client_ids:
                    all_users.append((tg_id, client_id))
            else:
                # Handle old format (single client ID)
                all_users.append((tg_id, client_ids))

    total_users = len(all_users)
    total_pages = max(1, (total_users + items_per_page - 1) // items_per_page)
    page = max(1, min(page, total_pages))

    # Calculate start and end indices for current page
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_users = all_users[start_idx:end_idx]

    # Get selected user-subscription pairs.
    selected_user_keys = set(context.user_data.get('selected_user_keys', []))

    keyboard = []

    # Display users for current page
    for tg_id, client_id in page_users:
        # Get client info from the map
        client_info = client_info_map.get(client_id, {})
        desc = client_info.get('desc', 'No description')

        # Check if this specific subscription is selected.
        selection_key = f"{tg_id}:{client_id}"
        is_selected = selection_key in selected_user_keys

        # Create display text with selection indicator
        display_text = f"{desc}"
        if len(display_text) > 20:
            display_text = display_text[:18] + "..."

        # Add selection indicator (checkmark) and Telegram ID
        if is_selected:
            display_text = f"✅ {display_text} (TG: {tg_id})"
        else:
            display_text = f"👤 {display_text} (TG: {tg_id})"

        keyboard.append([InlineKeyboardButton(
            display_text,
            callback_data=f'broadcast_user_{tg_id}_{client_id}'
        )])

    # If no users found
    if total_users == 0:
        keyboard.append([InlineKeyboardButton("➕ Add User", callback_data='add_link_help')])

    # Add pagination buttons if needed
    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f'broadcast_page_{page-1}'))

    pagination_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data='current_page'))

    if page < total_pages:
        pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f'broadcast_page_{page+1}'))

    if pagination_row:
        keyboard.append(pagination_row)

    # Selection summary and action buttons
    selection_count = len(selected_user_keys)
    keyboard.extend([
        [InlineKeyboardButton(f"📋 Selected: {selection_count} User", callback_data='broadcast_selection_summary')],
        [InlineKeyboardButton("✅ Confirm Selection", callback_data='broadcast_confirm_selection')],
        [InlineKeyboardButton("🗑️ Clear Selection", callback_data='broadcast_clear_selection')],
        [InlineKeyboardButton("🔙 بازگشت", callback_data='broadcast_message')]
    ])

    context.user_data['broadcast_type'] = 'specific'
    context.user_data['broadcast_page'] = page

    selection_text = ""
    if selection_count > 0:
        selection_text = f"\n✅ {selection_count} User Are Selected."

    await query.edit_message_text(
        "📨 **Send Broadcast To Specific Users**\n\n"
        "Choose Users:\n"
        "(Click On Each User To Select/Deselect)\n\n"
        f"📊 Total Users: {total_users} User (Page {page}/{total_pages}){selection_text}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def broadcast_selection_summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show summary of selected users for broadcast"""
    query = update.callback_query
    await localized_query_answer(query)

    user_id = query.from_user.id
    if not is_admin(user_id):
        await localized_query_answer(query, "❌ Only admin", show_alert=True)
        return

    selected_user_keys = context.user_data.get('selected_user_keys', [])

    if not selected_user_keys:
        await localized_query_answer(query, "❌ No User Is Selected.", show_alert=True)
        return

    # Get clients info for selected users
    clients = await get_all_clients_list()
    client_info_map = {}
    for client in clients:
        client_id = client.get('id')
        if client_id:
            client_info_map[client_id] = {
                'name': client.get('name', 'Unknown'),
                'desc': client.get('desc', 'No description')
            }

    msg = "📋 **Selected Users:**\n\n"
    for i, selection_key in enumerate(selected_user_keys, 1):
        try:
            tg_id_str, client_id_str = selection_key.split(":", 1)
            tg_id = int(tg_id_str)
            client_id = int(client_id_str)
        except (TypeError, ValueError):
            continue
        client_info = client_info_map.get(client_id, {})
        desc = md_escape(client_info.get('desc', 'No description'))
        name = md_escape(client_info.get('name', 'Unknown'))

        msg += f"{i}. {desc} ({name})\n"
        msg += f"   📱 Telegram ID: {tg_id}\n"
        msg += f"   🆔 Client ID: {client_id if client_id else 'N/A'}\n\n"

    msg += f"📊 Total: {len(selected_user_keys)} User"

    current_page = context.user_data.get('broadcast_page', 1)
    keyboard = [
    [InlineKeyboardButton("🔙 Return To List", callback_data=f'broadcast_page_{current_page}')],
    [InlineKeyboardButton("✅ Continue To Send", callback_data='broadcast_confirm_selection')]
    ]

    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def broadcast_clear_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all selected users"""
    query = update.callback_query
    await localized_query_answer(query)

    user_id = query.from_user.id
    if not is_admin(user_id):
        await localized_query_answer(query, "❌ Only admin", show_alert=True)
        return

    context.user_data['selected_user_keys'] = []
    context.user_data['selected_users'] = []
    await localized_query_answer(query, "✅ All Selection Cleared.", show_alert=True)

    # Refresh current page
    current_page = context.user_data.get('broadcast_page', 1)
    await show_broadcast_users_page(query, context, page=current_page)

# ==================== SYSTEM MONITORING FUNCTIONS ====================

async def monitor_system_resources(app):
    """Monitor local system resources and send alerts to admin"""
    logger.info("System resource monitoring started")

    while True:
        try:
            # Get current system usage
            cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 1)
            memory = psutil.virtual_memory()
            ram_percent = memory.percent

            # Check and send alerts if needed
            await check_resource_alerts(app, cpu_percent, ram_percent)

            # Log monitoring activity (optional, for debugging)
            # logger.debug(f"Monitoring - CPU: {cpu_percent}%, RAM: {ram_percent}%")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"System monitoring error: {e}")

        # Wait before next check
        await asyncio.sleep(MONITOR_INTERVAL)

async def check_resource_alerts(app, cpu_percent, ram_percent):
    """Check if resources exceed thresholds and send alerts"""
    current_time = datetime.now().timestamp()

    # CPU Alerts
    if cpu_percent >= CPU_ALERT_THRESHOLD:
        if current_time - alert_state['cpu_alert_sent'] > ALERT_COOLDOWN:
            await send_alert(app, 'cpu', cpu_percent)
            alert_state['cpu_alert_sent'] = current_time
            alert_state['cpu_recovered'] = False
    elif not alert_state['cpu_recovered'] and cpu_percent < (CPU_ALERT_THRESHOLD - 10):
        # CPU recovered (10% below threshold to prevent flapping)
        await send_recovery(app, 'cpu', cpu_percent)
        alert_state['cpu_recovered'] = True

    # RAM Alerts
    if ram_percent >= RAM_ALERT_THRESHOLD:
        if current_time - alert_state['ram_alert_sent'] > ALERT_COOLDOWN:
            await send_alert(app, 'ram', ram_percent)
            alert_state['ram_alert_sent'] = current_time
            alert_state['ram_recovered'] = False
    elif not alert_state['ram_recovered'] and ram_percent < (RAM_ALERT_THRESHOLD - 10):
        # RAM recovered (10% below threshold to prevent flapping)
        await send_recovery(app, 'ram', ram_percent)
        alert_state['ram_recovered'] = True

async def send_alert(app, resource_type, usage_percent):
    """Send alert message to admin"""
    if resource_type == 'cpu':
        message = (
            f"🚨 **CPU Alert**\n\n"
            f"CPU usage is at `{usage_percent}%` (Threshold: {CPU_ALERT_THRESHOLD}%)\n\n"
            f"⚠️ Please check server performance and running processes!"
        )
    else:
        memory = psutil.virtual_memory()
        used_gb = memory.used / (1024**3)
        total_gb = memory.total / (1024**3)
        message = (
            f"🚨 **RAM Alert**\n\n"
            f"Memory usage is at `{usage_percent}%` (Threshold: {RAM_ALERT_THRESHOLD}%)\n"
            f"Usage: `{used_gb:.1f}GB / {total_gb:.1f}GB`\n\n"
            f"⚠️ Consider optimizing memory usage or restarting services!"
        )

    try:
        await app.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=message,
            parse_mode='Markdown'
        )
        logger.warning(f"Alert sent: {resource_type.upper()} at {usage_percent}%")
    except Exception as e:
        logger.error(f"Failed to send alert: {e}")

async def send_recovery(app, resource_type, usage_percent):
    """Send recovery notification to admin"""
    message = (
        f"✅ **{resource_type.upper()} Recovered**\n\n"
        f"{resource_type.upper()} usage is now at `{usage_percent}%`\n"
        f"System has returned to normal levels."
    )

    try:
        await app.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=message,
            parse_mode='Markdown'
        )
        logger.info(f"Recovery notification sent: {resource_type} at {usage_percent}%")
    except Exception as e:
        logger.error(f"Failed to send recovery notification: {e}")

async def main():
    global rate_limiter, redis_client, inbounds_cache
    load_assignments()
    load_cached_sub_uri()
    inbounds_cache = load_cached_inbounds()
    try:
        refreshed = await refresh_server_metadata()
        if not refreshed:
            logger.warning("Startup metadata refresh failed; cached subscription URI/inbounds remain active")
    except Exception:
        logger.exception("Startup metadata refresh failed; cached subscription URI/inbounds remain active")

    metrics.metrics['start_time'] = datetime.now().isoformat()
    metrics.save_metrics()

    if REDIS_ENABLED and REDIS_AVAILABLE:
        try:
            redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
            await redis_client.ping()
            logger.info("Redis rate limiting enabled at %s:%s", REDIS_HOST, REDIS_PORT)
            rate_limiter = RateLimiter(redis_client)
        except Exception as e:
            if redis_client is not None:
                await redis_client.aclose()
                redis_client = None
            logger.warning("Redis unavailable (%s); using in-memory rate limiting", e)
            rate_limiter = RateLimiter(None)
    else:
        if REDIS_ENABLED and not REDIS_AVAILABLE:
            logger.warning("REDIS_ENABLED is true but the Redis package is unavailable; using in-memory rate limiting")
        rate_limiter = RateLimiter(None)

    polling_request = HTTPXRequest(
        connection_pool_size=1,
        read_timeout=40,
        connect_timeout=10,
        pool_timeout=10,
    )
    app = ApplicationBuilder().bot(
        LocalizedExtBot(token=BOT_TOKEN, get_updates_request=polling_request)
    ).post_init(setup_bot_commands).build()

    workflow_conv = mixed_conversation_handler(
        # All stateful bot workflows share one conversation.  This prevents an
        # abandoned editor (for example, display-name settings) from consuming
        # text intended for a newly started create/edit workflow.
        entry_points=[
            CommandHandler('createuser', create_user_start),
            CommandHandler('edituser', edit_user_start),
            CommandHandler('deleteuser', delete_user_start),
            CommandHandler('restore', restore_backup_start),
            CallbackQueryHandler(button_callback, pattern='^broadcast_all$|^broadcast_confirm_selection$'),
            CallbackQueryHandler(
                settings_card_start,
                pattern=(
                    '^settings_set_card_number$|^settings_set_card_holder$|'
                    '^settings_set_display_name$|^settings_set_monthly_price$'
                ),
            ),
            CallbackQueryHandler(connection_guide_add_start, pattern='^settings_guides_add$'),
            CallbackQueryHandler(
                connection_guide_edit_start,
                pattern='^settings_guides_(title|replace|append)_',
            ),
            # مدیریت پلن‌ها و قیمت‌گذاری فروشگاه
            CallbackQueryHandler(plan_add_start, pattern='^plan_add$'),
            CallbackQueryHandler(plan_edit_start, pattern='^plan_edit_'),
            CallbackQueryHandler(
                pricing_input_start,
                pattern=r'^pricing_set_(per_gb|monthly|month_\d+)$',
            ),
            # قیمت اختصاصی مشتری
            CallbackQueryHandler(uprice_new_start, pattern=r'^uprice_new$|^uprice_edit_\d+$'),
            CallbackQueryHandler(uprice_callback, pattern=r'^uprice_(set_disc|set_price|pick|clear)\b|^pricing_user_menu$'),
        ],
        states={
            CREATE_USER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_name)],
            CREATE_USER_INBOUNDS: [CallbackQueryHandler(create_user_inbound_callback)],
            CREATE_USER_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_volume), CallbackQueryHandler(create_user_volume)],
            CREATE_USER_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_expiry), CallbackQueryHandler(create_user_expiry)],
            CREATE_USER_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_desc), CallbackQueryHandler(create_user_desc)],
            CREATE_USER_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_group), CallbackQueryHandler(create_user_group)],
            CREATE_USER_REMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_remark), CallbackQueryHandler(create_user_remark)],
            CREATE_USER_LIFECYCLE: [CallbackQueryHandler(create_user_lifecycle)],
            CREATE_USER_RESET_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_reset_days)],
            EDIT_USER_GET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_get_id)],
            EDIT_USER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_name)],
            EDIT_USER_INBOUNDS: [CallbackQueryHandler(edit_user_inbound_callback)],
            EDIT_USER_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_volume), CallbackQueryHandler(edit_user_volume)],
            EDIT_USER_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_expiry), CallbackQueryHandler(edit_user_expiry)],
            EDIT_USER_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_desc), CallbackQueryHandler(edit_user_desc)],
            EDIT_USER_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_group), CallbackQueryHandler(edit_user_group)],
            EDIT_USER_REMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_remark), CallbackQueryHandler(edit_user_remark)],
            EDIT_USER_LIFECYCLE: [CallbackQueryHandler(edit_user_lifecycle)],
            EDIT_USER_RESET_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_reset_days)],
            EDIT_USER_ENABLE: [CallbackQueryHandler(edit_user_enable)],
            EDIT_USER_REGEN: [CallbackQueryHandler(edit_user_regen)],
            DELETE_USER_GET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_user_get_id)],
            DELETE_USER_CONFIRM: [CallbackQueryHandler(delete_user_confirm)],
            BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message_handler)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_execute_callback, pattern='^broadcast_execute$'),
                                CallbackQueryHandler(broadcast_cancel_callback, pattern='^broadcast_cancel$')],
            SETTINGS_CARD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_card_number_input)],
            SETTINGS_CARD_HOLDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_card_holder_input)],
            SETTINGS_DISPLAY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_display_name_input)],
            SETTINGS_MONTHLY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_monthly_price_input)],
            CONNECTION_GUIDE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, connection_guide_title_input)],
            CONNECTION_GUIDE_CONTENT: [
                CommandHandler('done', connection_guide_done),
                CommandHandler('cancel', connection_guide_cancel),
                MessageHandler(filters.ALL, connection_guide_content_input),
            ],
            CONNECTION_GUIDE_EDIT_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connection_guide_edit_title_input),
            ],
            CONNECTION_GUIDE_EDIT_ITEM: [MessageHandler(filters.ALL & ~filters.COMMAND, connection_guide_edit_item_input)],
            CONNECTION_GUIDE_APPEND_ITEM: [MessageHandler(filters.ALL & ~filters.COMMAND, connection_guide_edit_item_input)],
            PLAN_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_title_input),
                         CommandHandler('skip', plan_field_skip)],
            PLAN_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_gb_input),
                      CommandHandler('skip', plan_field_skip)],
            PLAN_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_days_input),
                        CommandHandler('skip', plan_field_skip)],
            PLAN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_price_input),
                         CommandHandler('skip', plan_field_skip)],
            PLAN_RESELLER: [CallbackQueryHandler(plan_reseller_choice, pattern='^plan_res_(no|yes)$')],
            PRICING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pricing_value_input)],
            UPRICE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, uprice_user_input)],
            UPRICE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, uprice_value_dispatch),
            ],
            RESTORE_BACKUP_FILE: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, restore_backup_file)],
        },
        fallbacks=[
            CommandHandler('cancel', workflow_cancel),
            CallbackQueryHandler(
                settings_navigation_exit,
                pattern='^(settings_payments|settings_admin_tools|admin_settings|main_menu)$',
            ),
        ],
        allow_reentry=True,
    )

    app.add_handler(workflow_conv)
    app.add_handler(CommandHandler("start", start))
    # دکمه چهارگوش «⬜» بالای صفحه‌کلید — باز/بسته کردن پیام منو
    app.add_handler(MessageHandler(
        filters.Regex(r'^\s*⬜\s*$'),
        menu_toggle_handler,
    ))
    # ضربه به دکمهٔ قدیمی چسبیدهٔ 🏠 → حذف کیبورد قدیمی (یک‌بار)
    app.add_handler(MessageHandler(
        filters.Regex(r'^\s*🏠\s*$'),
        stale_home_tap_handler,
    ))
    app.add_handler(CommandHandler("usage", usage))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("deploy", deploy_command))
    app.add_handler(CommandHandler("topup", topup_command))
    app.add_handler(CallbackQueryHandler(support_contact_callback, pattern='^support_contact$'))
    app.add_handler(CallbackQueryHandler(support_send_start, pattern='^support_send$'))
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r'^(admin_finance_open|res_panel_open|res_settle_request)$'))
    # ورودی آیدی ادمین (گروه ۱ — تنها هندلر این گروه تا پیام دیگران خورده نشود)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_input), group=1)
    # پیام متنی در حالت پشتیبانی → گروه 3 (قبل از هندلرهای فروشگاه در 4/5)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, support_message_handler), group=3)
    app.add_handler(CommandHandler("metrics", metrics_command))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("diag", diag_command))
    app.add_handler(CallbackQueryHandler(diag_rerun_callback, pattern='^diag_rerun$'))
    app.add_handler(CommandHandler("assign", assign))
    app.add_handler(CommandHandler("unlink", unlink_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, interactive_assign_user_shared))
    # دیتای مینی‌اپ منو (دکمهٔ مربعی کنار کادر تایپ) → باز کردن بخش مربوطه در ربات
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_menu_data_handler), group=2)
    app.add_handler(MessageHandler(
        filters.Regex(r'^❌ Cancel Interactive Assignment$'), interactive_assign_cancel
    ))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, renew_receipt_handler))
    # shop_* به shop_callback می‌رسد (ثبت بعدی)؛ بقیه به این هندلر
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r'^(?!shop_)'))
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("checkinactive", check_inactive_command))

    # --- فروشگاه کارت‌به‌کارت (v2) + درگاه آنلاین/کیف پول/تخفیف/تست (v3) ---
    _zarinpal = None
    _payments = None
    payment_server_runner = None
    if SETTINGS.store_enabled:
        try:
            _ibs = await get_inbounds_list(force_refresh=not inbounds_cache)
            store_inbounds = [int(ib.get("id")) for ib in (_ibs or []) if ib.get("id") is not None]

            # v3: درگاه زرین‌پال (اختیاری)
            _zarinpal = None
            _payments = None
            if getattr(SETTINGS, "zarinpal_merchant_id", ""):
                try:
                    from .zarinpal_core import PaymentsStore, ZarinPalClient
                    _zarinpal = ZarinPalClient(
                        SETTINGS.zarinpal_merchant_id,
                        sandbox=SETTINGS.zarinpal_sandbox,
                    )
                    _payments = PaymentsStore(managed_data_path(
                        str(SETTINGS.store_data_dir).replace("shop_orders.json", "payments.json")
                        if SETTINGS.store_data_dir.endswith("shop_orders.json") else "payments.json"
                    ))
                    logger.info("ZarinPal gateway enabled (sandbox=%s)", SETTINGS.zarinpal_sandbox)
                except Exception:
                    logger.exception("ZarinPal init failed — online payments disabled")
                    _zarinpal = None
                    _payments = None

            # v3: کد تخفیف
            _promo_store = None
            try:
                from .promo_core import DiscountStore
                _promo_store = DiscountStore(managed_data_path(
                    str(SETTINGS.store_data_dir).replace("shop_orders.json", "discounts.json")
                    if SETTINGS.store_data_dir.endswith("shop_orders.json") else "discounts.json"
                ))
            except Exception:
                logger.exception("Discount store init failed")

            # v3: اکانت تست رایگان
            _trial_store = None
            try:
                from .trial_core import TrialStore
                _trial_store = TrialStore(managed_data_path(
                    str(SETTINGS.store_data_dir).replace("shop_orders.json", "trial_users.json")
                    if SETTINGS.store_data_dir.endswith("shop_orders.json") else "trial_users.json"
                ))
            except Exception:
                logger.exception("Trial store init failed")

            # v3: کیف پول (همان استور نمایندگی — wallet مشترک)
            _reseller_store = None
            try:
                _reseller_store = ResellerStore(managed_data_path(
                    str(SETTINGS.store_data_dir).replace("shop_orders.json", "resellers.json")
                    if SETTINGS.store_data_dir.endswith("shop_orders.json") else "resellers.json"
                ))
            except Exception:
                logger.exception("Reseller/wallet store init failed")

            # username ربات (برای لینک fallback درگاه)
            try:
                bot_username = (await app.bot.get_me()).username or ""
            except Exception:
                bot_username = ""

            register_store_handlers(
                app,
                store=OrderStore(managed_data_path(SETTINGS.store_data_dir)),
                admin_id=ADMIN_TELEGRAM_ID,
                card_number=PAYMENT_CARD_NUMBER,
                card_holder=PAYMENT_CARD_HOLDER,
                builder=build_client_data_new,
                create_client=create_or_edit_client,
                assign=add_client_assignment,
                find_client_id=find_shop_client_id,
                name_exists=shop_name_exists,
                inbound_ids=store_inbounds,
                sub_base_url=await get_subscription_base_url(),
                is_admin=is_admin,
                admins=list(ADMIN_IDS),
                plans_store=_plans_store,
                zarinpal=_zarinpal,
                payments=_payments,
                promo=_promo_store,
                trial=_trial_store,
                resellers=_reseller_store,
                trial_inbound_id=SETTINGS.trial_inbound_id,
                callback_base=SETTINGS.zarinpal_callback_base,
                bot_username=bot_username,
                zarinpal_min_toman=SETTINGS.zarinpal_min_toman,
            )
            logger.info("Store (card-to-card) enabled with %s inbounds (%s admin(s))", len(store_inbounds), len(ADMIN_IDS))

            # v3: سرور callback زرین‌پال — فقط وقتی آدرس عمومی تنظیم شده باشد
            if _zarinpal is not None and _payments is not None and SETTINGS.zarinpal_callback_base:
                try:
                    from .payment_server import start_payment_server
                    _pay_runner = await start_payment_server(
                        app.bot, app.bot_data["store"], _payments, _zarinpal,
                        SETTINGS.zarinpal_callback_bind, SETTINGS.zarinpal_callback_port,
                    )
                    payment_server_runner = _pay_runner
                    logger.info("ZarinPal callback URL: %s/pay/callback", SETTINGS.zarinpal_callback_base)
                except Exception:
                    logger.exception("Failed to start ZarinPal callback server")
        except Exception:
            logger.exception("Failed to register store handlers")

        # --- نمایندگی فروش ---
        try:
            if "resellers" not in app.bot_data:
                app.bot_data["resellers"] = ResellerStore(
                    managed_data_path(str(SETTINGS.store_data_dir).replace("shop_orders.json", "resellers.json"))
                )
            register_reseller_handlers(app)
            logger.info("Reseller program enabled (default pct=%s%%)", 20)
        except Exception:
            logger.exception("Failed to register reseller handlers")
    else:
        logger.info("Store disabled (STORE_ENABLED=false)")

    logger.info(
        "Starting SUI Bot with %s Telegram assignment(s); rate limiter=%s",
        len(telegram_clients), "redis" if rate_limiter.use_redis else "memory",
    )

    background_tasks: list[asyncio.Task] = []
    application_started = False
    polling_started = False
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    registered_signals = []
    try:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(shutdown_signal, stop_event.set)
                registered_signals.append(shutdown_signal)
            except (NotImplementedError, RuntimeError):
                # add_signal_handler is unavailable on Windows and in some embedded loops.
                break

        # تلگرام موقتاً غیرقابل‌دسترس؟ initialize تا ۱۰ بار با فاصله تکرار شود
        # (مثلاً اینترنت/VPN قطع است — سرویس نباید بمیرد). initialize یک‌بار
        # اینجا انجام می‌شود؛ `async with app` بعدی idempotent است.
        for attempt in range(1, 11):
            try:
                await app.initialize()
                break
            except (NetworkError, TimedOut) as exc:
                if attempt == 10:
                    raise
                logger.warning(
                    "Telegram unreachable (attempt %s/10: %s); retrying in 30s",
                    attempt, type(exc).__name__,
                )
                await asyncio.sleep(30)

        async with app:
            try:
                await app.start()
                application_started = True
                # post_init در این فلو دستی اجرا نمی‌شود (فقط run_polling/run_webhook) —
                # پس ثبت منوی دستورات را صراحتاً همین‌جا انجام می‌دهیم.
                try:
                    await setup_bot_commands(app)
                except Exception:
                    logger.exception("setup_bot_commands failed")
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    timeout=30,
                    error_callback=polling_error_callback,
                )
                polling_started = True
                background_tasks = [
                    asyncio.create_task(daily_subscription_reminder(app), name="daily_subscription_reminder"),
                    asyncio.create_task(monitor_expired_subscriptions(app), name="monitor_expired_subscriptions"),
                    asyncio.create_task(daily_backup(app), name="daily_backup"),
                    asyncio.create_task(monitor_system_resources(app), name="monitor_system_resources"),
                    asyncio.create_task(cleanup_deleted_clients(app), name="cleanup_deleted_clients"),
                ]
                logger.info("SUI Bot is running")
                await stop_event.wait()
            finally:
                for task in background_tasks:
                    task.cancel()
                if background_tasks:
                    await asyncio.gather(*background_tasks, return_exceptions=True)
                if polling_started:
                    try:
                        await app.updater.stop()
                    except Exception as exc:
                        logger.warning("Telegram polling cleanup failed: %s", exc)
                if application_started:
                    try:
                        await app.stop()
                    except Exception as exc:
                        logger.warning("Telegram application cleanup failed: %s", exc)
    finally:
        for shutdown_signal in registered_signals:
            loop.remove_signal_handler(shutdown_signal)
        if payment_server_runner is not None:
            try:
                await payment_server_runner.cleanup()
            except Exception as exc:
                logger.warning("ZarinPal callback server cleanup failed: %s", exc)
        try:
            await api_client.close()
        except Exception as exc:
            logger.warning("S-UI HTTP session cleanup failed: %s", exc)
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception as exc:
                logger.warning("Redis cleanup failed: %s", exc)
        metrics.save_metrics()
        logger.info("SUI Bot stopped")

def run() -> None:
    try:
        asyncio.run(main())
    except InvalidToken:
        logger.critical("Telegram rejected BOT_TOKEN; update it with 'sudo sui-bot config'")
        raise SystemExit(78) from None
    except (KeyboardInterrupt, SystemExit):
        logger.info("SUI Bot interrupted")


if __name__ == "__main__":
    run()
