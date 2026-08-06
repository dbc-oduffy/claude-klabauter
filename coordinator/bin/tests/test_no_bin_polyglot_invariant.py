#!/usr/bin/env python3
"""test_no_bin_polyglot_invariant.py — regression net for the PM-ratified
2026-07-21 rule "the `#!/bin/sh` sh/python polyglot wrapper is legacy debt,
not a blessed class": no tracked file under `coordinator/` may carry
`#!/bin/sh` as its line-1 shebang, and no tracked file may carry the
polyglot trampoline literal as a live line-1-adjacent header. The blessed
replacement shape (DR-076, cross-platform-invocation-parity) is a
`#!/usr/bin/env python3` shebang plus a co-located `.cmd` launcher — this
suite ALSO asserts that pairing (Task 2): an extensionless
`coordinator/bin/` entrypoint that has already flipped to the python3
shebang must not have lost its `.cmd` half, which is what makes it runnable
on Windows (the primary platform — CLAUDE.md § Runtime conventions). Losing
the `.cmd` while keeping the python3 shebang is BROKEN ON WINDOWS and is
exactly the trap this suite exists to catch.

Scans the REAL repo via `git ls-files -- coordinator` (not a scratch
fixture repo) and reads each candidate's first ~20 lines via `git show
:<path>` (the INDEX-staged blob, not the working tree) — the migration this
suite ratchets against is landing concurrently as multiple agents edit the
working tree, so a worktree-based scan would read a moving target;  the
INDEX is what actually ships in the next commit. Matches the same
INDEX-not-worktree posture as the sibling
test_shebang_index_mode_invariant.py.

FALSE-POSITIVE TRAP (read before touching EXCLUDED_TRAMPOLINE_DOC_FILES):
a naive whole-file substring `grep -l` for either the `#!/bin/sh` shebang
or the trampoline literal matches files that legitimately CONTAIN either
string as documentation prose or test-fixture data, not as a live header —
e.g. `check-bin-sh-polyglot.py`'s own module comment quotes the trampoline
verbatim to describe the invariant it enforces, and
`check-sh-suffix-polyglot.py`'s module docstring does the same. Both quotes
land within the first 20 lines (right where a live trampoline would sit),
so window-scoping alone does not exclude them — hence the explicit,
justified EXCLUDED_TRAMPOLINE_DOC_FILES list below, in the same idiom as
`verify-cc-root-source-guard-sync.py`'s `EXCLUDED_SUFFIXES`. This suite
avoids the trap structurally by (a) reading line 1 in isolation for the
shebang assertion — never a whole-file substring search — and (b) scoping
the trampoline-literal assertion to a small header window (first
`TRAMPOLINE_WINDOW` lines, matching the window `check-bin-sh-polyglot.py`
and `docs/wiki/coordinator-tripwires.md` already use to define "live
trampoline" vs. incidental mention) rather than a whole-file search.

TIMING (2026-07-21): 85 files are mid-migration to the DR-076 shape as this
suite lands. It is EXPECTED that both invariant assertions currently report
offenders — the offender list IS the migration's remaining work, not a
defect in this test. Do not weaken an assertion to make it pass against a
half-migrated tree; re-run after the migration lands.

Run: python3 coordinator/bin/tests/test_no_bin_polyglot_invariant.py
Exit 0 = both invariants hold (zero offenders); non-zero = at least one
tracked file still carries the legacy shape — offending paths are printed.

NEGATIVE SPEC
    - Does NOT flag `#!/usr/bin/env bash` shell scripts — the invariant is
      specifically about the `/bin/sh` polyglot shim, not about shell
      scripts existing. A pure-bash `.sh` file with a `bash` shebang is a
      different, unrelated class and is silently out of scope.
    - Does NOT whole-file substring-match the trampoline literal — see the
      FALSE-POSITIVE TRAP note above. Only a header-window hit counts.
    - Does NOT assert the `.cmd`-pairing invariant for every extensionless
      file, only for one whose shebang has ALREADY flipped to
      `#!/usr/bin/env python3` — a file still mid-migration (old `#!/bin/sh`
      shebang) is caught by the first assertion instead; asserting `.cmd`
      presence on it too would double-count the same offender under a
      second, less-precise label.

Spec backlink: PM ruling 2026-07-21 (`#!/bin/sh` sh/python polyglot wrapper
retired as legacy debt), docs/wiki/cross-platform-invocation-parity.md,
DR-076.
"""
from __future__ import annotations

import os

