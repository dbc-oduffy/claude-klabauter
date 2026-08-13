"""test_no_bin_docstring_command_substitution.py — RATCHET regression net for
the PRECISE hazard behind the retired sh/python polyglot trampoline: a
command substitution span embedded in a module docstring, not the mere
presence of a module docstring.

DO NOT re-scope this to "no module docstring" (an earlier revision of this
suite did, and was corrected 2026-07-28) — this repo's CLAUDE.md §
Implementation Standards makes module/file-top purpose docstrings
REQUIRED, not optional, at structural boundaries (the RAG-bait-at-
structural-boundaries carve-out). A gate that forces every CLI to give up
`__doc__` to satisfy a bash-parsing accident collides head-on with that
doctrine and would be wrong every time someone documents a new CLI
correctly. Module docstrings on python3-shebang entrypoints are fine and
expected; this suite gates something narrower.

MECHANISM (why this hazard exists at all): `bash <file>` parses a
`#!/usr/bin/env python3` Python file as shell text (the shebang line is
just a comment to sh). A `#` comment header is inert to bash. A triple-
quoted module docstring is read as quoted shell words — inert on its own —
but bash STILL performs command substitution on any backtick-delimited
span (`` `...` ``) or `$(...)` span found inside those quoted words,
executing whatever is between the delimiters as a subshell command. Most
such spans are harmless prose accidents (a backtick-quoted symbol name like
`` `_main()` `` just becomes a failed "command not found" — noisy but not
dangerous); the failure mode this suite exists to prevent is a substitution
span that resolves to an interactive/stdin-blocking command with no further
arguments (`` `python3` ``, `` `bash` ``, `` `cat` ``) — that hangs FOREVER
with an open stdin. A hang is not a non-zero exit, so it survives
`try/except`, exit-code gating, `|| true`, and `2>/dev/null`.

WHY THIS SUITE GATES ON "ANY command-substitution span", NOT ONLY ON THE
NARROWER "span resolves to a bare stdin-blocking command" condition: the
narrower condition requires an enumerated allowlist of stdin-blocking
binaries (`python`, `python3`, `py`, `bash`, `sh`, `cat`, …) that is
inherently incomplete — `sort`, `wc`, `grep`, `sed`, `ssh`, `node`, and bare
`python` all block on stdin too, and a future interpreter or wrapper this
suite's author never enumerated would silently reopen the hazard. Banning
the SYNTAX (any command substitution span) rather than the SEMANTICS (only
spans that happen to name a known stdin-reader) is the structurally
complete rule: it cannot be defeated by a command this suite's author
didn't think of. A narrower allowlist-based variant was considered and
declined (PM ruling, 2026-07-28) precisely because it buys a smaller
offender count by making the gate less true — false assurance is worse
than a longer worklist. The cost of that completeness is real (see the
ratchet-not-retroactive-gate section below) and is paid honestly via the
baseline mechanism, not by narrowing the rule.

WHY THE TRAMPOLINE ITSELF STAYS RETIRED EVEN THOUGH THIS GATE ADMITS
DOCSTRINGS: the sh/python polyglot trampoline (`''''exec "$(command -v
python3 || command -v python || command -v py)" "$0" "$@" #'''`) is not
retired ONLY because of the hang hazard above — that half of the
rationale reads as avoidable-in-principle (a careful docstring author
could just never write a command-substitution span). The second,
load-bearing half is a measured, UNCONDITIONAL per-invocation cost on
Windows: the sh-shim re-exec the trampoline performs costs
~326ms/invocation on Windows versus invoking the same Python body directly
(1306ms via the shim vs 980ms direct, byte-identical output — measured in
`state/audits/2026-07-20-sh-suffixed-python-trampolines.md` in the
Coordinator-claude clone, not this repo; the path is qualified deliberately, and
its absence here is not evidence it is missing — see
`coordinator/bin/check-sh-suffix-polyglot.py`'s own docstring, which cites
the same figure the same way). That tax is paid on EVERY invocation,
unconditionally, regardless of whether the docstring hazard this suite
gates ever fires. `_write_agent_forwarder`
(`coordinator_core/install/substrate.py`) installs this same template as
~290 forwarders repo-wide, so it is not one file's cost — it is ~290
cold-`bash.exe`-avoidance wins per install, on Windows, which CLAUDE.md §
Runtime conventions treats as the primary platform and names a
`bash`-spawning script on the commit/session hot path as break-class, not
a someday-migration.

STRUCTURAL DETECTION: extracts the REAL docstring via `ast.get_docstring()`
(never a whole-file substring search — see the sibling
`test_no_bin_polyglot_invariant.py`'s FALSE-POSITIVE TRAP note for why),
then scans ONLY that extracted string for a backtick-delimited span or a
`$(...)` span. A backtick or `$(` appearing in a `#`-comment (outside the
docstring) is out of scope by construction — comments are never quoted
shell words to begin with.

Shares its INDEX-blob-reading primitives with `test_no_bin_polyglot_invariant.py`
via `_polyglot_git_scan` — see that module's docstring for why the scan
reads `git show :<path>` (the INDEX-staged blob) rather than the working
tree.

RATCHET, NOT A RETROACTIVE GATE (converted 2026-07-28, matching the shape
`coordinator/bin/check-sh-suffix-polyglot.py` already uses for its own
`.sh`-suffix-polyglot ratchet — read that script before changing this
one): this repo's own convention is to backtick-quote symbol/path
identifiers in prose, and that convention alone is enough to trip the "any
command-substitution span" rule on a large slice of already-documented
entrypoints. Gating on the full rule the day it lands would leave 31
pre-existing offenders permanently red with no path to green — worse than
useless, since a permanently-red suite gets excluded from attention
entirely. Those 31 are recorded in the checked-in baseline
(`command-substitution-docstring-baseline.txt`, co-located with this file,
repo-relative paths) and do NOT fail this suite. Only a NEW offender — a
python3-shebang extensionless entrypoint not in the baseline that
introduces a command-substitution span in its docstring, or a baselined
file that gains a hazard beyond what got it baselined — fails. This is a
REAL, ENFORCING gate (no `designed_red` marker — an earlier revision of
this suite was `designed_red`; that marker is gone now that a baseline
makes the suite green on a clean tree, and it runs in both the fast and
full test tiers).

BASELINE HYGIENE — stale entries are NOT silently tolerated: unlike
`check-sh-suffix-polyglot.py`'s ratchet (which only checks the forward
direction — un-baselined offenders fail, but a baselined entry that stops
offending is never re-examined), this suite ALSO asserts every baseline
entry still exists as a tracked python3-shebang extensionless file AND
still carries a command-substitution span in its docstring. A baseline
line whose file was fixed, renamed, or deleted fails loudly with "remove
this line from the baseline" — the ratchet's whole value is being current
inventory, not a fossilizing exemption list. Fix a file's docstring
(neutralize the span, keep the prose and the required module docstring)
and drop its line from the baseline in the SAME change.

NEGATIVE SPEC
    - Does NOT flag the mere presence of a module docstring — a docstring
      with zero backtick/`$(` spans is fully compliant and expected.
    - Does NOT flag `#`-comment headers (only `ast`-recognized module
      docstrings are scanned).
    - Does NOT narrow to a stdin-reader allowlist (see rationale above) —
      any command-substitution span counts, even one that would merely
      exec a harmless-but-noisy "command not found" rather than hang.
    - Does NOT flag files whose line-1 shebang is anything other than
      `#!/usr/bin/env python3`, or non-extensionless files (`*.py` etc.) —
      see `test_no_bin_polyglot_invariant.py` for that hazard class.
    - Does NOT silently tolerate a baselined file that no longer offends —
      see BASELINE HYGIENE above; that is a hard failure, not a no-op.

Spec backlink: PM ruling 2026-07-21 (`#!/bin/sh` sh/python polyglot wrapper
retired as legacy debt), docs/wiki/cross-platform-invocation-parity.md,
DR-076; the 2026-07-28 PM amendments that (1) re-scoped this suite from
"no module docstring" to this command-substitution-span rule, to avoid
colliding with CLAUDE.md's required-purpose-docstring convention, and (2)
converted it from a `designed_red` worklist into a baseline-backed ratchet,
matching `check-sh-suffix-polyglot.py`'s existing shape in this repo.

Run: python3 -m pytest coordinator/bin/tests/test_no_bin_docstring_command_substitution.py
Exit 0 = no NEW offender beyond the baseline, and no stale baseline entry;
non-zero = either a new offending path or a baseline entry needing removal
— both are printed.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ._polyglot_git_scan import blob_first_line, blob_full_text, tracked_files_under_coordinator

_PYTHON3_SHEBANG = "#!/usr/bin/env python3"

_TESTS_DIR = Path(__file__).resolve().parent
BASELINE_FILE = _TESTS_DIR / "command-substitution-docstring-baseline.txt"

# A backtick-delimited span (`` `...` ``, non-empty) or a `$(...)` span.
# Deliberately NOT narrowed to spans whose content names a known
# stdin-blocking command — see the module docstring's "WHY THIS SUITE
# GATES ON ANY command-substitution span" section for why completeness
# beats a smaller, allowlist-dependent offender list.
_BACKTICK_SPAN = re.compile(r"`[^`]+`")
_DOLLAR_PAREN_SPAN = re.compile(r"\$\([^)]*\)")


def _has_command_substitution(docstring: str) -> bool:
    return bool(_BACKTICK_SPAN.search(docstring) or _DOLLAR_PAREN_SPAN.search(docstring))


def _load_baseline() -> set[str]:
    """Read command-substitution-docstring-baseline.txt: one repo-relative
    path per line, `#`-comments and blank lines ignored. A missing baseline
    file is an ERROR (fail loud), matching check-sh-suffix-polyglot.py's
    `_load_baseline` — an absent baseline is indistinguishable from "every
    offender is new" and would flag all 31 pre-existing entries at once."""
    if not BASELINE_FILE.is_file():
        _fail(
            "command-substitution-docstring-baseline scan",
            f"baseline file not found: {BASELINE_FILE}",
        )
    entries = set()
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return entries


