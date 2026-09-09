"""One-off live probe of the panel's read-only GET endpoints (no mutations)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

BASE = "https://tnt.traviann.ir:2095/app"
TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""

PROBES = [
    ("load", "apiv2/load", None),
    ("clients", "apiv2/clients", None),
    ("inbounds", "apiv2/inbounds", None),
    ("onlines", "apiv2/onlines", None),
    ("status", "apiv2/status", {"r": "cpu,mem,sys,sbd"}),
    ("settings", "apiv2/settings", None),
    ("users", "apiv2/users", None),
    ("logs", "apiv2/logs", {"c": "3", "l": "info"}),
    ("changes", "apiv2/changes", {"c": "3"}),
    ("keypairs", "apiv2/keypairs", None),
]


def fetch(endpoint: str, params: dict | None):
    url = f"{BASE}/{endpoint}"
    if params:
        from urllib.parse import urlencode

        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"Token": TOKEN})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


async def main() -> None:
    import ssl

    _ = ssl  # urllib handles TLS; placeholder to keep async signature simple
    loop = asyncio.get_running_loop()
    ok_count = 0
    for name, endpoint, params in PROBES:
        try:
            status, body = await loop.run_in_executor(None, fetch, endpoint, params)
            try:
                payload = json.loads(body)
                obj = payload.get("obj") if isinstance(payload, dict) else None
                success = isinstance(payload, dict) and payload.get("success") is True
                if isinstance(obj, dict):
                    shape = f"dict keys={list(obj)[:6]}"
                elif isinstance(obj, list):
                    shape = f"list n={len(obj)}"
                else:
                    shape = f"obj={type(obj).__name__}"
                mark = "OK " if success else "FAIL"
                ok_count += success
                print(f"{mark} {name:10s} http={status} {shape} msg={str(payload.get('msg'))[:60] if isinstance(payload, dict) else ''}")
            except json.JSONDecodeError:
                print(f"OK  {name:10s} http={status} binary bytes={len(body)}")
                ok_count += 1
        except Exception as exc:
            print(f"ERR {name:10s} {type(exc).__name__}: {str(exc)[:80]}")
    print(f"\n{ok_count}/{len(PROBES)} endpoints healthy")


if __name__ == "__main__":
    asyncio.run(main())
