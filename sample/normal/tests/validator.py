#!/usr/bin/env python3
"""Accept exactly two bounded integers and a final newline."""

import re
import sys

data = sys.stdin.buffer.read(128)
valid = re.fullmatch(rb"-?(0|[1-9][0-9]*) -?(0|[1-9][0-9]*)\n", data)
if not valid or any(abs(int(value)) > 10**9 for value in data.split()):
    sys.exit(1)
