"""Can an operator see what happened to the caller's beta header?

The filtering is invisible from outside: a request succeeds either way, and
the feature the caller asked for is simply absent. This drives a real request
through the app and checks that both channels report it -- the log line at the
moment of divergence, and /api/betas for the state that persists after.

Needs HUB_DATA_DIR with a working OAuth token. Costs a little quota.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from hub import anthropic_adapter as aa  # noqa: E402
from hub import server, store  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"
REFUSED = "advisor-tool-2026-03-01"
HDR = f"claude-code-20250219,{REFUSED},context-1m-2025-08-07"

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("hub").addHandler(cap)
logging.getLogger("hub").setLevel(logging.INFO)

aa.reset_learned_betas()
base = {"model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]}

with TestClient(server.app) as c:
    # Mint a session directly. The admin password is whatever this database
    # was configured with, and what is under test here is /api/betas, not login.
    tok = server._new_session()

    print("[1] first call: the refusal is discovered")
    r = c.post("/v1/messages", json=base,
               headers={"content-type": "application/json", "anthropic-beta": HDR})
    check("request still succeeds", r.status_code == 200, str(r.status_code))
    retry_logged = [ln for ln in cap.lines if "refused beta" in ln]
    check("the retry is logged", bool(retry_logged),
          retry_logged[0][:70] if retry_logged else "no line")

    print("[2] second call: the divergence is logged")
    cap.lines.clear()
    r = c.post("/v1/messages", json=base,
               headers={"content-type": "application/json", "anthropic-beta": HDR})
    check("request succeeds", r.status_code == 200, str(r.status_code))
    div = [ln for ln in cap.lines if "anthropic-beta in=" in ln]
    check("in/out comparison is logged", bool(div),
          div[0][:100] if div else "no line")
    if div:
        check("it names what was withheld", REFUSED in div[0], div[0][:100])
        check("it shows the header actually sent",
              "context-1m-2025-08-07" in div[0], div[0][:100])

    print("[3] /api/betas reports the state")
    if tok:
        r = c.get("/api/betas", headers={"x-admin-token": tok})
        check("endpoint answers", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            d = r.json()
            check("refused_now lists it", REFUSED in d.get("refused_now", []),
                  str(d.get("refused_now")))
            check("seeded list present", len(d.get("seeded", [])) == 9,
                  str(len(d.get("seeded", []))))
            check("ttl reported", d.get("refusal_ttl_seconds") == 6 * 3600,
                  str(d.get("refusal_ttl_seconds")))
            check("gated fields reported",
                  "context_management" in (d.get("beta_gated_fields") or {}),
                  str(d.get("beta_gated_fields")))
    else:
        check("admin login for /api/betas", False, "could not log in")

    print("[4] endpoint is admin-gated")
    r = c.get("/api/betas")
    check("no token -> refused", r.status_code in (401, 403), str(r.status_code))

aa.reset_learned_betas()
print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
