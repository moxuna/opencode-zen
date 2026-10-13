"""Upstream request/response fixups required by the OpenCode free tier gate.

Measured upstream requirements (2026-10-13, see REQUIREMENTS.md):

  1. User-Agent must start with "opencode/"
  2. X-Opencode-Session must be a plausible client session id (counter < 64)
  3. body["stream"] must be exactly true
  4. body["tools"] must contain a tool named "read" and one named "bash"/"shell"

This module handles (3) and (4) and neutralises calls to the tools we injected,
so the client never sees a tool it did not declare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

NOTE_TEMPLATE = (
    "[zen-gateway] The model tried to call `{name}`, a stub tool this gateway injected to satisfy "
    "the upstream free-tier check; your client has no such tool, so the call was suppressed. "
    "Call one of your own tools instead{hint}."
)

# The upstream gate wants tools literally named `read` and `bash`/`shell`. Clients name their
# equivalents differently (Codex: exec_command, pi: bash, OpenCode: shell, ...), so instead of
# injecting a look-alike stub - which models happily call, breaking the client's tool loop - the
# gateway mirrors the client's own tool under the required name and renames calls back on the way out.
SHELL_TOOL_HINTS = ("bash", "shell", "sh", "exec", "exec_command", "run_command", "command",
                    "terminal", "powershell", "cmd")
READ_TOOL_HINTS = ("read", "read_file", "readfile", "view_file", "view", "cat", "file_read",
                   "open_file", "read_text_file")

REQUIRED_READ = "read"
REQUIRED_SHELL = "bash"


@dataclass(frozen=True)
class GatePlan:
    """What patch_body added to a request and how to translate the answers back."""

    injected: frozenset[str] = frozenset()          # tool names added to the request
    aliases: dict[str, str] = field(default_factory=dict)   # added name -> client tool name
    stubs: frozenset[str] = frozenset()             # added generic stubs (calls get neutralised)
    hint: str = ""                                  # text appended to the neutralisation note

    @property
    def changed(self) -> bool:
        return bool(self.injected)

STUB_READ: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read the contents of a file from the local filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path of the file to read."},
                "offset": {"type": "integer", "description": "First line to read (1-based)."},
                "limit": {"type": "integer", "description": "Maximum number of lines to read."},
            },
            "required": ["path"],
        },
    },
}

STUB_BASH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["command"],
        },
    },
}

# Responses API tool shape, used when the client talks to /v1/responses
STUB_READ_RESPONSES: dict[str, Any] = {
    "type": "function",
    "name": "read",
    "description": STUB_READ["function"]["description"],
    "parameters": STUB_READ["function"]["parameters"],
}
STUB_BASH_RESPONSES: dict[str, Any] = {
    "type": "function",
    "name": "bash",
    "description": STUB_BASH["function"]["description"],
    "parameters": STUB_BASH["function"]["parameters"],
}


def tool_name(tool: Any) -> str | None:
    """Tool name from either the chat shape ({function:{name}}) or the responses shape."""
    if not isinstance(tool, dict):
        return None
    inner = tool.get("function")
    if isinstance(inner, dict) and isinstance(inner.get("name"), str):
        return inner["name"]
    name = tool.get("name")
    return name if isinstance(name, str) else None


def tool_names(tools: Iterable[Any]) -> set[str]:
    return {name for name in (tool_name(tool) for tool in tools or []) if name}


def has_gate_tools(tools: Iterable[Any]) -> bool:
    names = tool_names(tools)
    return "read" in names and bool(names & {"bash", "shell"})


def find_client_tool(tools: list[Any], hints: tuple[str, ...], taken: set[str]) -> tuple[str, Any] | None:
    """Best candidate among the client's own tools for the given role."""
    candidates = [(tool_name(tool) or "", tool) for tool in tools]
    for hint in hints:  # exact match wins, in hint priority order
        for name, tool in candidates:
            if name == hint and name not in taken:
                return name, tool
    for hint in hints:  # then substring match
        for name, tool in candidates:
            if hint in name.lower() and name not in taken:
                return name, tool
    return None


def rename_tool(tool: Any, name: str, responses_api: bool) -> dict[str, Any]:
    """Copy a client tool definition under a different name (same schema)."""
    if responses_api or not isinstance(tool.get("function"), dict):
        renamed = dict(tool)
        renamed["name"] = name
        return renamed
    renamed = dict(tool)
    renamed["function"] = {**tool["function"], "name": name}
    return renamed


