"""Bridge between the OpenAI Responses API and chat/completions.

Some upstream models only speak /v1/chat/completions (mimo-v2.6-flash-free,
big-pickle, cat-jam-free, ...) while some clients only speak the Responses API
(Codex CLI with `wire_api = "responses"`, newer OpenAI SDK usages). Without a
bridge the upstream answers:

    {"type":"error","error":{"type":"ModelProtocolUnsupported",
     "message":"Model does not support this protocol."}}

This module converts a Responses request into a chat request and the chat SSE
stream back into Responses SSE events, so every free model works with every
client.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

# chat/completions fields we are willing to forward when translating a request
CHAT_PASSTHROUGH = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop",
    "presence_penalty", "frequency_penalty", "seed", "logit_bias", "n", "user",
    "parallel_tool_calls", "response_format",
)


# --------------------------------------------------------------------- request


def _text_from_parts(parts: Any) -> str | list[dict[str, Any]] | None:
    """Convert Responses content parts into chat content (str or part list for images)."""
    if parts is None:
        return None
    if isinstance(parts, str):
        return parts
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in parts if isinstance(parts, list) else [parts]:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text", "summary_text"):
            texts.append(str(part.get("text") or ""))
        elif kind == "refusal":
            texts.append(str(part.get("refusal") or ""))
        elif kind == "input_image":
            url = part.get("image_url")
            if isinstance(url, str) and url:
                images.append({"type": "image_url", "image_url": {"url": url}})
    if not images:
        return "".join(texts)
    return list({"type": "text", "text": t} for t in texts if t) + images


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        mapped = _text_from_parts(output)
        if isinstance(mapped, str):
            return mapped
        return json.dumps(mapped, ensure_ascii=False)
    if isinstance(output, dict) and "output" in output:
        return _tool_output_text(output["output"])
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False)


def chat_tools_from_responses(tools: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Responses function tools -> chat tools. Server-side tools are dropped."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind == "function":
            name = tool.get("name")
            if not name:
                continue
            function: dict[str, Any] = {"name": name, "description": tool.get("description") or ""}
            if isinstance(tool.get("parameters"), dict):
                function["parameters"] = tool["parameters"]
            converted.append({"type": "function", "function": function})
        elif kind == "custom" and tool.get("name"):
            # Codex's free-form tools: expose them as a single string argument function
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": {
                        "type": "object",
                        "properties": {"input": {"type": "string", "description": "Free-form input."}},
                        "required": ["input"],
                    },
                },
            })
    return converted


def messages_from_responses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build chat messages out of Responses `instructions` + `input`."""
    raw = payload.get("input")
    items: list[Any] = raw if isinstance(raw, list) else [raw] if raw is not None else []

    system_parts: list[str] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append(instructions)
    if isinstance(instructions, list):
        text = _text_from_parts(instructions)
        if isinstance(text, str) and text.strip():
            system_parts.append(text)

    messages: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in (None, "message"):
            role = item.get("role") or "user"
            content = _text_from_parts(item.get("content"))
            if role in ("developer", "system"):
                if isinstance(content, str) and content.strip():
                    system_parts.append(content)
                continue
            if role not in ("user", "assistant", "tool"):
                role = "user"
            if content is None or content == "" or content == []:
                continue
            previous_message = messages[-1] if messages else {}
            mergeable = (
                role == "assistant"
                and previous_message.get("role") == "assistant"
                and previous_message.get("content")
                and not previous_message.get("tool_calls")
            )
            if mergeable:
                # merge consecutive assistant text turns
                previous = messages[-1]["content"]
                messages[-1]["content"] = (previous if isinstance(previous, str) else "") + (content if isinstance(content, str) else "")
                continue
            messages.append({"role": role, "content": content})
        elif kind == "function_call":
            call = {
                "id": item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                "type": "function",
                "function": {"name": item.get("name") or "", "arguments": item.get("arguments") or "{}"},
            }
            if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls") and not messages[-1].get("content"):
                messages[-1]["tool_calls"].append(call)  # parallel calls share one assistant turn
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": _tool_output_text(item.get("output")),
            })
        # reasoning / item_reference / anything else has no chat equivalent: dropped

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    return messages


