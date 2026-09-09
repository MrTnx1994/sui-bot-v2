"""Tests for sui_bot.bot pure helpers (no network, no Telegram)."""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(scope="module", autouse=True)
def bot_module(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    os.environ.setdefault("SUI_HOST", "https://panel.example.com")
    os.environ.setdefault("SUI_TOKEN", "test-token")
    os.environ.setdefault("BOT_TOKEN", "123456:TEST")
    os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["REDIS_ENABLED"] = "false"
    os.environ["BOT_LOG_FILE"] = ""
    import sui_bot.bot as bot

    return importlib.reload(bot)


class TestFormatBytes:
    def test_bytes(self, bot_module):
        assert bot_module.format_bytes(500) == "500.00 B"

    def test_kilobytes(self, bot_module):
        assert bot_module.format_bytes(2048) == "2.00 KB"

    def test_gigabytes(self, bot_module):
        assert bot_module.format_bytes(3 * 1024 ** 3) == "3.00 GB"

    def test_terabytes(self, bot_module):
        assert bot_module.format_bytes(1.5 * 1024 ** 4) == "1.50 TB"


class TestParseRenewalMonthOptions:
    def test_valid_options(self, bot_module):
        assert bot_module.parse_renewal_month_options("1,3,2") == [1, 2, 3]

    def test_dedupes_and_sorts(self, bot_module):
        assert bot_module.parse_renewal_month_options("3,1,3,2") == [1, 2, 3]

    def test_ignores_out_of_range(self, bot_module):
        assert bot_module.parse_renewal_month_options("0,25,2") == [2]

    def test_falls_back_to_defaults(self, bot_module):
        assert bot_module.parse_renewal_month_options("") == [1, 2, 3]
        assert bot_module.parse_renewal_month_options("abc") == [1, 2, 3]


class TestSafeCallbackData:
    def test_accepts_simple(self, bot_module):
        assert bot_module.is_safe_callback_data("user_page_2")

    def test_accepts_allowed_symbols(self, bot_module):
        assert bot_module.is_safe_callback_data("client:12-edit")

    def test_rejects_newlines(self, bot_module):
        assert not bot_module.is_safe_callback_data("abc\ndef")

    def test_rejects_too_long(self, bot_module):
        assert not bot_module.is_safe_callback_data("a" * 65)


class TestRandomConfigs:
    def test_has_every_protocol_key(self, bot_module):
        configs = bot_module.random_configs("alice")
        expected = {
            "mixed", "socks", "http", "shadowsocks", "shadowsocks16", "shadowtls",
            "vmess", "vless", "anytls", "trojan", "naive", "hysteria", "tuic", "hysteria2",
        }
        assert set(configs) == expected

    def test_names_are_set(self, bot_module):
        configs = bot_module.random_configs("alice")
        assert configs["vless"]["name"] == "alice"
        assert configs["mixed"]["username"] == "alice"

    def test_uuid_shared_by_uuid_protocols(self, bot_module):
        configs = bot_module.random_configs("alice")
        assert configs["vless"]["uuid"] == configs["vmess"]["uuid"] == configs["tuic"]["uuid"]


class TestUpdateConfigs:
    def test_renames_name_and_username(self, bot_module):
        configs = bot_module.random_configs("old")
        bot_module.update_configs(configs, "new")
        assert configs["vless"]["name"] == "new"
        assert configs["mixed"]["username"] == "new"


class TestShuffleConfigs:
    def test_regenerates_targeted_password(self, bot_module):
        configs = bot_module.random_configs("alice")
        before = configs["trojan"]["password"]
        bot_module.shuffle_configs(configs, "trojan")
        assert configs["trojan"]["password"] != before

    def test_regenerates_uuid(self, bot_module):
        configs = bot_module.random_configs("alice")
        before = configs["vless"]["uuid"]
        bot_module.shuffle_configs(configs, "vless")
        assert configs["vless"]["uuid"] != before


class TestBuildClientDataNew:
    def test_payload_shape(self, bot_module):
        payload = bot_module.build_client_data_new(
            name="alice",
            volume_bytes=1024,
            expiry_timestamp=1700000000,
            desc="d",
            group="g",
            inbounds=[1, 2],
            enable=True,
        )
        assert payload["name"] == "alice"
        assert payload["volume"] == 1024
        assert payload["expiry"] == 1700000000
        assert list(payload["inbounds"]) == [1, 2]
        assert payload["enable"] is True
        assert isinstance(payload["config"], dict)
        assert "vless" in payload["config"] and "trojan" in payload["config"]


class TestBuildClientRenewalData:
    def test_renewal_resets_traffic(self, bot_module):
        original = {"id": 7, "name": "alice", "up": 100, "down": 200, "totalUp": 5, "totalDown": 9, "expiry": 1}
        renewed = bot_module.build_client_renewal_data(original, 7, 123)
        assert renewed["expiry"] == 123
        assert renewed["enable"] is True
        assert renewed["up"] == 0 and renewed["down"] == 0
        assert renewed["totalUp"] == 0 and renewed["totalDown"] == 0
        assert renewed["name"] == "alice"


class TestApiClientErrorReason:
    def test_record_and_clear(self, bot_module):
        client = bot_module.APIClient("https://panel.example.com", "tok")
        client.record_error("apiv2/clients?id=5", "HTTP 502")
        assert "HTTP 502" in client.error_reason("apiv2/clients")
        client.clear_error("apiv2/clients")
        assert client.error_reason("apiv2/clients") == ""

    def test_reason_includes_hint(self, bot_module):
        client = bot_module.APIClient("https://panel.example.com", "tok")
        client.record_error("apiv2/save", "boom")
        assert client.error_reason("apiv2/save") == " (Reason: boom)"
