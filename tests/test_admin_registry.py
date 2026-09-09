"""Tests for multi-admin registry: identity, ownership, routing, persistence."""

from __future__ import annotations

from sui_bot.admin_registry import AdminRegistry, parse_admin_ids


class TestParseAdminIds:
    def test_primary_always_first(self):
        assert parse_admin_ids("", 100) == [100]

    def test_csv_with_primary_deduped(self):
        assert parse_admin_ids("200,100,300", 100) == [100, 200, 300]

    def test_garbage_ignored(self):
        assert parse_admin_ids("abc, -5, 200, ,", 100) == [100, 200]

    def test_persian_comma(self):
        assert parse_admin_ids("200،300", 100) == [100, 200, 300]


class TestAdminIdentity:
    def test_all_listed_admins_are_admin(self):
        reg = AdminRegistry(100, [100, 200])
        assert reg.is_admin(100)
        assert reg.is_admin(200)
        assert not reg.is_admin(300)

    def test_customer_is_not_admin(self):
        reg = AdminRegistry(100, [100, 200])
        assert not reg.is_admin(999)


class TestOwnership:
    def test_default_owner_is_primary(self):
        reg = AdminRegistry(100, [100, 200])
        assert reg.owner_of(777) == 100

    def test_set_owner(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], owners_file=tmp_path / "owners.json")
        assert reg.set_owner(777, 200) is True
        assert reg.owner_of(777) == 200

    def test_only_admins_can_own(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], owners_file=tmp_path / "owners.json")
        assert reg.set_owner(777, 555) is False
        assert reg.owner_of(777) == 100

    def test_ownership_persists(self, tmp_path):
        path = tmp_path / "owners.json"
        reg1 = AdminRegistry(100, [100, 200], owners_file=path)
        reg1.set_owner(777, 200)
        reg1.set_owner(888, 200)
        reg2 = AdminRegistry(100, [100, 200], owners_file=path)
        assert reg2.owner_of(777) == 200
        assert reg2.customers_of(200) == [777, 888]


class TestRouting:
    def test_support_routes_to_owner(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], owners_file=tmp_path / "o.json")
        reg.set_owner(777, 200)
        assert reg.recipients_for("support", customer_id=777) == [200]

    def test_finance_routes_to_owner(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], owners_file=tmp_path / "o.json")
        reg.set_owner(777, 200)
        assert reg.recipients_for("finance", customer_id=777) == [200]

    def test_support_without_customer_goes_primary(self):
        reg = AdminRegistry(100, [100, 200])
        assert reg.recipients_for("support") == [100]

    def test_system_goes_to_primary_only(self):
        reg = AdminRegistry(100, [100, 200])
        assert reg.recipients_for("system") == [100]

    def test_all_goes_to_every_admin(self):
        reg = AdminRegistry(100, [100, 200, 300])
        assert reg.recipients_for("all") == [100, 200, 300]

    def test_unknown_kind_raises(self):
        import pytest

        reg = AdminRegistry(100, [100])
        with pytest.raises(ValueError):
            reg.recipients_for("nope")


class TestRuntimeAdmins:
    def test_add_and_remove(self, tmp_path):
        reg = AdminRegistry(100, [100], admins_file=tmp_path / "admins.json")
        assert reg.add_admin(200) == "added"
        assert reg.is_admin(200)
        assert reg.all_admins == [100, 200]
        assert reg.add_admin(200) == "exists"
        assert reg.add_admin("abc") == "invalid"
        assert reg.add_admin(-5) == "invalid"
        assert reg.remove_admin(100) == "primary"
        assert reg.remove_admin(999) == "not_found"
        assert reg.remove_admin(200) == "removed"
        assert not reg.is_admin(200)

    def test_runtime_admins_persist(self, tmp_path):
        path = tmp_path / "admins.json"
        reg1 = AdminRegistry(100, [100], admins_file=path)
        assert reg1.add_admin(200) == "added"
        reg2 = AdminRegistry(100, [100], admins_file=path)
        assert reg2.is_admin(200)
        assert reg2.all_admins == [100, 200]

    def test_removed_env_admin_stays_removed(self, tmp_path):
        path = tmp_path / "admins.json"
        reg1 = AdminRegistry(100, [100, 200], admins_file=path)
        assert reg1.remove_admin(200) == "removed"
        reg2 = AdminRegistry(100, [100, 200], admins_file=path)
        assert not reg2.is_admin(200)
        assert reg2.all_admins == [100]


class TestGroupScopes:
    def test_none_means_unrestricted(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], admins_file=tmp_path / "a.json")
        assert reg.groups_of(200) is None
        assert reg.groups_of(100) is None

    def test_set_and_persist(self, tmp_path):
        path = tmp_path / "a.json"
        reg1 = AdminRegistry(100, [100, 200], admins_file=path)
        reg1.set_groups(200, ["B", "A", "A", " ", ""])
        assert reg1.groups_of(200) == ["A", "B"]
        reg2 = AdminRegistry(100, [100, 200], admins_file=path)
        assert reg2.groups_of(200) == ["A", "B"]

    def test_empty_list_locks_and_clear_restores(self, tmp_path):
        reg = AdminRegistry(100, [100, 200], admins_file=tmp_path / "a.json")
        reg.set_groups(200, ["A"])
        reg.set_groups(200, [])
        assert reg.groups_of(200) == []
        reg.clear_groups(200)
        assert reg.groups_of(200) is None

    def test_primary_cannot_be_scoped(self, tmp_path):
        reg = AdminRegistry(100, [100], admins_file=tmp_path / "a.json")
        reg.set_groups(100, ["A"])
        assert reg.groups_of(100) is None

    def test_removal_drops_scope(self, tmp_path):
        path = tmp_path / "a.json"
        reg1 = AdminRegistry(100, [100, 200], admins_file=path)
        reg1.set_groups(200, ["A"])
        reg1.remove_admin(200)
        reg2 = AdminRegistry(100, [100, 200], admins_file=path)
        assert reg2.groups_of(200) is None
