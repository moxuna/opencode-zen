"""HTTP client for the upstream OpenCode Zen endpoint."""

from __future__ import annotations

import httpx

from .config import Settings, settings
from .zen_session import mint_session


class Upstream:
    """Thin wrapper that always sends the headers the free tier gate expects."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self.client = httpx.AsyncClient(
            base_url=self.cfg.zen_base_url,
            timeout=httpx.Timeout(self.cfg.upstream_timeout, connect=self.cfg.connect_timeout),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            trust_env=True,  # honour HTTPS_PROXY / NO_PROXY if the host needs them
        )

    def headers(self, key_name: str, stream: bool = True) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "user-agent": self.cfg.upstream_user_agent,
            "authorization": "Bearer %s" % self.cfg.opencode_api_key,
            "x-opencode-client": "cli",
            "x-opencode-project": "global",
            "x-opencode-session": mint_session(key_name),
        }

    async def get_json(self, path: str, key_name: str = "gateway") -> tuple[int, dict]:
        response = await self.client.get(path, headers=self.headers(key_name, stream=False))
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:2000]}
        return response.status_code, payload

    async def aclose(self) -> None:
        await self.client.aclose()
