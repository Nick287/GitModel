"""Check what total_tokens actually counts.

The Anthropic passthrough records total as input + output. Under prompt
caching almost the entire context is cache_read or cache_write, so a total
built from input alone omits nearly everything that was actually processed
-- and everything that was actually billed.
"""
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
i, cr, cw, o, t = con.execute(
    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cached_tokens),0), "
    "COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(output_tokens),0), "
    "COALESCE(SUM(total_tokens),0) FROM usage_v2"
).fetchone()

print(f"  input         {i:>14,}")
print(f"  cache_read    {cr:>14,}")
print(f"  cache_write   {cw:>14,}")
print(f"  output        {o:>14,}")
print("  " + "-" * 30)
print(f"  total_tokens  {t:>14,}   <- what the portal shows")
print(f"  input+output  {i + o:>14,}")
print(f"  everything    {i + cr + cw + o:>14,}   <- actually processed")
print()
print(f"  total == input+output : {t == i + o}")
print(f"  cache counted in total: {t > i + o}")
if t and not t > i + o:
    print(f"  understated by        : {(i + cr + cw + o) / t:.1f}x")

# Per-endpoint, since the two passthroughs build `total` differently.
print()
print(f"  {'endpoint':<12} {'rows':>6} {'total':>14} {'in+out':>14} {'+cache':>14}")
for ep, n, si, scr, scw, so, st in con.execute(
    "SELECT endpoint, COUNT(*), COALESCE(SUM(input_tokens),0), "
    "COALESCE(SUM(cached_tokens),0), COALESCE(SUM(cache_write_tokens),0), "
    "COALESCE(SUM(output_tokens),0), COALESCE(SUM(total_tokens),0) "
    "FROM usage_v2 GROUP BY endpoint"
):
    print(f"  {ep:<12} {n:>6} {st:>14,} {si + so:>14,} {si + scr + scw + so:>14,}")
