"""Runtime configuration, all from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Models usable with the anonymous "public" key. Everything else needs OPENCODE_API_KEY.
FREE_MODEL_EXTRA = {"big-pickle"}
FREE_MODEL_SUFFIX = "-free"


def is_free_model(model_id: str) -> bool:
    return bool(model_id) and (model_id.endswith(FREE_MODEL_SUFFIX) or model_id in FREE_MODEL_EXTRA)


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [part.strip() for part in _str(name, "").split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # server
    port: int = field(default_factory=lambda: _int("PORT", 8211))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "info").upper())

    # upstream
    zen_base_url: str = field(default_factory=lambda: _str("ZEN_BASE_URL", "https://opencode.ai/zen/v1").rstrip("/"))
    opencode_api_key: str = field(default_factory=lambda: _str("OPENCODE_API_KEY", "public"))
    upstream_user_agent: str = field(default_factory=lambda: _str("UPSTREAM_UA", "opencode/latest/2.0.11/cli"))
    upstream_timeout: float = field(default_factory=lambda: _float("UPSTREAM_TIMEOUT", 600.0))
    connect_timeout: float = field(default_factory=lambda: _float("CONNECT_TIMEOUT", 20.0))

    # free tier gate fixups
    inject_stub_tools: bool = field(default_factory=lambda: _bool("INJECT_STUB_TOOLS", True))
    # text   = replace calls to injected stubs with a text note (default, cleanest for clients)
    # drop   = silently remove calls to injected stubs
    # passthrough = forward the stub tool call as-is (client may not know the tool)
    stub_call_mode: str = field(default_factory=lambda: _str("STUB_CALL_MODE", "text").lower())
    # mirror = satisfy the gate by exposing the client's own shell/read tool under the
    #          required name and renaming calls back (keeps the client's tool loop working)
    # stub   = inject a generic look-alike stub and neutralise calls to it
    stub_tool_mode: str = field(default_factory=lambda: _str("STUB_TOOL_MODE", "mirror").lower())

    # Responses API handling
    #   auto   - models in RESPONSES_NATIVE_MODELS go through /responses, everything else is
    #            bridged through /chat/completions (and auto-falls back when the upstream
    #            answers ModelProtocolUnsupported)
    #   native - always use /responses
    #   bridge - always translate through /chat/completions
    responses_mode: str = field(default_factory=lambda: _str("RESPONSES_MODE", "auto").lower())
    responses_native_models: list[str] = field(
        default_factory=lambda: _list("RESPONSES_NATIVE_MODELS")
        or ["muse-spark-1.2-contributor-free", "muse-spark-1.3-contributor-free"]
    )
    bridge_reasoning: bool = field(default_factory=lambda: _bool("BRIDGE_REASONING", False))

    # access control
    key_file: str = field(default_factory=lambda: _str("KEY_FILE", "/data/api-keys.txt"))
    extra_keys: list[str] = field(default_factory=lambda: _list("PROXY_API_KEYS"))
    ip_allowlist: list[str] = field(default_factory=lambda: _list("IP_ALLOWLIST"))
    trust_proxy: bool = field(default_factory=lambda: _bool("TRUST_PROXY", False))
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 0))

    # misc
    free_only: bool = field(default_factory=lambda: _bool("FREE_ONLY", True))
    log_requests: bool = field(default_factory=lambda: _bool("LOG_REQUESTS", True))
    access_log: str = field(default_factory=lambda: _str("ACCESS_LOG", "/data/access.jsonl"))
    max_aggregate_bytes: int = field(default_factory=lambda: _int("MAX_AGGREGATE_BYTES", 8 * 1024 * 1024))
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", 32 * 1024 * 1024))

    @property
    def using_real_key(self) -> bool:
        return self.opencode_api_key not in ("", "public")

    @property
    def enforce_free_only(self) -> bool:
        return self.free_only and not self.using_real_key


settings = Settings()
