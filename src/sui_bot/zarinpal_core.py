"""درگاه پرداخت زرین‌پال + استور تراکنش‌ها — هستهٔ خالص (بدون تلگرام).

ZarinPalClient  → درخواست/تأیید پرداخت (API v4، ریال)
PaymentsStore   → همهٔ تراکنش‌های آنلاین (خرید سفارش / شارژ کیف پول) در payments.json

نکتهٔ واحد پول: همه‌جا تومان ذخیره می‌کنیم؛ هنگام تماس با زرین‌پال ×۱۰ (ریال) می‌شود.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("sui_bot.zarinpal")

CODE_OK = 100          # موفق
CODE_ALREADY_VERIFIED = 101  # قبلاً تأیید شده (idempotent)


class ZarinPalError(RuntimeError):
    """خطای تماس با درگاه — msg برای نمایش به ادمین/کاربر."""


def toman_to_rial(amount_toman: int) -> int:
    return int(amount_toman) * 10


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    ref_id: str | None = None
    code: int = 0
    message: str = ""


class ZarinPalClient:
    """کلاینت API v4 زرین‌پال — فقط دو عملیات: request و verify."""

    PRODUCTION = "https://payment.zarinpal.com"
    SANDBOX = "https://sandbox.zarinpal.com"

    def __init__(self, merchant_id: str, *, sandbox: bool = False, timeout: int = 20):
        self.merchant_id = (merchant_id or "").strip()
        if not self.merchant_id:
            raise ValueError("ZARINPAL_MERCHANT_ID is required")
        self.base = self.SANDBOX if sandbox else self.PRODUCTION
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    # --- payloads (برای تست بدون شبکه) ----------------------------------------
    @staticmethod
    def request_payload(amount_toman: int, callback_url: str, description: str) -> dict[str, Any]:
        return {
            "merchant_id": "{MERCHANT}",
            "amount": toman_to_rial(amount_toman),
            "callback_url": callback_url,
            "description": description[:255],
        }

    @staticmethod
    def verify_payload(amount_toman: int, authority: str) -> dict[str, Any]:
        return {
            "merchant_id": "{MERCHANT}",
            "amount": toman_to_rial(amount_toman),
            "authority": authority,
        }

    def _fill(self, payload: dict[str, Any]) -> dict[str, Any]:
        filled = dict(payload)
        filled["merchant_id"] = self.merchant_id
        return filled

    # --- HTTP ------------------------------------------------------------------
    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=self._fill(payload)) as response:
                    response.raise_for_status()
                    return await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ZarinPalError(f"خطای شبکه در تماس با زرین‌پال: {type(exc).__name__}") from exc
        except Exception as exc:
            raise ZarinPalError(f"پاسخ نامعتبر زرین‌پال: {exc}") from exc

    @staticmethod
    def _parse_envelope(body: dict[str, Any]) -> dict[str, Any]:
        """پاسخ v4 → بخش data؛ خطا را به ZarinPalError تبدیل کن."""
        if not isinstance(body, dict):
            raise ZarinPalError("پاسخ زرین‌پال JSON نبود")
        errors = body.get("errors") or {}
        data = body.get("data")
        if errors:
            if isinstance(errors, list) and errors:
                message = str(errors[0].get("message") or errors[0])
            else:
                message = str(errors)
            raise ZarinPalError(f"خطای زرین‌پال: {message}")
        if not isinstance(data, dict):
            raise ZarinPalError("پاسخ زرین‌پال بدون data")
        return data

    # --- عملیات ----------------------------------------------------------------
    async def request_payment(
        self, amount_toman: int, callback_url: str, description: str
    ) -> str:
        """authority برای ساخت لینک پرداخت؛ خطا → ZarinPalError."""
        if int(amount_toman) <= 0:
            raise ZarinPalError("مبلغ نامعتبر است")
        body = await self._post(
            "/pg/v4/payment/request.json",
            self.request_payload(amount_toman, callback_url, description),
        )
        data = self._parse_envelope(body)
        authority = str(data.get("authority") or "")
        if data.get("code") != CODE_OK or not authority:
            raise ZarinPalError(f"درگاه درخواست را نپذیرفت (code={data.get('code')})")
        return authority

    async def verify_payment(self, amount_toman: int, authority: str) -> VerifyResult:
        """تأیید پرداخت — code=100 موفق، 101 قبلاً تأییدشده (باز هم ok)."""
        if not authority:
            return VerifyResult(False, None, 0, "authority خالی است")
        try:
            body = await self._post(
                "/pg/v4/payment/verify.json",
                self.verify_payload(amount_toman, authority),
            )
            data = self._parse_envelope(body)
        except ZarinPalError as exc:
            return VerifyResult(False, None, 0, str(exc))
        code = int(data.get("code") or 0)
        ref_id = str(data.get("ref_id") or "") or None
        if code in (CODE_OK, CODE_ALREADY_VERIFIED):
            return VerifyResult(True, ref_id, code, "")
        messages = {
            -9: "مبلغ یا authority نامعتبر است",
            -50: "مبلغ پرداخت‌شده با مبلغ تراکنش متفاوت است",
            -51: "پرداخت ناموفق یا لغو شده است",
            -53: "پرداخت به درگاه دیگری تعلق دارد",
            -54: "authority نامعتبر است",
        }
        return VerifyResult(False, ref_id, code, messages.get(code, f"تأیید ناموفق (code={code})"))

    def start_pay_url(self, authority: str) -> str:
        return f"{self.base}/pg/StartPay/{authority}"


# ---------------------------------------------------------------------------
# استور تراکنش‌های آنلاین
# ---------------------------------------------------------------------------
class PaymentStatus:
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


class PaymentsStore:
    """payments.json — هر تراکنش آنلاین: خرید سفارش یا شارژ کیف پول."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"payments": [], "seq": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("payments"), list):
                return data
        except Exception as exc:
            logger.error("payments file corrupt, starting fresh: %s", exc)
        return {"payments": [], "seq": 0}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --- CRUD -----------------------------------------------------------------
    def create(
        self,
        tg_id: int,
        amount_toman: int,
        *,
        kind: str,                       # order | deposit
        order_id: int | None = None,
        authority: str = "",
    ) -> dict[str, Any]:
        data = self._load()
        data["seq"] += 1
        payment = {
            "id": data["seq"],
            "tg_id": int(tg_id),
            "kind": kind,
            "order_id": int(order_id) if order_id else None,
            "amount_toman": int(amount_toman),
            "authority": authority,
            "ref_id": None,
            "status": PaymentStatus.PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "paid_at": None,
        }
        data["payments"].append(payment)
        self._save(data)
        return payment

    def update(self, payment_id: int, **fields: Any) -> dict[str, Any] | None:
        data = self._load()
        for payment in data["payments"]:
            if payment["id"] == int(payment_id):
                payment.update(fields)
                self._save(data)
                return payment
        return None

    def get(self, payment_id: int) -> dict[str, Any] | None:
        for payment in self._load()["payments"]:
            if payment["id"] == int(payment_id):
                return payment
        return None

    def find_by_authority(self, authority: str) -> dict[str, Any] | None:
        key = (authority or "").strip()
        if not key:
            return None
        for payment in self._load()["payments"]:
            if payment.get("authority") == key:
                return payment
        return None

    def mark_paid(self, payment_id: int, ref_id: str | None) -> dict[str, Any] | None:
        """فقط وقتی pending است paid می‌شود (ضد double-spend)."""
        payment = self.get(payment_id)
        if payment is None:
            return None
        if payment["status"] != PaymentStatus.PENDING:
            return payment  # قبلاً پردازش شده — همان رکورد برگردد
        return self.update(
            payment_id,
            status=PaymentStatus.PAID,
            ref_id=ref_id,
            paid_at=datetime.now(timezone.utc).isoformat(),
        )

    def mark_failed(self, payment_id: int, message: str = "") -> dict[str, Any] | None:
        payment = self.get(payment_id)
        if payment is None:
            return None
        if payment["status"] != PaymentStatus.PENDING:
            return payment
        return self.update(payment_id, status=PaymentStatus.FAILED, ref_id=message or None)

    def pending_for(self, tg_id: int) -> list[dict[str, Any]]:
        rows = [
            p for p in self._load()["payments"]
            if p["tg_id"] == int(tg_id) and p["status"] == PaymentStatus.PENDING
        ]
        rows.sort(key=lambda p: p["id"], reverse=True)
        return rows
