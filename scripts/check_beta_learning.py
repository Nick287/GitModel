"""Check the runtime learning of refused betas.

The allowlist cannot be complete -- it was measured on one account, and betas
appear without notice -- so unknown ones are forwarded and any refusal is
learned from the upstream's own wording. What matters most is what this must
NOT learn: an ordinary 400 has to leave the set alone, or a bad model name
would quietly disable features that work.
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


NEW = "brand-new-beta-2026-09-01"
OTHER = "another-new-beta-2026-10-01"

print("[1] parsing the upstream's wording")
aa.reset_learned_betas()
check("single beta",
      aa.parse_refused_betas('{"error":{"message":"unsupported beta header(s): '
                             + NEW + '"}}'),
      {NEW})
check("two betas, comma separated",
      aa.parse_refused_betas("unsupported beta header(s): a-1, b-2"), {"a-1", "b-2"})
check("trailing prose is not swallowed",
      aa.parse_refused_betas('unsupported beta header(s): a-1"}}\nmore'), {"a-1"})

print("[2] what it must NOT learn")
for label, msg in [
    ("a model error", '{"error":{"message":"The requested model is not supported."}}'),
    ("a size error", "Request Entity Too Large"),
    ("an effort error", '{"error":{"code":"invalid_reasoning_effort"}}'),
    ("empty body", ""),
    ("a bare 500", "Internal Server Error"),
]:
    check(label, aa.parse_refused_betas(msg), set())

print("[3] learning, and not relearning")
aa.reset_learned_betas()
err = "unsupported beta header(s): " + NEW
check("first sight is a discovery", aa.learn_refused_betas(err), {NEW})
check("second sight is not", aa.learn_refused_betas(err), set())
check("it is remembered", NEW in aa.refused_betas(), True)

print("[4] a verified beta is never disabled by one bad response")
aa.reset_learned_betas()
known = "context-management-2025-06-27"
aa.learn_refused_betas("unsupported beta header(s): " + known)
check("seed beta not learned as refused", known in aa.refused_betas(), False)
check("seed beta still forwarded", aa.forwardable_betas(known), known)

print("[5] forwarding reflects what has been learned")
aa.reset_learned_betas()
check("unknown beta forwarded optimistically", aa.forwardable_betas(NEW), NEW)
aa.learn_refused_betas("unsupported beta header(s): " + NEW)
check("after learning, withheld", aa.forwardable_betas(NEW), None)
check("others in the same header survive",
      aa.forwardable_betas(f"{NEW},{known}"), known)

print("[6] a gated field follows its beta out")
aa.reset_learned_betas()
CM = {"edits": []}
gate = "context-management-2025-06-27"
payload = {"model": "m", "context_management": CM}
out, _ = aa.strip_unsupported(payload, aa.forwardable_betas(gate))
check("kept while the beta is forwarded", "context_management" in out, True)
aa._REFUSED_BETAS.add(gate)  # simulate an account that refuses it
out, dropped = aa.strip_unsupported(payload, aa.forwardable_betas(gate))
check("dropped once the beta is withheld", "context_management" in out, False)
check("and reported", dropped, ["context_management"])
aa.reset_learned_betas()

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
