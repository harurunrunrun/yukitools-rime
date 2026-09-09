#!/usr/bin/env python3
"""Run through Rime; never create testcase files next to TESTSET."""

from pathlib import Path

if __name__ == "__main__":
    for index, value in enumerate([2, 17, 1000000000], start=1):
        Path(f"sample_{index:02}.in").write_text(f"{value}\n", encoding="utf-8", newline="\n")
