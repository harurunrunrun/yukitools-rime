#!/usr/bin/env python3
"""Run through Rime: its working directory is rime-out/tests."""

from pathlib import Path

CASES = {
    "small_01": (1, 2),
    "small_02": (0, 0),
    "large_01": (10**9, 10**9),
    "large_02": (-(10**9), 10**9),
}

if __name__ == "__main__":
    for name, (a, b) in CASES.items():
        Path(f"{name}.in").write_text(f"{a} {b}\n", encoding="utf-8")
