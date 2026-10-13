"""Stub compressor CLI for tests: reads the prompt on stdin, prints one short line.

STUB_MODE=fail exits 1 (CLI failure). Otherwise prints 'stub[<kind>] <first 60 chars of the item>'.
Each call appends one line to $STUB_CALLS (if set) so tests can count calls.
"""
import os
import re
import sys

prompt = sys.stdin.read()
if os.environ.get("STUB_CALLS"):
    with open(os.environ["STUB_CALLS"], "a") as f:
        f.write("call\n")
if os.environ.get("STUB_MODE") == "fail":
    sys.exit(1)
m = re.search(r"<(event|lines)>\n(.*?)\n</\1>", prompt, re.S)
kind, body = (m.group(1), m.group(2)) if m else ("?", "")
print(f"stub[{kind}] " + " ".join(body.split())[:60])