from ._polyglot_git_scan import (
    blob_first_line,
    blob_header,
    tracked_bin_direct_children,
    tracked_files_under_coordinator,
)

# Header window (lines) within which the trampoline literal is considered a
# live header rather than an incidental/documentation mention. Matches the
# window `check-bin-sh-polyglot.py`'s `_has_trampoline()` uses (see its
# comment: "Trampoline past line 20 is invisible to sh at exec time") and
# the window `docs/wiki/coordinator-tripwires.md` cites for the same
# invariant ("no trampoline in its first 20 lines").
TRAMPOLINE_WINDOW = 20

# The verbatim polyglot trampoline literal every live bearer carries.
TRAMPOLINE = (
    '\'\'\'\'exec "$(command -v python3 || command -v python || command -v py)" '
    '"$0" "$@" #\'\'\''
)

# ---------------------------------------------------------------------------
# EXCLUDED_TRAMPOLINE_DOC_FILES — documented exclusion list, same idiom as
# verify-cc-root-source-guard-sync.py's EXCLUDED_SUFFIXES. Each entry
# legitimately CONTAINS the trampoline literal within the header window as
# prose/docstring describing the invariant, not as a live re-exec line.
# Path relative to repo root. Does NOT exclude these files from the
# `#!/bin/sh`-shebang assertion — that assertion reads line 1 in isolation,
# which neither file trips (both already carry a non-`/bin/sh` shebang).
# ---------------------------------------------------------------------------
EXCLUDED_TRAMPOLINE_DOC_FILES = {
    # module docstring (lines 6-8) quotes the trampoline verbatim to describe
    # the invariant this checker enforces — the SAME invariant this suite
    # now also enforces. Not a live header: this script's own shebang is
    # `#!/usr/bin/env python3` (ported from the pre-DR-076 `.sh` polyglot
    # shim; the `.sh` path this exclusion previously named no longer exists
    # on disk — re-anchored on the current `.py` filename), and the quoted
    # text sits inside the triple-quoted module docstring, never executed
    # as a re-exec line.
    "coordinator/bin/check-bin-sh-polyglot.py",
    # module docstring (line 7) quotes the trampoline verbatim for the same
    # documentation reason. Not a live header: shebang is
    # `#!/usr/bin/env python3` and the quote sits inside the triple-quoted
    # module docstring, never executed as a re-exec line.
    "coordinator/bin/check-sh-suffix-polyglot.py",
}

# ---------------------------------------------------------------------------
# _SH_SHEBANG_EXEMPT — documented exclusion list for the `#!/bin/sh`-shebang
# assertion ONLY (test_no_bin_sh_shebang). Distinct from
# EXCLUDED_TRAMPOLINE_DOC_FILES above: that list exempts files from the
# polyglot-trampoline-literal assertion (a file merely quoting the literal in
# a comment/docstring); this list exempts files that DELIBERATELY keep a
# live `#!/bin/sh` shebang because the invariant they implement REQUIRES
# POSIX sh — a genuinely different reason, so it gets its own set rather
# than folding into the trampoline list. Each entry must name the specific
# reason the file cannot be ported to Python. Path relative to repo root.
# ---------------------------------------------------------------------------
_SH_SHEBANG_EXEMPT = {
    # Detects whether the CURRENT invoking shell is bash >= 4 — it runs
    # BEFORE/to-decide the interpreter, so it cannot itself require a
    # Python (or bash>=4) runtime without begging the question it exists to
    # answer. Deliberately kept as POSIX sh by the de-polyglot migration
    # (commit 28a7b868: "invoking-shell-bash4-probe.sh correctly kept as
    # POSIX sh"). Permanent exemption, not a migration backlog item.
    # (DR-076 governs the de-polyglot migration this exemption departs from.)
    "coordinator/scripts/lib/invoking-shell-bash4-probe.sh",
}

def _pass(label: str) -> None:
    print(f"  PASS: {label}")


def _fail(label: str, detail: str = "") -> None:
    msg = label if not detail else f"{label}\n    {detail}"
    raise AssertionError(msg)


