#!/usr/bin/env python3
"""Readiness probe for the OpenCode Zen free tier.

Run this ON THE VPS (or any machine that will host the gateway) BEFORE deploying:

    python3 scripts/probe_zen.py

It checks, in order:
  1. DNS + TLS reachability of opencode.ai
  2. GET  /zen/v1/models                     -> 200 + model list
  3. POST /zen/v1/chat/completions           -> 200 (free tier gate passed)

The free tier gate (measured 2026-10-13) requires ALL of:
  * User-Agent starting with "opencode/"
  * X-Opencode-Session = "ses_" + 12 hex + 14 base62, per-ms counter < 64
  * body "stream": true
  * body tools containing a tool named "read" and one named "bash"/"shell"

Only stdlib, no dependencies. HTTPS_PROXY / HTTP_PROXY env vars are honoured.

Exit code 0 = ready to deploy, 1 = not ready.
"""

import json
import os

import secrets
import socket
import ssl
import string
import sys
import time
import urllib.error
import urllib.request

HOST = "opencode.ai"
BASE = "https://%s/zen/v1" % HOST
UA = "opencode/latest/2.0.11/cli"
B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
TIMEOUT = 30

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail))


def mint_session(ts_ms=None, counter=1):
    """Mint a session id the way the OpenCode client does (gate requirement #2)."""
    ts = (ts_ms if ts_ms is not None else int(time.time() * 1000)) % (1 << 36)
    r = ts * 4096 + counter
    a = ((1 << 48) - 1 - r) & ((1 << 48) - 1)
    return "ses_%012x" % a + "".join(secrets.choice(B62) for _ in range(14))


def request(path, body=None, headers=None, stream_read=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    if stream_read:
        first = b""
        for line in resp:
            if line.startswith(b"data:"):
                first = line
                break
        resp.close()
        return resp.status, first
    payload = resp.read()
    resp.close()
    return resp.status, payload


def check_dns_tls():
    try:
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
    except Exception as exc:
        record("DNS resolve %s" % HOST, False, "cannot resolve: %s" % exc)
        return None
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((HOST, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                peer = tls.getpeercert().get("subjectAltName", ())
                tls_version = tls.version()
        record("TLS handshake %s:443" % HOST, True, "%s, %s, ips=%s" % (tls_version, peer[:1] or "no SAN", ",".join(ips[:2])))
        return ips
    except Exception as exc:
        record("TLS handshake %s:443" % HOST, False, "handshake failed: %s (proxy/firewall?)" % exc)
        return None


def check_models():
    try:
        status, payload = request("/models")
        doc = json.loads(payload)
        ids = [m.get("id") for m in doc.get("data", [])]
    except urllib.error.HTTPError as exc:
        record("GET /zen/v1/models", False, "HTTP %s" % exc.code)
        return []
    except Exception as exc:
        record("GET /zen/v1/models", False, "%s" % exc)
        return []
    free = [i for i in ids if i and (i.endswith("-free") or i in ("big-pickle", "cat-jam-free"))]
    record("GET /zen/v1/models", status == 200 and bool(ids), "HTTP %s, %d models, %d free" % (status, len(ids), len(free)))
    return ids


def gate_body(model, tools=True):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK."}],
        "stream": True,
    }
    if tools:
        body["tools"] = [gate_tool("read", "path"), gate_tool("bash", "command")]
    return body


def gate_tool(name: str, arg: str) -> dict:
    """Minimal tool definition; the free tier gate needs tools named read + bash/shell."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "gate stub",
            "parameters": {"type": "object", "properties": {arg: {"type": "string"}}, "required": [arg]},
        },
    }


def check_free_gate(models):
    model = "big-pickle" if "big-pickle" in models or not models else models[0]
    hdrs = {"X-Opencode-Session": mint_session()}
    try:
        status, first = request("/chat/completions", gate_body(model), hdrs, stream_read=True)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace").replace("\n", " ")
        record("POST /chat/completions (%s)" % model, False, "HTTP %s %s" % (exc.code, detail))
        if "FreeTierError" in detail:
            print("        -> upstream rejected the gate: check User-Agent / session id / stream / tools")
        return False
    except Exception as exc:
        record("POST /chat/completions (%s)" % model, False, "%s" % exc)
        return False
    ok = status == 200 and first.startswith(b"data:")
    record("POST /chat/completions (%s)" % model, ok, "HTTP %s, first SSE: %s" % (status, first[:60].decode("utf-8", "replace")))

    # informational: the same request without tools is expected to be rejected
    try:
        request("/chat/completions", gate_body(model, tools=False), {"X-Opencode-Session": mint_session()}, stream_read=True)
        print("        [note] tool-less request was ACCEPTED (gate may have loosened)")
    except urllib.error.HTTPError as exc:
        body = exc.read()[:120].decode("utf-8", "replace")
        if "FreeTierError" in body:
            print("        [note] tool-less request rejected as expected (403 FreeTierError)")
        else:
            print("        [note] tool-less request -> HTTP %s %s" % (exc.code, body))
    except Exception:
        pass
    return ok


def main():
    print("OpenCode Zen readiness probe")
    print("  target : %s" % BASE)
    print("  python : %s" % sys.version.split()[0])
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    print("  proxy  : %s" % (proxy or "(none)"))
    print("")
    ips = check_dns_tls()
    models = check_models() if ips else []
    if ips:
        check_free_gate(models)
    print("")
    failed = [n for n, ok, _ in results if not ok]
    if not failed:
        print("READY: %d/%d checks passed - deploy the gateway here." % (len(results), len(results)))
        return 0
    print("NOT READY: failed -> %s" % ", ".join(failed))
    print("Fixes: open 443 egress, set HTTPS_PROXY if behind a firewall, or move to another VPS/region.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
