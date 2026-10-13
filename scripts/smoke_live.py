#!/usr/bin/env python3
"""End-to-end smoke test against the REAL opencode.ai upstream.

Not part of the offline test suite: this needs working egress to opencode.ai
(run scripts/probe_zen.py first). It boots three gateway instances with
different settings and drives them over HTTP.

    python3 scripts/smoke_live.py            # default port 8211
    python3 scripts/smoke_live.py --port 8399 --keep-temp

Checks include: first-start key generation, auth, /v1/models, streaming and
non-streaming chat, stub-tool injection and neutralisation, /v1/responses,
error handling, key hot reload, SIGTERM restart without key rotation,
concurrent streams, client disconnect, rate limiting and the IP allowlist.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "", soft: bool = False) -> bool:
    label = "WARN" if (soft and not ok) else ("PASS" if ok else "FAIL")
    RESULTS.append((name, ok or soft, "%s %s" % (label, detail)))
    print("  [%s] %-46s %s" % (label, name, detail))
    return ok


class Server:
    def __init__(self, port: int, env: dict[str, str], tag: str, tmp: str) -> None:
        self.port = port
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

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 300, raw_body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = dict(headers or {})
        if payload is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        conn.close()
        return status, data, response_headers

    def stream(self, path: str, body: dict, headers: dict, max_bytes: int = 4000, stop_after_data: bool = False):
        """Read part of an SSE response; returns (status, collected bytes, headers)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        payload = json.dumps(body).encode()
        hdrs = dict(headers)
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(payload))
        conn.request("POST", path, body=payload, headers=hdrs)
        response = conn.getresponse()
        collected = b""
        try:
            while len(collected) < max_bytes:
                chunk = response.read(256)
                if not chunk:
                    break
                collected += chunk
                if stop_after_data and b"data:" in collected:
                    break
        finally:
            conn.close()
        return response.status, collected, dict(response.getheaders())

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


