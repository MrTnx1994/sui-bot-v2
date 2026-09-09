"""Smoke test: every module imports and the handler surface stays intact."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module")
def bot_module():
    import sui_bot.bot as bot

    return bot


class TestImports:
    def test_reseller_settle_handler_is_imported(self, bot_module):
        """Dکمه تسویه نماینده باید هندلر واقعی داشته باشد (قبلاً NameError بی‌صدا بود)."""
        from sui_bot import reseller_bot

        assert bot_module._res_settle_request is reseller_bot._res_settle_request

    def test_diagnostics_helpers_available(self, bot_module):
        assert callable(bot_module.probe_endpoints)
        assert callable(bot_module.format_diag_report)

    def test_sui_payload_accepts_list(self, bot_module):
        assert bot_module.sui_payload({"success": True, "obj": [1, 2]}) == [1, 2]
        assert bot_module.sui_payload({"success": True, "obj": {"a": 1}}) == {"a": 1}
        assert bot_module.sui_payload({"success": False, "obj": [1]}) is None

    def test_sui_response_object_still_dict_only(self, bot_module):
        assert bot_module.sui_response_object({"success": True, "obj": [1]}) is None
        assert bot_module.sui_response_object({"success": True, "obj": {"a": 1}}) == {"a": 1}

    def test_diag_handlers_exist(self, bot_module):
        assert asyncio.iscoroutinefunction(bot_module.diag_command)
        assert asyncio.iscoroutinefunction(bot_module.diag_rerun_callback)

    def test_error_handler_is_coroutine(self, bot_module):
        assert asyncio.iscoroutinefunction(bot_module.error_handler)


class TestNoAdminShadowing:
    def test_no_function_shadows_is_admin_while_calling_it(self):
        """رگرسیون: هیچ تابعی نباید هم is_admin را local تعریف کند هم صدا بزند
        (باگ UnboundLocalError که همه‌ی دکمه‌ها را خاموش کرد)."""
        import ast
        from pathlib import Path

        import sui_bot.bot as _bot

        source = Path(_bot.__file__).read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns = [
                n.targets[0].id
                for n in ast.walk(node)
                if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "is_admin"
            ]
            if not assigns:
                continue
            calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "is_admin"
            ]
            assert not calls, (
                f"{node.name} assigns is_admin={assigns} and also calls it — "
                "rename the local variable"
            )