def responses_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Full translation of a Responses request body into a chat body."""
    body: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages_from_responses(payload),
        "stream": True,
    }
    tools = chat_tools_from_responses(payload.get("tools"))
    if tools:
        body["tools"] = tools
    choice = payload.get("tool_choice")
    if choice in ("auto", "none", "required"):
        body["tool_choice"] = choice
    for key in CHAT_PASSTHROUGH:
        if key in payload and payload[key] is not None:
            body[key] = payload[key]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        body["reasoning_effort"] = reasoning["effort"]
    if isinstance(payload.get("max_output_tokens"), int):
        body["max_tokens"] = payload["max_output_tokens"]
    return body


# -------------------------------------------------------------------- response


class ChatToResponsesConverter:
    """Turn streamed chat.completion chunks into Responses SSE events."""

    def __init__(self, model: str, response_id: str | None = None, keep_reasoning: bool = False) -> None:
        self.model = model
        self.response_id = response_id or ("resp_" + uuid.uuid4().hex)
        self.created_at = int(time.time())
        self.keep_reasoning = keep_reasoning
        self.seq = 0
        self.items: list[dict[str, Any]] = []       # final items, in output order
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self._text_item: dict[str, Any] | None = None
        self._tool_items: dict[int, dict[str, Any]] = {}
        self._reasoning_item: dict[str, Any] | None = None
        self._next_index = 0

    # -- helpers

    def _event(self, type_: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        return {"type": type_, "sequence_number": self.seq, **fields}

    def _snapshot(self, status: str) -> dict[str, Any]:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": list(self.items),
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _close_text(self) -> list[dict[str, Any]]:
        item = self._text_item
        if item is None:
            return []
        text = "".join(item["_text"])
        part = {"type": "output_text", "text": text, "annotations": []}
        item["content"] = [part]
        item["status"] = "completed"
        events = [
            self._event("response.output_text.done", item_id=item["id"], output_index=item["_index"],
                        content_index=0, text=text),
            self._event("response.content_part.done", item_id=item["id"], output_index=item["_index"],
                        content_index=0, part=part),
            self._event("response.output_item.done", output_index=item["_index"], item=self._public(item)),
        ]
        self.items.append(self._public(item))
        self._text_item = None
        return events

    def _close_tool(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item["status"] = "completed"
        events = [
            self._event("response.function_call_arguments.done", item_id=item["id"], output_index=item["_index"],
                        arguments=item["arguments"]),
            self._event("response.output_item.done", output_index=item["_index"], item=self._public(item)),
        ]
        self.items.append(self._public(item))
        return events

    def _open_text(self) -> list[dict[str, Any]]:
        index = self._next_index
        self._next_index += 1
        item = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "status": "in_progress",
                "role": "assistant", "content": [], "_text": [], "_index": index}
        self._text_item = item
        return [
            self._event("response.output_item.added", output_index=index,
                        item={"id": item["id"], "type": "message", "status": "in_progress",
                              "role": "assistant", "content": []}),
            self._event("response.content_part.added", item_id=item["id"], output_index=index, content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []}),
        ]

    # -- streaming API

    def start(self, tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        snapshot = self._snapshot("in_progress")
        if tools:
            snapshot["tools"] = tools
        return [
            self._event("response.created", response=snapshot),
            self._event("response.in_progress", response={"id": self.response_id, "object": "response",
                                                          "status": "in_progress"}),
        ]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning and self.keep_reasoning:
                events += self._feed_reasoning(reasoning)
            content = delta.get("content")
            if content:
                if self._text_item is None:
                    for item in list(self._tool_items.values()):
                        if item.get("status") == "in_progress":
                            events += self._close_tool(item)
                    self._tool_items.clear()
                    events += self._open_text()
                self._text_item["_text"].append(content)
                events.append(self._event("response.output_text.delta", item_id=self._text_item["id"],
                                          output_index=self._text_item["_index"], content_index=0, delta=content))
            for call in delta.get("tool_calls") or []:
                events += self._feed_tool(call)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def _feed_reasoning(self, text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self._reasoning_item is None:
            index = self._next_index
            self._next_index += 1
            self._reasoning_item = {"id": "rs_" + uuid.uuid4().hex[:24], "type": "reasoning", "status": "in_progress",
                                    "summary": [], "_text": [], "_index": index}
            events.append(self._event("response.output_item.added", output_index=index,
                                      item={"id": self._reasoning_item["id"], "type": "reasoning",
                                            "status": "in_progress", "summary": []}))
        self._reasoning_item["_text"].append(text)
        events.append(self._event("response.reasoning_summary_text.delta", item_id=self._reasoning_item["id"],
                                  output_index=self._reasoning_item["_index"], summary_index=0, delta=text))
        return events

    def _close_reasoning(self) -> list[dict[str, Any]]:
        item = self._reasoning_item
        if item is None:
            return []
        text = "".join(item["_text"])
        item["summary"] = [{"type": "summary_text", "text": text}]
        item["status"] = "completed"
        events = [
            self._event("response.reasoning_summary_text.done", item_id=item["id"], output_index=item["_index"],
                        summary_index=0, text=text),
            self._event("response.output_item.done", output_index=item["_index"], item=self._public(item)),
        ]
        self.items.append(self._public(item))
        self._reasoning_item = None
        return events

    def _feed_tool(self, call: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        index = call.get("index", 0)
        function = call.get("function") or {}
        item = self._tool_items.get(index)
        if item is None:
            events += self._close_reasoning()
            events += self._close_text()
            output_index = self._next_index
            self._next_index += 1
            item = {
                "id": "fc_" + uuid.uuid4().hex[:24],
                "type": "function_call",
                "status": "in_progress",
                "name": function.get("name") or "",
                "arguments": "",
                "call_id": call.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                "_index": output_index,
            }
            self._tool_items[index] = item
            events.append(self._event("response.output_item.added", output_index=output_index, item=self._public(item)))
        if function.get("name"):
            item["name"] = function["name"]
        arguments = function.get("arguments")
        if arguments:
            item["arguments"] += arguments
            events.append(self._event("response.function_call_arguments.delta", item_id=item["id"],
                                      output_index=item["_index"], delta=arguments))
        return events

    def finish(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        events += self._close_reasoning()
        events += self._close_text()
        for index in sorted(self._tool_items):
            item = self._tool_items[index]
            if item.get("status") != "completed":
                events += self._close_tool(item)
        self._tool_items.clear()
        response = self._snapshot("completed")
        response["usage"] = usage_to_responses(self.usage)
        if self.finish_reason:
            response["incomplete_details"] = None
        events.append(self._event("response.completed", response=response))
        return events

    # -- non-streaming API

    def result(self) -> dict[str, Any]:
        """Final response object (same shape the /v1/responses endpoint returns)."""
        response = self._snapshot("completed")
        response["usage"] = usage_to_responses(self.usage)
        return response


def usage_to_responses(usage: dict[str, Any] | None) -> dict[str, Any]:
    if not usage:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "input_tokens_details": {"cached_tokens": prompt_details.get("cached_tokens", 0)},
        "output_tokens": usage.get("completion_tokens"),
        "output_tokens_details": {"reasoning_tokens": completion_details.get("reasoning_tokens", 0)},
        "total_tokens": usage.get("total_tokens"),
    }
