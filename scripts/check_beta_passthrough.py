"""Check beta-header handling and the conditional field stripping it drives.

Two rules interact here. The upstream validates `anthropic-beta`
all-or-nothing, so what gets forwarded decides whether a request survives at
all. And `context_management` is accepted only when its header rides along, so
whether to strip it depends on what will actually be sent -- not on what the
caller sent, which differs once a beta has been learned as refused.

The learning half is covered by check_beta_learning.py; this file is about the
filtering and stripping given a known state.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import anthropic_adapter as aa  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f": {got!r} != {want!r}"))
    if not ok:
        failures.append(label)


CM = {"edits": [{"type": "clear_tool_uses_20250919"}]}
BETA = "context-management-2025-06-27"
UNKNOWN = "zz-unknown-2026-01-01"

aa.reset_learned_betas()

print("[1] header filtering")
check("None stays None", aa.forwardable_betas(None), None)
check("empty stays None", aa.forwardable_betas(""), None)
check("known beta survives", aa.forwardable_betas(BETA), BETA)
# Unknown betas go out on purpose: dropping them silently disabled features
# the backend supports. The refusal path in check_beta_learning.py is what
# makes that safe.
check("unknown beta forwarded optimistically", aa.forwardable_betas(UNKNOWN), UNKNOWN)
check("whitespace tolerated", aa.forwardable_betas(f"  {BETA} "), BETA)
check("sorted and deduped",
      aa.forwardable_betas(f"{BETA},claude-code-20250219,{BETA}"),
      f"claude-code-20250219,{BETA}")

print("[2] a learned refusal is withheld, the rest still go")
aa.reset_learned_betas()
refused = "advisor-tool-2026-03-01"
cc_header = (f"claude-code-20250219,{refused},context-1m-2025-08-07,"
             f"effort-2025-11-24,{BETA}")
check("before learning, all five forwarded",
      len((aa.forwardable_betas(cc_header) or "").split(",")), 5)
aa.learn_refused_betas(f"unsupported beta header(s): {refused}")
out = aa.forwardable_betas(cc_header) or ""
check("after learning, it is gone", refused in out, False)
check("the other four survive", len(out.split(",")), 4)
aa.reset_learned_betas()

print("[3] context_management follows the header that will be sent")
p, dropped = aa.strip_unsupported({"model": "m", "context_management": CM})
check("no header -> stripped", "context_management" in p, False)
check("no header -> reported", dropped, ["context_management"])
p, dropped = aa.strip_unsupported({"model": "m", "context_management": CM}, BETA)
check("with header -> kept", p.get("context_management"), CM)
check("with header -> nothing dropped", dropped, [])
p, _ = aa.strip_unsupported({"model": "m", "context_management": CM}, UNKNOWN)
check("unrelated header -> still stripped", "context_management" in p, False)

print("[4] fields that are always or never allowed")
p, _ = aa.strip_unsupported(
    {"model": "m", "fallbacks": ["x"], "mcp_servers": [{"a": 1}]}, BETA)
check("fallbacks stripped even with a header", "fallbacks" in p, False)
check("mcp_servers stripped even with a header", "mcp_servers" in p, False)
p, dropped = aa.strip_unsupported({"model": "m", "container": {"id": "t"}})
check("container is no longer stripped", p.get("container"), {"id": "t"})
check("container not reported as dropped", dropped, [])

print("[5] the untouched path stays allocation-free")
clean = {"model": "m", "messages": []}
p, dropped = aa.strip_unsupported(clean, BETA)
check("same object returned", p is clean, True)
check("nothing dropped", dropped, [])

aa.reset_learned_betas()
print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
