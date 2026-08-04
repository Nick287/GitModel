"""Async GitHub Copilot client.

Handles the two-stage auth (long-lived OAuth token -> short-lived API token)
and forwards both buffered and streaming requests to the Copilot backend.

Mirrors the logic in the reference ``copilot.py`` / ``git_model.py`` but is
adapted for a long-running server: the OAuth token lives in the SQLite store
(or ``COPILOT_OAUTH_TOKEN``), and the API token is cached in memory.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, AsyncIterator

import httpx

from . import store
from .config import get_settings

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # public copilot.vim client_id
COPILOT_API_BASE = "https://api.githubcopilot.com"

# Mandatory editor-spoof headers — the Copilot backend rejects requests
# without them (HTTP 401 / 403).
EDITOR_HEADERS: dict[str, str] = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GitHubCopilotChat/0.22.0",
}

_api_token_mem: dict[str, Any] = {}
# Last known verdict on whether GitHub still accepts the stored OAuth token,
# so the portal can distinguish "a token is stored" from "the token works"
# without asking GitHub on every status poll.
_credential_state: dict[str, Any] = {}
_api_token_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None


class NotAuthenticatedError(RuntimeError):
    """Raised when no Copilot OAuth token is configured."""


class CredentialsRejectedError(NotAuthenticatedError):
    """The stored OAuth token exists but GitHub refuses it.

    Distinct from its parent because the two need different words in front of
    a user: "log in" versus "your login stopped working". Subclasses it so
    every existing handler keeps treating this as an auth failure rather than
    letting it escape as an opaque 500 -- which is what used to happen, since
    the plain RuntimeError this replaces matched no handler at all.

    GitHub revokes the previous token whenever the same account completes the
    device flow again for this client_id, so a hub that was working can be
    logged out by a login somewhere else. That is GitHub's behaviour, not a
    bug here; the job of this class is to make it legible instead of fatal.
    """


class UpstreamError(RuntimeError):
    """A non-200 from the Copilot backend, with status and body kept intact.

    Subclasses RuntimeError so existing ``except RuntimeError`` handlers still
    catch it, but carries the two things a caller needs to mirror the upstream
    response rather than flatten it: the status code and the parsed body. The
    string form stays ``"<status>: <raw>"`` for logs.
    """

    def __init__(self, status_code: int, raw: str) -> None:
        super().__init__(f"{status_code}: {raw}")
        self.status_code = status_code
        self.raw = raw

    @property
    def body(self) -> dict[str, Any]:
        """Upstream body as JSON, wrapped in an error envelope if unparseable.

        Matches what :func:`post_json` returns for the same response, so both
        paths hand the caller the same shape.
        """
        try:
            parsed = json.loads(self.raw)
        except ValueError:
            parsed = None
        return parsed if isinstance(parsed, dict) else {"error": {"message": self.raw}}


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
    return _http_client


# --------------------------------------------------------------------------- #
# OAuth token resolution
# --------------------------------------------------------------------------- #
def get_oauth_token() -> str:
    """Return the long-lived Copilot OAuth token or raise."""
    token = get_settings().copilot_oauth_token or store.get_oauth_token()
    if not token:
        raise NotAuthenticatedError(
            "No Copilot OAuth token configured. Log in via the web portal first."
        )
    return token


def is_authenticated() -> bool:
    return bool(get_settings().copilot_oauth_token or store.get_oauth_token())


def logout() -> None:
    store.clear_oauth_token()
    _api_token_mem.clear()
    _credential_state.clear()


# --------------------------------------------------------------------------- #
# Device flow (web-portal friendly: start + poll)
# --------------------------------------------------------------------------- #
async def device_flow_start() -> dict[str, Any]:
    r = await _client().post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
    )
    r.raise_for_status()
    info = r.json()
    return {
        "device_code": info["device_code"],
        "user_code": info["user_code"],
        "verification_uri": info["verification_uri"],
        "interval": info.get("interval", 5),
        "expires_in": info.get("expires_in", 900),
    }


async def device_flow_poll(device_code: str) -> dict[str, Any]:
    """Poll once. Returns {status: 'pending'|'success'|'error', ...}."""
    r = await _client().post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    data = r.json()
    if "access_token" in data:
        token = data["access_token"]
        store.set_oauth_token(token)
        _api_token_mem.clear()
        _credential_state.clear()
        return {"status": "success"}
    err = data.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending", "error": err}
    return {"status": "error", "error": err or "unknown", "detail": data}


# --------------------------------------------------------------------------- #
# Short-lived API token exchange (~30 min) — cached in memory
# --------------------------------------------------------------------------- #
async def _exchange_for_api_token() -> tuple[str, str, float]:
    oauth = get_oauth_token()
    r = await _client().get(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {oauth}",
            "Accept": "application/json",
            **EDITOR_HEADERS,
        },
    )
    if r.status_code in (401, 403):
        # 401 = the token itself is no longer valid (revoked, most often by
        # this same account completing the device flow elsewhere). 403 = the
        # token is fine but the account lacks Copilot access. Different
        # fixes, so say which one it is instead of listing both as guesses.
        if r.status_code == 401:
            detail = (
                "GitHub rejected the stored login (Bad credentials). It was "
                "most likely invalidated by signing in to Copilot with this "
                "same GitHub account somewhere else -- GitHub keeps only one "
                "active login per account. Log in again to restore service."
            )
        else:
            detail = (
                "GitHub accepted the login but denied Copilot access. The "
                "account may not have an active Copilot subscription."
            )
        raise CredentialsRejectedError(detail)
    if r.status_code != 200:
        raise RuntimeError(
            f"Failed to exchange API token {r.status_code}: {r.text}."
        )
    data = r.json()
    endpoint = data.get("endpoints", {}).get("api", COPILOT_API_BASE)
    return data["token"], endpoint, float(data["expires_at"])


async def get_api_token() -> tuple[str, str]:
    now = time.time()
    if _api_token_mem and _api_token_mem.get("expires_at", 0) - 300 > now:
        return _api_token_mem["token"], _api_token_mem["endpoint"]
    async with _api_token_lock:
        if _api_token_mem and _api_token_mem.get("expires_at", 0) - 300 > time.time():
            return _api_token_mem["token"], _api_token_mem["endpoint"]
        try:
            token, endpoint, expires_at = await _exchange_for_api_token()
        except CredentialsRejectedError:
            # Drop the cached API token: it was minted from an OAuth token
            # GitHub no longer honours, so serving it would produce 401s from
            # the backend instead of one honest error here.
            _api_token_mem.clear()
            _credential_state.update(ok=False, checked_at=time.time())
            raise
        _api_token_mem.update(token=token, endpoint=endpoint, expires_at=expires_at)
        _credential_state.update(ok=True, checked_at=time.time())
        return token, endpoint


async def check_credentials(max_age: float = 300.0) -> bool:
    """Whether GitHub currently accepts the stored login.

    Unlike :func:`is_authenticated`, which only reports that some string is
    stored, this reflects whether that string still works -- the distinction
    that let the portal show "logged in" while every request failed. Result is
    cached for ``max_age`` seconds so polling the status endpoint does not hit
    GitHub on every call; a successful token exchange anywhere else refreshes
    it for free.
    """
    if not is_authenticated():
        return False
    fresh = time.time() - _credential_state.get("checked_at", 0.0) < max_age
    if fresh and "ok" in _credential_state:
        return bool(_credential_state["ok"])
    try:
        await get_api_token()
        return True
    except NotAuthenticatedError:
        return False
    except Exception:
        # A network blip is not evidence the credentials are bad; report the
        # last known verdict rather than inventing a logout.
        return bool(_credential_state.get("ok", True))


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
async def list_models() -> list[dict[str, Any]]:
    token, endpoint = await get_api_token()
    r = await _client().get(
        f"{endpoint}/models",
        headers={"Authorization": f"Bearer {token}", **EDITOR_HEADERS},
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def post_json(
    path: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Buffered POST. Returns (status_code, json_body)."""
    token, endpoint = await get_api_token()
    r = await _client().post(
        f"{endpoint}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **EDITOR_HEADERS,
            **(extra_headers or {}),
        },
        json=payload,
    )
    try:
        body = r.json()
    except Exception:
        body = {"error": {"message": r.text}}
    return r.status_code, body


