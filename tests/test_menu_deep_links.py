"""کارت‌های منوی وب: deep-link به /start + fallback به sendData."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _subpage_module(monkeypatch):
    monkeypatch.setenv("SUI_HOST", "https://panel.invalid:2095/app")
    monkeypatch.setenv("SUI_TOKEN", "test-token")
    import importlib
    import sys

    src = str(SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module("sui_bot.subpage")


class TestMenuPage:
    def test_deep_link_cards(self, monkeypatch):
        sp = _subpage_module(monkeypatch)
        page = sp._menu_page("https://t.me/TestBot")
        assert 'href="https://t.me/TestBot?start=shop"' in page
        assert "tg.sendData" not in page
        for token in ("__BOT_LINK__", "__EXTRA_JS__", "__TITLE__"):
            assert token not in page

    def test_fallback_send_data_cards(self, monkeypatch):
        sp = _subpage_module(monkeypatch)
        page = sp._menu_page("")
        assert 'href="#?start=shop"' in page
        assert "tg.sendData" in page
        assert "__BOT_LINK__" not in page and "__EXTRA_JS__" not in page

    def test_all_actions_covered(self, monkeypatch):
        sp = _subpage_module(monkeypatch)
        page = sp._menu_page("https://t.me/TestBot")
        for act in ("shop", "usage", "wallet", "trial", "support"):
            assert f"?start={act}" in page, act
            assert f'data-act="{act}"' in page, act


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

    def test_webapp_handler_shares_action_runner(self):
        tree = self._tree()
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "webapp_menu_data_handler"
        )
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "_open_menu_action" in names

    def test_env_samples_document_bot_username(self):
        for name in ("sui-bot.env.sample", ".env.example"):
            text = (SRC.parent / name).read_text(encoding="utf-8")
            assert "BOT_USERNAME" in text, name

    def test_no_crlf_in_shell_scripts(self):
        root = SRC.parent
        for name in ("install.sh", "update.sh", "uninstall.sh", "sui-bot"):
            data = (root / name).read_bytes()
            assert b"\r\n" not in data, f"{name} has CRLF line endings"
