"""FastAPI application exposing OpenAI- and Anthropic-compatible endpoints
backed by a personal GitHub Copilot subscription, plus a management portal.

Endpoints
---------
OpenAI-compatible (for Codex, OpenAI SDK, curl):
    GET  /v1/models
    POST /v1/chat/completions      (stream + non-stream)
    POST /v1/responses             (stream + non-stream)

Anthropic-compatible (for Claude Code, Anthropic SDK):
    POST /v1/messages              (stream + non-stream)

Management portal API:
    GET  /api/status
    POST /api/auth/device/start
    POST /api/auth/device/poll
    POST /api/auth/logout
    GET  /api/models
    GET  /api/usage
    GET  /api/usage/recent
    GET/POST /api/keys, DELETE /api/keys/{key}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import httpx

from . import anthropic_adapter as aa
from . import copilot_client as cc
from . import image_client as ic
from . import store
from .config import get_settings

log = logging.getLogger("hub")
app = FastAPI(title="GitModel Hub", version="0.1.0")
_STATIC = Path(__file__).parent / "static"

# In-memory admin sessions: token -> expiry epoch. Cleared on restart.
_SESSIONS: dict[str, float] = {}
_SESSION_TTL = 12 * 3600  # 12h

# In-memory admin login throttle: client IP -> {"fails": int, "until": epoch}.
# Cleared on restart. Thresholds come from settings (env-configurable).
_LOGIN_ATTEMPTS: dict[str, dict[str, float]] = {}


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    store.ensure_admin_defaults()


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def _extract_client_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xkey = request.headers.get("x-api-key")
    if xkey:
        return xkey.strip()
    return None


def _check_client_auth(request: Request) -> str | None:
    """Return the client key (for usage attribution); enforce auth if required."""
    key = _extract_client_key(request)
    s = get_settings()
    if store.get_require_auth(s.require_auth):
        if not key or not store.is_valid_api_key(key):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key


def _new_session() -> str:
    import secrets
    import time

    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    import time

    if not token:
        return False
    exp = _SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        _SESSIONS.pop(token, None)
        return False
    return True


def _check_admin(x_admin_token: str | None) -> None:
    """Require a valid admin session token (or the env override token)."""
    env_token = get_settings().admin_token
    if env_token and (x_admin_token or "") == env_token:
        return
    if _valid_session(x_admin_token):
        return
    raise HTTPException(status_code=401, detail="Admin login required")


# --------------------------------------------------------------------------- #
# Admin login brute-force throttle (per client IP, in-memory)
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str:
    """Best-effort real client IP, honoring the proxy's X-Forwarded-For.

    Azure Container Apps (and most reverse proxies) put the original client
    address first in X-Forwarded-For; request.client.host is the proxy.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_check_locked(ip: str) -> None:
    """Raise 429 if this IP is currently locked out."""
    import time

    rec = _LOGIN_ATTEMPTS.get(ip)
    if rec and rec.get("until", 0) > time.time():
        wait = int(rec["until"] - time.time())
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试",
        )


def _login_fail(ip: str) -> None:
    """Record one failed attempt; lock the IP once the threshold is hit."""
    import time

    s = get_settings()
    if s.login_max_fails <= 0:  # throttling disabled
        return
    rec = _LOGIN_ATTEMPTS.get(ip) or {"fails": 0.0, "until": 0.0}
    rec["fails"] = rec.get("fails", 0) + 1
    if rec["fails"] >= s.login_max_fails:
        rec["until"] = time.time() + s.login_lock_seconds
        rec["fails"] = 0.0  # reset counter; re-counts after the lock expires
    _LOGIN_ATTEMPTS[ip] = rec


def _login_success(ip: str) -> None:
    """Clear an IP's failure record on a successful login."""
    _LOGIN_ATTEMPTS.pop(ip, None)