def test_no_bin_sh_shebang() -> None:
    """Assertion (a): no tracked file under coordinator/ has `#!/bin/sh` as
    its line-1 shebang — the retired polyglot shebang, banned outright per
    the PM ruling regardless of whether a trampoline follows it."""
    files = tracked_files_under_coordinator()
    if not files:
        _fail(
            "no-bin-sh-shebang scan",
            "found zero tracked files under coordinator/ — scan is "
            "almost certainly mis-scoped (repo root resolution bug), not a "
            "genuinely-empty repo",
        )
        return

    offenders = [
        f
        for f in files
        if blob_first_line(f) == "#!/bin/sh" and f not in _SH_SHEBANG_EXEMPT
    ]

    if offenders:
        detail = "\n    ".join(offenders)
        _fail(
            f"{len(offenders)} tracked file(s) still carry #!/bin/sh as line-1 shebang",
            f"offending paths:\n    {detail}\n"
            "    Fix: migrate to #!/usr/bin/env python3 + a co-located .cmd "
            "launcher (DR-076, coordinator/bin/gen-launcher-shim.py).",
        )
    else:
        _pass(f"no #!/bin/sh offenders among {len(files)} tracked files under coordinator/")


def test_no_polyglot_trampoline_header() -> None:
    """Assertion (b): no tracked file under coordinator/ (outside the
    documented exclusion list) carries the polyglot trampoline literal
    within its header window — a live re-exec line, not an incidental
    mention."""
    files = tracked_files_under_coordinator()
    if not files:
        _fail(
            "no-polyglot-trampoline-header scan",
            "found zero tracked files under coordinator/ — scan is "
            "almost certainly mis-scoped (repo root resolution bug), not a "
            "genuinely-empty repo",
        )
        return

    offenders = []
    for f in files:
        if f in EXCLUDED_TRAMPOLINE_DOC_FILES:
            continue
        header = blob_header(f, TRAMPOLINE_WINDOW)
        if any(TRAMPOLINE in line for line in header):
            offenders.append(f)

    if offenders:
        detail = "\n    ".join(offenders)
        _fail(
            f"{len(offenders)} tracked file(s) still carry the polyglot trampoline "
            f"literal in the first {TRAMPOLINE_WINDOW} lines",
            f"offending paths:\n    {detail}\n"
            "    Fix: migrate to #!/usr/bin/env python3 + a co-located .cmd "
            "launcher (DR-076, coordinator/bin/gen-launcher-shim.py) and remove "
            "the trampoline line entirely.",
        )
    else:
        _pass(
            f"no polyglot-trampoline-header offenders among {len(files)} "
            f"tracked files under coordinator/ ({len(EXCLUDED_TRAMPOLINE_DOC_FILES)} "
            "documented doc/fixture exclusion(s) applied)"
        )


def test_python3_extensionless_entrypoints_have_cmd_launcher() -> None:
    """Task 2: every extensionless coordinator/bin/ entrypoint that has
    already flipped to the #!/usr/bin/env python3 shebang MUST have a
    co-located `.cmd` launcher. A depolyglotted script that loses its .cmd
    half is broken on Windows, which this repo treats as the primary
    platform (CLAUDE.md § Runtime conventions) — this is the assertion
    that would have caught that trap during the migration."""
    candidates = tracked_bin_direct_children()
    if not candidates:
        _fail(
            "python3-extensionless-cmd-pairing scan",
            "found zero tracked files directly under coordinator/bin/ — scan is "
            "almost certainly mis-scoped, not a genuinely-empty directory",
        )
        return

    cmd_paths = {f for f in candidates if os.path.basename(f).endswith(".cmd")}
    extensionless = [f for f in candidates if "." not in os.path.basename(f)]

    python3_entrypoints = [
        f for f in extensionless if blob_first_line(f) == "#!/usr/bin/env python3"
    ]

    offenders = [f for f in python3_entrypoints if f"{f}.cmd" not in cmd_paths]

    if not python3_entrypoints:
        # Not a failure: a not-yet-migrated tree legitimately has zero
        # candidates for this assertion — there is nothing to pair yet.
        # Printed (not silent) so a reviewer scanning output sees this
        # branch was reached rather than skipped by a scoping bug.
        _pass(
            "python3-extensionless-cmd-pairing: zero extensionless "
            "coordinator/bin/ entrypoints carry the #!/usr/bin/env python3 "
            "shebang yet — nothing to pair-check (pre-migration state)"
        )
        return

    if offenders:
        detail = "\n    ".join(offenders)
        _fail(
            f"{len(offenders)} python3-shebang extensionless entrypoint(s) missing "
            "their co-located .cmd launcher",
            f"offending paths (each needs a sibling <path>.cmd):\n    {detail}\n"
            "    Fix: coordinator/bin/gen-launcher-shim.py <name> to regenerate "
            "the .cmd (and .ps1, where applicable) launcher.",
        )
    else:
        _pass(
            f"all {len(python3_entrypoints)} python3-shebang extensionless "
            "coordinator/bin/ entrypoint(s) have a co-located .cmd launcher"
        )


