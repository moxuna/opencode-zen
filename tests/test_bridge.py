#!/usr/bin/env python3
"""Offline tests for the Responses <-> chat/completions bridge (no network).

    python3 tests/test_bridge.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bridge import (  # noqa: E402
    ChatToResponsesConverter,
    chat_tools_from_responses,
    messages_from_responses,
    responses_to_chat,
    usage_to_responses,
)
from app.gate import ChatRewriter  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s %s" % (name, detail))


def delta(content: str = None, reasoning: str = None, tool: dict = None, finish: str = None) -> dict:
    d: dict = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    if tool is not None:
        d["tool_calls"] = [tool]
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}


def codex_like_payload() -> dict:
    """The shape Codex CLI actually sends (captured with a local sink)."""
    return {
        "model": "mimo-v2.6-flash-free",
        "stream": True,
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex."}]},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills/>"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "tools": [
            {"type": "function", "name": "exec_command", "strict": False, "description": "run",
             "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
            {"type": "namespace", "name": "multi_agent_v1", "tools": []},
            {"type": "web_search"},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"summary": "auto"},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "01a1357a-6514-7bb0",
        "client_metadata": {"session_id": "x"},
        "max_output_tokens": 4096,
    }


def test_request_translation() -> None:
    print("request translation (Codex shape)")
    body = responses_to_chat(codex_like_payload())
    check("stream forced true", body["stream"] is True)
    check("single merged system message first", body["messages"][0]["role"] == "system"
          and "You are Codex." in body["messages"][0]["content"] and "<skills/>" in body["messages"][0]["content"],
          str(body["messages"][0])[:80])
    check("user message kept", body["messages"][1] == {"role": "user", "content": "hi"}, str(body["messages"][1]))
    check("developer roles all folded into system", all(m["role"] != "developer" for m in body["messages"]))
    names = [t["function"]["name"] for t in body.get("tools", [])]
    check("only function tools survive", names == ["exec_command"], str(names))
    check("strict flag dropped", "strict" not in body["tools"][0]["function"])
    check("server-side fields dropped", not any(k in body for k in ("store", "include", "prompt_cache_key",
                                                                   "client_metadata", "reasoning", "max_output_tokens")))
    check("max_output_tokens -> max_tokens", body.get("max_tokens") == 4096)
    check("tool_choice forwarded", body.get("tool_choice") == "auto")
    check("parallel_tool_calls forwarded", body.get("parallel_tool_calls") is True)

    print("request translation (tool round trip)")
    payload = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
            {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "call_1"},
            {"type": "function_call_output", "call_id": "call_1", "output": "a.txt\nb.txt"},
        ],
    }
    messages = messages_from_responses(payload)
    check("3 messages produced", len(messages) == 3, str([m["role"] for m in messages]))
    check("function_call -> assistant tool_calls", messages[1]["role"] == "assistant"
          and messages[1]["tool_calls"][0]["id"] == "call_1"
          and messages[1]["tool_calls"][0]["function"]["name"] == "exec_command", str(messages[1])[:120])
    check("function_call_output -> tool role", messages[2]["role"] == "tool"
          and messages[2]["tool_call_id"] == "call_1" and "a.txt" in messages[2]["content"], str(messages[2])[:120])

    print("request translation (edge cases)")
    check("plain string input", messages_from_responses({"input": "hello"}) == [{"role": "user", "content": "hello"}])
    check("instructions become system", messages_from_responses({"input": "x", "instructions": "be terse"})[0]["role"] == "system")
    parallel = messages_from_responses({"input": [
        {"type": "function_call", "name": "a", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call", "name": "b", "arguments": "{}", "call_id": "c2"},
    ]})
    check("parallel calls share one assistant turn", len(parallel) == 1 and len(parallel[0]["tool_calls"]) == 2, str(parallel)[:120])
    check("reasoning items dropped", messages_from_responses({"input": [
        {"type": "reasoning", "summary": []}, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]}]}) == [{"role": "user", "content": "x"}])
    custom = chat_tools_from_responses([{"type": "custom", "name": "apply_patch", "description": "d"}])
    check("custom tool becomes string function", custom[0]["function"]["name"] == "apply_patch"
          and "input" in custom[0]["function"]["parameters"]["properties"])


def test_converter_text() -> None:
    print("converter: text stream")
    converter = ChatToResponsesConverter("mimo-v2.6-flash-free")
    events = converter.start() + converter.feed(delta(content="hello ")) + converter.feed(delta(content="world")) \
        + converter.feed(delta(finish="stop")) + converter.feed({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}) \
        + converter.finish()
    types = [e["type"] for e in events]
    check("starts with response.created", types[0] == "response.created", types[0])
    check("emits item + part lifecycle", types.count("response.output_item.added") == 1
          and "response.content_part.added" in types and "response.output_text.delta" in types
          and "response.output_text.done" in types and "response.output_item.done" in types, str(types))
    check("ends with response.completed", types[-1] == "response.completed", types[-1])
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    check("deltas preserved in order", deltas == "hello world", deltas)
    final = events[-1]["response"]
    check("completed status + text", final["status"] == "completed"
          and final["output"][0]["content"][0]["text"] == "hello world", str(final["output"])[:140])
    check("usage mapped to responses shape", final["usage"]["total_tokens"] == 7
          and final["usage"]["input_tokens"] == 5 and final["usage"]["output_tokens"] == 2, str(final["usage"]))
    check("sequence numbers increase", [e["sequence_number"] for e in events] == list(range(1, len(events) + 1)))
    check("result() matches completed output", converter.result()["output"][0]["content"][0]["text"] == "hello world")


def test_converter_tools() -> None:
    print("converter: tool call stream")
    converter = ChatToResponsesConverter("mimo-v2.6-flash-free")
    events = converter.start()
    events += converter.feed(delta(tool={"index": 0, "id": "call_abc", "type": "function",
                                        "function": {"name": "exec_command", "arguments": '{"cmd"'}}))
    events += converter.feed(delta(tool={"index": 0, "function": {"arguments": ': "ls"}'}}))
    events += converter.feed(delta(finish="tool_calls"))
    events += converter.finish()
    final = events[-1]["response"]
    item = final["output"][0]
    check("function_call item produced", item["type"] == "function_call" and item["name"] == "exec_command", str(item)[:120])
    check("call_id preserved for round trip", item["call_id"] == "call_abc", item.get("call_id"))
    check("arguments accumulated", item["arguments"] == '{"cmd": "ls"}', item["arguments"])
    check("item id is fc_ prefixed", item["id"].startswith("fc_"), item["id"])
    check("no message item when only tools", all(i["type"] != "message" for i in final["output"]), str(final["output"])[:120])
    check("argument deltas emitted", sum(1 for e in events if e["type"] == "response.function_call_arguments.delta") == 2)

    print("converter: text then tool call (item ordering)")
    converter = ChatToResponsesConverter("m")
    converter.start()
    converter.feed(delta(content="let me check"))
    converter.feed(delta(tool={"index": 0, "id": "c1", "function": {"name": "exec_command", "arguments": "{}"}}))
    final = converter.finish()[-1]["response"]
    kinds = [i["type"] for i in final["output"]]
    check("message then function_call", kinds == ["message", "function_call"], str(kinds))


def test_converter_with_stub_neutralisation() -> None:
    print("converter: injected stub calls are neutralised (gateway integration)")
    rewriter = ChatRewriter(frozenset({"read"}), "text", forward_usage=False)
    converter = ChatToResponsesConverter("mimo-v2.6-flash-free")
    events = converter.start()
    for chunk in (
        delta(tool={"index": 0, "id": "call_x", "type": "function", "function": {"name": "read", "arguments": '{"path":"/etc/hosts"}'}}),
        delta(finish="tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ):
        rewritten = rewriter.feed(chunk)
        if rewritten is not None:
            events += converter.feed(rewritten)
    events += converter.finish()
    final = events[-1]["response"]
    check("stub call became a text message", [i["type"] for i in final["output"]] == ["message"], str(final["output"])[:100])
    text = final["output"][0]["content"][0]["text"]
    check("client sees the note, not a tool call", "[zen-gateway]" in text, text[:80])
    check("no function_call leaked", all(i["type"] != "function_call" for i in final["output"]))


def test_usage_mapping() -> None:
    print("usage mapping")
    mapped = usage_to_responses({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                                 "prompt_tokens_details": {"cached_tokens": 3},
                                 "completion_tokens_details": {"reasoning_tokens": 2}})
    check("details mapped", mapped["input_tokens_details"]["cached_tokens"] == 3
          and mapped["output_tokens_details"]["reasoning_tokens"] == 2, str(mapped))
    check("missing usage handled", usage_to_responses(None)["total_tokens"] is None)


def main() -> int:
    test_request_translation()
    test_converter_text()
    test_converter_tools()
    test_converter_with_stub_neutralisation()
    test_usage_mapping()
    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all bridge tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