def _copilot_usage(obj: dict[str, Any] | None) -> tuple[int, dict[str, int]]:
    """Extract Copilot's authoritative billing block.

    Returns ``(total_nano_aiu, {token_type: count})``. Copilot attaches a
    ``copilot_usage`` block to the OpenAI, Responses and Anthropic shapes
    alike, on buffered responses and once per stream. ``total_nano_aiu`` is its
    own computed charge (verified to equal the sum of count x rate on every
    model tested), so we take it verbatim rather than recomputing.

    ``token_details`` is variable-length -- do not assume a fixed set of
    buckets. Most models report four (input / output / cache_read /
    cache_write), but gpt-5.5, grok-4.5 and mai-code-1-flash-picker report
    three, omitting cache_write entirely. Reading by key, as below, degrades
    correctly: an absent bucket simply reads as 0.

    Some models report an all-zero rate -- they bill against the flat Copilot
    premium-request quota rather than per token -- and come back as 0 nano_aiu,
    so the caller falls back to the local pricing table. This is not a
    vendor-wide property: among Azure OpenAI models gpt-4o reports zero while
    gpt-4 reports a real rate, so never infer it from the model name.
    """
    if not isinstance(obj, dict):
        return 0, {}
    cu = obj.get("copilot_usage")
    if not isinstance(cu, dict):
        return 0, {}
    counts: dict[str, int] = {}
    for d in cu.get("token_details") or []:
        if isinstance(d, dict) and d.get("token_type"):
            counts[str(d["token_type"])] = int(d.get("token_count") or 0)
    try:
        nano = int(cu.get("total_nano_aiu") or 0)
    except (TypeError, ValueError):
        nano = 0
    return nano, counts


def _norm_usage(usage: dict[str, Any] | None, *, responses_shape: bool) -> tuple[int, int, int, int]:
    if not usage:
        return 0, 0, 0, 0
    if responses_shape:
        i = usage.get("input_tokens", 0)
        o = usage.get("output_tokens", 0)
        t = usage.get("total_tokens", i + o)
        # Responses API nests cache info under input_tokens_details.
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    else:
        i = usage.get("prompt_tokens", 0)
        o = usage.get("completion_tokens", 0)
        t = usage.get("total_tokens", i + o)
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    return i, o, t, cached


def _passthrough_status(upstream: int) -> int:
    """Map an upstream status onto one that means the same thing to our client.

    Client-side 4xx pass through unchanged: a rejected parameter is the
    caller's to fix, and relabelling it 502 both hides the cause and invites a
    retry that cannot succeed. The exceptions are 401/403, which mean *our*
    Copilot credentials were rejected -- the caller's key was already accepted
    at the door, so echoing 401 would send them to audit a key that is fine.
    Upstream 5xx stay 502: the failure is above us, not in us.
    """
    if upstream in (401, 403) or upstream >= 500:
        return 502
    return upstream


def _record(
    *, client_key, model, served, endpoint, usage3, streamed, estimated,
    status=200, cached=0, cache_write=0, nano_aiu=0,
) -> None:
    i, o, t = usage3
    store.record_usage(
        api_key=client_key,
        model=model,
        served_model=served,
        endpoint=endpoint,
        input_tokens=i,
        output_tokens=o,
        total_tokens=t,
        streamed=streamed,
        estimated=estimated,
        status=status,
        cached_tokens=cached,
        cache_write_tokens=cache_write,
        nano_aiu=nano_aiu,
    )


def _parse_sse_usage(
    text: str, *, responses_shape: bool
) -> tuple[dict[str, Any] | None, int, dict[str, int]]:
    """Scrape the final usage and Copilot billing block out of an SSE stream.

    Returns ``(usage, nano_aiu, copilot_counts)``. Copilot emits its
    ``copilot_usage`` block exactly once per stream, carrying final totals
    (not increments), so last-one-wins is safe and cannot double-count.
    """
    usage: dict[str, Any] | None = None
    nano = 0
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        n, c = _copilot_usage(obj)
        if n or c:
            nano, counts = n, c
        if responses_shape:
            resp = obj.get("response") if isinstance(obj, dict) else None
            cand = (resp or {}).get("usage") if isinstance(resp, dict) else None
            cand = cand or obj.get("usage")
        else:
            cand = obj.get("usage")
        if cand:
            usage = cand
    return usage, nano, counts


