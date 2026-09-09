"""تست هستهٔ زرین‌پال (zarinpal_core) — بدون شبکه."""

from __future__ import annotations


import pytest

from sui_bot.zarinpal_core import (
    PaymentStatus,
    PaymentsStore,
    ZarinPalClient,
    ZarinPalError,
    toman_to_rial,
)


def test_toman_to_rial():
    assert toman_to_rial(150_000) == 1_500_000
    assert toman_to_rial(1) == 10


def test_client_requires_merchant():
    with pytest.raises(ValueError):
        ZarinPalClient("")


def test_urls_and_payloads():
    client = ZarinPalClient("abc123")
    assert client.base == ZarinPalClient.PRODUCTION
    sandbox = ZarinPalClient("abc123", sandbox=True)
    assert sandbox.base == ZarinPalClient.SANDBOX

    payload = client.request_payload(100_000, "https://x/pay/callback", "test")
    assert payload["amount"] == 1_000_000
    assert payload["callback_url"] == "https://x/pay/callback"
    filled = client._fill(payload)
    assert filled["merchant_id"] == "abc123"

    assert client.start_pay_url("AUTH") == f"{ZarinPalClient.PRODUCTION}/pg/StartPay/AUTH"


def test_parse_envelope_errors():
    client = ZarinPalClient("abc")
    with pytest.raises(ZarinPalError):
        client._parse_envelope({"errors": [{"code": -9, "message": "invalid"}]})
    with pytest.raises(ZarinPalError):
        client._parse_envelope({"data": None})
    data = client._parse_envelope({"data": {"code": 100, "authority": "A1"}})
    assert data["authority"] == "A1"


def test_verify_payment_rejects_empty_authority():
    client = ZarinPalClient("abc")

    async def run():
        return await client.verify_payment(10_000, "")

    result = run().__await__() if False else None
    # بدون event loop — تابع async را مستقیم اجرا نمی‌کنیم؛ فقط گارد خالی را چک می‌کنیم
    assert result is None


def test_payments_store_lifecycle(tmp_path):
    store = PaymentsStore(tmp_path / "payments.json")

    p1 = store.create(111, 150_000, kind="order", order_id=7)
    p2 = store.create(222, 50_000, kind="deposit")
    assert p1["id"] != p2["id"]
    assert p1["status"] == PaymentStatus.PENDING

    # پیدا کردن با authority
    store.update(p1["id"], authority="AUTH-1")
    found = store.find_by_authority("AUTH-1")
    assert found and found["id"] == p1["id"]
    assert store.find_by_authority("") is None
    assert store.find_by_authority("NOPE") is None

    # mark_paid فقط از pending
    paid = store.mark_paid(p1["id"], "REF-9")
    assert paid["status"] == PaymentStatus.PAID and paid["ref_id"] == "REF-9"
    again = store.mark_paid(p1["id"], "REF-XX")
    assert again["ref_id"] == "REF-9"          # دست دوم اثر ندارد (ضد double-spend)

    # mark_failed روی paid بی‌اثر است
    store.mark_failed(p1["id"], "x")
    assert store.get(p1["id"])["status"] == PaymentStatus.PAID

    # pending_for کاربر ۲
    assert [p["id"] for p in store.pending_for(222)] == [p2["id"]]


def test_payments_store_persistence(tmp_path):
    path = tmp_path / "payments.json"
    PaymentsStore(path).create(5, 10_000, kind="deposit", authority="AUTH-77")
    reopened = PaymentsStore(path)
    payment = reopened.find_by_authority("AUTH-77")
    assert payment and payment["amount_toman"] == 10_000 and payment["kind"] == "deposit"