def check_main_instance(args) -> None:
    tmp = tempfile.mkdtemp(prefix="zen-smoke-main-")
    key_file = os.path.join(tmp, "api-keys.txt")
    access_log = os.path.join(tmp, "access.jsonl")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": access_log, "PORT": str(args.port), "LOG_LEVEL": "INFO"}
    server = Server(args.port, env, "main", tmp)
    try:
        print("\n== instance 1: normal settings ==")
        if not record("server boots and /healthz answers", server.wait_ready(), "port %d" % args.port):
            print(server.log_text()[-2000:])
            return

        record("first start generated a key file", os.path.exists(key_file), key_file)
        key = read_key(key_file)
        record("key format sk-oczen-<32hex>", key.startswith("sk-oczen-") and len(key) == len("sk-oczen-") + 32, "len=%d" % len(key))
        record("plaintext key printed once to the log", key in server.log_text())

        status, data, _ = server.request("GET", "/readyz")
        ready = json.loads(data) if status == 200 else {}
        record("/readyz ready", status == 200 and ready.get("status") == "ready", json.dumps(ready)[:160])
        record("session counter < 64", isinstance(ready.get("session_counter"), int) and ready["session_counter"] < 64, str(ready.get("session_counter")))

        status, data, _ = server.request("GET", "/v1/models")
        record("/v1/models without key -> 401", status == 401, "HTTP %s" % status)
        status, data, _ = server.request("GET", "/v1/models", headers=bearer(key))
        models = [m["id"] for m in json.loads(data)["data"]] if status == 200 else []
        record("/v1/models lists only free models", status == 200 and 0 < len(models) <= 20 and all(m.endswith("-free") or m == "big-pickle" for m in models),
               "%d models" % len(models))

        print("  -- chat/completions (non-stream, no tools: stub injection path) --")
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key),
                                         body={"model": "big-pickle", "messages": [{"role": "user", "content": "Reply with exactly: smoke-ok"}]})
        doc = json.loads(data) if status == 200 else {}
        content = (doc.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        record("non-stream chat works without client tools", status == 200 and "smoke-ok" in content.lower(), "HTTP %s content=%r" % (status, content[:60]))
        record("aggregated response carries usage", bool(doc.get("usage")), json.dumps(doc.get("usage"))[:80])

        print("  -- UTF-8 / Chinese round trip --")
        chinese = "只回复四个字：网关正常"
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key),
                                         body={"model": "big-pickle", "messages": [{"role": "user", "content": chinese}]})
        try:
            text = data.decode("utf-8")
            doc = json.loads(text)
            reply = (doc.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            clean = "\ufffd" not in text and "\ufffd" not in reply
        except Exception as exc:
            reply, clean, text = "", False, str(exc)
        record("utf-8 request/response clean", status == 200 and clean and bool(reply), "reply=%r" % reply[:60])

        print("  -- streaming --")
        status, raw, headers = server.stream("/v1/chat/completions",
                                             {"model": "big-pickle", "messages": [{"role": "user", "content": "Count 1 to 5."}], "stream": True},
                                             bearer(key))
        text = raw.decode("utf-8", "replace")
        record("stream returns SSE", status == 200 and headers.get("content-type", "").startswith("text/event-stream"),
               "HTTP %s ct=%s" % (status, headers.get("content-type")))
        record("stream carries data chunks", "data:" in text, text[:60].replace("\n", " "))
        record("stream is utf-8 clean", "\ufffd" not in text)

        print("  -- client-supplied tools are passed through --")
        tools = [
            {"type": "function", "function": {"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "bash", "description": "Run a shell command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
        ]
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key),
                                         body={"model": "big-pickle", "tools": tools,
                                               "messages": [{"role": "user", "content": "Run the bash tool with the command: echo hello"}]})
        doc = json.loads(data) if status == 200 else {}
        calls = (doc.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
        names = [c.get("function", {}).get("name") for c in calls]
        record("client tool call survives", status == 200 and (not names or "bash" in names), "HTTP %s tool_calls=%s" % (status, names), soft=True)

        print("  -- injected stub call is neutralised --")
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key),
                                         body={"model": "big-pickle", "messages": [{"role": "user", "content": "Use the read tool to read /etc/hostname and tell me the content."}]})
        doc = json.loads(data) if status == 200 else {}
        choice = (doc.get("choices") or [{}])[0]
        leaked = choice.get("message", {}).get("tool_calls")
        note = "[zen-gateway]" in (choice.get("message", {}).get("content") or "")
        record("no unknown tool_calls leaked to client", status == 200 and not leaked, "tool_calls=%s" % (leaked,))
        record("stub call surfaced as text note", note or not leaked, "note=%s finish=%s" % (note, choice.get("finish_reason")), soft=True)

        print("  -- responses endpoint --")
        status, data, _ = server.request("POST", "/v1/responses", headers=bearer(key),
                                         body={"model": "muse-spark-1.3-contributor-free", "input": "Reply with exactly: resp-ok"})
        doc = json.loads(data) if status == 200 else {}
        texts = ["".join(p.get("text", "") for p in item.get("content") or []) for item in doc.get("output") or [] if item.get("type") == "message"]
        record("responses non-stream aggregates", status == 200 and doc.get("object") == "response" and doc.get("status") == "completed",
               "HTTP %s text=%r" % (status, (texts[0] if texts else "")[:40]))
        status, raw, headers = server.stream("/v1/responses",
                                             {"model": "muse-spark-1.3-contributor-free", "input": "Say OK.", "stream": True}, bearer(key))
        text = raw.decode("utf-8", "replace")
        record("responses stream emits events", status == 200 and "event: response." in text, text[:50].replace("\n", " "))

        print("  -- error handling --")
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key), raw_body=b"{not json")
        record("invalid JSON -> 400", status == 400, "HTTP %s %s" % (status, data[:80].decode("utf-8", "replace")))
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key), body={"messages": []})
        record("missing model -> 400", status == 400, "HTTP %s" % status)
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key), body={"model": "claude-opus-5", "messages": []})
        record("paid model -> 400", status == 400, "HTTP %s" % status)
        big = {"model": "big-pickle", "messages": [{"role": "user", "content": "x" * (33 * 1024 * 1024)}]}
        status, data, _ = server.request("POST", "/v1/chat/completions", headers=bearer(key), body=big)
        record("oversized body -> 413", status == 413, "HTTP %s" % status)

        print("  -- concurrency and client disconnect --")
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                status, _, _ = server.stream("/v1/chat/completions",
                                             {"model": "big-pickle", "messages": [{"role": "user", "content": "Say OK."}], "stream": True},
                                             bearer(key), max_bytes=200, stop_after_data=True)
            except Exception:
                status = -1
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        record("4 concurrent streams all 200", results == [200, 200, 200, 200], str(results))

        try:
            server.stream("/v1/chat/completions",
                          {"model": "big-pickle", "messages": [{"role": "user", "content": "Write a very long essay about tea."}], "stream": True},
                          bearer(key), max_bytes=200, stop_after_data=True)
        except Exception:
            pass
        status, data, _ = server.request("GET", "/v1/models", headers=bearer(key))
        record("server healthy after client disconnect", status == 200, "HTTP %s" % status)

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
            log_clean = "\ufffd" not in log_text
        except Exception as exc:
            lines, log_clean, log_text = [], False, str(exc)
        record("access log is valid jsonl", len(lines) > 8, "%d lines" % len(lines))
        record("access log is utf-8 clean", log_clean)
        fields_ok = all(k in (lines[-1] if lines else {}) for k in ("ts", "path", "key", "model", "stream", "status", "duration_ms"))
        record("access log has required fields", fields_ok, json.dumps(lines[-1])[:140] if lines else "")

        print("  -- restart behaviour --")
        server.stop()
        server2 = Server(args.port, env, "restart", tmp)
        try:
            record("restart comes back up", server2.wait_ready(), "port %d" % args.port)
            record("restart did NOT mint a new key", "Generated a new gateway API key" not in server2.log_text())
            status, _, _ = server2.request("GET", "/v1/models", headers=bearer(key))
            record("existing key still valid after restart", status == 200, "HTTP %s" % status)
        finally:
            server2.stop()
    finally:
        server.stop()
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("  temp dir kept: %s" % tmp)