# =========================================================================== #
# OpenAI-compatible endpoints
# =========================================================================== #
def _openai_models_payload(models: list[dict[str, Any]]) -> dict[str, Any]:
    data = []
    for m in models:
        data.append(
            {
                "id": m.get("id"),
                "object": "model",
                "created": 0,
                "owned_by": m.get("vendor", "github-copilot"),
            }
        )
    return {"object": "list", "data": data}


@app.get("/v1/models")
async def v1_models(request: Request) -> JSONResponse:
    _check_client_auth(request)
    try:
        models = await cc.list_models()
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except httpx.HTTPStatusError as e:
        log.error("list_models failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned {e.response.status_code} for the model list.",
        )
    except RuntimeError as e:
        log.error("list_models failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    payload = _openai_models_payload(models)
    if ic.is_configured():
        payload["data"].append(
            {
                "id": ic.get_model(),
                "object": "model",
                "created": 0,
                "owned_by": "azure-openai",
            }
        )
    return JSONResponse(payload)


async def _passthrough(
    request: Request, path: str, *, responses_shape: bool
) -> Any:
    client_key = _check_client_auth(request)
    body = await request.json()
    model = body.get("model")
    stream = bool(body.get("stream"))
    endpoint = path.strip("/").replace("/", ".")

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    vision_headers = (
        {"Copilot-Vision-Request": "true"}
        if aa.has_image_content(body)
        else None
    )

    if not stream:
        try:
            status, data = await cc.post_json(path, body, vision_headers)
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if status != 200:
            _record(client_key=client_key, model=model, served=None,
                    endpoint=endpoint, usage3=(0, 0, 0), streamed=False,
                    estimated=False, status=status)
            return JSONResponse(data, status_code=status)
        served = data.get("model") if isinstance(data, dict) else None
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=responses_shape)
        nano, counts = _copilot_usage(data)
        _record(client_key=client_key, model=model, served=served,
                endpoint=endpoint, usage3=(i, o, t), streamed=False,
                estimated=False, cached=cached or counts.get("cache_read", 0),
                cache_write=counts.get("cache_write", 0), nano_aiu=nano)
        return JSONResponse(data)

    # Streaming passthrough. Ask the backend to report usage for chat.
    if not responses_shape:
        body.setdefault("stream_options", {})
        if isinstance(body["stream_options"], dict):
            body["stream_options"].setdefault("include_usage", True)
    est_input = aa.estimate_prompt_tokens(body)

    # Connect up front so failures are real HTTP errors, not a truncated
    # chunked body. See the matching comment in v1_messages.
    try:
        chunks, _ = await cc.open_stream(path, body, vision_headers)
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except cc.UpstreamError as e:
        # Mirror the upstream failure the same way the non-streaming branch
        # above does. Collapsing it into a 502 destroys the one thing that
        # explains the request: a rejected parameter then reads as "the
        # gateway is broken" instead of "this model does not take that
        # field", and invites a retry that cannot succeed.
        log.error("upstream stream failed path=%s model=%s: %s", path, model, e)
        status = _passthrough_status(e.status_code)
        _record(client_key=client_key, model=model, served=None,
                endpoint=endpoint, usage3=(0, 0, 0), streamed=True,
                estimated=False, status=status)
        return JSONResponse(e.body, status_code=status)
    except RuntimeError as e:
        # Not the upstream's verdict -- a token exchange or transport failure
        # on our side, which is what 502 actually describes.
        log.error("stream setup failed path=%s model=%s: %s", path, model, e)
        _record(client_key=client_key, model=model, served=None,
                endpoint=endpoint, usage3=(0, 0, 0), streamed=True,
                estimated=False, status=502)
        raise HTTPException(status_code=502, detail=str(e))

    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
        try:
            async for chunk in chunks:
                collected.append(chunk.decode("utf-8", "replace"))
                yield chunk
        finally:
            usage, nano, counts = _parse_sse_usage(
                "".join(collected), responses_shape=responses_shape
            )
            i, o, t, cached = _norm_usage(usage, responses_shape=responses_shape)
            # Copilot's own counts are authoritative when present.
            if counts.get("input"):
                i = counts["input"]
            if counts.get("output"):
                o = counts["output"]
            cached = cached or counts.get("cache_read", 0)
            t = t or (i + o)
            # Backend often omits prompt_tokens on streams — estimate input.
            input_estimated = not i
            if input_estimated:
                i = est_input
                t = i + o
            _record(client_key=client_key, model=model, served=None,
                    endpoint=endpoint, usage3=(i, o, t), streamed=True,
                    estimated=(usage is None and not nano) or input_estimated,
                    cached=cached, cache_write=counts.get("cache_write", 0),
                    nano_aiu=nano)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request) -> Any:
    return await _passthrough(request, "/chat/completions", responses_shape=False)


