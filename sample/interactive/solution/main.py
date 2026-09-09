#!/usr/bin/env python3
"""Binary search with flushing and explicit termination handling."""

upper = int(input())
low, high = 1, upper
while low <= high:
    middle = (low + high) // 2
    print("?", middle, flush=True)
    reply = input()
    if reply == "-1":
        break
    if reply == "EQUAL":
        print("!", middle, flush=True)
        input()  # OK, then terminate without further output.
        break
    if reply == "LESS":
        high = middle - 1
    elif reply == "GREATER":
        low = middle + 1
    else:
        break
