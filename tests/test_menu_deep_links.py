"""deep-link های /start=shop|usage|wallet|trial|support — بدون مینی‌اپ (کارت‌های وب حذف شدند)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


class TestBotActionsSurface:
    def test_actions_present_in_bot(self):
        text = (SRC / "sui_bot" / "bot.py").read_text(encoding="utf-8")
        for act in ("menu", "shop", "usage", "wallet", "trial", "support"):
            assert f'"{act}"' in text, act
        assert "_shop_command" in text
        assert "_wallet_command" in text
        assert "_trial_command" in text
        assert "await usage(" in text


class TestBotRoutingSurface:
    def _tree(self):
        return ast.parse((SRC / "sui_bot" / "bot.py").read_text(encoding="utf-8"))

    def test_helpers_defined(self):
        tree = self._tree()
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "_open_menu_action" in names

    def test_start_routes_deep_links(self):
        tree = self._tree()
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "start"
        )
        used = {
            n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and n.id in ("MENU_DEEP_ACTIONS", "_open_menu_action")
        }
        assert used == {"MENU_DEEP_ACTIONS", "_open_menu_action"}

    def test_no_webapp_menu_left_in_bot(self):
        """دکمهٔ مربعی = لیست دستورات نیتیو؛ هیچ وب‌اپ/مینی‌اپی نباید ثبت شود."""
        text = (SRC / "sui_bot" / "bot.py").read_text(encoding="utf-8")
        assert "MenuButtonWebApp" not in text
        assert "WebAppInfo" not in text
        assert "WEB_APP_DATA" not in text
        assert "MenuButtonCommands()" in text

    def test_no_crlf_in_shell_scripts(self):
        root = SRC.parent
        for name in ("install.sh", "update.sh", "uninstall.sh", "sui-bot"):
            data = (root / name).read_bytes()
            assert b"\r\n" not in data, f"{name} has CRLF line endings"