async def open_stream(
    path: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[AsyncIterator[bytes], int]:
    """Begin a streaming POST and return ``(chunks, status)`` once connected.

    Everything that can fail -- resolving the API token, connecting, and the
    upstream's own status code -- happens before this returns, so the caller
    can still emit a proper HTTP error. The returned iterator only yields body
    bytes, and closes the response when exhausted.

    Connecting up front is what makes streaming endpoints usable at all. A
    plain async generator defers every failure to its first iteration, which
    for a caller wrapping it in a StreamingResponse is already too late: the
    200 and the chunked-transfer header are out, so raising can only abort the
    connection mid-body. That leaves a truncated chunked stream -- which curl
    tolerates silently, while Node's undici (Claude Code) and Python's
    http.client raise. The bug is invisible through curl.

    Raises NotAuthenticatedError, or UpstreamError on a non-200 -- the latter
    carrying the upstream status and body so the caller can mirror them
    instead of collapsing every failure into one status.
    """
    token, endpoint = await get_api_token()
    ctx = _client().stream(
        "POST",
        f"{endpoint}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **EDITOR_HEADERS,
            **(extra_headers or {}),
        },
        json=payload,
    )
    resp = await ctx.__aenter__()
    try:
        if resp.status_code != 200:
            body = await resp.aread()
            raise UpstreamError(
                resp.status_code, body.decode("utf-8", "replace")
            )
    except BaseException:
        await ctx.__aexit__(*sys.exc_info())
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await ctx.__aexit__(None, None, None)

    return body(), resp.status_code
