"""Compare the liang database before and after the image upgrade.

Answers the only question that mattered about upgrading: did the stored
GitHub sign-in survive? Takes the pre-upgrade backup and the live database
and reports whether the OAuth token is byte-identical, plus whether history
and API keys came through.
"""
import sqlite3
import sys


def probe(path):
    con = sqlite3.connect(path)
    tabs = sorted(r[0] for r in con.execute(
        "select name from sqlite_master where type = ?", ("table",)))
    row = con.execute("select value from kv where key = ?", ("oauth_token",)).fetchone()
    return {
        "token": row[0] if row else None,
        "api_keys": con.execute("select count(*) from api_keys").fetchone()[0],
        "usage": con.execute("select count(*) from usage").fetchone()[0],
        "usage_v2": (con.execute("select count(*) from usage_v2").fetchone()[0]
                     if "usage_v2" in tabs else "-"),
        "tables": tabs,
    }


before, after = probe(sys.argv[1]), probe(sys.argv[2])
same = before["token"] is not None and before["token"] == after["token"]

print(f"  {'':16s} {'BEFORE (backup)':<20s} AFTER (live)")
print(f"  {'-' * 56}")
for label, key in (("oauth_token", "token"), ("api_keys", "api_keys"),
                   ("usage rows", "usage"), ("usage_v2 rows", "usage_v2")):
    b, a = before[key], after[key]
    if key == "token":
        b = (b[:8] + "...") if b else "*** MISSING ***"
        a = (a[:8] + "...") if a else "*** LOST ***"
    print(f"  {label:16s} {str(b):<20s} {a}")
print(f"  {'-' * 56}")
print(f"  token byte-identical: {same}")
print(f"  tables after: {after['tables']}")

ok = same and after["api_keys"] == before["api_keys"] and after["usage"] >= before["usage"]
print()
print("PASSED -- sign-in survived the upgrade" if ok else "FAILED -- data did not survive")
raise SystemExit(0 if ok else 1)
