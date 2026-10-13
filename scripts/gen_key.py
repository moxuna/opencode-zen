#!/usr/bin/env python3
"""Generate a gateway API key, optionally appending it to the key file.

Examples
--------
    python3 scripts/gen_key.py                       # just print a new key
    python3 scripts/gen_key.py --name phone          # print a key named "phone"
    python3 scripts/gen_key.py --name phone --file data/api-keys.txt
                                                     # append "phone:<key>" to the file
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

KEY_PREFIX = "sk-oczen-"
HEADER = (
    "# OpenCode Zen gateway API keys - one per line, format:  name:key\n"
    "# Lines starting with '#' are ignored. Edit and save: takes effect immediately.\n"
    "\n"
)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_hex(16)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an OpenCode Zen gateway API key")
    parser.add_argument("-n", "--name", default=None, help="key name (used as the 'name:key' prefix)")
    parser.add_argument("-f", "--file", default=None, help="append the key to this key file")
    args = parser.parse_args()

    key = generate_key()
    if not args.file:
        print(key)
        return 0

    name = args.name or "default"
    path = args.file
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if needs_header:
            handle.write(HEADER)
        handle.write("%s:%s\n" % (name, key))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print("%s:%s" % (name, key), file=sys.stderr)
    print("appended to %s" % path, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
