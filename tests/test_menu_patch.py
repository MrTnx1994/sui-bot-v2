"""تست پچر مینی‌اپ منو (deploy/patch_menu_webapp.py) — روی فایل شبیه‌سازی‌شدهٔ قدیمی."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_patch_module():
    spec = importlib.util.spec_from_file_location(
        "patch_menu_webapp", REPO / "deploy" / "patch_menu_webapp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_old_style_subpage(tmp_path: Path) -> Path:
    """نسخهٔ «قدیمی» subpage.py — بدون بلوک منو و روت‌هایش."""
    from sui_bot import subpage as current

    source = Path(current.__file__).read_text(encoding="utf-8")
    # حذف بلوک منو
    start = source.find("# ---------------------------------------------------------------------------\n# مینی‌اپ منوی اصلی")
    end = source.find("def make_app()")
    assert start != -1 and end != -1
    source = source[:start] + source[end:]
    # حذف روت‌های منو
    source = source.replace('\n    app.router.add_get("/sub/menu", handle_menu)', "")
    source = source.replace('\n    app.router.add_get("/menu", handle_menu)', "")
    assert "MENU_PAGE" not in source and "/sub/menu" not in source
    target = tmp_path / "subpage.py"
    target.write_text(source, encoding="utf-8")
    return target


def test_patch_applies_and_is_idempotent(tmp_path, monkeypatch):
    patcher = load_patch_module()
    target = build_old_style_subpage(tmp_path)

    monkeypatch.setattr(patcher, "find_subpage_path", lambda: target)
    assert patcher.main() == 0

    patched = target.read_text(encoding="utf-8")
    assert "MENU_PAGE" in patched
    assert 'app.router.add_get("/sub/menu", handle_menu)' in patched
    assert 'app.router.add_get("/menu", handle_menu)' in patched
    # py_compile داخل main() گرفته شده — اگر سینتکس خراب بود restore می‌شد

    # اجرای دوباره → کاری نکند (idempotent)
    assert patcher.main() == 0
    again = target.read_text(encoding="utf-8")
    # یک بار تعریف + یک بار استفاده در handle_menu
    assert again.count("MENU_PAGE = ") == 1
    assert again.count('app.router.add_get("/menu", handle_menu)') == 1


def test_repo_subpage_has_menu(tmp_path, monkeypatch):
    """فایل داخل ریپو خودش مسیرهای منو را دارد."""
    patcher = load_patch_module()
    # دوباره‌سازی: پچر روی فایل ریپو باید بگوید «قبلاً پچ شده»
    monkeypatch.setattr(patcher, "find_subpage_path", lambda: Path(patcher.__file__).parents[1] / "src" / "sui_bot" / "subpage.py")
    assert patcher.main() == 0