def plan_gate(tools: list[Any], responses_api: bool = False, mode: str = "mirror") -> GatePlan:
    """Decide what has to be added so the upstream free-tier gate is satisfied."""
    names = tool_names(tools)
    aliases: dict[str, str] = {}
    stubs: set[str] = set()
    mirrored: set[str] = set()

    if REQUIRED_READ not in names:
        found = find_client_tool(tools, READ_TOOL_HINTS, mirrored) if mode == "mirror" else None
        if found:
            aliases[REQUIRED_READ] = found[0]
            mirrored.add(found[0])
        else:
            stubs.add(REQUIRED_READ)
    if not names & {"bash", "shell"}:
        found = find_client_tool(tools, SHELL_TOOL_HINTS, mirrored) if mode == "mirror" else None
        if found:
            aliases[REQUIRED_SHELL] = found[0]
            mirrored.add(found[0])
        else:
            stubs.add(REQUIRED_SHELL)

    hint = ""
    if stubs:
        available = ", ".join(sorted(n for n in names if n)[:4])
        hint = " (%s)" % available if available else ""
    return GatePlan(injected=frozenset(set(aliases) | stubs), aliases=aliases, stubs=frozenset(stubs), hint=hint)


def patch_body(
    body: dict[str, Any],
    inject: bool = True,
    responses_api: bool = False,
    mode: str = "mirror",
) -> tuple[dict[str, Any], GatePlan]:
    """Return (patched body, gate plan).

    The upstream gate needs stream=true always, so a client asking for a
    non-streaming answer still gets a streamed upstream request that the caller
    aggregates afterwards.
    """
    patched = dict(body)
    patched["stream"] = True
    if not inject:
        return patched, GatePlan()

    tools = body.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    plan = plan_gate(tools, responses_api=responses_api, mode=mode)

    for required, client_name in plan.aliases.items():
        source = next((tool for tool in tools if tool_name(tool) == client_name), None)
        if source is not None:
            tools.append(rename_tool(source, required, responses_api))
    for stub_name in sorted(plan.stubs):
        if responses_api:
            tools.append(dict(STUB_READ_RESPONSES if stub_name == REQUIRED_READ else STUB_BASH_RESPONSES))
        else:
            tools.append(dict(STUB_READ if stub_name == REQUIRED_READ else STUB_BASH))

    if tools:
        patched["tools"] = tools
    return patched, plan


def as_plan(plan: GatePlan | frozenset[str] | set[str]) -> GatePlan:
    """Accept a plain set of injected names for backwards compatibility."""
    if isinstance(plan, GatePlan):
        return plan
    names = frozenset(plan)
    return GatePlan(injected=names, stubs=names)


def _content_delta(delta: dict[str, Any], note: str) -> None:
    existing = delta.get("content")
    delta["content"] = (existing or "") + note


# --------------------------------------------------------------------------- chat


class ChatRewriter:
    """Rewrite one streamed chat.completion chunk.

    Stateful: tracks tool-call indexes so streamed argument fragments can be
    attributed to the right tool name.
    """

    def __init__(self, plan: GatePlan | frozenset[str], mode: str, forward_usage: bool = True) -> None:
        self.plan = as_plan(plan)
        self.injected = self.plan.injected
        self.aliases = self.plan.aliases
        self.stubs = self.plan.stubs
        self.note_hint = self.plan.hint
        self.mode = mode
        self.forward_usage = forward_usage
        self.names: dict[int, str] = {}
        self.announced: set[int] = set()
        self.real_calls: set[int] = set()
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not self.forward_usage and not any(c.get("delta") or c.get("finish_reason") for c in choices):
                return None
        if not self.injected or self.mode == "passthrough":
            return chunk
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                kept: list[Any] = []
                notes: list[str] = []
                for call in calls:
                    index = call.get("index", 0)
                    function = call.get("function") or {}
                    name = function.get("name")
                    if isinstance(name, str) and name:
                        self.names[index] = name
                    current = self.names.get(index)
                    if current in self.aliases:
                        # mirrored client tool: hand the call back under the client's own name
                        if isinstance(call.get("function"), dict):
                            call["function"]["name"] = self.aliases[current]
                        self.real_calls.add(index)
                        kept.append(call)
                        continue
                    if current in self.stubs:
                        if index not in self.announced:
                            self.announced.add(index)
                            if self.mode == "text":
                                notes.append(NOTE_TEMPLATE.format(name=current, hint=self.note_hint))
                        continue
                    self.real_calls.add(index)
                    kept.append(call)
                if kept:
                    delta["tool_calls"] = kept
                else:
                    delta.pop("tool_calls", None)
                if notes:
                    _content_delta(delta, "".join(notes))
            # the final chunk carries finish_reason="tool_calls" with an empty delta:
            # downgrade it when the only tool calls were the stubs we removed
            if choice.get("finish_reason") == "tool_calls" and not self.real_calls:
                choice["finish_reason"] = "stop"
        return chunk


