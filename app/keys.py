"""API key store backed by a text file that can be edited while the gateway runs.

File format (see README):

    # comment lines start with '#'
    default:sk-oczen-3f9a...e2
    phone:sk-oczen-77c1...a4
    sk-oczen-just-the-key-is-fine-too

The file is re-read whenever its mtime changes, so editing it takes effect
immediately - no restart, no rebuild.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading

log = logging.getLogger("zen.keys")

KEY_PREFIX = "sk-oczen-"
HEADER = (
    "# OpenCode Zen gateway API keys - one per line, format:  name:key\n"
    "# Lines starting with '#' are ignored. Edit and save: takes effect immediately.\n"
    "# Keep this file private (chmod 600).\n"
    "\n"
)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_hex(16)




class KeyStore:
    def __init__(self, path: str, extra_keys: list[str] | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._keys: dict[str, str] = {}
        self._env_keys: dict[str, str] = {}
        for index, key in enumerate(extra_keys or [], start=1):
            self._env_keys[key] = "env[%d]" % index

    # ------------------------------------------------------------------ file

    def ensure_file(self) -> str | None:
        """Create the key file with one random key when it does not exist yet.

        Returns the new plaintext key, or None when the file already exists, so
        the caller can print it exactly once at startup.
        """
        if os.path.exists(self.path):
            return None
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        key = generate_key()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(HEADER)
            handle.write("default:%s\n" % key)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._mtime = None
        return key

    def load(self, force: bool = False) -> None:
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except OSError:
                mtime = None
            if not force and mtime == self._mtime and self._keys:
                return
            parsed: dict[str, str] = {}
            if mtime is not None:
                try:
                    with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                        for lineno, raw in enumerate(handle, start=1):
                            line = raw.strip()
                            if not line or line.startswith("#"):
                                continue
                            if ":" in line:
                                name, _, key = line.partition(":")
                                name, key = name.strip(), key.strip()
                            else:
                                name, key = "key%d" % lineno, line
                            if key:
                                parsed[key] = name or "key%d" % lineno
                except OSError as exc:
                    log.error("cannot read key file %s: %s", self.path, exc)
            self._keys = parsed
            self._mtime = mtime

    # -------------------------------------------------------------- verification

    def verify(self, token: str | None) -> str | None:
        """Return the key name for a valid token, else None."""
        if not token:
            return None
        self.load()
        for candidate, name in self._keys.items():
            if secrets.compare_digest(candidate, token):
                return name
        for candidate, name in self._env_keys.items():
            if secrets.compare_digest(candidate, token):
                return name
        return None

    def names(self) -> list[str]:
        self.load()
        return sorted(list(self._keys.values()) + list(self._env_keys.values()))

    def count(self) -> int:
        self.load()
        return len(self._keys) + len(self._env_keys)

