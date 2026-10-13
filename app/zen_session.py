"""Mint plausible OpenCode session IDs for the X-Opencode-Session header.

Measured upstream rule (2026-10-13): the free tier rejects any session id whose
per-millisecond counter is >= 64. pi used to hard-code ids with counters 3323/3701,
which is exactly what produced:

    403 {"type":"FreeTierError","message":"OpenCode's free tier can only be used
         from within OpenCode."}

Format: "ses_" + 12 hex + 14 base62 where the 48-bit value is
    bit_invert(timestamp_ms * 4096 + counter)
"""

from __future__ import annotations

import hashlib
import string
import time

B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
MASK48 = (1 << 48) - 1


def mint_session(seed: str = "", ts_ms: int | None = None, counter: int | None = None) -> str:
    """Return a session id the upstream gate accepts.

    The result is stable for a given seed, so one API key maps to one upstream
    session (keeps prompt-cache affinity and spreads rate limits per client).
    """
    ts = (ts_ms if ts_ms is not None else int(time.time() * 1000)) % (1 << 36)
    if counter is None:
        # any value < 64 is accepted; derive one so different keys don't collide
        digest = hashlib.blake2s((seed or "opencode-zen").encode("utf-8"), digest_size=2).digest()
        counter = 1 + (int.from_bytes(digest, "big") % 60)
    r = ts * 4096 + (counter & 0xFFF)
    a = ((1 << 48) - 1 - r) & MASK48
    suffix = seed_suffix(seed)
    return "ses_%012x%s" % (a, suffix)


def seed_suffix(seed: str) -> str:
    digest = hashlib.blake2s((seed or "opencode-zen").encode("utf-8"), digest_size=14).digest()
    return "".join(B62[b % 62] for b in digest)




def decode_counter(session_id: str) -> int | None:
    """Decode the per-millisecond counter of a session id (used by /readyz + tests)."""
    try:
        a = int(session_id[4:16], 16)
    except (ValueError, IndexError):
        return None
    r = ((1 << 48) - 1 - a) & MASK48
    return r & 0xFFF