def check_rate_limit(port: int) -> None:
    tmp = tempfile.mkdtemp(prefix="zen-smoke-rate-")
    key_file = os.path.join(tmp, "api-keys.txt")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": os.path.join(tmp, "access.jsonl"), "PORT": str(port),
           "LOG_LEVEL": "INFO", "RATE_LIMIT_PER_MIN": "2"}
    server = Server(port, env, "rate", tmp)
    try:
        print("\n== instance 2: RATE_LIMIT_PER_MIN=2 ==")
        if not record("rate-limited instance boots", server.wait_ready(), "port %d" % port):
            return
        key = read_key(key_file)
        codes = [server.request("GET", "/v1/models", headers=bearer(key))[0] for _ in range(3)]
        record("third request in the minute -> 429", codes == [200, 200, 429], str(codes))
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def check_ip_allowlist(port: int) -> None:
    tmp = tempfile.mkdtemp(prefix="zen-smoke-ip-")
    key_file = os.path.join(tmp, "api-keys.txt")
    env = {"KEY_FILE": key_file, "ACCESS_LOG": os.path.join(tmp, "access.jsonl"), "PORT": str(port),
           "LOG_LEVEL": "INFO", "IP_ALLOWLIST": "10.99.99.99/32"}
    server = Server(port, env, "ip", tmp)
    try:
        print("\n== instance 3: IP_ALLOWLIST=10.99.99.99/32 ==")
        if not record("allowlist instance boots", server.wait_ready(), "port %d" % port):
            return
        key = read_key(key_file)
        status, data, _ = server.request("GET", "/v1/models", headers=bearer(key))
        record("loopback blocked by allowlist", status == 403 and b"not allowed" in data, "HTTP %s %s" % (status, data[:80].decode("utf-8", "replace")))
        record("/healthz stays open for docker healthcheck", server.request("GET", "/healthz")[0] == 200)
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="live smoke test for the OpenCode Zen gateway")
    parser.add_argument("--port", type=int, default=8211)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    print("Live smoke test (real upstream: %s)" % os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1"))
    started = time.time()
    check_main_instance(args)
    check_rate_limit(args.port + 1)
    check_ip_allowlist(args.port + 2)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d checks in %.1fs" % (len(RESULTS), time.time() - started))
    if failed:
        print("FAILED (%d): %s" % (len(failed), ", ".join(failed)))
        return 1
    print("all live smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
