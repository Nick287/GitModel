"""Verify how the hub reports a GitHub sign-in that has been revoked.

Regression context: GitHub keeps one active OAuth token per account per
client_id, so signing in to Copilot elsewhere with the same account silently
invalidates the hub's stored token. The portal kept showing "connected"
because the check was `bool(stored_string)`, while every request failed with
an opaque 500 -- a total outage that looked healthy.

Run with a throwaway HUB_DATA_DIR; it never touches a real database.
"""
import asyncio
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")
os.environ["HUB_DATA_DIR"] = tempfile.mkdtemp(prefix="gm_test_")

from fastapi.testclient import TestClient  # noqa: E402

from hub import copilot_client as cc  # noqa: E402
from hub import server, store  # noqa: E402

state = {"code": 401, "calls": 0}


class FakeResp:
    def __init__(self, code):
        self.status_code = code
        self.text = '{"message": "Bad credentials"}'

    def json(self):
        return {}


class FakeClient:
    async def get(self, url, headers=None, **kw):
        state["calls"] += 1
        return FakeResp(state["code"])


cc._client = lambda: FakeClient()
failures = []


def check(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  -- ' + extra if extra else ''}")
    if not cond:
        failures.append(label)


with TestClient(server.app) as c:
    store.set_oauth_token("gm_test-revoked-token")
    cc._api_token_mem.clear()
    cc._credential_state.clear()

    print("[1] GitHub 401 -- status must stop claiming healthy")
    d = c.get("/api/status").json()
    check("a token is still stored", d["logged_in"] is True, json.dumps(d))
    check("but it does not read as usable", d["credentials_ok"] is False)

    print("[2] error type carries the right meaning")
    try:
        asyncio.new_event_loop().run_until_complete(cc.get_api_token())
        check("raises", False)
    except cc.CredentialsRejectedError as e:
        check("stays catchable as an auth error",
              isinstance(e, cc.NotAuthenticatedError))
        check("explains the elsewhere-login cause",
              "somewhere else" in str(e), str(e)[:60] + "...")
    check("stale API token dropped", cc._api_token_mem == {})

    print("[3] model list no longer an opaque 500")
    r = c.get("/v1/models", headers={"x-api-key": "k"})
    check("/v1/models -> 503", r.status_code == 503,
          f"{r.status_code} {json.dumps(r.json())[:60]}")

    print("[4] 403 (no subscription) is a different message")
    state["code"] = 403
    cc._api_token_mem.clear()
    cc._credential_state.clear()
    r = c.get("/v1/models", headers={"x-api-key": "k"})
    check("403 mentions Copilot access",
          "Copilot access" in r.json().get("detail", ""),
          r.json().get("detail", "")[:60])

    print("[5] status polling is cached, not hammering GitHub")
    state["code"] = 401
    cc._api_token_mem.clear()
    cc._credential_state.clear()
    c.get("/api/status")
    before = state["calls"]
    for _ in range(5):
        c.get("/api/status")
    check("5 extra polls cost 0 extra GitHub calls",
          state["calls"] - before == 0, f"{state['calls'] - before} calls")

    print("[6] a fresh login clears the stale verdict")
    cc._credential_state.update(ok=False, checked_at=9e9)
    cc.logout()
    check("logout resets the verdict", cc._credential_state == {})

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
