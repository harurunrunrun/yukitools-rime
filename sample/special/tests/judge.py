#!/usr/bin/env python3
"""Rime flag mode / yukicoder argv[1] + contestant stdout on stdin."""

import argparse
import sys
from pathlib import Path


def accepts(n: int, answer: str) -> bool:
    try:
        values = list(map(int, answer.split()))
    except ValueError:
        return False
    return len(values) == 2 and all(1 <= x < n for x in values) and sum(values) == n


def main() -> int:
    if sys.argv[1:2] == ["--infile"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--infile", required=True)
        parser.add_argument("--difffile", required=True)
        parser.add_argument("--outfile", required=True)
        args = parser.parse_args()
        n = int(Path(args.infile).read_text(encoding="utf-8"))
        with Path(args.outfile).open(encoding="utf-8") as output:
            answer = output.read(128)
    else:
        # yukicoder also supplies answer/source/score paths; this checker needs only input.
        n = int(Path(sys.argv[1]).read_text(encoding="utf-8"))
        answer = sys.stdin.read(128)
    # Bounded read: huge or malformed output is a wrong answer.
    return 0 if len(answer) < 128 and accepts(n, answer) else 1


if __name__ == "__main__":
    sys.exit(main())