def _pass(label: str) -> None:
    print(f"  PASS: {label}")


def _fail(label: str, detail: str = "") -> None:
    msg = label if not detail else f"{label}\n    {detail}"
    raise AssertionError(msg)


def test_python3_shebang_docstrings_have_no_new_command_substitution() -> None:
    """Every tracked extensionless file under coordinator/ whose line-1
    shebang is `#!/usr/bin/env python3` and which carries a module
    docstring with a command-substitution span (backtick or `$(...)`) must
    be a KNOWN, baselined offender — not a new one. The baseline itself
    must stay current: every listed entry must still be a tracked
    python3-shebang extensionless file that still offends, or the entry is
    stale and must be removed."""
    files = tracked_files_under_coordinator()
    if not files:
        _fail(
            "no-bin-docstring-command-substitution scan",
            "found zero tracked files under coordinator/ — scan is "
            "almost certainly mis-scoped (repo root resolution bug), not a "
            "genuinely-empty repo",
        )
        return

    extensionless = [f for f in files if "." not in os.path.basename(f)]
    candidates = [f for f in extensionless if blob_first_line(f) == _PYTHON3_SHEBANG]
    candidate_set = set(candidates)

    baseline = _load_baseline()

    offenders = []
    parse_errors = []
    for f in candidates:
        text = blob_full_text(f)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            parse_errors.append((f, str(exc)))
            continue
        doc = ast.get_docstring(tree)
        if doc and _has_command_substitution(doc):
            offenders.append(f)
    offender_set = set(offenders)

    if parse_errors:
        detail = "\n    ".join(f"{f}: {err}" for f, err in parse_errors)
        _fail(
            f"{len(parse_errors)} python3-shebang extensionless entrypoint(s) "
            "failed to parse as Python — cannot verify the "
            "no-command-substitution-in-docstring invariant for them",
            f"offending paths:\n    {detail}",
        )
        return

    new_offenders = sorted(offender_set - baseline)
    stale_baseline_entries = sorted(baseline - offender_set)

    failures = []
    if new_offenders:
        detail = "\n    ".join(new_offenders)
        failures.append(
            f"{len(new_offenders)} NEW python3-shebang extensionless "
            "entrypoint(s) carry a command-substitution span (backtick or "
            "$(...)) inside their module docstring, and are not in the "
            "checked-in baseline:\n    "
            f"{detail}\n"
            "    Fix: rewrite the docstring so no backtick pair or $(...) "
            "span survives — neutralize by removing the backticks around "
            "an inline-code identifier (keep the prose), or rephrase. Do "
            "NOT delete the docstring; a module docstring is required at "
            "this structural boundary (CLAUDE.md § Implementation "
            "Standards). If this file is intentionally joining the "
            f"tracked backlog, add its path to {BASELINE_FILE} instead."
        )
    if stale_baseline_entries:
        detail = "\n    ".join(stale_baseline_entries)
        reason = (
            "no longer a tracked python3-shebang extensionless file, or its "
            "docstring no longer carries a command-substitution span"
        )
        failures.append(
            f"{len(stale_baseline_entries)} baseline entr(y/ies) no longer "
            f"offend ({reason}) and must be removed from the baseline:\n    "
            f"{detail}\n"
            f"    Fix: delete these line(s) from {BASELINE_FILE} — a stale "
            "entry left in place is a silent exemption, not a record."
        )

    if failures:
        _fail(
            f"{len(new_offenders)} new offender(s), "
            f"{len(stale_baseline_entries)} stale baseline entr(y/ies)",
            "\n\n".join(failures),
        )
    else:
        _pass(
            f"no new command-substitution offenders among {len(candidates)} "
            f"python3-shebang extensionless coordinator/ entrypoint(s) "
            f"({len(baseline)} baselined, all still current)"
        )
