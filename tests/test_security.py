"""Tests for security helpers (transport validation + ACL)."""

from __future__ import annotations

import pytest

from sui_bot.security import can_access_client, is_public_callback, validate_service_url


class TestValidateServiceUrl:
    def test_https_is_accepted(self):
        assert validate_service_url("https://panel.example.com/") == "https://panel.example.com"

    def test_http_remote_is_rejected(self):
        with pytest.raises(RuntimeError, match="HTTPS"):
            validate_service_url("http://panel.example.com")

    def test_http_loopback_is_allowed(self):
        assert validate_service_url("http://localhost:2095") == "http://localhost:2095"
        assert validate_service_url("http://127.0.0.1:2095") == "http://127.0.0.1:2095"

    def test_insecure_http_flag_allows_remote(self):
        url = validate_service_url("http://panel.example.com", allow_insecure_http=True)
        assert url == "http://panel.example.com"

    def test_embedded_credentials_rejected(self):
        with pytest.raises(RuntimeError, match="credentials"):
            validate_service_url("https://user:pass@panel.example.com")

    def test_query_string_rejected(self):
        with pytest.raises(RuntimeError, match="query"):
            validate_service_url("https://panel.example.com/?x=1")


class TestCanAccessClient:
    def test_admin_always_access(self):
        assert can_access_client(1, 99, {}, admin_id=1) is True

    def test_list_assignment(self):
        assert can_access_client(5, 3, {5: [3, 4]}, admin_id=1) is True
        assert can_access_client(5, 9, {5: [3, 4]}, admin_id=1) is False

    def test_scalar_assignment(self):
        assert can_access_client(5, 3, {5: 3}, admin_id=1) is True
        assert can_access_client(5, 4, {5: 3}, admin_id=1) is False


class TestIsPublicCallback:
    def test_public_actions(self):
        assert is_public_callback("main_menu") is True
        assert is_public_callback("my_usage") is True

    def test_public_prefixes(self):
        assert is_public_callback("lang_set_en") is True
        assert is_public_callback("shop_plan_1") is True

    def test_admin_callbacks_are_private(self):
        assert is_public_callback("server_status") is False
        assert is_public_callback("diag_rerun") is False
