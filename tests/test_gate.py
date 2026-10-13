#!/usr/bin/env python3
"""Offline tests for the gate fixups (no network needed).

    python3 tests/test_gate.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gate import (  # noqa: E402
    ChatAggregator,
    ChatRewriter,
    ResponsesRewriter,
    has_gate_tools,
    patch_body,
    plan_gate,
)
from app.zen_session import decode_counter, mint_session  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s %s" % (name, detail))


def chat_chunk(name: str | None = None, arguments: str | None = None, index: int = 0, finish: str | None = None):
    delta: dict = {}
    if name is not None:
        delta["tool_calls"] = [{"index": index, "id": "call_1", "type": "function",
                                "function": {"name": name, "arguments": arguments or ""}}]
    elif arguments is not None:
        delta["tool_calls"] = [{"index": index, "function": {"arguments": arguments}}]
    return {"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def test_session_ids() -> None:
    print("session ids")
    for seed in ("", "default", "phone", "x" * 40):
        sid = mint_session(seed)
        counter = decode_counter(sid)
        check("shape %r" % seed[:12], sid.startswith("ses_") and len(sid) == 30, sid)
        check("counter < 64 for %r" % seed[:12], counter is not None and counter < 64, str(counter))
    check("stable per seed", mint_session("a")[16:] == mint_session("a")[16:])


def test_patch_body() -> None:
    print("patch_body")
    patched, plan = patch_body({"model": "m", "messages": []})
    names = {t["function"]["name"] for t in patched["tools"]}
    check("stream forced true", patched["stream"] is True)
    check("injects read+bash", names == {"read", "bash"} and plan.injected == frozenset({"read", "bash"}), str(names))
    check("no client tool to mirror -> both are stubs", plan.stubs == frozenset({"read", "bash"}) and plan.aliases == {})

    mine = [{"type": "function", "function": {"name": "read", "description": "d", "parameters": {}}},
            {"type": "function", "function": {"name": "bash", "description": "d", "parameters": {}}}]
    patched, plan = patch_body({"model": "m", "tools": mine})
    check("client tools kept untouched", plan.injected == frozenset() and patched["tools"] == mine)

    patched, plan = patch_body({"model": "m", "tools": [{"type": "function", "function": {"name": "read"}}]})
    check("only missing bash injected", plan.injected == frozenset({"bash"}))
    check("has_gate_tools", has_gate_tools(patched["tools"]))

    print("mirror mode: client tools are reused under the required names")
    codex_tools = [
        {"type": "function", "function": {"name": "exec_command", "description": "run", "parameters": {
            "type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
        {"type": "function", "function": {"name": "write_stdin", "description": "d", "parameters": {"type": "object", "properties": {}}}},
    ]
    patched, plan = patch_body({"model": "m", "tools": codex_tools}, mode="mirror")
    by_name = {t["function"]["name"]: t for t in patched["tools"]}
    check("client tools untouched", "exec_command" in by_name and "write_stdin" in by_name)
    check("bash is a mirror of exec_command", plan.aliases.get("bash") == "exec_command"
          and by_name["bash"]["function"]["parameters"]["properties"] == {"cmd": {"type": "string"}}, str(plan.aliases))
    check("read has no client equivalent -> stub", plan.stubs == frozenset({"read"}) and "read" in by_name)
    check("gate satisfied", has_gate_tools(patched["tools"]))
    check("stub mode disables mirroring", plan_gate(codex_tools, mode="stub").aliases == {})

    responses_tools = [{"type": "function", "name": "exec_command", "description": "d",
                        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
    patched, plan = patch_body({"model": "m", "tools": responses_tools}, responses_api=True, mode="mirror")
    mirrored = [t for t in patched["tools"] if t.get("name") == "bash"]
    check("responses shape mirrored too", bool(mirrored) and mirrored[0]["parameters"]["properties"] == {"cmd": {"type": "string"}})

    print("mirror mode: outgoing calls are renamed back to the client tool")
    plan = plan_gate(codex_tools, mode="mirror")
    rewriter = ChatRewriter(plan, "text")
    chunk = rewriter.feed(chat_chunk(name="bash", arguments='{"cmd":"ls"}'))
    call = chunk["choices"][0]["delta"]["tool_calls"][0]
    check("bash call renamed to exec_command", call["function"]["name"] == "exec_command", str(call))
    final = rewriter.feed(chat_chunk(finish="tool_calls"))
    check("real tool keeps finish_reason", final["choices"][0]["finish_reason"] == "tool_calls")
    stub_chunk = rewriter.feed(chat_chunk(name="read", arguments='{"path":"/etc/hosts"}'))
    check("pure stub still neutralised", "tool_calls" not in stub_chunk["choices"][0]["delta"]
          and "[zen-gateway]" in stub_chunk["choices"][0]["delta"].get("content", ""))


def test_chat_rewriter() -> None:
    print("chat rewriter: stub call suppressed")
    rewriter = ChatRewriter(frozenset({"read"}), "text")
    first = rewriter.feed(chat_chunk(name="read", arguments='{"path":"/etc/hostname"}'))
    check("tool call removed", "tool_calls" not in first["choices"][0]["delta"])
    check("note added as content", "[zen-gateway]" in first["choices"][0]["delta"].get("content", ""))
    second = rewriter.feed(chat_chunk(arguments="/etc"))
    check("argument fragments dropped", "tool_calls" not in second["choices"][0]["delta"])
    final = rewriter.feed(chat_chunk(finish="tool_calls"))
    check("finish_reason downgraded", final["choices"][0]["finish_reason"] == "stop", str(final))

    print("chat rewriter: client tool passes through")
    rewriter = ChatRewriter(frozenset({"read"}), "text")
    chunk = rewriter.feed(chat_chunk(name="bash", arguments='{"command":"ls"}'))
    check("client tool kept", chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "bash")
    final = rewriter.feed(chat_chunk(finish="tool_calls"))
    check("real tool keeps finish_reason", final["choices"][0]["finish_reason"] == "tool_calls")

    print("chat rewriter: mixed stub + client tool")
    rewriter = ChatRewriter(frozenset({"read"}), "text")
    mixed = {"id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"name": "read", "arguments": "{}"}},
        {"index": 1, "function": {"name": "grep", "arguments": "{}"}},
    ]}, "finish_reason": None}]}
    out = rewriter.feed(mixed)
    kept = [c["function"]["name"] for c in out["choices"][0]["delta"]["tool_calls"]]
    check("only real tool survives", kept == ["grep"], str(kept))

    print("aggregator: non-stream result")
    rewriter = ChatRewriter(frozenset({"read"}), "text")
    aggregator = ChatAggregator()
    for chunk in (
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi "}, "finish_reason": None}]},
        chat_chunk(name="read", arguments="{}"),
        chat_chunk(finish="tool_calls"),
        {"id": "c", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ):
        rewritten = rewriter.feed(chunk)
        if rewritten is not None:
            aggregator.feed(rewritten)
    result = aggregator.result()
    choice = result["choices"][0]
    check("no tool_calls in result", "tool_calls" not in choice["message"])
    check("content carries note", "[zen-gateway]" in choice["message"]["content"])
    check("finish_reason stop", choice["finish_reason"] == "stop")
    check("usage preserved", result["usage"]["total_tokens"] == 7)


def test_responses_rewriter() -> None:
    print("responses rewriter")
    rewriter = ResponsesRewriter(frozenset({"bash"}), "text")
    added = rewriter.feed({"type": "response.output_item.added", "output_index": 1, "item": {
        "id": "fc_1", "type": "function_call", "name": "bash", "arguments": ""}})
    check("stub call replaced by text delta", len(added) == 1 and added[0]["type"] == "response.output_text.delta")
    check("args events dropped", rewriter.feed({"type": "response.function_call_arguments.done", "item_id": "fc_1"}) == [])
    check("item done dropped", rewriter.feed({"type": "response.output_item.done", "item": {
        "id": "fc_1", "type": "function_call", "name": "bash"}}) == [])
    completed = rewriter.feed({"type": "response.completed", "response": {"output": [
        {"id": "fc_1", "type": "function_call", "name": "bash", "arguments": "{}"},
        {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "done"}]},
    ]}})[0]
    types = [item["type"] for item in completed["response"]["output"]]
    check("completed drops stub, keeps message", types == ["message", "message"], str(types))

    print("responses rewriter: client tool untouched")
    rewriter = ResponsesRewriter(frozenset({"bash"}), "text")
    event = {"type": "response.output_item.added", "item": {"id": "fc_2", "type": "function_call", "name": "grep"}}
    check("non-stub event passes", rewriter.feed(event) == [event])


def test_usage_gating() -> None:
    print("usage chunk forwarding")
    rewriter = ChatRewriter(frozenset(), "text", forward_usage=False)
    usage_only = {"id": "c", "choices": [], "usage": {"total_tokens": 3}}
    check("usage-only chunk dropped when client did not ask", rewriter.feed(usage_only) is None)
    check("usage captured for logging", rewriter.usage == {"total_tokens": 3})
    rewriter = ChatRewriter(frozenset(), "text", forward_usage=True)
    check("usage-only chunk kept when client asked", rewriter.feed(usage_only) is usage_only)


def main() -> int:
    test_session_ids()
    test_patch_body()
    test_chat_rewriter()
    test_responses_rewriter()
    test_usage_gating()
    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all gate tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
