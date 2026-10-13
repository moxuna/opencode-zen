#!/usr/bin/env python3
"""End-to-end smoke test for the OpenCode Zen gateway.

Two modes:

    # 1) local: boots gateway instances itself and drives them over HTTP
    python3 scripts/smoke_live.py [--port 8211] [--keep-temp]

    # 2) remote: tests an already deployed gateway (e.g. on your VPS)
    python3 scripts/smoke_live.py --base-url http://1.2.3.4:8211 --key sk-oczen-...

Both modes talk to the REAL opencode.ai upstream, so run scripts/probe_zen.py
first if you are unsure about egress. Checks that need filesystem access
(first-start key generation, access log, restart behaviour, per-instance
settings) run in local mode only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "", soft: bool = False) -> bool:
    label = "WARN" if (soft and not ok) else ("PASS" if ok else "FAIL")
    RESULTS.append((name, ok or soft, "%s %s" % (label, detail)))
    print("  [%s] %-46s %s" % (label, name, detail))
    return ok


# --------------------------------------------------------------------------- http


class HttpTarget:
    """Minimal HTTP/1.1 client with SSE support (no proxy handling on purpose)."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.label = "%s:%d" % (host, port)

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 300, raw_body: bytes | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = dict(headers or {})
        if payload is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(payload))
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            response = conn.getresponse()
            data = response.read()
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def stream(self, path: str, body: dict, headers: dict, max_bytes: int = 4000, stop_after_data: bool = False):
        """Read part of an SSE response; returns (status, collected bytes, headers)."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
        payload = json.dumps(body).encode()
        hdrs = dict(headers)
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(payload))
        try:
            conn.request("POST", path, body=payload, headers=hdrs)
            response = conn.getresponse()
            collected = b""
            while len(collected) < max_bytes:
                chunk = response.read(256)
                if not chunk:
                    break
                collected += chunk
                if stop_after_data and b"data:" in collected:
                    break
            return response.status, collected, dict(response.getheaders())
        finally:
            conn.close()

    def json_or_none(self, data: bytes):
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None


class Server(HttpTarget):
    """A locally spawned gateway plus helpers to inspect its files."""

    def __init__(self, port: int, env: dict[str, str], tag: str, tmp: str) -> None:
        super().__init__("127.0.0.1", port)
        self.tag = tag
        self.tmp = tmp
        self.log_path = os.path.join(tmp, "server-%s.log" % tag)
        self.log = open(self.log_path, "wb")
        full_env = os.environ.copy()
        full_env.update(env)
        full_env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
            cwd=ROOT, env=full_env, stdout=self.log, stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 45.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                status, _, _ = self.request("GET", "/healthz", timeout=2)
                if status == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def log_text(self) -> str:
        self.log.flush()
        with open(self.log_path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


def read_key(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                return line.split(":", 1)[1].strip() if ":" in line else line
    raise RuntimeError("no key in %s" % path)


def bearer(key: str) -> dict:
    return {"Authorization": "Bearer %s" % key}


TOOLS = [
    {"type": "function", "function": {"name": "read", "description": "Read a file",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command",
                                      "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]


# ------------------------------------------------------------------------- checks


def check_endpoints(api: HttpTarget, key: str) -> None:
    print("  -- health, readiness, model list --")
    status, data, _ = api.request("GET", "/healthz")
    record("/healthz ok", status == 200 and b"ok" in data, "HTTP %s" % status)

    status, data, _ = api.request("GET", "/readyz")
    ready = api.json_or_none(data) or {}
    record("/readyz ready (upstream reachable)", status == 200 and ready.get("status") == "ready", json.dumps(ready)[:170])
    counter = ready.get("session_counter")
    record("session id counter < 64 (upstream gate)", isinstance(counter, int) and counter < 64, str(counter))

    status, data, _ = api.request("GET", "/v1/models", headers=bearer(key))
    payload = api.json_or_none(data) or {}
    models = [m.get("id") for m in payload.get("data") or []]
    record("/v1/models authorised", status == 200 and bool(models), "HTTP %s, %d models" % (status, len(models)))
    record("only free models listed", bool(models) and all(m.endswith("-free") or m == "big-pickle" for m in models), str(models[:3]))
    if models:
        status, _, _ = api.request("GET", "/v1/models/%s" % models[0], headers=bearer(key))
        record("GET /v1/models/{id} works", status == 200, "HTTP %s" % status)
    status, _, _ = api.request("GET", "/v1/models/claude-opus-5", headers=bearer(key))
    record("paid model hidden from /v1/models/{id}", status == 404, "HTTP %s" % status)


def check_auth(api: HttpTarget, key: str) -> None:
    print("  -- authentication --")
    status, _, _ = api.request("GET", "/v1/models")
    record("no key -> 401", status == 401, "HTTP %s" % status)
    status, _, _ = api.request("GET", "/v1/models", headers=bearer("sk-oczen-" + "0" * 32))
    record("wrong key -> 401", status == 401, "HTTP %s" % status)
    status, _, _ = api.request("GET", "/v1/models", headers={"x-api-key": key})
    record("x-api-key header accepted", status == 200, "HTTP %s" % status)
    status, _, _ = api.request("GET", "/v1/models?key=%s" % key)
    record("?key= query accepted", status == 200, "HTTP %s" % status)


def check_chat(api: HttpTarget, key: str) -> None:
    print("  -- chat/completions, non-streaming, no client tools (stub injection path) --")
    status, data, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key),
                                  body={"model": "big-pickle", "messages": [{"role": "user", "content": "Reply with exactly: smoke-ok"}]})
    doc = api.json_or_none(data) or {}
    content = ((doc.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    record("non-stream chat works without client tools", status == 200 and "smoke-ok" in content.lower(),
           "HTTP %s content=%r" % (status, content[:50]))
    record("aggregated response carries usage", bool(doc.get("usage")), json.dumps(doc.get("usage"))[:70])

    print("  -- UTF-8 / Chinese round trip --")
    status, data, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key),
                                  body={"model": "big-pickle", "messages": [{"role": "user", "content": "只回复四个字：网关正常"}]})
    try:
        text = data.decode("utf-8")
        reply = ((json.loads(text).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        clean = "\ufffd" not in text and "\ufffd" not in reply
    except Exception as exc:
        reply, clean = "", str(exc)
    record("utf-8 request/response clean", status == 200 and clean and bool(reply), "reply=%r" % reply[:40])

    print("  -- streaming --")
    status, raw, headers = api.stream("/v1/chat/completions",
                                      {"model": "big-pickle", "messages": [{"role": "user", "content": "Count 1 to 5."}], "stream": True},
                                      bearer(key))
    text = raw.decode("utf-8", "replace")
    record("stream returns SSE", status == 200 and headers.get("content-type", "").startswith("text/event-stream"),
           "HTTP %s ct=%s" % (status, headers.get("content-type")))
    record("stream carries data chunks", "data:" in text, text[:60].replace("\n", " "))
    record("stream is utf-8 clean", "\ufffd" not in text)


def check_tools(api: HttpTarget, key: str) -> None:
    print("  -- tool handling --")
    status, data, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key),
                                  body={"model": "big-pickle", "tools": TOOLS,
                                        "messages": [{"role": "user", "content": "Run the bash tool with the command: echo hello"}]})
    doc = api.json_or_none(data) or {}
    message = (doc.get("choices") or [{}])[0].get("message") or {}
    names = [c.get("function", {}).get("name") for c in message.get("tool_calls") or []]
    record("client-declared tool call survives", status == 200 and (not names or "bash" in names),
           "HTTP %s tool_calls=%s" % (status, names), soft=True)

    status, data, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key),
                                  body={"model": "big-pickle",
                                        "messages": [{"role": "user", "content": "Use the read tool to read /etc/hostname and tell me its content."}]})
    doc = api.json_or_none(data) or {}
    choice = (doc.get("choices") or [{}])[0]
    leaked = choice.get("message", {}).get("tool_calls")
    note = "[zen-gateway]" in (choice.get("message", {}).get("content") or "")
    record("injected stub call never leaks to the client", status == 200 and not leaked, "tool_calls=%s" % (leaked,))
    record("stub call surfaced as a text note", note or not leaked,
           "note=%s finish=%s" % (note, choice.get("finish_reason")), soft=True)


def check_responses(api: HttpTarget, key: str) -> None:
    print("  -- /v1/responses --")
    status, data, _ = api.request("POST", "/v1/responses", headers=bearer(key),
                                  body={"model": "muse-spark-1.3-contributor-free", "input": "Reply with exactly: resp-ok"})
    doc = api.json_or_none(data) or {}
    texts = ["".join(p.get("text", "") for p in item.get("content") or [])
             for item in doc.get("output") or [] if item.get("type") == "message"]
    record("responses non-stream aggregates", status == 200 and doc.get("object") == "response" and doc.get("status") == "completed",
           "HTTP %s text=%r" % (status, (texts[0] if texts else "")[:30]))
    status, raw, _ = api.stream("/v1/responses",
                                {"model": "muse-spark-1.3-contributor-free", "input": "Say OK.", "stream": True}, bearer(key))
    text = raw.decode("utf-8", "replace")
    record("responses stream emits events", status == 200 and "event: response." in text, text[:50].replace("\n", " "))


def check_errors(api: HttpTarget, key: str, big_body: bool = True) -> None:
    print("  -- error handling --")
    status, _, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key), raw_body=b"{not json")
    record("invalid JSON -> 400", status == 400, "HTTP %s" % status)
    status, _, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key), body={"messages": []})
    record("missing model -> 400", status == 400, "HTTP %s" % status)
    status, data, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key), body={"model": "claude-opus-5", "messages": []})
    record("paid model -> 400 model_not_free", status == 400 and b"model_not_free" in data, "HTTP %s %s" % (status, data[:60].decode("utf-8", "replace")))
    status, _, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key), body={"model": "big-pickle", "messages": []})
    record("empty messages -> upstream error surfaced", status >= 400, "HTTP %s" % status, soft=True)
    if big_body:
        big = {"model": "big-pickle", "messages": [{"role": "user", "content": "x" * (33 * 1024 * 1024)}]}
        started = time.time()
        status, _, _ = api.request("POST", "/v1/chat/completions", headers=bearer(key), body=big)
        record("oversized body -> 413", status == 413, "HTTP %s in %.1fs" % (status, time.time() - started))
    status, _, _ = api.request("GET", "/v1/does-not-exist", headers=bearer(key))
    record("unknown path -> 404", status == 404, "HTTP %s" % status)


def check_load(api: HttpTarget, key: str) -> None:
    print("  -- concurrency and client disconnect --")
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            status, _, _ = api.stream("/v1/chat/completions",
                                      {"model": "big-pickle", "messages": [{"role": "user", "content": "Say OK."}], "stream": True},
                                      bearer(key), max_bytes=200, stop_after_data=True)
        except Exception:
            status = -1
        with lock:
            codes.append(status)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    record("4 concurrent streams all 200", sorted(codes) == [200, 200, 200, 200], str(sorted(codes)))

    try:
        api.stream("/v1/chat/completions",
                   {"model": "big-pickle", "messages": [{"role": "user", "content": "Write a long essay about tea."}], "stream": True},
                   bearer(key), max_bytes=200, stop_after_data=True)
    except Exception:
        pass
    status, _, _ = api.request("GET", "/v1/models", headers=bearer(key))
    record("still healthy after a client disconnect", status == 200, "HTTP %s" % status)


def check_local_extras(server: Server, key: str, key_file: str, access_log: str) -> None:
    print("  -- key hot reload --")
    new_key = "sk-oczen-" + "f" * 32
    with open(key_file, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("smoke:%s\n" % new_key)
    status, _, _ = server.request("GET", "/v1/models", headers=bearer(new_key))
    record("hand-edited key works without restart", status == 200, "HTTP %s" % status)
    with open(key_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("default:%s\n" % key)
    status, _, _ = server.request("GET", "/v1/models", headers=bearer(new_key))
    record("removed key rejected", status == 401, "HTTP %s" % status)

    print("  -- access log --")
    with open(access_log, "rb") as handle:
        raw_log = handle.read()
    try:
        log_text = raw_log.decode("utf-8")
        lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
        clean = "\ufffd" not in log_text
    except Exception as exc:
        lines, clean, log_text = [], False, str(exc)
    record("access log is valid jsonl", len(lines) > 8, "%d lines" % len(lines))
    record("access log is utf-8 clean", clean)
    fields_ok = all(k in (lines[-1] if lines else {}) for k in ("ts", "path", "key", "model", "stream", "status", "duration_ms"))
    record("access log has required fields", fields_ok, json.dumps(lines[-1])[:120] if lines else "")


def run_shared_checks(api: HttpTarget, key: str, big_body: bool = True) -> None:
    check_endpoints(api, key)
    check_auth(api, key)
    check_chat(api, key)
    check_tools(api, key)
    check_responses(api, key)
    check_errors(api, key, big_body=big_body)
    check_load(api, key)


# --------------------------------------------------------------------- scenarios


def run_local(args) -> None:
    tmp = tempfile.mkdtemp(prefix="zen-smoke-main-")
    key_file = os.path.join(tmp, "api-keys.txt")
    access_log = os.path.join(tmp, "access.jsonl")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": access_log, "PORT": str(args.port), "LOG_LEVEL": "INFO"}
    server = Server(args.port, env, "main", tmp)
    try:
        print("\n== instance 1: normal settings ==")
        if not record("server boots and /healthz answers", server.wait_ready(), server.label):
            print(server.log_text()[-2000:])
            return
        record("first start generated a key file", os.path.exists(key_file), key_file)
        key = read_key(key_file)
        record("key format sk-oczen-<32hex>", key.startswith("sk-oczen-") and len(key) == len("sk-oczen-") + 32, "len=%d" % len(key))
        record("plaintext key printed once to the log", key in server.log_text())

        run_shared_checks(server, key, big_body=not args.skip_big_body)
        check_local_extras(server, key, key_file, access_log)

        print("  -- restart behaviour --")
        server.stop()
        server2 = Server(args.port, env, "restart", tmp)
        try:
            record("restart comes back up", server2.wait_ready(), server2.label)
            record("restart did NOT mint a new key", "Generated a new gateway API key" not in server2.log_text())
            status, _, _ = server2.request("GET", "/v1/models", headers=bearer(key))
            record("existing key still valid after restart", status == 200, "HTTP %s" % status)
        finally:
            server2.stop()
    finally:
        server.stop()
        if args.keep_temp:
            print("  temp dir kept: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def run_remote(args) -> None:
    parsed = urlparse(args.base_url if "://" in args.base_url else "http://" + args.base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    print("\n== remote gateway %s (%s) ==" % (args.base_url, "https" if parsed.scheme == "https" else "http"))
    api = HttpTarget(host, port)
    try:
        status, _, _ = api.request("GET", "/healthz", timeout=10)
    except Exception as exc:
        record("remote gateway reachable", False, "%s" % exc)
        return
    record("remote gateway reachable", status == 200, "HTTP %s" % status)
    run_shared_checks(api, args.key, big_body=not args.skip_big_body)


def check_local_settings(port: int) -> None:
    tmp = tempfile.mkdtemp(prefix="zen-smoke-rate-")
    key_file = os.path.join(tmp, "api-keys.txt")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": os.path.join(tmp, "access.jsonl"), "PORT": str(port),
           "LOG_LEVEL": "INFO", "RATE_LIMIT_PER_MIN": "2"}
    server = Server(port, env, "rate", tmp)
    try:
        print("\n== instance 2: RATE_LIMIT_PER_MIN=2 ==")
        if not record("rate-limited instance boots", server.wait_ready(), server.label):
            return
        key = read_key(key_file)
        codes = [server.request("GET", "/v1/models", headers=bearer(key))[0] for _ in range(3)]
        record("third request in the minute -> 429", codes == [200, 200, 429], str(codes))
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    tmp = tempfile.mkdtemp(prefix="zen-smoke-ip-")
    key_file = os.path.join(tmp, "api-keys.txt")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": os.path.join(tmp, "access.jsonl"), "PORT": str(port + 1),
           "LOG_LEVEL": "INFO", "IP_ALLOWLIST": "10.99.99.99/32"}
    server = Server(port + 1, env, "ip", tmp)
    try:
        print("\n== instance 3: IP_ALLOWLIST=10.99.99.99/32 ==")
        if not record("allowlist instance boots", server.wait_ready(), server.label):
            return
        key = read_key(key_file)
        status, data, _ = server.request("GET", "/v1/models", headers=bearer(key))
        record("loopback blocked by allowlist", status == 403 and b"not allowed" in data,
               "HTTP %s %s" % (status, data[:60].decode("utf-8", "replace")))
        record("/healthz stays open for docker healthcheck", server.request("GET", "/healthz")[0] == 200)
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="live smoke test for the OpenCode Zen gateway")
    parser.add_argument("--port", type=int, default=8211, help="port to spawn local instances on")
    parser.add_argument("--base-url", default=None, help="test an already deployed gateway, e.g. http://1.2.3.4:8211")
    parser.add_argument("--key", default=None, help="API key for --base-url mode")
    parser.add_argument("--skip-big-body", action="store_true", help="skip the 33 MiB 413 check (slow uplink)")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    print("Live smoke test against the real upstream (%s)" % os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1"))
    started = time.time()

    if args.base_url:
        if not args.key:
            parser.error("--base-url requires --key")
        run_remote(args)
    else:
        run_local(args)
        check_local_settings(args.port)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d checks in %.1fs" % (len(RESULTS), time.time() - started))
    if failed:
        print("FAILED (%d): %s" % (len(failed), ", ".join(failed)))
        return 1
    print("all live smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
