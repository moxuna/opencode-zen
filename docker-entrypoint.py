#!/usr/bin/env python3
"""Container entrypoint: make the mounted volume writable, then drop privileges.

Docker creates a bind-mounted directory as root when it does not exist yet, so a
fresh `docker compose up -d` would leave /data owned by root and the gateway
(uid 10001) could not write api-keys.txt on first start.

Starting as root, fixing the ownership of the data directories and re-executing
the command as the unprivileged user keeps `docker compose up -d` working with no
host-side chown, while the gateway process itself never runs as root.

Set DATA_DIR / GATEWAY_USER to override the defaults.
"""

from __future__ import annotations

import os
import sys

try:  # pwd is POSIX-only; importing it must still work on a Windows dev box
    import pwd
except ImportError:  # pragma: no cover
    pwd = None  # type: ignore[assignment]

DATA_DIR = os.environ.get("DATA_DIR", "/data")
GATEWAY_USER = os.environ.get("GATEWAY_USER", "gateway")


def target_dirs() -> list[str]:
    """Every directory the gateway may need to write to."""
    candidates = [
        DATA_DIR,
        os.path.dirname(os.environ.get("KEY_FILE", os.path.join(DATA_DIR, "api-keys.txt"))),
        os.path.dirname(os.environ.get("ACCESS_LOG", os.path.join(DATA_DIR, "access.jsonl"))),
    ]
    seen: list[str] = []
    for path in candidates:
        if path and path not in seen:
            seen.append(path)
    return seen


def fix_ownership(uid: int, gid: int) -> None:
    for directory in target_dirs():
        if not os.path.isdir(directory):
            continue
        try:
            os.chown(directory, uid, gid)
        except OSError:
            pass
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            try:
                os.chown(os.path.join(directory, name), uid, gid)
            except OSError:
                pass


def main() -> None:
    if sys.argv[1:2] == []:
        print("[entrypoint] no command given", file=sys.stderr)
        raise SystemExit(2)

    # Not root (or not POSIX, e.g. running the script on Windows for a smoke test):
    # nothing to fix, just run the command.
    if pwd is None or not hasattr(os, "setuid") or os.geteuid() != 0:
        os.execvp(sys.argv[1], sys.argv[1:])

    try:
        record = pwd.getpwnam(GATEWAY_USER)
    except KeyError:
        print("[entrypoint] user %r not found, staying root" % GATEWAY_USER, file=sys.stderr)
        os.execvp(sys.argv[1], sys.argv[1:])

    fix_ownership(record.pw_uid, record.pw_gid)
    os.setgid(record.pw_gid)
    os.setuid(record.pw_uid)
    os.environ["HOME"] = record.pw_dir
    os.environ.setdefault("USER", GATEWAY_USER)
    print("[entrypoint] running as %s (uid=%d)" % (GATEWAY_USER, record.pw_uid), flush=True)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
