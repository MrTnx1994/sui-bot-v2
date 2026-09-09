"""Panel endpoint diagnostics for the /diag command.

Every probe is a read-only GET against the panel's apiv2 endpoints.  The
result of each probe is a plain dict so tests can exercise the formatting
logic without a live panel.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

# (نام نمایشی، endpoint، params) — فقط GET؛ هیچ probe ی اثر جانبی ندارد.
PROBES: list[tuple[str, str, dict[str, str] | None]] = [
    ("load (subURI+inbounds)", "apiv2/load", None),
    ("clients", "apiv2/clients", None),
    ("inbounds", "apiv2/inbounds", None),
    ("onlines", "apiv2/onlines", None),
    ("status", "apiv2/status", {"r": "cpu,mem,sys,sbd"}),
    ("settings", "apiv2/settings", None),
]

MAX_DETAIL_CHARS = 160


def _shorten(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = str(text).strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_probe(
    name: str,
    response: object,
    elapsed_ms: int,
    last_error: str | None = None,
) -> dict[str, Any]:
    """Turn a raw response envelope (or None) into one diag row."""
    row: dict[str, Any] = {
        "name": name,
        "ok": False,
        "detail": "",
        "elapsed_ms": elapsed_ms,
    }
    if response is None:
        row["detail"] = _shorten(last_error or "no response (network/timeout)")
        return row
    if not isinstance(response, dict) or response.get("success") is not True:
        reason = response.get("msg") if isinstance(response, dict) else None
        row["detail"] = _shorten(reason or "unsuccessful API response")
        return row

    obj = response.get("obj")
    if isinstance(obj, dict):
        if name.startswith("load"):
            has_sub = "subURI" in obj and bool(obj.get("subURI"))
            inbounds = obj.get("inbounds")
            inb_count = len(inbounds) if isinstance(inbounds, list) else "?"
            row["detail"] = f"subURI={'yes' if has_sub else 'MISSING'}; inbounds={inb_count}"
            row["ok"] = has_sub
        elif name == "clients":
            clients = obj.get("clients")
            row["ok"] = isinstance(clients, list)
            row["detail"] = f"clients={len(clients) if isinstance(clients, list) else 'bad'}"
        elif name == "inbounds":
            inbounds = obj.get("inbounds")
            row["ok"] = isinstance(inbounds, list)
            row["detail"] = f"inbounds={len(inbounds) if isinstance(inbounds, list) else 'bad'}"
        elif name == "onlines":
            users = obj.get("user") if isinstance(obj.get("user"), list) else None
            row["ok"] = users is not None
            row["detail"] = f"online={len(users) if users is not None else 'bad'}"
        elif name == "status":
            row["ok"] = isinstance(obj.get("sys"), (dict, str)) or "sys" in obj or bool(obj)
            row["detail"] = f"keys={len(obj)}"
        else:
            row["ok"] = bool(obj)
            row["detail"] = f"keys={len(obj)}" if obj else "empty obj"
    elif isinstance(obj, list):
        row["ok"] = True
        row["detail"] = f"items={len(obj)}"
    elif isinstance(obj, str) and obj:
        row["ok"] = True
        row["detail"] = _shorten(obj)
    else:
        row["detail"] = "empty obj"
    return row


async def probe_endpoints(
    get: Callable[..., Awaitable[object | None]],
    probes: list[tuple[str, str, dict[str, str] | None]] | None = None,
) -> list[dict[str, Any]]:
    """Run every probe sequentially with a single attempt each."""
    rows: list[dict[str, Any]] = []
    for name, endpoint, params in probes or PROBES:
        started = time.perf_counter()
        try:
            response = await get(endpoint, params=params, attempts=1, log_failure=False)
        except Exception as exc:  # noqa: BLE001 — diag must never crash the bot
            response = None
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            last_error = None
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        rows.append(describe_probe(name, response, elapsed_ms, last_error))
    return rows


def format_diag_report(rows: list[dict[str, Any]]) -> str:
    ok_count = sum(1 for row in rows if row["ok"])
    lines = [
        f"🩺 S-UI Panel Diagnostics  [{ok_count}/{len(rows)} OK]",
        "",
    ]
    for row in rows:
        mark = "✅" if row["ok"] else "❌"
        suffix = "" if row["ok"] else f" — {row['detail']}"
        lines.append(f"{mark} {row['name']}: {row['elapsed_ms']}ms{suffix}")
    lines.append("")
    if ok_count == len(rows):
        lines.append("All endpoints healthy.")
    else:
        lines.append("❌ endpoints above show the panel's own error message.")
    return "\n".join(lines)
