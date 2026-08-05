"""Check that newly recorded rows count cache tokens in total_tokens.

Rows written before the fix have total = input + output; rows written after
should have total = input + cache_read + cache_write + output. Rows with no
cache activity are identical either way and are reported as such.
"""
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
con.row_factory = sqlite3.Row
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8

print(f"  {'model':<18} {'in':>7} {'cache_rd':>9} {'cache_wr':>9} "
      f"{'out':>7} {'total':>10}  verdict")
print("  " + "-" * 76)
for r in con.execute(
    "SELECT model, input_tokens, cached_tokens, cache_write_tokens, "
    "output_tokens, total_tokens FROM usage_v2 ORDER BY id DESC LIMIT ?", (limit,)
):
    whole = (r["input_tokens"] + r["cached_tokens"]
             + r["cache_write_tokens"] + r["output_tokens"])
    io = r["input_tokens"] + r["output_tokens"]
    if whole == io:
        verdict = "no cache (same either way)"
    elif r["total_tokens"] == whole:
        verdict = "counts cache"
    else:
        verdict = f"MISSING {whole - r['total_tokens']:,}"
    print(f"  {(r['model'] or '?')[:18]:<18} {r['input_tokens']:>7} "
          f"{r['cached_tokens']:>9} {r['cache_write_tokens']:>9} "
          f"{r['output_tokens']:>7} {r['total_tokens']:>10}  {verdict}")
