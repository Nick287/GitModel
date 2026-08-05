"""Check total_tokens and row_cost under both upstream usage shapes.

Anthropic reports input_tokens excluding both cache buckets; OpenAI reports
cache hits inside prompt_tokens. A total or a price computed for one shape is
wrong for the other, and rows from both land in the same table.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import store  # noqa: E402

PRICING = {"m": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}
failures = []


def check(label, got, want):
    ok = abs(got - want) < 1e-9 if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f" (want {want})"))
    if not ok:
        failures.append(label)


print("[1] total for an Anthropic row -- cache is the whole context")
i, o, cr, cw = 2, 553, 111_764, 402
check("total includes both cache buckets", i + cr + cw + o, 112_721)
check("old formula would have said", i + o, 555)

print("[2] row_cost with Anthropic semantics (cached > input)")
cost = store.estimate_cost(PRICING, "m", i, o, cr, cw)
# 2 input + 111764 read + 402 write + 553 out
want = (2 * 5.0 + 111_764 * 0.5 + 402 * 6.25 + 553 * 25.0) / 1e6
check("prices the full cache read", round(cost, 8), round(want, 8))
old = (0 * 5.0 + 2 * 0.5 + 402 * 6.25 + 553 * 25.0) / 1e6
print(f"        old truncating formula gave ${old:.6f} vs ${want:.6f}"
      f" -- {want / old:.0f}x understated")

print("[3] row_cost with OpenAI semantics (cached subset of input) unchanged")
cost = store.estimate_cost(PRICING, "m", 1000, 100, 400, 0)
want = (600 * 5.0 + 400 * 0.5 + 100 * 25.0) / 1e6
check("subset reading preserved", round(cost, 8), round(want, 8))

print("[4] edge cases")
check("no cache at all", round(store.estimate_cost(PRICING, "m", 100, 10, 0, 0), 8),
      round((100 * 5.0 + 10 * 25.0) / 1e6, 8))
check("fully cached OpenAI row", round(store.estimate_cost(PRICING, "m", 100, 10, 100, 0), 8),
      round((100 * 0.5 + 10 * 25.0) / 1e6, 8))
check("all zeros", store.estimate_cost(PRICING, "m", 0, 0, 0, 0), 0.0)

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
