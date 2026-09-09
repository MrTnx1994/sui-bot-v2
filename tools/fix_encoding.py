"""Reverse UTF-8→cp1252 double-encoding corruption (v2 — full cp1252 set).

Runs are built from every character that cp1252 maps to a byte >= 0x80
(includes Œ, Ž, ‰, ™, …). A run is restored only if it round-trips cleanly
(cp1252-encode → utf-8-decode); otherwise it is left untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REVERSE_CP1252: dict[str, int] = {}
for _b in range(0x80, 0x100):
    try:
        _ch = bytes([_b]).decode("cp1252")
    except UnicodeDecodeError:
        continue
    _REVERSE_CP1252[_ch] = _b


def is_corrupt_char(ch: str) -> bool:
    return ch in _REVERSE_CP1252


def fix_text(text: str) -> tuple[str, int]:
    out: list[str] = []
    buf: list[str] = []
    fixed = 0

    def flush() -> None:
        nonlocal fixed
        if not buf:
            return
        run = "".join(buf)
        try:
            out.append(run.encode("cp1252").decode("utf-8"))
            fixed += 1
        except (UnicodeEncodeError, UnicodeDecodeError):
            out.append(run)
        buf.clear()

    for ch in text:
        if is_corrupt_char(ch):
            buf.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out), fixed


def main() -> None:
    for name in sys.argv[1:]:
        path = Path(name)
        if not path.is_file():
            print(f"skip {path}")
            continue
        text = path.read_text(encoding="utf-8")
        for _ in range(3):
            text, n = fix_text(text)
        remaining = sum(1 for line in text.split("\n") if any(is_corrupt_char(ch) for ch in line))
        path.write_text(text, encoding="utf-8")
        print(f"{path.name}: restored, lines still containing cp1252 chars: {remaining}")


if __name__ == "__main__":
    main()
