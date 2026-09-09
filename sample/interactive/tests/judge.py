#!/usr/bin/env python3
"""Shared interactor, local process bridge, and Rime verdict-file checker."""

import argparse
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import TextIO


def interact(secret: int, receive: TextIO, send: TextIO) -> bool:
    def reply(text: str) -> None:
        send.write(text + "\n")
        send.flush()

    reply("100")
    queries = 0
    while True:
        line = receive.readline(65)
        parts = line.split()
        if len(line) > 64 or not line.endswith("\n") or len(parts) != 2:
            reply("-1")
            return False
        command, value_text = parts
        try:
            value = int(value_text)
        except ValueError:
            reply("-1")
            return False
        if not 1 <= value <= 100:
            reply("-1")
            return False
        if command == "!":
            accepted = value == secret
            reply("OK" if accepted else "-1")
            return accepted
        if command != "?" or queries >= 7:
            reply("-1")
            return False
        queries += 1
        reply("LESS" if secret < value else "GREATER" if secret > value else "EQUAL")


def local(command: list[str]) -> int:
    secret = int(sys.stdin.readline())
    # No shell; argv preserves paths containing spaces. This is not a sandbox.
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        # Also bounds a stalled reference solution, for which Rime passes no timeout.
        timer = threading.Timer(2.0, process.kill)
        timer.start()
        accepted = False
        try:
            accepted = interact(secret, process.stdout, process.stdin)
            process.stdin.close()
            trailing = process.stdout.read(65)
            accepted = accepted and not trailing and process.wait() == 0
        except (BrokenPipeError, OSError, UnicodeError):
            accepted = False
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            with suppress(BrokenPipeError, OSError):
                process.stdin.close()
            timer.cancel()
            timer.join()
    print("AC" if accepted else "WA")
    # Rime's separate judge maps this verdict to AC/WA.
    return 0


def main() -> int:
    if sys.argv[1:2] == ["--local"]:
        return local(sys.argv[2:])
    if sys.argv[1:2] == ["--infile"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--infile", required=True)
        parser.add_argument("--difffile", required=True)
        parser.add_argument("--outfile", required=True)
        args = parser.parse_args()
        with Path(args.outfile).open(encoding="utf-8") as output:
            return 0 if output.read(8) == "AC\n" else 1
    # yukicoder: argv[1] is the hidden testcase, stdin/stdout connect to the solution.
    secret = int(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        return 0 if interact(secret, sys.stdin, sys.stdout) else 1
    except (BrokenPipeError, UnicodeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
