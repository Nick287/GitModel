"""Check attribution and per-row cost on the recent-requests table.

The table showed usage with no caller and no price, so a spike could not be
traced to a key or weighed against the bill. Both are joined/derived in the
store rather than assembled in the browser, and the per-row cost has to agree
with the summary it sits beside -- two figures for the same traffic that
disagree are worse than one.

Pass a database path.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["HUB_DATA_DIR"] = str(Path(sys.argv[1]).resolve().parent)

from hub import store  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


rows = store.recent_usage(limit=12)
check("rows returned", bool(rows), f"{len(rows)} rows")
if not rows:
    raise SystemExit(1)

check("key_name present", "key_name" in rows[0])
check("equivalent_cost present", "equivalent_cost" in rows[0])
check("some row is attributed", any(r.get("key_name") for r in rows),
      f"{sum(1 for r in rows if r.get('key_name'))}/{len(rows)} named")

print()
print(f"  {'key_name':<14} {'key tail':<10} {'model':<18} {'cost':>10}  status")
print("  " + "-" * 62)
for r in rows[:8]:
    k = r.get("api_key") or ""
    c = r.get("equivalent_cost") or 0
    tail = ("..." + k[-6:]) if k else "(none)"
    cost = ("$%.4f" % c) if c else "-"
    print(f"  {(r.get('key_name') or '(unnamed)'):<14} {tail:<10} "
          f"{(r.get('model') or '?')[:18]:<18} {cost:>10}  {r.get('status')}")

# Per-row costs summed by model must match the summary over the same window.
now = time.time()
all_rows = store.recent_usage(limit=100000)
if all_rows:
    oldest = min(r["ts"] for r in all_rows)
    summary = store.usage_summary_with_cost(oldest - 1, now + 1)
    by_model = {m["model"]: m.get("equivalent_cost", 0.0) for m in summary["by_model"]}
    per_model = {}
    for r in all_rows:
        per_model[r["model"]] = per_model.get(r["model"], 0.0) + (r["equivalent_cost"] or 0)
    print()
    print(f"  {'model':<20} {'sum of rows':>13} {'summary':>13}  agree")
    print("  " + "-" * 58)
    for m, c in sorted(per_model.items(), key=lambda kv: -kv[1])[:6]:
        s = by_model.get(m, 0.0)
        ok = abs(c - s) < max(0.005, s * 0.01)
        print(f"  {(m or '?')[:20]:<20} {c:>13.4f} {s:>13.4f}  {'yes' if ok else 'NO'}")
        if not ok:
            failures.append(f"cost mismatch for {m}")

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