@app.post("/v1/responses")
async def v1_responses(request: Request) -> Any:
    return await _passthrough(request, "/responses", responses_shape=True)


# =========================================================================== #
# Image generation endpoints (Azure OpenAI gpt-image backend)
# =========================================================================== #
def _record_image(client_key, model, endpoint, data, status) -> None:
    """Record a usage row for an image request, reusing the Responses-shaped
    usage parser (Azure image ``usage`` matches that shape)."""
    if status == 200 and isinstance(data, dict):
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=True)
        served = data.get("model")
    else:
        i, o, t, cached, served = 0, 0, 0, 0, None
    _record(client_key=client_key, model=model, served=served,
            endpoint=endpoint, usage3=(i, o, t), streamed=False,
            estimated=False, status=status, cached=cached)


@app.post("/v1/images/generations")
async def v1_images_generations(request: Request) -> Any:
    client_key = _check_client_auth(request)
    body = await request.json()
    if not ic.is_configured():
        raise HTTPException(
            status_code=503, detail="Image backend not configured"
        )
    model = body.get("model") or ic.get_model()
    status, data = await ic.generate(body)
    _record_image(client_key, model, "images.generations", data, status)
    return JSONResponse(data, status_code=status)


@app.post("/v1/images/edits")
async def v1_images_edits(request: Request) -> Any:
    client_key = _check_client_auth(request)
    if not ic.is_configured():
        raise HTTPException(
            status_code=503, detail="Image backend not configured"
        )
    form = await request.form()
    data: dict[str, Any] = {}
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for field, value in form.multi_items():
        if hasattr(value, "read"):  # an UploadFile
            content = await value.read()
            files.append(
                (
                    field,
                    (
                        value.filename or field,
                        content,
                        value.content_type or "application/octet-stream",
                    ),
                )
            )
        else:
            data[field] = value
    model = data.get("model") or ic.get_model()
    status, resp = await ic.edit(data, files)
    _record_image(client_key, model, "images.edits", resp, status)
    return JSONResponse(resp, status_code=status)


# =========================================================================== #
# Anthropic-compatible endpoint
# =========================================================================== #
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(request: Request) -> Any:
    """Anthropic token-counting endpoint — passed through to Copilot.

    Claude Code calls this to size the context before a turn. It is optional:
    with no route here the client falls back to estimating locally, which is
    how this gateway ran until now and worked fine. Serving it replaces that
    estimate with the upstream's own model-specific count.

    Declared before /v1/messages purely for readability — Starlette matches on
    the full path, so the more specific route does not need to come first.

    No usage row is recorded: counting is free and billed nothing upstream, so
    logging it would inflate the request count with rows that have no tokens.
    """
    _check_client_auth(request)
    body = await request.json()
    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    mapped = aa.map_model(body.get("model"))
    if mapped != body.get("model"):
        body = {**body, "model": mapped}
    beta_in = request.headers.get("anthropic-beta")
    body_original = body
    body, headers = _prepare_upstream(body, beta_in)
    try:
        status, data = await cc.post_json("/v1/messages/count_tokens", body, headers)
        if status != 200:
            retry = _beta_retry(body_original, beta_in, json.dumps(data))
            if retry:
                body, headers = retry
                status, data = await cc.post_json(
                    "/v1/messages/count_tokens", body, headers)
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        # A failed token exchange raises rather than returning a status, and an
        # unhandled one here becomes an opaque 500. Surface the upstream reason
        # so a client sees "Bad credentials" instead of Internal Server Error.
        raise HTTPException(status_code=502, detail=str(e))
    return JSONResponse(data, status_code=status)


