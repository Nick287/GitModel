"""Check beta-header filtering and the conditional field stripping it drives.

The upstream validates `anthropic-beta` all-or-nothing, so forwarding one
unverified beta fails the whole request while dropping it costs only that
feature. And `context_management` is accepted only when its header rides
along, so whether to strip it depends on what the caller sent.
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

print("[1] beta header filtering (allowlist)")
check("None stays None", aa.forwardable_betas(None), None)
check("empty stays None", aa.forwardable_betas(""), None)
check("known beta survives", aa.forwardable_betas(BETA), BETA)
check("unknown beta dropped",
      aa.forwardable_betas("advisor-tool-2026-03-01"), None)
check("mixed keeps only the known one",
      aa.forwardable_betas(f"advisor-tool-2026-03-01,{BETA}"), BETA)
check("whitespace tolerated", aa.forwardable_betas(f"  {BETA} , junk "), BETA)
check("output is sorted and deduped",
      aa.forwardable_betas(f"{BETA},claude-code-20250219,{BETA}"),
      f"claude-code-20250219,{BETA}")
# The real Claude Code header: nine accepted plus one the upstream refuses.
cc_header = ("claude-code-20250219,advisor-tool-2026-03-01,"
             "context-1m-2025-08-07,effort-2025-11-24," + BETA)
out = aa.forwardable_betas(cc_header)
check("advisor-tool removed from a real Claude Code header",
      "advisor-tool-2026-03-01" in (out or ""), False)
check("the other four survive", len((out or "").split(",")), 4)

print("[2] context_management is stripped only without its header")
p, dropped = aa.strip_unsupported({"model": "m", "context_management": CM})
check("no header -> stripped", "context_management" in p, False)
check("no header -> reported", dropped, ["context_management"])
p, dropped = aa.strip_unsupported({"model": "m", "context_management": CM}, BETA)
check("with header -> kept", p.get("context_management"), CM)
check("with header -> nothing dropped", dropped, [])
p, _ = aa.strip_unsupported({"model": "m", "context_management": CM},
                            "advisor-tool-2026-03-01")
check("wrong header -> still stripped", "context_management" in p, False)

print("[3] fields that are always/never allowed")
p, dropped = aa.strip_unsupported(
    {"model": "m", "fallbacks": ["x"], "mcp_servers": [{"a": 1}]}, BETA)
check("fallbacks stripped even with a header", "fallbacks" in p, False)
check("mcp_servers stripped even with a header", "mcp_servers" in p, False)
p, dropped = aa.strip_unsupported({"model": "m", "container": {"id": "t"}})
check("container is no longer stripped", p.get("container"), {"id": "t"})
check("container not reported as dropped", dropped, [])

print("[4] the untouched path stays allocation-free")
clean = {"model": "m", "messages": []}
p, dropped = aa.strip_unsupported(clean, BETA)
check("same object returned", p is clean, True)
check("nothing dropped", dropped, [])

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
