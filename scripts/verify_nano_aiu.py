"""Check Copilot's nano_aiu charge against the published Anthropic rates.

The hub bills from `copilot_usage.total_nano_aiu`, taken verbatim from the
upstream, rather than from the local price table. This recomputes a few real
rows from their token counts to show the two agree -- and, in passing, that
cache writes are billed at 1.25x input, which is why hiding that column hid
most of the cost.
"""
import sqlite3
import sys

NANO_AIU_PER_USD = 1e11
# Published Claude Opus 5 rates, USD per 1M tokens.
IN, OUT = 5.00, 25.00
CACHE_READ, CACHE_WRITE = IN * 0.10, IN * 1.25

con = sqlite3.connect(sys.argv[1])
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT input_tokens, output_tokens, cached_tokens, cache_write_tokens, nano_aiu "
    "FROM usage_v2 WHERE model = ? AND nano_aiu > 0 ORDER BY id DESC LIMIT 8",
    ("claude-opus-5",),
).fetchall()

print(f"  {'in':>5} {'cache_rd':>9} {'cache_wr':>9} {'out':>6} "
      f"{'upstream $':>11} {'recomputed $':>13}  match")
print("  " + "-" * 66)
worst = 0.0
for r in rows:
    upstream = r["nano_aiu"] / NANO_AIU_PER_USD
    mine = (r["input_tokens"] * IN
            + r["cached_tokens"] * CACHE_READ
            + r["cache_write_tokens"] * CACHE_WRITE
            + r["output_tokens"] * OUT) / 1e6
    drift = abs(mine - upstream) / upstream if upstream else 0
    worst = max(worst, drift)
    print(f"  {r['input_tokens']:>5} {r['cached_tokens']:>9} "
          f"{r['cache_write_tokens']:>9} {r['output_tokens']:>6} "
          f"{upstream:>11.4f} {mine:>13.4f}  {'yes' if drift < 0.01 else f'{drift:.1%} off'}")

total = con.execute(
    "SELECT COALESCE(SUM(nano_aiu),0) FROM usage_v2").fetchone()[0] / NANO_AIU_PER_USD
print()
print(f"  worst drift on these rows: {worst:.2%}")
print(f"  total billed so far (all rows): ${total:.4f}")
