"""FastAPI app: an OpenAI-compatible gateway in front of opencode.ai/zen/v1.

Clients authenticate with a key from the key file. Every upstream request is
rewritten so the OpenCode free tier accepts it (see app/gate.py).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .access import AccessLog, RateLimiter
from .config import is_free_model, settings
from .gate import (
    ChatAggregator,
    ChatRewriter,
    ResponsesRewriter,
    patch_body,
    responses_completed,
    usage_from_response,
)
from .keys import KeyStore
from .upstream import Upstream
from .zen_session import decode_counter, mint_session

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zen")

keystore = KeyStore(settings.key_file, settings.extra_keys)
upstream = Upstream(settings)
access_log = AccessLog(settings.access_log, settings.log_requests)
limiter = RateLimiter(settings.rate_limit_per_min)

allowlist: list[Any] = []
for entry in settings.ip_allowlist:
    try:
        allowlist.append(ipaddress.ip_network(entry, strict=False))
    except ValueError:
        log.warning("ignoring invalid IP_ALLOWLIST entry %r", entry)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type_ = type_
        self.param = param
        self.code = code

    def payload(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.type_, "param": self.param, "code": self.code}}


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        generated = keystore.ensure_file()
    except OSError as exc:
        raise RuntimeError(
            "Cannot create the key file at %s (%s).\n"
            "Fix it on the host with:  mkdir -p data && sudo chown -R 10001:10001 data\n"
            "or point KEY_FILE at a writable path." % (settings.key_file, exc)
        ) from exc
    if generated:
        log.warning("=" * 74)
        log.warning("Generated a new gateway API key and wrote it to %s", settings.key_file)
        log.warning("    %s", generated)
        log.warning("Point your clients at this gateway with:")
        log.warning("    Base URL : http://<your-vps>:<PORT>/v1")
        log.warning("    API key  : %s", generated)
        log.warning("Edit %s to add or replace keys (takes effect immediately).", settings.key_file)
        log.warning("=" * 74)
    keystore.load(force=True)
    log.info(
        "opencode-zen gateway %s ready -> %s (keys=%d, free_only=%s, inject_stub_tools=%s, stub_call_mode=%s)",
        __version__,
        settings.zen_base_url,
        keystore.count(),
        settings.enforce_free_only,
        settings.inject_stub_tools,
        settings.stub_call_mode,
    )
    yield
    await upstream.aclose()


app = FastAPI(title="OpenCode Zen Gateway", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.payload(), status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "Invalid request: %s" % exc.errors(), "type": "invalid_request_error", "param": None, "code": None}},
        status_code=400,
    )


# --------------------------------------------------------------------------- auth


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return header.strip()
    for name in ("x-api-key", "api-key"):
        value = request.headers.get(name)
        if value:
            return value.strip()
    return request.query_params.get("key")


async def authenticate(request: Request) -> str:
    ip = client_ip(request)
    if allowlist:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ApiError(403, "Cannot determine client address (%s) for the IP allowlist" % ip, type_="permission_error") from exc
        if not any(address in net for net in allowlist):
            raise ApiError(403, "Client IP %s is not allowed" % ip, type_="permission_error", code="ip_not_allowed")
    name = keystore.verify(extract_token(request))
    if not name:
        raise ApiError(
            401,
            "Invalid API key. Send 'Authorization: Bearer <key>' with a key from %s." % settings.key_file,
            type_="authentication_error",
            code="invalid_api_key",
        )
    wait = limiter.check(name)
    if wait:
        raise ApiError(
            429,
            "Rate limit exceeded for key '%s'; retry in about %ss" % (name, wait),
            type_="rate_limit_error",
            code="rate_limit_exceeded",
        )
    return name


# ------------------------------------------------------------------------ helpers


async def read_json(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
        raise ApiError(
            413,
            "Request body is larger than MAX_BODY_BYTES (%d bytes)" % settings.max_body_bytes,
            type_="invalid_request_error",
            code="body_too_large",
        )
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(400, "Request body must be valid JSON", code="invalid_json") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return body


def guard_model(body: dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ApiError(400, "Missing required parameter: 'model'", param="model", code="missing_model")
    if settings.enforce_free_only and not is_free_model(model):
        raise ApiError(
            400,
            "Model '%s' is not part of the free tier. Set OPENCODE_API_KEY in .env to unlock paid "
            "models, or set FREE_ONLY=false." % model,
            param="model",
            code="model_not_free",
        )
    return model


def meta_from(request: Request, key: str, model: str, stream: bool, path: str, injected: frozenset[str]) -> dict[str, Any]:
    return {
        "path": path,
        "key": key,
        "model": model,
        "stream": stream,
        "ip": client_ip(request),
        "injected_tools": sorted(injected),
        "started": time.time(),
    }


def finish_meta(meta: dict[str, Any], usage: dict[str, Any] | None, status: int) -> None:
    record = {k: v for k, v in meta.items() if k != "started"}
    record["status"] = status
    record["duration_ms"] = int((time.time() - meta["started"]) * 1000)
    if usage:
        record["prompt_tokens"] = usage.get("prompt_tokens")
        record["completion_tokens"] = usage.get("completion_tokens")
        record["total_tokens"] = usage.get("total_tokens")
    access_log.write(**record)


async def upstream_post(path: str, key: str, body: dict[str, Any]):
    """Open a streamed upstream request; raise ApiError on transport failures."""
    request = upstream.client.build_request("POST", path, json=body, headers=upstream.headers(key, stream=True))
    try:
        response = await upstream.client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise ApiError(502, "Upstream request failed: %s" % exc, type_="api_error", code="upstream_unreachable") from exc
    if response.status_code >= 400:
        payload = await response.aread()
        await response.aclose()
        return None, Response(
            content=payload,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )
    return response, None


# --------------------------------------------------------------------------- chat


async def chat_sse(response: httpx.Response, rewriter: ChatRewriter, meta: dict[str, Any]) -> AsyncIterator[str]:
    status = 200
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                yield "data: [DONE]\n\n"
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if isinstance(chunk.get("error"), (dict, str)):
                # some providers report mid-stream failures as an error payload
                status = 502
                yield "data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)
                continue
            rewritten = rewriter.feed(chunk)
            if rewritten is None:
                continue
            yield "data: %s\n\n" % json.dumps(rewritten, ensure_ascii=False)
    except httpx.HTTPError as exc:
        status = 502
        yield "data: %s\n\n" % json.dumps({"error": {"message": "Upstream stream failed: %s" % exc, "type": "api_error"}})
    finally:
        with suppress(Exception):
            await response.aclose()
        finish_meta(meta, rewriter.usage, status)


async def chat_json(response: httpx.Response, rewriter: ChatRewriter, meta: dict[str, Any]) -> JSONResponse:
    aggregator = ChatAggregator()
    consumed = 0
    try:
        async for line in response.aiter_lines():
            consumed += len(line.encode("utf-8", "replace"))
            if consumed > settings.max_aggregate_bytes:
                finish_meta(meta, rewriter.usage, 502)
                raise ApiError(
                    502,
                    "Upstream response exceeded MAX_AGGREGATE_BYTES (%d). Use stream=true or raise the limit."
                    % settings.max_aggregate_bytes,
                    type_="api_error",
                    code="aggregate_too_large",
                )
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if isinstance(chunk.get("error"), (dict, str)):
                error = chunk["error"]
                message = error.get("message") if isinstance(error, dict) else str(error)
                finish_meta(meta, rewriter.usage, 502)
                raise ApiError(502, "Upstream error: %s" % message, type_="api_error", code="upstream_error")
            rewritten = rewriter.feed(chunk)
            if rewritten is None:
                continue
            aggregator.feed(rewritten)
    except httpx.HTTPError as exc:
        finish_meta(meta, rewriter.usage, 502)
        raise ApiError(502, "Upstream stream failed: %s" % exc, type_="api_error") from exc
    finally:
        with suppress(Exception):
            await response.aclose()
    result = aggregator.result()
    finish_meta(meta, aggregator.usage or rewriter.usage, 200)
    return JSONResponse(result)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, key: str = Depends(authenticate)) -> Response:
    body = await read_json(request)
    model = guard_model(body)
    want_stream = body.get("stream") is True
    stream_options = body.get("stream_options")
    wants_usage = bool(stream_options.get("include_usage")) if isinstance(stream_options, dict) else False

    patched, injected = patch_body(body, inject=settings.inject_stub_tools, responses_api=False)
    merged_options = dict(stream_options) if isinstance(stream_options, dict) else {}
    merged_options["include_usage"] = True  # lets the gateway log token usage
    patched["stream_options"] = merged_options

    meta = meta_from(request, key, model, want_stream, "/v1/chat/completions", injected)
    response, error_response = await upstream_post("/chat/completions", key, patched)
    if error_response is not None:
        finish_meta(meta, None, error_response.status_code)
        return error_response

    # a non-streaming client gets one aggregated object, so usage must be kept;
    # a streaming client only receives it when it asked for include_usage
    rewriter = ChatRewriter(injected, settings.stub_call_mode, forward_usage=wants_usage or not want_stream)
    if want_stream:
        return StreamingResponse(
            chat_sse(response, rewriter, meta),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no", "connection": "keep-alive"},
        )
    return await chat_json(response, rewriter, meta)


# ---------------------------------------------------------------------- responses


async def responses_sse(response: httpx.Response, rewriter: ResponsesRewriter, meta: dict[str, Any]) -> AsyncIterator[str]:
    status = 200
    final: dict[str, Any] | None = None
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if event.get("type") == "error":
                status = 502
                yield "event: error\ndata: %s\n\n" % json.dumps(event, ensure_ascii=False)
                continue
            for rewritten in rewriter.feed(event):
                final = responses_completed(rewritten) or final
                yield "event: %s\ndata: %s\n\n" % (
                    rewritten.get("type", "message"),
                    json.dumps(rewritten, ensure_ascii=False),
                )
    except httpx.HTTPError as exc:
        status = 502
        yield "event: error\ndata: %s\n\n" % json.dumps({"type": "error", "message": "Upstream stream failed: %s" % exc})
    finally:
        with suppress(Exception):
            await response.aclose()
        finish_meta(meta, usage_from_response(final) if final else None, status)


async def responses_json(response: httpx.Response, rewriter: ResponsesRewriter, meta: dict[str, Any]) -> JSONResponse:
    final: dict[str, Any] | None = None
    consumed = 0
    try:
        async for line in response.aiter_lines():
            consumed += len(line.encode("utf-8", "replace"))
            if consumed > settings.max_aggregate_bytes:
                raise ApiError(
                    502,
                    "Upstream response exceeded MAX_AGGREGATE_BYTES (%d). Use stream=true or raise the limit."
                    % settings.max_aggregate_bytes,
                    type_="api_error",
                    code="aggregate_too_large",
                )
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if event.get("type") in ("error", "response.failed"):
                error = event.get("error") or event.get("response", {}).get("error") or event
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise ApiError(502, "Upstream error: %s" % message, type_="api_error", code="upstream_error")
            for rewritten in rewriter.feed(event):
                final = responses_completed(rewritten) or final
    except httpx.HTTPError as exc:
        finish_meta(meta, None, 502)
        raise ApiError(502, "Upstream stream failed: %s" % exc, type_="api_error") from exc
    except ApiError:
        finish_meta(meta, None, 502)
        raise
    finally:
        with suppress(Exception):
            await response.aclose()
    if final is None:
        finish_meta(meta, None, 502)
        raise ApiError(502, "Upstream did not return a completed response", type_="api_error")
    finish_meta(meta, usage_from_response(final), 200)
    return JSONResponse(final)


@app.post("/v1/responses")
async def create_response(request: Request, key: str = Depends(authenticate)) -> Response:
    body = await read_json(request)
    model = guard_model(body)
    want_stream = body.get("stream") is True

    patched, injected = patch_body(body, inject=settings.inject_stub_tools, responses_api=True)
    meta = meta_from(request, key, model, want_stream, "/v1/responses", injected)
    response, error_response = await upstream_post("/responses", key, patched)
    if error_response is not None:
        finish_meta(meta, None, error_response.status_code)
        return error_response

    rewriter = ResponsesRewriter(injected, settings.stub_call_mode)
    if want_stream:
        return StreamingResponse(
            responses_sse(response, rewriter, meta),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no", "connection": "keep-alive"},
        )
    return await responses_json(response, rewriter, meta)


# -------------------------------------------------------------------------- misc


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "opencode-zen-gateway",
        "version": __version__,
        "upstream": settings.zen_base_url,
        "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/responses", "/healthz", "/readyz"],
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    session = mint_session("readyz")
    details: dict[str, Any] = {
        "upstream": settings.zen_base_url,
        "keys": keystore.count(),
        "session_counter": decode_counter(session),
        "free_only": settings.enforce_free_only,
    }
    try:
        status, payload = await upstream.get_json("/models", "readyz")
    except httpx.HTTPError as exc:
        details["upstream_status"] = "error"
        details["error"] = str(exc)
        return JSONResponse({"status": "unavailable", **details}, status_code=503)
    models = payload.get("data") if isinstance(payload, dict) else None
    details["upstream_status"] = status
    details["models"] = len(models) if isinstance(models, list) else None
    ready = status == 200 and isinstance(models, list) and len(models) > 0
    return JSONResponse({"status": "ready" if ready else "unavailable", **details}, status_code=200 if ready else 503)


@app.get("/v1/models")
async def list_models(key: str = Depends(authenticate)) -> Response:
    try:
        status, payload = await upstream.get_json("/models", key)
    except httpx.HTTPError as exc:
        raise ApiError(502, "Upstream request failed: %s" % exc, type_="api_error", code="upstream_unreachable") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(data, list):
        return JSONResponse(payload, status_code=status if status >= 400 else 502)
    if settings.enforce_free_only:
        data = [item for item in data if isinstance(item, dict) and is_free_model(str(item.get("id") or ""))]
    return JSONResponse({"object": "list", "data": data})


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, key: str = Depends(authenticate)) -> Response:
    try:
        status, payload = await upstream.get_json("/models", key)
    except httpx.HTTPError as exc:
        raise ApiError(502, "Upstream request failed: %s" % exc, type_="api_error", code="upstream_unreachable") from exc
    data = payload.get("data") if isinstance(payload, dict) else []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("id") == model_id:
            if settings.enforce_free_only and not is_free_model(model_id):
                break
            return JSONResponse(item)
    raise ApiError(404, "Model '%s' not found" % model_id, type_="not_found_error", code="model_not_found")