class ChatAggregator:
    """Collect rewritten chat chunks into a single non-streaming chat.completion."""

    def __init__(self) -> None:
        self.id: str | None = None
        self.created: int | None = None
        self.model: str | None = None
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.role = "assistant"
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.system_fingerprint: str | None = None

    def feed(self, chunk: dict[str, Any]) -> None:
        self.id = chunk.get("id") or self.id
        self.created = chunk.get("created") or self.created
        self.model = chunk.get("model") or self.model
        self.system_fingerprint = chunk.get("system_fingerprint") or self.system_fingerprint
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("role"), str):
                self.role = delta["role"]
            if isinstance(delta.get("content"), str):
                self.content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                self.reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                slot = self.tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                if call.get("type"):
                    slot["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] = function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def result(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": "".join(self.content) or None}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
            if message["content"] is None:
                message["content"] = ""
        result: dict[str, Any] = {
            "id": self.id or "chatcmpl-gateway",
            "object": "chat.completion",
            "created": self.created or 0,
            "model": self.model or "",
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason or "stop"}],
        }
        if self.system_fingerprint:
            result["system_fingerprint"] = self.system_fingerprint
        if self.usage:
            result["usage"] = self.usage
        return result


# ---------------------------------------------------------------------- responses


class ResponsesRewriter:
    """Rewrite streamed Responses API events, hiding calls to injected stub tools."""

    def __init__(self, plan: GatePlan | frozenset[str], mode: str) -> None:
        self.plan = as_plan(plan)
        self.injected = self.plan.injected
        self.aliases = self.plan.aliases
        self.stubs = self.plan.stubs
        self.mode = mode
        self.stub_items: set[str] = set()
        self.notes: list[str] = []

    def _note(self, item: dict[str, Any]) -> str:
        note = NOTE_TEMPLATE.format(name=item.get("name") or "stub", hint=self.plan.hint)
        self.notes.append(note)
        return note

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.injected or self.mode == "passthrough":
            return [event]
        etype = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None

        if etype == "response.output_item.added" and item and item.get("type") == "function_call":
            if item.get("name") in self.aliases:
                renamed = dict(event)
                renamed["item"] = {**item, "name": self.aliases[item["name"]]}
                return [renamed]
            if item.get("name") in self.stubs:
                item_id = item.get("id") or item.get("call_id")
                if item_id:
                    self.stub_items.add(item_id)
                if self.mode == "drop":
                    return []
                note = self._note(item)
                return [{
                    "type": "response.output_text.delta",
                    "sequence_number": event.get("sequence_number"),
                    "item_id": item_id or "stub",
                    "output_index": event.get("output_index", 0),
                    "content_index": 0,
                    "delta": note,
                }]

        if etype in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            if event.get("item_id") in self.stub_items:
                return []
            if etype == "response.function_call_arguments.done" and event.get("name") in self.aliases:
                return [{**event, "name": self.aliases[event["name"]]}]

        if etype == "response.output_item.done" and item:
            if item.get("type") == "function_call":
                if item.get("name") in self.aliases:
                    renamed = dict(event)
                    renamed["item"] = {**item, "name": self.aliases[item["name"]]}
                    return [renamed]
                if item.get("name") in self.stubs or item.get("id") in self.stub_items:
                    return []

        if etype == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                response["output"] = self._filter_output(response.get("output") or [])
        return [event]

    def _filter_output(self, output: list[Any]) -> list[Any]:
        kept: list[Any] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                kept.append(item)  # messages, reasoning, ... pass through untouched
                continue
            name = item.get("name")
            if name in self.aliases:
                kept.append({**item, "name": self.aliases[name]})  # mirrored client tool
                continue
            if name not in self.stubs:
                kept.append(item)  # the client's own tool call
                continue
            if self.mode == "text":
                kept.append({
                    "id": (item.get("id") or "msg") + ":stub-note",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text",
                                 "text": NOTE_TEMPLATE.format(name=name, hint=self.plan.hint),
                                 "annotations": []}],
                })
        return kept


def responses_completed(event_or_response: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the final response object from a response.completed event."""
    if event_or_response.get("type") == "response.completed":
        response = event_or_response.get("response")
        return response if isinstance(response, dict) else None
    if event_or_response.get("object") == "response":
        return event_or_response
    return None


def usage_from_response(response: dict[str, Any]) -> dict[str, Any] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