def _beta_retry(
    req_original: dict[str, Any], beta_in: str | None, upstream_error: str,
) -> tuple[dict[str, Any], dict[str, str] | None] | None:
    """Payload and headers to retry with after a beta refusal, or None.

    The upstream names the betas it will not take, so a refusal is a fact to
    record rather than a guess to make. Returns None unless this particular
    error named at least one beta we had not already learned -- which keeps an
    ordinary 400 (bad model, oversized request) from triggering a pointless
    second attempt, and stops a repeat refusal from looping.

    Rebuilding the payload matters as much as the header: if the refused beta
    was the one gating a field, that field has to come out too, or the retry
    fails exactly as the first attempt did.
    """
    if not aa.learn_refused_betas(upstream_error):
        return None
    log.info("upstream refused beta(s); retrying without: %s",
             ", ".join(sorted(aa.refused_betas())))
    return _prepare_upstream(req_original, beta_in)


def _prepare_upstream(
    req: dict[str, Any], beta_in: str | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Body and headers to send upstream for an Anthropic-shaped request.

    Field stripping keys off the header we will actually send, not the one the
    caller sent: a beta we have learned is refused gets filtered out, and any
    field it gates has to go with it or the request is rejected anyway.
    """
    beta_out = aa.forwardable_betas(beta_in)
    body, dropped = aa.strip_unsupported(req, beta_out)
    if dropped:
        log.info("dropped unsupported fields for upstream: %s", ", ".join(dropped))
    headers: dict[str, str] = {}
    if beta_out:
        headers["anthropic-beta"] = beta_out
    if aa.has_image_content_anthropic(body):
        headers["Copilot-Vision-Request"] = "true"
    return body, (headers or None)


@app.post("/v1/messages")
async def v1_messages(request: Request) -> Any:
    """Anthropic Messages API — passed straight through to Copilot.

    Copilot serves the Anthropic shape natively at ``/v1/messages`` (verified:
    native content blocks, real stop_reason, and the full SSE event sequence),
    so no translation happens here. Only the model name is mapped, since the
    backend knows its own slugs rather than the official dated model IDs.
    """
    client_key = _check_client_auth(request)
    req = await request.json()
    model = req.get("model")
    stream = bool(req.get("stream"))

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    mapped = aa.map_model(model)
    if mapped != model:
        req = {**req, "model": mapped}

    # Passthrough forwards the body verbatim, so fields and betas the backend
    # will not take have to be filtered here or they fail the whole request.
    beta_in = request.headers.get("anthropic-beta")
    req_original = req
    req, vision_headers = _prepare_upstream(req, beta_in)

    if not stream:
        try:
            status, data = await cc.post_json("/v1/messages", req, vision_headers)
            if status != 200:
                retry = _beta_retry(req_original, beta_in, json.dumps(data))
                if retry:
                    req, vision_headers = retry
                    status, data = await cc.post_json(
                        "/v1/messages", req, vision_headers)
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if status != 200:
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(0, 0, 0), streamed=False,
                    estimated=False, status=status)
            return JSONResponse(data, status_code=status)
        u = data.get("usage") or {}
        i = u.get("input_tokens", 0)
        o = u.get("output_tokens", 0)
        cached = u.get("cache_read_input_tokens", 0)
        cache_write = u.get("cache_creation_input_tokens", 0)
        nano, counts = _copilot_usage(data)
        # Anthropic's input_tokens excludes both cache buckets, so i + o would
        # count almost nothing once prompt caching is on -- the context lives
        # in cache_read/cache_write. Summing all four matches what the OpenAI
        # path already stores in `total`, whose upstream figure is inclusive.
        _record(client_key=client_key, model=model, served=data.get("model"),
                endpoint="messages", usage3=(i, o, i + cached + cache_write + o),
                streamed=False, estimated=False,
                cached=cached, cache_write=cache_write,
                nano_aiu=nano)
        return JSONResponse(data)

    # Connect before returning a StreamingResponse. Once that response starts,
    # the 200 and the chunked header are already on the wire and any error can
    # only truncate the body -- which strict clients (Node undici, Python
    # http.client) reject as a protocol error rather than surfacing the real
    # cause. Failing here instead keeps a bad token or a 5xx upstream an
    # ordinary, readable HTTP error.
    try:
        chunks, _ = await cc.open_stream("/v1/messages", req, vision_headers)
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except cc.UpstreamError as e:
        # A refused beta is recoverable and worth one more attempt: the
        # upstream said which ones, so the retry is a corrected request rather
        # than a repeat. Any other failure falls through to be reported.
        retry = _beta_retry(req_original, beta_in, e.raw)
        if retry:
            req, vision_headers = retry
            try:
                chunks, _ = await cc.open_stream("/v1/messages", req, vision_headers)
            except cc.UpstreamError as e2:
                e = e2
                retry = None
            except cc.NotAuthenticatedError as e2:
                raise HTTPException(status_code=503, detail=str(e2))
        if not retry:
            # Log the upstream's own message (model not supported, payload
            # rejected, quota, ...) -- it is the only clue to why a request
            # failed, and it would otherwise appear nowhere in the server log.
            # Include the model so a failure specific to one model is visible
            # without reproducing it. Then hand the client the upstream status
            # and body verbatim, exactly as the non-streaming branch does: a
            # 400 for an unsupported parameter has to reach the caller as a
            # 400, or the only actionable part of the answer is lost.
            log.error("upstream stream failed model=%s: %s", req.get("model"), e)
            status = _passthrough_status(e.status_code)
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(0, 0, 0), streamed=True,
                    estimated=False, status=status)
            return JSONResponse(e.body, status_code=status)
    except RuntimeError as e:
        # Not the upstream's verdict -- a token exchange or transport failure
        # on our side, which is what 502 actually describes.
        log.error("stream setup failed model=%s: %s", req.get("model"), e)
        _record(client_key=client_key, model=model, served=None,
                endpoint="messages", usage3=(0, 0, 0), streamed=True,
                estimated=False, status=502)
        raise HTTPException(status_code=502, detail=str(e))

    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
        try:
            async for chunk in chunks:
                collected.append(chunk.decode("utf-8", "replace"))
                yield chunk
        finally:
            text = "".join(collected)
            nano, counts = 0, {}
            i = o = cached = cache_write = 0
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                n, c = _copilot_usage(obj)
                if n or c:
                    nano, counts = n, c
                # usage appears on message_start (input) and message_delta
                # (output); merge rather than overwrite.
                for src in (obj.get("usage"), (obj.get("message") or {}).get("usage")
                            if isinstance(obj.get("message"), dict) else None):
                    if not isinstance(src, dict):
                        continue
                    i = src.get("input_tokens", i) or i
                    o = src.get("output_tokens", o) or o
                    cached = src.get("cache_read_input_tokens", cached) or cached
                    cache_write = (
                        src.get("cache_creation_input_tokens", cache_write) or cache_write
                    )
            # Copilot's own counts are authoritative when present.
            i = counts.get("input") or i
            o = counts.get("output") or o
            cached = counts.get("cache_read") or cached
            cache_write = counts.get("cache_write") or cache_write
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages",
                    usage3=(i, o, i + cached + cache_write + o),
                    streamed=True, estimated=not (i or nano),
                    cached=cached, cache_write=cache_write, nano_aiu=nano)

    return StreamingResponse(gen(), media_type="text/event-stream")


# =========================================================================== #
# Management portal API
# =========================================================================== #
@app.post("/api/login")
async def api_login(request: Request) -> Any:
    ip = _client_ip(request)
    _login_check_locked(ip)
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not store.verify_admin(username, password):
        _login_fail(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _login_success(ip)
    return {"token": _new_session(), "username": username}


@app.post("/api/logout")
async def api_admin_logout(x_admin_token: str | None = Header(default=None)) -> Any:
    if x_admin_token:
        _SESSIONS.pop(x_admin_token, None)
    return {"ok": True}


@app.get("/api/me")
async def api_me(x_admin_token: str | None = Header(default=None)) -> Any:
    return {
        "authenticated": _valid_session(x_admin_token)
        or (bool(get_settings().admin_token)
            and x_admin_token == get_settings().admin_token),
        "username": store.get_admin_username(),
    }


@app.post("/api/admin/password")
async def api_change_password(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    new_username = (body.get("username") or store.get_admin_username()).strip()
    new_password = body.get("password") or ""
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="密码至少 4 位")
    store.set_admin_credentials(new_username, new_password)
    return {"ok": True}


@app.get("/api/settings")
async def api_get_settings(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    s = get_settings()
    img = store.get_image_config()
    return {
        "require_auth": store.get_require_auth(s.require_auth),
        "pricing": store.get_pricing(),
        "image": {
            "endpoint": img.get("endpoint", ""),
            "model": img.get("model", "") or ic.DEFAULT_IMAGE_MODEL,
            "configured": bool(img.get("endpoint") and img.get("api_key")),
        },
    }


@app.post("/api/settings")
async def api_set_settings(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    if "require_auth" in body:
        store.set_require_auth(bool(body["require_auth"]))
    if "pricing" in body and isinstance(body["pricing"], dict):
        store.set_pricing(body["pricing"])
    if "image" in body and isinstance(body["image"], dict):
        img = body["image"]
        store.set_image_config(
            img.get("endpoint"), img.get("api_key"), img.get("model")
        )
    s = get_settings()
    img = store.get_image_config()
    return {
        "require_auth": store.get_require_auth(s.require_auth),
        "pricing": store.get_pricing(),
        "image": {
            "endpoint": img.get("endpoint", ""),
            "model": img.get("model", "") or ic.DEFAULT_IMAGE_MODEL,
            "configured": bool(img.get("endpoint") and img.get("api_key")),
        },
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    # `logged_in` reports only that a token is stored -- keep it, the portal
    # keys off it. `credentials_ok` reports whether GitHub still accepts that
    # token, which is the question the portal was really asking: a login
    # revoked elsewhere left `logged_in` true while every request failed.
    logged_in = cc.is_authenticated()
    return {
        "logged_in": logged_in,
        "credentials_ok": await cc.check_credentials() if logged_in else False,
        "require_auth": store.get_require_auth(get_settings().require_auth),
    }


@app.post("/api/auth/device/start")
async def api_device_start(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    return await cc.device_flow_start()


@app.post("/api/auth/device/poll")
async def api_device_poll(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    device_code = body.get("device_code")
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")
    return await cc.device_flow_poll(device_code)


@app.post("/api/auth/copilot/logout")
async def api_copilot_logout(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    cc.logout()
    return {"ok": True}


@app.get("/api/models")
async def api_models(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    try:
        models = await cc.list_models()
    except cc.NotAuthenticatedError as e:
        # Covers CredentialsRejectedError too -- the case that used to escape
        # as an opaque 500, leaving the portal with a generic failure toast
        # and the actual reason only in the server log.
        raise HTTPException(status_code=503, detail=str(e))
    except httpx.HTTPStatusError as e:
        log.error("list_models failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned {e.response.status_code} for the model list.",
        )
    except RuntimeError as e:
        log.error("list_models failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    chat_models = [
        {
            "id": m.get("id"),
            "vendor": m.get("vendor"),
            "type": (m.get("capabilities") or {}).get("type"),
        }
        for m in models
    ]
    if ic.is_configured():
        chat_models.append(
            {"id": ic.get_model(), "vendor": "azure-openai", "type": "image"}
        )
    return {"data": chat_models}


@app.get("/api/usage")
async def api_usage(
    since: float | None = None,
    until: float | None = None,
    x_admin_token: str | None = Header(default=None),
) -> Any:
    _check_admin(x_admin_token)
    return store.usage_summary_with_cost(since, until)


@app.get("/api/usage/recent")
async def api_usage_recent(
    limit: int = 50, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    return {"data": store.recent_usage(limit)}


@app.get("/api/keys")
async def api_keys_list(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    return {"data": store.list_api_keys()}


@app.post("/api/keys")
async def api_keys_create(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    name = (body.get("name") or "unnamed").strip()
    return store.create_api_key(name)


@app.delete("/api/keys/{key}")
async def api_keys_revoke(
    key: str, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    store.revoke_api_key(key)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Portal (static)
# --------------------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")
