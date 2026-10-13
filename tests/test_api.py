#!/usr/bin/env python3
"""HTTP-layer tests for the gateway with a mocked upstream (no network).

    python3 tests/test_api.py

Covers auth, model guard, body limits, stub injection at the HTTP boundary,
streaming pass-through, non-stream aggregation, upstream error surfacing and
key-file hot reload.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

TMPDIR = tempfile.mkdtemp(prefix="zen-gateway-test-")
os.environ["KEY_FILE"] = os.path.join(TMPDIR, "api-keys.txt")
os.environ["ACCESS_LOG"] = os.path.join(TMPDIR, "access.jsonl")
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["RATE_LIMIT_PER_MIN"] = "0"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as gateway  # noqa: E402

FAILURES: list[str] = []
SEEN_REQUESTS: list[httpx.Request] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s %s" % (name, detail))


def sse(*chunks: dict, done: bool = True) -> bytes:
    body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    if done:
        body += b"data: [DONE]\n\n"
    return body


def chat_delta(content: str | None = None, role: str | None = None, tool: dict | None = None, finish: str | None = None) -> dict:
    delta: dict = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool:
        delta["tool_calls"] = [tool]
    return {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "big-pickle",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def handler(request: httpx.Request) -> httpx.Response:
    SEEN_REQUESTS.append(request)
    path = request.url.path

    if path.endswith("/models"):
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "big-pickle", "object": "model", "created": 1, "owned_by": "opencode"},
            {"id": "mimo-v2.6-flash-free", "object": "model", "created": 1, "owned_by": "opencode"},
            {"id": "claude-opus-5", "object": "model", "created": 1, "owned_by": "opencode"},
        ]})

    if path.endswith("/chat/completions"):
        payload = json.loads(request.content)
        if "gatefail" in json.dumps(payload.get("messages")):
            return httpx.Response(403, json={"type": "error", "error": {"type": "FreeTierError", "message": "nope"}})
        if "boom" in json.dumps(payload.get("messages")):
            return httpx.Response(200, content=sse({"id": "x", "choices": [], "error": {"message": "upstream exploded"}}), headers={"content-type": "text/event-stream"})
        if "toolplease" in json.dumps(payload.get("messages")):
            chunk = chat_delta(tool={"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path":"/etc/hosts"}'}})
            return httpx.Response(200, content=sse(chunk, chat_delta(finish="tool_calls")), headers={"content-type": "text/event-stream"})
        chunks = [
            chat_delta(role="assistant", content="hello "),
            chat_delta(content="世界"),  # non-ASCII round trip
            chat_delta(finish="stop"),
            {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "big-pickle", "choices": [],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
        ]
        return httpx.Response(200, content=sse(*chunks), headers={"content-type": "text/event-stream"})

    if path.endswith("/responses"):
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "object": "response"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message"}},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "hi"},
            {"type": "response.completed", "response": {"id": "resp_1", "object": "response", "status": "completed",
                                                        "output": [{"id": "msg_1", "type": "message", "role": "assistant",
                                                                    "content": [{"type": "output_text", "text": "hi"}]}],
                                                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}},
        ]
        body = b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.Response(404, json={"error": "unexpected path %s" % path})


def bearer(client: TestClient) -> dict:
    key = open(os.environ["KEY_FILE"], encoding="utf-8").read()
    key = [line.split(":", 1)[1].strip() for line in key.splitlines() if line and not line.startswith("#")][0]
    return {"Authorization": "Bearer %s" % key}


def run() -> int:
    transport = httpx.MockTransport(handler)
    gateway.upstream.client = httpx.AsyncClient(transport=transport, base_url="https://upstream.test/zen/v1", timeout=30)

    with TestClient(gateway.app) as client:
        key_path = os.environ["KEY_FILE"]
        check("key file auto-created", os.path.exists(key_path))
        headers = bearer(client)

        print("auth")
        check("no key -> 401", client.get("/v1/models").status_code == 401)
        check("bad key -> 401", client.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401)
        check("x-api-key accepted", client.get("/v1/models", headers={"x-api-key": headers["Authorization"].split()[1]}).status_code == 200)
        check("?key= accepted", client.get("/v1/models", params={"key": headers["Authorization"].split()[1]}).status_code == 200)
        check("healthz open", client.get("/healthz").status_code == 200)
        check("readyz open", client.get("/readyz").status_code == 200)

        print("models")
        payload = client.get("/v1/models", headers=headers).json()
        ids = [m["id"] for m in payload["data"]]
        check("free models only", ids == ["big-pickle", "mimo-v2.6-flash-free"], str(ids))
        check("model by id", client.get("/v1/models/big-pickle", headers=headers).status_code == 200)
        check("paid model hidden", client.get("/v1/models/claude-opus-5", headers=headers).status_code == 404)
        check("unknown model 404", client.get("/v1/models/nope", headers=headers).status_code == 404)

        print("request guards")
        check("invalid json -> 400", client.post("/v1/chat/completions", headers={**headers, "Content-Type": "application/json"}, content=b"{oops").status_code == 400)
        check("missing model -> 400", client.post("/v1/chat/completions", headers=headers, json={"messages": []}).status_code == 400)
        check("paid model -> 400", client.post("/v1/chat/completions", headers=headers, json={"model": "claude-opus-5", "messages": []}).status_code == 400)
        too_big = {"model": "big-pickle", "messages": [{"role": "user", "content": "x" * (33 * 1024 * 1024)}]}
        check("huge body -> 413", client.post("/v1/chat/completions", headers=headers, json=too_big).status_code == 413)

        print("chat: non-stream aggregation + stub injection")
        SEEN_REQUESTS.clear()
        response = client.post("/v1/chat/completions", headers=headers,
                               json={"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]})
        check("200", response.status_code == 200, response.text[:200])
        doc = response.json()
        check("object is chat.completion", doc["object"] == "chat.completion")
        choice = doc["choices"][0]
        check("content concatenated (utf-8 safe)", choice["message"]["content"] == "hello 世界", repr(choice["message"]["content"]))
        check("usage preserved", doc["usage"]["total_tokens"] == 18)
        check("finish_reason stop", choice["finish_reason"] == "stop")
        upstream_body = json.loads(SEEN_REQUESTS[-1].content)
        check("upstream stream forced true", upstream_body["stream"] is True)
        tool_names = {t["function"]["name"] for t in upstream_body["tools"]}
        check("stub tools injected", tool_names == {"read", "bash"}, str(tool_names))
        check("upstream UA", SEEN_REQUESTS[-1].headers["user-agent"].startswith("opencode/"), SEEN_REQUESTS[-1].headers["user-agent"])
        check("upstream session header", SEEN_REQUESTS[-1].headers["x-opencode-session"].startswith("ses_"))
        check("usage requested upstream", upstream_body["stream_options"]["include_usage"] is True)

        print("chat: streaming pass-through")
        SEEN_REQUESTS.clear()
        with client.stream("POST", "/v1/chat/completions", headers=headers,
                           json={"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}], "stream": True}) as stream:
            check("200 stream", stream.status_code == 200)
            check("content-type sse", stream.headers["content-type"].startswith("text/event-stream"), stream.headers["content-type"])
            raw = b"".join(stream.iter_bytes())
        text = raw.decode("utf-8")
        check("sse has deltas", "hello " in text and "世界" in text, text[:200])
        check("sse terminates with [DONE]", text.rstrip().endswith("data: [DONE]"), text[-40:])
        upstream_body = json.loads(SEEN_REQUESTS[-1].content)
        check("client did not ask usage -> still requested upstream", upstream_body["stream_options"]["include_usage"] is True)

        print("chat: stub tool call neutralised")
        response = client.post("/v1/chat/completions", headers=headers,
                               json={"model": "big-pickle", "messages": [{"role": "user", "content": "toolplease read a file"}]})
        doc = response.json()
        choice = doc["choices"][0]
        check("no tool_calls leaked", "tool_calls" not in choice["message"], str(choice["message"].get("tool_calls")))
        check("note in content", "[zen-gateway]" in (choice["message"]["content"] or ""))
        check("finish_reason downgraded", choice["finish_reason"] == "stop", choice["finish_reason"])

        print("chat: client tools untouched")
        SEEN_REQUESTS.clear()
        client.post("/v1/chat/completions", headers=headers, json={
            "model": "big-pickle", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "read", "description": "d", "parameters": {"type": "object", "properties": {}}}},
                      {"type": "function", "function": {"name": "bash", "description": "d", "parameters": {"type": "object", "properties": {}}}}]})
        upstream_body = json.loads(SEEN_REQUESTS[-1].content)
        check("client tools passed through unchanged", len(upstream_body["tools"]) == 2 and upstream_body["tools"][0]["function"]["description"] == "d")

        print("upstream errors")
        response = client.post("/v1/chat/completions", headers=headers,
                               json={"model": "big-pickle", "messages": [{"role": "user", "content": "gatefail"}]})
        check("upstream 403 passed through", response.status_code == 403 and "FreeTierError" in response.text, response.text[:120])
        response = client.post("/v1/chat/completions", headers=headers, json={"model": "big-pickle", "messages": [{"role": "user", "content": "boom"}]})
        check("mid-stream error -> 502", response.status_code == 502, response.text[:200])

        print("responses endpoint")
        response = client.post("/v1/responses", headers=headers, json={"model": "muse-spark-1.3-contributor-free", "input": "hi"})
        check("non-stream 200", response.status_code == 200, response.text[:200])
        doc = response.json()
        check("returns response object", doc.get("object") == "response" and doc.get("status") == "completed", str(doc)[:150])
        check("usage mapped to tokens", doc["usage"]["total_tokens"] == 4)
        with client.stream("POST", "/v1/responses", headers=headers,
                           json={"model": "muse-spark-1.3-contributor-free", "input": "hi", "stream": True}) as stream:
            raw = b"".join(stream.iter_bytes()).decode("utf-8")
        check("responses sse keeps event names", "event: response.completed" in raw, raw[:120])
        check("stub tools injected for responses shape", any(
            "read" == t.get("name") for t in json.loads(SEEN_REQUESTS[-1].content)["tools"]))

        print("key hot reload")
        token = headers["Authorization"].split()[1]
        with open(key_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("phone:sk-oczen-ffffffffffffffffffffffffffffffff\n")
        check("new key works without restart", client.get("/v1/models", headers={"Authorization": "Bearer sk-oczen-ffffffffffffffffffffffffffffffff"}).status_code == 200)
        check("old key still works", client.get("/v1/models", headers={"Authorization": "Bearer %s" % token}).status_code == 200)

        print("access log")
        lines = [json.loads(line) for line in open(os.environ["ACCESS_LOG"], encoding="utf-8") if line.strip()]
        check("access log written", len(lines) > 5, str(len(lines)))
        last_chat = [line for line in lines if line.get("path") == "/v1/chat/completions"][-1]
        check("log has key/model/status/usage", all(k in last_chat for k in ("key", "model", "status", "duration_ms", "injected_tools")), str(last_chat))
        check("log has model name", last_chat["model"] in ("big-pickle", "mimo-v2.6-flash-free"))

    gateway.upstream.client = httpx.AsyncClient(base_url="https://upstream.test/zen/v1")
    return 0


def main() -> int:
    run()
    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all api tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
