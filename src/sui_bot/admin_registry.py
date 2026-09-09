"""Multi-admin registry: roles, customer ownership, persistence.

درخت تصمیم اعلان‌ها:
- support  → مالک مشتری (fallback: ادمین اصلی)
- finance  → مالک (سفارش/رسید) (fallback: ادمین اصلی)
- system   → فقط ادمین اصلی
- all      → همه‌ی ادمین‌ها
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

NOTIFICATION_KINDS = {"support", "finance", "system", "all"}


def parse_admin_ids(raw: str | None, primary: int) -> list[int]:
    """ADMIN_IDS csv -> unique positive ids, primary always first."""
    ids: list[int] = []
    for part in str(raw or "").replace("،", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            value = int(part)
            if value not in ids:
                ids.append(value)
    if primary in ids:
        ids.remove(primary)
    ids.insert(0, primary)
    return ids


class AdminRegistry:
    def __init__(
        self,
        primary_id: int,
        admin_ids: list[int] | None = None,
        owners_file: str | Path | None = None,
        admins_file: str | Path | None = None,
    ):
        self.primary_id = int(primary_id)
        self._env_admins = [int(a) for a in (admin_ids or []) if int(a) > 0]
        if self.primary_id not in self._env_admins:
            self._env_admins.insert(0, self.primary_id)
        self.admin_ids = list(self._env_admins)
        self.owners_file = Path(owners_file) if owners_file else None
        self.admins_file = Path(admins_file) if admins_file else None
        self._lock = threading.Lock()
        # customer_tg_id -> admin_tg_id
        self._owners: dict[int, int] = {}
        if self.owners_file:
            self._load()
        # runtime-added / runtime-removed admin ids (persisted in admins_file)
        self._extra_admins: list[int] = []
        self._removed_admins: set[int] = set()
        # admin_tg_id(str) -> list of S-UI client groups; key absent = unlimited
        self._groups: dict[str, list[str]] = {}
        if self.admins_file:
            self._load_admins()

    # --- identity -----------------------------------------------------------
    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.admin_ids

    @property
    def all_admins(self) -> list[int]:
        return list(self.admin_ids)

    def set_admins_file(self, path: str | Path | None) -> None:
        """Late-bind the runtime-admin persistence file (mirrors owners_file)."""
        self.admins_file = Path(path) if path else None
        if self.admins_file:
            self._load_admins()

    # --- runtime admin management (primary manages via bot UI) --------------
    def add_admin(self, user_id: object) -> str:
        """Add a runtime admin. Returns added|exists|invalid."""
        try:
            uid = int(str(user_id).strip())
        except (TypeError, ValueError):
            return "invalid"
        if uid <= 0 or uid == self.primary_id and uid in self.admin_ids:
            return "exists" if uid in self.admin_ids else "invalid"
        with self._lock:
            if uid in self.admin_ids:
                return "exists"
            if uid not in self._extra_admins:
                self._extra_admins.append(uid)
            self._removed_admins.discard(uid)
            self._rebuild_admin_ids()
            self._save_admins()
        return "added"

    def remove_admin(self, user_id: object) -> str:
        """Remove a runtime (or env) admin. Returns removed|primary|not_found|invalid."""
        try:
            uid = int(str(user_id).strip())
        except (TypeError, ValueError):
            return "invalid"
        with self._lock:
            if uid == self.primary_id:
                return "primary"
            if uid not in self.admin_ids:
                return "not_found"
            if uid in self._extra_admins:
                self._extra_admins.remove(uid)
            # env-listed admins stay removed via blocklist (persists restarts)
            self._removed_admins.add(uid)
            self._groups.pop(str(uid), None)
            self._rebuild_admin_ids()
            self._save_admins()
        return "removed"

    # --- per-admin group scopes (primary manages via bot UI) ----------------
    def groups_of(self, admin_id: int) -> list[str] | None:
        """Group whitelist for an admin; None = no restriction."""
        if int(admin_id) == self.primary_id:
            return None
        entry = self._groups.get(str(int(admin_id)))
        return None if entry is None else list(entry)

    def set_groups(self, admin_id: int, groups: list[str]) -> None:
        """Restrict an admin to the given S-UI client groups (empty = locked)."""
        with self._lock:
            cleaned = sorted({str(g).strip() for g in groups if str(g).strip()})
            self._groups[str(int(admin_id))] = cleaned
            self._save_admins()

    def clear_groups(self, admin_id: int) -> None:
        """Remove the group restriction (admin becomes unrestricted again)."""
        with self._lock:
            self._groups.pop(str(int(admin_id)), None)
            self._save_admins()

    def _rebuild_admin_ids(self) -> None:
        ordered: list[int] = []
        for aid in [self.primary_id, *self._env_admins, *self._extra_admins]:
            aid = int(aid)
            if aid not in ordered and aid not in self._removed_admins:
                ordered.append(aid)
        self.admin_ids = ordered

    def _load_admins(self) -> None:
        if not self.admins_file:
            return
        try:
            data = json.loads(Path(self.admins_file).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        extra = data.get("extra", []) if isinstance(data, dict) else []
        removed = data.get("removed", []) if isinstance(data, dict) else []
        groups = data.get("groups", {}) if isinstance(data, dict) else {}
        with self._lock:
            self._extra_admins = [
                int(a) for a in extra
                if str(a).strip().isdigit() and int(a) > 0 and int(a) != self.primary_id
            ]
            self._removed_admins = {
                int(a) for a in removed
                if str(a).strip().isdigit() and int(a) > 0 and int(a) != self.primary_id
            }
            self._groups = {
                str(int(k)): [str(g).strip() for g in v if str(g).strip()]
                for k, v in groups.items()
                if str(k).strip().isdigit() and int(k) > 0 and isinstance(v, list)
            }
            self._rebuild_admin_ids()

    def _save_admins(self) -> None:
        if not self.admins_file:
            return
        path = Path(self.admins_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "extra": list(self._extra_admins),
            "removed": sorted(self._removed_admins),
            "groups": {k: list(v) for k, v in self._groups.items()},
        }
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- ownership ----------------------------------------------------------
    def owner_of(self, customer_id: int) -> int:
        return self._owners.get(int(customer_id), self.primary_id)

    def set_owner(self, customer_id: int, admin_id: int) -> bool:
        """Bind a customer to an admin. Only admins can be owners; returns changed."""
        admin_id = int(admin_id)
        if not self.is_admin(admin_id):
            return False
        customer_id = int(customer_id)
        changed = self._owners.get(customer_id) != admin_id
        with self._lock:
            self._owners[customer_id] = admin_id
            if self.owners_file:
                self._save()
        return changed

    def customers_of(self, admin_id: int) -> list[int]:
        admin_id = int(admin_id)
        return sorted(c for c, a in self._owners.items() if a == admin_id)

    # --- routing ------------------------------------------------------------
    def recipients_for(self, kind: str, customer_id: int | None = None) -> list[int]:
        if kind not in NOTIFICATION_KINDS:
            raise ValueError(f"unknown notification kind: {kind}")
        if kind == "all":
            return self.all_admins
        if kind == "system":
            return [self.primary_id]
        # support / finance → owner (fallback primary)
        if customer_id is not None:
            return [self.owner_of(customer_id)]
        return [self.primary_id]

    # --- persistence ----------------------------------------------------------
    def _load(self) -> None:
        if not self.owners_file:
            return
        self.owners_file = Path(self.owners_file)
        if not self.owners_file.is_file():
            return
        try:
            data = json.loads(self.owners_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._owners = {int(k): int(v) for k, v in data.items() if v > 0}
        except (OSError, ValueError, json.JSONDecodeError):
            self._owners = {}

    def _save(self) -> None:
        assert self.owners_file is not None
        self.owners_file = Path(self.owners_file)
        self.owners_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.owners_file.name}.", dir=self.owners_file.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._owners, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.owners_file)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
