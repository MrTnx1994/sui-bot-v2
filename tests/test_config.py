"""Tests for Settings validation."""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def fresh_config(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("SUI_", "BOT_", "ADMIN_", "REDIS_", "DATA_DIR", "BOT_LOG", "PAYMENT_", "STORE_", "RATE_", "MAX_", "BLOCK_", "ITEMS_", "SUB_", "ASSIGN", "METRICS", "REMINDER", "RENEWAL", "BACKUP", "DB_", "ALLOW_", "WEB_", "HIDE_", "BOT_DISPLAY")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUI_HOST", "https://panel.example.com")
    monkeypatch.setenv("SUI_TOKEN", "tok")
    monkeypatch.setenv("BOT_TOKEN", "1:T")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "1")
    import sui_bot.config as config

    return importlib.reload(config)


class TestSettingsFromEnv:
    def test_minimum_valid(self, fresh_config):
        settings = fresh_config.Settings.from_env()
        assert settings.sui_host == "https://panel.example.com"
        assert settings.admin_telegram_id == 1

    def test_missing_required(self, fresh_config, monkeypatch):
        monkeypatch.delenv("SUI_TOKEN")
        with pytest.raises(RuntimeError, match="SUI_TOKEN"):
            fresh_config.Settings.from_env()

    def test_invalid_bool(self, fresh_config, monkeypatch):
        monkeypatch.setenv("REDIS_ENABLED", "maybe")
        with pytest.raises(RuntimeError, match="REDIS_ENABLED"):
            fresh_config.Settings.from_env()

    def test_invalid_admin_id(self, fresh_config, monkeypatch):
        monkeypatch.setenv("ADMIN_TELEGRAM_ID", "zero")
        with pytest.raises(RuntimeError, match="ADMIN_TELEGRAM_ID"):
            fresh_config.Settings.from_env()

    def test_invalid_renewal_options(self, fresh_config, monkeypatch):
        monkeypatch.setenv("RENEWAL_MONTH_OPTIONS", "a,b")
        with pytest.raises(RuntimeError, match="RENEWAL_MONTH_OPTIONS"):
            fresh_config.Settings.from_env()
