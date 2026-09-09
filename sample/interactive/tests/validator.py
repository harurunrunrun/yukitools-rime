#!/usr/bin/env python3
import re
import sys

data = sys.stdin.buffer.read(64)
if not re.fullmatch(rb"[1-9][0-9]*\n", data) or not 1 <= int(data) <= 100:
    sys.exit(1)
