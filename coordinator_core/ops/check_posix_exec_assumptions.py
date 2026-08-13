"""coordinator_core.ops.check_posix_exec_assumptions — fleet-wide RED-on-
existence guard for POSIX-only execution assumptions.

Purpose: Windows is the P0 primary platform (PM ruling, 2026-07-28). Twelve
violation classes, grouped by failure axis and tiered by detection
confidence -- the tier determines whether a class can block or only report:

  - **Tier A** (`TIER_A_CLASSES`) — checkout-breakers: the repo cannot be
    cloned/checked out AT ALL on Windows (or, for `case_collision`, on
    Windows AND macOS), a strictly worse failure than a script that merely
    won't run. Exact git-index/path facts, zero AST, zero false-positive
    risk, fleet-measured near-zero -- so these are a HARD ZERO-TOLERANCE
    gate with NO baseline at all (`check_tier_a_zero_tolerance()`).
  - **Tier B** (part of `CLASSES`) — exact file-metadata facts (env-
    stripped shebang, extensionless `#!`-exec, git mode 100755). Large
    pre-existing debt fleet-wide, so these are BLOCKING but ratcheted
    against a frozen, shrink-only baseline.
  - **Tier C, promoted** (part of `CLASSES`) — `path_separator` and
    `posix_mode_bits` are code-content patterns, AST-detected, that started
    life BLOCKING, were briefly DEMOTED to report-only on 2026-07-28 over
    one specific defect (a blocking class firing on `os.access()` inside
    `if os.name != "nt":` -- correctly platform-guarded code, not debt),
    and were PROMOTED BACK to blocking the same day once that defect was
    fixed: `_is_windows_guarded()` (below) recognizes both a nested `If`
    guard and the harder short-circuit `and`-chain shape (the actual
    `retire-claude-bin.py` case), with positive-control fixtures proving
    the fix doesn't blanket-suppress detection entirely. Demoting once the
    cause is fixed would be consistency for its own sake at real
    enforcement cost -- report-only classes get skimmed and ignored. The
    residual gap (a bare early-return guard clause with no `else:`) is
    narrow and, when hit, surfaces as an immediately-resolvable EXEMPTIONS
    case rather than a silent trap -- see the per-class failure-message
    text in `check_against_baseline`.
  - **Tier D** (part of `REPORT_ONLY_CLASSES`) — `unresolved_cross_path`,
    a config/content class that must reconcile with (not duplicate)
    `check-machine-path-leak.py`.
  - **Residual** (part of `REPORT_ONLY_CLASSES`) — `unclassified`, a
    permanent catch-all so a Windows-hostile construct is never silently
    passed merely because no precise named class fits it yet.
  - **implicit_encoding** (`IMPLICIT_ENCODING_CLASSES`) — a separate failure
    axis from all of the above: a bare `open()` call with no explicit
    `encoding=` is a live cross-platform DATA-CORRUPTION bug (Windows
    Python's legacy default text encoding is locale-dependent, not UTF-8),
    not a checkout- or execution-assumption defect. Deliberately scoped to
    the builtin `open()` Name-call form only -- the `.open()`/`.read_text()`/
    `.write_text()` ATTRIBUTE-call form is NOT matched (see that constant's
    own comment for why: attribute-name-only matching produced ~400
    untrustworthy hits fleet-wide, and is absent here rather than shipped
    noisy). Zero-tolerance like Tier A (no baseline): the two repos this
    module has been run against had a small, mechanically-fixable count
    (34 files in claude-klabauter, 1 in example-doctrine-repo), fixed outright on
    2026-07-28 rather than grandfathered into a shrinking baseline.

Classes are independent tells and are NOT deduped against each other -- see
the Negative-spec below.

Tier A classes (git-index/path facts, zero-tolerance, no baseline -- see
`TIER_A_CLASSES` and `check_tier_a_zero_tolerance()` for the full contract):
`symlink_in_index`, `case_collision`, `reserved_filename`, `path_too_long`.

Tier B / blocking classes (ratcheted against a frozen baseline):

  - `env_shebang`        — a tracked file whose first line matches
                            `^#!/usr/bin/env`. Windows `CreateProcess` never
                            reads a shebang line at all, and `/usr/bin/env`
                            does not exist on Windows regardless.
  - `extensionless_exec` — a tracked file with no `.` in its basename (no
                            extension) whose first two bytes are `#!`. Stock
                            Windows resolves bare invocation via `PATHEXT`
                            (`.cmd`/`.exe`/...), never by shebang sniffing —
                            an extensionless file is invocable on POSIX by
                            shebang alone and dead on Windows.
  - `mode_100755`        — a tracked file whose git INDEX mode is `100755`.
                            NTFS has no POSIX exec bit; the mode is git
                            metadata with no meaning to a Windows checkout,
                            and its presence signals that some caller is
                            relying on POSIX exec permission as the actual
                            invocation mechanism.
  - `path_separator`     — Python code that hardcodes a platform path
                            separator instead of using `os.sep` /
                            `os.path.join` / `pathlib`: a `.replace("/",
                            "\\")`-shaped normalization hack (either
                            direction), a literal backslash string constant
                            (`"\\"`) used in string concatenation to build a
                            path, or `.split("\\")` to parse one. Detected
                            via AST on tracked `.py` files only — `os.sep`,
                            `os.path.join(...)`, and `pathlib.Path(...)` are
                            the fix, not a violation, and are structurally
                            invisible to these detectors (they match on
                            literal separator constants, not on path-shaped
                            strings generally).
  - `posix_mode_bits`    — Python code that reasons about POSIX permission
                            bits AT RUNTIME: `os.access(path, os.X_OK)`,
                            `os.chmod(path, mode)` where `mode` sets an exec
                            bit (literal int with `& 0o111` truthy, or a
                            `stat.S_IX*`/`S_IEXEC` reference), or
                            `<stat_result>.st_mode & <exec-bit-mask>`-shaped
                            tests. On Windows these are meaningless-to-lying
                            (`os.access(path, os.X_OK)` returns True for any
                            readable file regardless of any real
                            executability). Distinct from `mode_100755`
                            above: that class is git INDEX metadata about a
                            tracked file; this class is CODE that queries or
                            sets permission bits at runtime, wherever it
                            appears in the tree — the two are never
                            conflated.

    NEITHER `posix_mode_bits` NOR `path_separator` fires inside a branch (a
    nested `if os.name != "nt":` / `if sys.platform.startswith("win"):`
    etc., or an equivalent short-circuit `and`-chain) that structurally
    never runs on Windows — that is correct cross-platform code, not debt,
    and flagging it would train authors to route around the guard
    (`_is_windows_guarded`, precision fix 2026-07-28 after
    `retire-claude-bin.py:186` false-fired on exactly this shape). This fix,
    plus positive-control fixtures proving it doesn't blanket-suppress
    detection, is why both classes are BLOCKING here rather than demoted:
    they were briefly report-only the same day, then promoted back once the
    cause was fixed (see the CLASSES declaration's own comment for the full
    history). A bare early-return guard clause with no `else:` is NOT yet
    recognized — see that function's own docstring and the matching test's
    docstring for the stated gap; hitting it surfaces as an EXEMPTIONS-
    resolvable failure message, not a silent trap.

Tier D / residual — report-only classes (scanned, counted, and listed every
run, but NEVER fail `check_against_baseline` or `assert_baseline_not_grown`
— see Report-only contract below):

  - `unresolved_cross_path` — a tracked `.py` file containing a hardcoded
                            cross-machine/cross-drive path literal
                            (`/Users/<name>/...`, `/home/<name>/...`,
                            `<drive-letter>:\\...`) that does not resolve via
                            `${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-
                            claude-settings}` or the machine-local `repos.*`
                            registry. Scanned via AST on `.py` files only,
                            deliberately narrower than a repo-wide text-grep
                            (see Reconciliation with check-machine-path-
                            leak.py below) -- markdown/wiki prose citing an
                            example path is out of scope by construction, not
                            merely tolerated. The drive-letter pattern is
                            anchored to the ABSOLUTE start of the string (no
                            `re.MULTILINE`), closing the fleet's known
                            `[A-Za-z]:[/\\]`-matches-"https://" trap -- see
                            the pattern's own inline comment and
                            `test_scan_does_not_flag_url_with_single_letter_
                            looking_scheme` / `test_scan_does_not_flag_
                            short_scheme_url_variants` for the fixtures
                            proving it holds.
  - `unclassified`         — a residual bucket for other POSIX-only-smelling
                            constructs that don't yet warrant a dedicated
                            named class: `os.fork`/`os.uname`/
                            `pwd.getpwnam`/`grp.getgrnam`/`os.geteuid`/
                            `os.getegid`/`os.setuid`/`os.setgid` calls,
                            POSIX-only `signal.SIG*` names (`SIGHUP`,
                            `SIGUSR1`, `SIGUSR2`, `SIGCHLD`), and hardcoded
                            `/tmp/`-prefixed string literals (POSIX temp-dir
                            assumption; the fix is `tempfile.gettempdir()`).
                            Existence of this bucket is itself doctrine: an
                            empty/absent named-class match must never read as
                            "no violation" when it may just mean "no
                            detector for this shape yet" -- findings here are
                            candidates for promotion into a precise named
                            class over time, not a permanent home.

Report-only contract: `scan()` returns entries for BOTH the blocking classes
and the report-only classes (baseline-comparable shape either way), but
`check_against_baseline()` and `assert_baseline_not_grown()` operate ONLY
over the blocking `CLASSES` tuple -- report-only classes are never persisted
into the baseline JSON and never affect `ok`/exit code. `main()` prints a
separate, clearly-labeled report-only summary (current count + full list)
every run so a report-only finding is visible, never silent, even though it
cannot fail a gate.

This module is fleet-shaped: every function takes a `root` (the repo to
scan), so the SAME engine backs a guard in claude-klabauter's own tree and,
via cross-repo import (`_claude_klabauter_root.resolve_claude_klabauter_root()`), a guard
invoked from any sibling repo's own test tier (e.g. Example-doctrine-repo) against
ITS OWN tree and ITS OWN baseline. There is no notion of "the" tree here —
callers always name one.

Ratchet design (frozen baseline, shrink-only), blocking classes only.
~1,200 pre-existing violations existed across the fleet's two audited repos
as of 2026-07-28 for the original three classes; a hard RED on all of them
on day one would brick every ceremony gate. So: a baseline JSON file
(`state/posix-exec-baseline.json` by convention, but the path is
caller-supplied) enumerates the currently-known violations per blocking
class. `check_against_baseline()` fails only on a violation NOT already in
the baseline (a NEW regression) — existing debt is visible (the remaining
count is always printed) but non-blocking until fixed. `assert_baseline_
not_grown()` is the companion append-forbidden gate: it diffs the
baseline file's current on-disk content against a FIXED anchor commit — the
commit that first introduced the baseline file (resolved via `git log
--diff-filter=A`, not the moving tip `HEAD`) — and fails if any BLOCKING
class gained an entry relative to that anchor.

Anchor choice (2026-07-28 review fix, replacing an inert `HEAD`-relative
diff): comparing against `HEAD` is structurally unable to ever fire once a
widening commit has landed, because the instant that commit exists, `HEAD`
IS the widened content and `current == HEAD` trivially. The two callers of
this function (this repo's and example-doctrine-repo's own real-tree pytest suites) run
`assert_baseline_not_grown` as a plain pytest assertion, necessarily AFTER
any widening commit has already landed — neither repo wires this into a
git pre-commit hook (staged-vs-parent-HEAD is the only diff shape where
`HEAD` would be meaningful), and requiring one would only hold on machines
that have actually run the installer, which does not fit this fleet's
shared-branch, many-concurrent-session shape. Anchoring to the file's own
introducing commit instead means the diff is against something that never
moves, so it keeps firing no matter how many commits land afterward.
**Named residual gap** (mirrors this module's other honestly-documented
detector gaps, e.g. `_is_windows_guarded`'s early-return case above): an
entry that was part of the ORIGINAL frozen baseline, later fixed and
removed, and then maliciously re-added to hide a real regression would NOT
be caught by this function alone (it was never "new" relative to the fixed
anchor) — but `check_against_baseline()` independently catches that exact
regression from the other direction, because the live `scan()` would show
the violation again and the (shrunk) baseline `current` list no longer
contains it, so `check_against_baseline` fails as a genuinely NEW violation
regardless of what `assert_baseline_not_grown` does. The two functions are
a matched pair for this reason — neither alone is a full contract.

EXEMPTIONS mirrors `PY_ENTRYPOINT_EXEMPTIONS` in
`coordinator_core/test_bin_launcher_parity.py`: a per-class dict of relpath -> a written,
file-specific reason a tracked file is a genuine POSIX-only carve-out and
should never enter a baseline (i.e. is invisible to the guard, not merely
grandfathered into it). Widening this dict silently, without a reason
string, is exactly the failure mode PY_ENTRYPOINT_EXEMPTIONS's own
docstring warns against, and the same discipline applies here. EXEMPTIONS
applies uniformly to blocking AND report-only classes (an exempted file is
invisible everywhere, not merely non-blocking).

The admission test for an exemption is NOT "this file is meant to be typed
as a bare word" — anyone can declare that of any script, and it would turn
the dict into the loophole it exists not to be. It is: **a Windows-invocable
counterpart to this exact entrypoint demonstrably exists**, so the POSIX-only
file is one deliberate leg of a two-leg pair rather than a portability hole.
A file with no such counterpart is debt, not a carve-out, and belongs in a
baseline.

That two-leg-pair test is scoped to entrypoint-identity classes
(`env_shebang`, `extensionless_exec`, `mode_100755`) -- it asks whether a
Windows-invocable twin of THIS FILE exists. `path_separator` and
`posix_mode_bits` entries are a different construct-identity question (is
this literal separator/mode-bit use FOR host-independent string handling or
Windows-syntax parsing, vs. a live filesystem decision that silently lies
on Windows) and are governed by the parallel, sanctioned two-way test in
`docs/reference/posix-portability-fix-vs-carveout.md` instead, not by this
paragraph. Do not weaken this paragraph's bar for the entrypoint-identity
classes it does govern.

Keying (repo-scoped, closed 2026-08-03 — was the example-doctrine-repo memo of
2026-07-28's open caveat): EXEMPTIONS is keyed
`class -> repo_key -> relpath -> reason`, and `scan()` subtracts ONLY the
sub-dict belonging to the repo actually being scanned. Bare-relpath keying
(the prior shape) was a fleet-global over-exemption: this module is
fleet-shaped, so `coordinator/scripts/setup.py` — granted for claude-klabauter
— also exempted that exact relpath in EVERY sibling repo, silently hiding a
genuinely defective sibling file the guard exists to report. `repo_key` is
NOT a new identity mechanism: it is the fleet's existing `repos.<key>`
machine-local registry vocabulary (`repos.example_doctrine_repo`,
`repos.claude_klabauter`), derived from the repo directory basename by the
same normalization as `coordinator_core.install.first_run._derive_repo_key`,
`cross-repo-memo`'s `_receiver_repo_key`, and
`coordinator_core.ops.register_discovered_repos` — see `repo_key_for_root`.
**Named residual gap:** a clone checked out under a NON-canonical directory
name (`claude-klabauter-2`, a CI `work/` dir) derives a different key and gets
none of its exemptions, so the guard OVER-reports there. That is the
fail-loud direction — a visibly red ratchet an operator can diagnose in one
read — and is the deliberate trade against the silent under-report this
keying replaces. The remedy is naming the clone canonically, never widening
a key back to bare-relpath.

EXEMPT_PREFIXES (directory-scope exclusion, 2026-08-03) is the OTHER
carve-out mechanism, and it is deliberately not a convenience spelling of
EXEMPTIONS. EXEMPTIONS names one file and asks "is THIS file a two-leg
pair / a correct portable idiom?". EXEMPT_PREFIXES names a DIRECTORY whose
contents are not this repo's code at all, and asks a question about the
directory's PURPOSE. Admission test — a prefix qualifies only if BOTH hold:

  1. **Verbatim foreign or frozen bytes.** Every file under the prefix is a
     byte-for-byte copy of something authored for, or by, somewhere else —
     another repo's tree published back out untransformed, or a point-in-time
     snapshot of a reviewed diff. Byte-drift from its counterpart is the one
     thing the directory exists to prevent, so the edit this guard asks for
     is not merely inconvenient, it is *wrong*: it falsifies evidence, or it
     breaks the artifact at the destination it is reproduced into.
  2. **Whose runtime is it?** Nothing under the prefix is executed, imported,
     or installed by this repo or by anything this repo installs. The
     portability question ("will this run on Windows?") has no referent here,
     because the only runtime these files ever have belongs to someone else,
     on a platform contract this repo does not set. Where a live counterpart
     exists, it is scanned in the repo that owns it, where a fix would land.

Note what criterion 2 does NOT say: "not shipped". Content published verbatim
to a foreign destination still qualifies — `dist/mirror-native/` is exactly
that, and its POSIX shebangs and exec bits are load-bearing at the mirror's
own CI gate. Shipping is not the discriminator; owning the runtime is.

Fails the test, and therefore is NOT a prefix candidate: a directory that is
merely noisy or legacy; vendored-but-imported code (a dependency this repo
actually imports IS live code on the Windows path, wherever it sits); a
generated tree this repo then runs; or "we'll port it later" — that is debt,
and debt belongs in the baseline, which stays visible and shrink-only, not
behind an invisibility screen. "Don't check this directory" is not a reason;
"this directory's bytes are not ours to change, and nothing here runs here"
is.

Scope, narrowed on purpose: a prefix exclusion suppresses every class in
`PREFIX_EXCLUDABLE_CLASSES` — but NOT `TIER_A_CLASSES`. Tier A is
checkout-breakers: a case collision or a reserved device name inside a frozen
snapshot tree still breaks `git clone` on Windows for the WHOLE repo, and no
argument about the file being evidence changes that. So the big hammer stops
exactly where the failure stops being about the file's own executability and
starts being about the repo checking out at all — the one place where "we
can't edit the evidence" means "then this tree cannot be tracked here", which
is a call for the tree's owner, not a carve-out this module can grant.

Staleness (same discipline as `test_no_parity_exemption_is_stale` in
`coordinator_core/test_bin_launcher_parity.py`): a declared prefix matching NO
tracked file in the repo being scanned is a dead rule, and
`check_no_stale_exempt_prefixes()` fails on it. A prefix that silently
outlives its tree is how a directory-wide exclusion rots into a blanket
permission for whatever gets dropped at that path later.

FILE-based prefix exclusions (`_file_exempt_prefixes()`, 2026-08-03,
Example-retrieval-repo thread): `<root>/state/posix-exec-exempt-prefixes.json` is the
FILE-based sibling of the in-code `EXEMPT_PREFIXES` dict above, granting the
exact same admission test without a change to this module. `EXEMPT_PREFIXES`
suits a small set of grants this module's maintainer curates one at a time
fleet-wide; a caller with an entire vendored/third-party subtree to exclude
would otherwise face either hand-listing every file in EXEMPTIONS (the
one-file-at-a-time mechanism this coarser one exists to replace for that
case) or sending a change here for a tree it does not own. The file is read
from the SAME `root` `scan()` is scanning, so -- unlike `EXEMPT_PREFIXES`'s
`repo_key` keying, needed because one in-code dict serves every fleet repo --
it needs no repo key: a grant in claude-klabauter's tree can never leak into a
sibling's scan. Same shape bar as `EXEMPT_PREFIXES` (trailing `/`,
repo-relative, forward-slash spelled, non-escaping, written non-empty
reason), same staleness gate (`check_no_stale_exempt_prefixes()` reads both
sources), same scope limit (`PREFIX_EXCLUDABLE_CLASSES` -- never Tier A).
`is_prefix_excluded()`/`_exempt_prefixes()` take `root` as an OPTIONAL third
argument defaulting to `None` (file-based prefixes not consulted) so every
call site that predates this mechanism keeps its exact prior behavior.

Reconciliation with `check-machine-path-leak.py` (class 6 boundary): that
script is a pre-commit gate scoped to exactly two config surfaces —
`settings.json` (hard block on ANY machine-absolute path leaf) and
`working-repos.yaml` (soft warn, current-machine-home-rooted only; foreign-
machine catalog entries are deliberately NOT flagged there). `unresolved_
cross_path` here is a repo-wide `git ls-files` scan restricted to `.py`
source, i.e. a disjoint surface by file-type construction — it never
inspects `settings.json` or `working-repos.yaml` (neither is a `.py` file),
so the two guards cannot fight over the same file's disposition. If a
future change ever widened this class beyond `.py`, `settings.json` and
`working-repos.yaml` would need an explicit skip -- noted here so that
widening does not silently create the collision this design avoids today.

Negative-spec:
    - Does NOT walk the working tree or the filesystem directly — enumerates
      via `git ls-files` (and `git ls-files -s` for index mode), exactly
      like `check-machine-path-leak.py` and `test_bin_launcher_parity.py`,
      so untracked scratch never trips the guard.
    - Does NOT attempt to fix anything. Report-only in the sense of "no
      autofix" applies to every class, blocking or not; the baseline is the
      only state this module writes, and only a human/EM edit writes it —
      no `--update-baseline` autofix flag exists, on purpose (an autofix
      that silently widens the baseline is the append-forbidden hole this
      module exists to close).
    - Does NOT dedupe across classes. A single file can appear in more than
      one class's violation list (e.g. an extensionless `#!/usr/bin/env
      python3` file is simultaneously `env_shebang` and, if also mode
      100755, `mode_100755`; a `.py` file can be both `path_separator` and
      `unresolved_cross_path`) — this is intentional; each class is an
      independent tell with its own remediation.
    - Does NOT text-grep for classes 4-6. Every AST-scanned class parses the
      file with Python's own `ast` module and matches on syntax structure
      (call targets, binary-op operands, constant values), explicitly to
      avoid the false-positive shape `check-machine-path-leak.py`'s own
      docstring warns against ("text-grep false-positives on fixtures,
      commit-message args, and comment blocks"). Docstrings (the first
      `Expr(Constant(str))` statement of a module/class/function body) are
      excluded from `unresolved_cross_path` and `unclassified` literal
      matching for the same reason — a docstring citing an example path is
      documentation, not a hardcoded runtime assumption.
    - Does NOT scan non-`.py` files for classes 4-6. Bash is being retired
      fleet-wide (see example-doctrine-repo `coordinator.local.md` P0 bash-kill
      campaign) and the remaining count is near zero; adding a shell-syntax
      parser for a near-extinct substrate was not worth the added false-
      positive surface. A `.py`-only scope is stated as a real limitation,
      not implied to be complete coverage of every interpreter in the tree.

Spec backlink: example-doctrine-repo coordinator/docs/wiki/foreign-platform-path-guard.md
  (sibling guard for a related but distinct hazard class — settings.json /
  working-repos.yaml path leakage, not execution-assumption files)
Prior art: coordinator/bin/check-machine-path-leak.py (git-ls-files-based
  enumeration, staged-vs-explicit file discrimination, structural JSON/YAML
  parsing over text-grep); claude-klabauter
  coordinator_core/test_bin_launcher_parity.py (PY_ENTRYPOINT_EXEMPTIONS
  shape, design-as-offers failure messages naming the fix + the generating
  command).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from coordinator_core.win_portability import no_console_creationflags


_NO_WINDOW = no_console_creationflags()

# Blocking classes: ratcheted against the frozen baseline, gate the build.
#
# `path_separator` and `posix_mode_bits` were briefly DEMOTED to report-only
# on 2026-07-28 over one specific defect: the class fired on `os.access()`
# inside `if os.name != "nt":` -- correctly platform-guarded code, not debt.
# That defect is now FIXED (`_is_windows_guarded()` below, covering both a
# nested `If` guard and the harder short-circuit `and`-chain shape actually
# found in `retire-claude-bin.py`), with positive-control fixtures proving
# the fix doesn't blanket-suppress (an inverted branch still fires, an
# ungated call still fires) -- that evidence is what earns keeping this
# BLOCKING rather than demoting: demoting once the cause is fixed would be
# consistency for its own sake at the cost of real enforcement (report-only
# classes get skimmed and ignored). The one residual gap -- a bare
# early-return guard clause with no `else:` -- is narrow and, when hit,
# surfaces as a guard failure an author can immediately resolve via
# EXEMPTIONS (see the per-class failure-message text in
# `check_against_baseline`), not a silent trap.
CLASSES: Tuple[str, ...] = (
    "env_shebang",
    "extensionless_exec",
    "mode_100755",
    "path_separator",
    "posix_mode_bits",
)

# Report-only classes: scanned and printed every run, never gate the build,
# never persisted into the baseline JSON, never counted by the append-
# forbidden growth check.
REPORT_ONLY_CLASSES: Tuple[str, ...] = (
    "unresolved_cross_path",
    "unclassified",
)

# Tier A classes (2026-07-28 revised taxonomy, PM-reasoned): checkout-
# breakers, not run-breakers -- a file in this bucket keeps the REPO from
# cloning/checking out cleanly on Windows (or, for case_collision, on
# Windows AND macOS), a strictly worse failure than a script that merely
# won't execute. All four are exact git-index/path facts (no AST, no
# regex-on-content) and the fleet is measured near-zero on them, so they are
# a HARD ZERO-TOLERANCE gate -- no baseline, no ratchet, no "existing debt"
# concept. `check_tier_a_zero_tolerance()` fails on ANY current violation,
# full stop; EXEMPTIONS is the only escape, same admission test as every
# other class.
#   - `symlink_in_index`   — git index mode 120000. Needs Developer Mode or
#                            elevation to check out on stock Windows.
#   - `case_collision`     — two tracked paths identical except for case.
#                            Breaks checkout on Windows AND macOS (both
#                            default to case-insensitive filesystems).
#   - `reserved_filename`  — a path component matching a Windows reserved
#                            device name (CON/PRN/AUX/NUL/COM1-9/LPT1-9,
#                            case-insensitive, compared against the stem
#                            before the first `.`), OR a path containing a
#                            character forbidden in a Windows path
#                            (`: * ? " < > |`).
#   - `path_too_long`      — a tracked relpath over ~200 characters, sized
#                            against the 260-char Windows MAX_PATH ceiling
#                            once a clone root is prepended: the ~60-char
#                            margin assumes a clone root like
#                            `C:\Users\<name>\<repo>\` (a few named
#                            components, not a deeply nested install path).
#                            A clone root longer than that budget can still
#                            hit MAX_PATH even on a relpath this check
#                            passes -- this constant is a heuristic sized
#                            against a typical clone root, not a proof.
TIER_A_CLASSES: Tuple[str, ...] = (
    "symlink_in_index",
    "case_collision",
    "reserved_filename",
    "path_too_long",
)

_PATH_TOO_LONG_THRESHOLD = 200

_RESERVED_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}

_FORBIDDEN_PATH_CHARS = re.compile(r'[:*?"<>|]')

# `implicit_encoding` (2026-07-28, separate failure axis from Tier A/B/C):
# a bare `open()` call with no explicit `encoding=` and a non-binary mode.
# Windows Python's legacy default text encoding is `cp1252`/locale-
# dependent, not UTF-8 -- these are live cross-platform DATA-CORRUPTION
# bugs on read/write of any non-ASCII byte, not a style nit. Scoped
# DELIBERATELY NARROW to the builtin `open()` Name-call form only: an
# attribute-call form (`some_obj.open(...)`, `.read_text()`, `.write_text()`)
# is NOT matched here, because matching on attribute NAME ALONE (with no
# receiver-type confirmation that `some_obj` is actually a `pathlib.Path`)
# produced ~400 hits in a probe of this fleet's own repos and is untrustworthy
# -- it fires on ANY object exposing those method names. That form needs
# real receiver-type tracking to reach usable precision, which is its own
# separate, unbuilt task -- deliberately absent here rather than shipped as
# noisy. `binary_modes` (`'rb'`/`'wb'`/`'ab'`/`'xb'`/`'r+b'` etc., detected
# via a literal mode-string constant containing "b") are correctly excluded
# -- encoding is meaningless for a byte stream, not a violation.
#
# Zero-tolerance like Tier A (no baseline, no ratchet): the two repos this
# module has been run against had a small, mechanically-fixable violation
# count (34 files in claude-klabauter, 1 file in example-doctrine-repo, all fixed
# 2026-07-28 by inserting `encoding="utf-8"` at each site) -- small enough
# to fix outright rather than grandfather into a shrinking baseline.
#
# A call whose mode argument is NOT a string literal (`open(p, mode)`,
# `open(p, mode=m)`) is not classified as binary or text at all and never
# produces a finding (2026-08-03 narrowing, example-retrieval-repo thread) -- the
# prior behaviour treated an unresolved mode as "not binary", false-firing
# on a caller genuinely passing a binary mode through a variable. See
# `_open_call_mode`'s docstring for the sentinel shape.
IMPLICIT_ENCODING_CLASSES: Tuple[str, ...] = ("implicit_encoding",)

_BINARY_MODE_MARKER = "b"


# Sentinels for `_open_call_mode`'s return, distinguished from "no mode
# argument" (`_MODE_ABSENT`, defaults to text mode 'r' -- still a violation)
# because a call supplying a NON-literal mode (`open(p, mode)`, `open(p,
# mode=m)`) cannot be classified as binary or text at all: `mode` might
# resolve to `'rb'` at runtime, and this AST-only detector has no value to
# inspect. Treating that unresolved case as "not binary" (the pre-fix
# behaviour) produced false positives on exactly this shape -- a caller
# genuinely passing a binary mode through a variable was flagged as if it
# had hardcoded text mode. See `_scan_python_file`'s implicit_encoding block
# for how each sentinel is handled, and
# `test_scan_does_not_flag_open_with_variable_mode_argument` /
# `test_scan_does_not_flag_open_with_variable_mode_keyword` for the fixtures
# pinning this narrowing.
_MODE_ABSENT = object()
_MODE_DYNAMIC = object()


def _open_call_mode(call: ast.Call):
    """The literal mode string if `call` supplies one, `_MODE_ABSENT` if no
    mode argument/keyword was given at all, or `_MODE_DYNAMIC` if a mode
    argument exists but is not a string literal (a variable, an f-string, a
    non-string constant, or any other non-analyzable expression)."""
    if len(call.args) >= 2:
        arg = call.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        return _MODE_DYNAMIC
    for kw in call.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return _MODE_DYNAMIC
    return _MODE_ABSENT


def _open_call_has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


ALL_CLASSES: Tuple[str, ...] = (
    CLASSES + REPORT_ONLY_CLASSES + TIER_A_CLASSES + IMPLICIT_ENCODING_CLASSES
)

# Per-class exemption dict: relpath -> written, file-specific reason. A file
# in this dict is invisible to scan() entirely (never enters a baseline,
# never counts toward debt, never appears in a report-only listing) --
# distinct from a baseline entry, which is visible debt merely not yet
# fixed. Empty by design for the new classes as of authoring; see module
# docstring for the admission test before adding one.
_EXAMPLE_GAME_REPO_FORWARDER_REASON = (
    "example-game-workbench-repo plugin/example-game-repo-control/bin/example-game-repo-control — the "
    "POSIX leg of a deliberate two-leg pair, not a portability hole. The "
    "Windows leg is <settings-home>/bin/example-game-repo-control.cmd, written by "
    "example_game_repo_setup.sh phase_install_path_shim and PATH-injected on Windows "
    "(install-substrate.sh Step 3b). This static forwarder exists solely "
    "because <settings-home>/bin is NOT on PATH on macOS/Linux, so it rides "
    "the harness plugin-bin PATH injection to give bare-word "
    "`example-game-repo-control` reach there. Extensionless and shebanged is what "
    "makes it work on the only platforms it is for; giving it an extension "
    "defeats its entire purpose, and it is never the invocation path on "
    "Windows. Granted 2026-07-28 on example-game-repo-em's ask; the admission test it "
    "passes is the counterpart test in the module docstring, NOT a bare "
    "'meant to be typed' claim."
)

_DETECT_STAGED_ROLLBACK_REASON = (
    "coordinator/bin/detect-staged-rollback.py — the POSIX leg of a "
    "deliberate two-leg pair, not a portability hole. The Windows leg is "
    "the tracked sibling coordinator/bin/detect-staged-rollback.cmd, "
    "generator-owned by coordinator/bin/gen-launcher-shim.py (2026-07-19 "
    "Windows de-bash campaign) — it resolves a Python interpreter and runs "
    "this exact entrypoint python-direct, no bash re-exec. On POSIX the "
    "shebang + exec bit ARE the invocation mechanism (this is a direct-run "
    "CLI, not merely typed-as-bare-word); on Windows the `.cmd` is the "
    "actual invocation path and this file's shebang/mode bit are inert. "
    "Both legs are demonstrably present, satisfying the module's admission "
    "test."
)

_INSTALL_CLAUDE_KLABAUTER_PRECOMMIT_HOOK_REASON = (
    "coordinator/bin/install-claude-klabauter-precommit-hook.py — the POSIX leg of a "
    "deliberate two-leg pair, not a portability hole. The Windows leg is "
    "the tracked sibling coordinator/bin/install-claude-klabauter-precommit-hook.cmd, "
    "generator-owned by coordinator/bin/gen-launcher-shim.py (2026-07-19 "
    "Windows de-bash campaign) — it resolves a Python interpreter and runs "
    "this exact entrypoint python-direct, no bash re-exec. On POSIX the "
    "shebang + exec bit ARE the invocation mechanism (this is a direct-run "
    "CLI, not merely typed-as-bare-word); on Windows the `.cmd` is the "
    "actual invocation path and this file's shebang/mode bit are inert. "
    "Both legs are demonstrably present, satisfying the module's admission "
    "test."
)

_PLAN_TASKS_RESOLVE_REASON = (
    "coordinator/bin/plan-tasks-resolve — the POSIX leg of a deliberate "
    "two-leg pair, not a portability hole. The Windows leg is the tracked "
    "sibling coordinator/bin/plan-tasks-resolve.cmd, generator-owned by "
    "coordinator/bin/gen-launcher-shim.py (2026-07-19 Windows de-bash "
    "campaign) — it resolves a Python interpreter and runs this exact "
    "entrypoint python-direct, no bash re-exec. On POSIX the shebang + exec "
    "bit ARE the invocation mechanism (this is a direct-run CLI, not merely "
    "typed-as-bare-word); on Windows the `.cmd` is the actual invocation "
    "path and this file's shebang/mode bit are inert. Both legs are "
    "demonstrably present, satisfying the module's admission test. Not "
    "baseline-eligible: this file gained its shebang/exec bit today "
    "(edbf2cd18, fixing a real misfire — direct invocation without them "
    "silently failed), and the baseline is a frozen, shrink-only anchor "
    "that cannot grow to cover a new violation, however well-precedented "
    "its shape is among this directory's other ~60 same-shaped entrypoints."
)

_SCOPED_GIT_COMMIT_REASON = (
    "coordinator/bin/scoped-git-commit — the POSIX leg of a deliberate "
    "two-leg pair, not a portability hole. The Windows leg is the tracked "
    "sibling coordinator/bin/scoped-git-commit.cmd, generator-owned by "
    "coordinator/bin/gen-launcher-shim.py (2026-07-19 Windows de-bash "
    "campaign) — it resolves a Python interpreter and runs this exact "
    "entrypoint python-direct, no bash re-exec. On POSIX the shebang + exec "
    "bit ARE the invocation mechanism (this is a direct-run CLI, not merely "
    "typed-as-bare-word) — the em-operating-doctrine's `scoped-git-commit` "
    "PATH-bareword invocation shape depends on it; on Windows the `.cmd` is "
    "the actual invocation path and this file's shebang/mode bit are inert. "
    "Both legs are demonstrably present, satisfying the module's admission "
    "test. Not baseline-eligible: this file gained its shebang/exec bit "
    "today (edbf2cd18, fixing a real misfire — direct invocation without "
    "them silently failed), and the baseline is a frozen, shrink-only "
    "anchor that cannot grow to cover a new violation, however "
    "well-precedented its shape is among this directory's other ~60 "
    "same-shaped entrypoints."
)

_CHAIN_WALK_SETUP_SHIM_REASON = (
    "coordinator/scripts/setup.py — a compatibility forwarder with a "
    "tracked retirement, not a second entrypoint. The trampoline this file "
    "used to be now lives at coordinator/scripts/chain-walk.py, which "
    "carries this file's former env_shebang baseline entry, so the rename "
    "is net-zero against the frozen anchor. This shim survives only "
    "because two call sites in the coordinator-claude repo hardcode the "
    "old path; it inherits the shebang because callers invoke it exactly "
    "as they invoked its predecessor, and changing the invocation contract "
    "is the one thing a compat shim must not do. It retires with the shim "
    "once those call sites move to `python3 -m "
    "coordinator_core.ops.setup_chain_walker` (asked of example-doctrine-repo-em "
    "2026-08-03 via the doe-contract-stale-surfaces memo, item 4) — delete "
    "this entry then rather than letting it outlive the forwarder."
)

_M8_REVIEW_TRAIL_SNAPSHOT_REASON = (
    "FROZEN REVIEW-EVIDENCE SNAPSHOT, not live code. Everything under this "
    "prefix is a verbatim point-in-time copy of the claude-klabauter engine's own "
    "bash_guards/write_guards modules, checked into example-doctrine-repo's review trail so a "
    "landed review's subject can be re-read later; nothing imports, executes, "
    "or ships them, and the originals are already scanned in claude-klabauter "
    "where a fix would actually land. Scanning the snapshot double-counts the "
    "engine's own debt into a sibling repo's baseline and asks an author to "
    "edit an artifact whose whole purpose is to not change — editing it to "
    "satisfy the ratchet would falsify the evidence. Was three hand-listed "
    "`path_separator` relpaths until 2026-08-03; migrated wholesale to a "
    "prefix here because this reason's own text named that as the correct "
    "remedy, and because hand-listing guarantees a fourth entry the next time "
    "a snapshot lands."
)

_MIRROR_NATIVE_DESTINATION_REASON = (
    "VERBATIM DESTINATION-NATIVE MIRROR CONTENT, not this repo's code. "
    "dist/mirror-native/ is the source-of-truth home for files that live in a "
    "publish MIRROR's tree and are published outward byte-identically by "
    "percolate `inject` (no substitute, no depersonalize) — first tenant: "
    "claude-klabauter's .github/ CI harness, homed by 720d56204 under the "
    "plan docs/plans/2026-08-03-mirror-native-content-homed-and-injected.md, "
    "whose AC6 is that publishing reproduces that .github/ byte-identically "
    "from claude-klabauter alone. Nothing here is executed, imported, or installed by "
    "claude-klabauter or by anything claude-klabauter installs; the only runtime these files ever "
    "have is the destination's GitHub-Actions Linux runner, whose platform "
    "contract that repo sets and this one does not. Their "
    "`#!/usr/bin/env python3` lines and 100755 modes are not portability debt "
    "claude-klabauter may fix — they are LOAD-BEARING AT THE DESTINATION: the harness's "
    "own check-exec-bit.py gate fails the mirror's CI without them (which is "
    "why 720d56204 added the exec bit deliberately), and byte-drift from the "
    "mirror is the one thing a source-of-truth copy must not introduce. A "
    "genuine port lands in the mirror and is copied back here, never the "
    "other way round. Tier A still applies: a mirror tree that breaks the "
    "Windows CHECKOUT of THIS repo is not covered by this reason, because "
    "that failure is about the repo, not about the file's own runtime."
)

_REASON_CANON_STRING = (
    "This normalizes a path string -- a recorded frontmatter field, a "
    "tool_input.file_path payload, a git-diff/`ls-files` line, an env var, "
    "or a resolved-path fallback -- to a canonical forward-slash form for "
    "comparison or storage, independent of which platform the string's "
    "author or its current reader is on. os.sep is the wrong fix here: "
    "os.sep reflects the host running this code, not the platform the "
    "string came from, and is a no-op on POSIX that would leave a "
    "backslash written on Windows (or embedded in test fixture/frontmatter "
    "data) unnormalized. This is the correct, portable idiom for that job, "
    "not a POSIX assumption."
)

_REASON_TOOL_INPUT_PATH = (
    "Normalizes tool_input.file_path -- a string supplied by the editing "
    "tool, which may already contain either separator depending on the "
    "invoking platform -- to forward-slash form before this hook's own "
    "carve-out/suffix matching runs. os.sep would only recognize this "
    "host's native separator, missing a payload written with the other "
    "one; the literal .replace() is the fix, not the debt."
)

_REASON_WIN_SYNTAX_IN_TEXT = (
    "Parses Windows path syntax (a UNC \\\\host\\share prefix or a "
    "drive-letter form) appearing as literal text being scanned -- prose, "
    "a citation, or a bash-command argument -- not a filesystem path this "
    "process will open. The backslash is the syntax being recognized, "
    "correct on every host regardless of what os.sep is there; rewriting "
    "it to os.sep would stop recognizing the exact Windows-path shape this "
    "code exists to detect."
)

_REASON_HOST_NATIVE_DRIVE_RESOLVE = (
    "Review: code-reviewer flagged _REASON_WIN_SYNTAX_IN_TEXT as a false "
    "premise for this site -- it claims the string is never opened, but "
    "this function ends in Path(p).resolve(), which does stat the real "
    "filesystem. The construct is still a carve-out, on different grounds: "
    "_offer_normalize_path() first rewrites a Windows drive-letter prefix "
    "to MSYS/git-bash's forward-slash drive convention, THEN resolves it. "
    "Both the target and the cwd it's compared against are native to the "
    "SAME host this guard process is running on -- cwd is the guard's own "
    "process cwd, passed in by the caller, not a citation of a foreign "
    "machine's path. On a Windows box running git-bash, a `cd`-target "
    "written in native drive-letter form and the bash-reported cwd (in "
    "MSYS form) can differ only in drive-letter convention while naming "
    "the same real directory; without the rewrite step, resolve() would "
    "compare a Windows-form path against an MSYS-form one and never match "
    "even when they are the same place. The drive-letter rewrite is what "
    "makes the subsequent host-filesystem resolve() correct, not something "
    "in tension with it -- os.sep is still the wrong fix, since it does "
    "not know the MSYS convention either. This differs from "
    "REASON_CANON_STRING's territory: that class covers a string whose "
    "resolve is deliberately never taken (comparison/storage only), while "
    "this one's whole point is a correct host-filesystem resolve()."
)

_REASON_NOT_A_PATH = (
    '"\\\\" + c emits a regex escape character for a translated Python re '
    "character class -- this function has no relationship to filesystem "
    "paths at all; the AST shape (BinOp Add with a literal backslash "
    "constant) is structurally identical to a path-concatenation hack but "
    "the semantic content is unrelated. Renaming or restructuring this to "
    "satisfy a path-shaped detector would not make the code more portable "
    "-- it isn't path code to begin with."
)

_REASON_CHMOD_DIR_GAP = (
    "Simulates a write-blocked directory via os.chmod to exercise the "
    "corresponding error-handling path. On Windows, the read-only "
    "attribute os.chmod can toggle for a directory does not block writes "
    "into it the way a POSIX permission bit does -- this is a platform "
    "semantic gap in what chmod means for directories on NTFS, not a code "
    "defect fixable by passing different mode bits. No portable "
    "equivalent exists in the standard library; a genuine Windows-native "
    "write-block simulation (e.g. an ACL deny entry) is a separate "
    "mechanism, out of this convergence's scope. The test does not crash "
    "on Windows -- it degrades to weaker coverage there, which is a "
    "known, named limitation, not silently-passing debt."
)

_REASON_CHMOD_EXEC_FOR_SH = (
    "Sets the POSIX exec bit on a generated git-hook script that the "
    "sibling test in this file immediately executes via a hardcoded "
    "subprocess.run(['/bin/sh', str(hook)], ...) -- the exec bit is "
    "necessary and correct for that POSIX-only invocation path. The "
    "test's dependency on /bin/sh (not itself a posix_mode_bits-scanned "
    "construct) already makes this test POSIX-only end to end; making the "
    "isolated chmod call portable would not make the test runnable on "
    "Windows and would be cosmetic. A genuine Windows-parity leg for this "
    "test (installed .cmd hooks, no /bin/sh dependency) is a real "
    "follow-up but a materially larger change than this convergence's "
    "scope -- not taken here."
)

_REASON_CHMOD_MODE_PRESERVATION = (
    "Asserts a POSIX permission-bit VALUE (0o644 vs 0o755) is preserved "
    "byte-exact by --fix, the regression lock for '--fix must never apply "
    "or clear an exec bit on the live destination'. Windows chmod() only "
    "toggles the read-only attribute -- it cannot represent this "
    "owner/group/other distinction at all, so the assertion is meaningless "
    "there, not merely inconvenient. The test is skipif(win32)-guarded at "
    "the pytest-decorator level (a guard shape `_is_windows_guarded` does "
    "not see -- it only recognizes an inline branch, per that function's "
    "own docstring), so this is a real POSIX-only test, not workaroundable "
    "debt."
)

_REASON_SHELL_EMBED_FORWARD_SLASH = (
    "Rewrites a pathlib-rendered path (which on Windows renders with "
    "backslashes) to forward-slash form before interpolating it into a "
    "string handed to `sh -c`. This is not a comparison/storage "
    "canonicalization (REASON_CANON_STRING's territory) -- it exists "
    "because outside quotes, POSIX sh treats a bare backslash as an "
    "escape-sequence introducer (`\\U`, `\\A`, ...) and strips it, "
    "corrupting the embedded path; a forward-slash path is accepted "
    "unchanged by both Windows Python and POSIX sh, and is a no-op on "
    "POSIX where the path was never backslashed to begin with. os.sep is "
    "the wrong fix: it reflects the host running the test, not the shell "
    "dialect the resulting string will be parsed by, which is always "
    "POSIX sh here regardless of host."
)

_REASON_WIN_SYNTAX_AS_FIXTURE = (
    "Constructs a Windows-shaped path string as FIXTURE DATA fed to the "
    "guard under test -- the sibling of _REASON_WIN_SYNTAX_IN_TEXT, which "
    "covers the detector side of the same pair (that reason's text says "
    "'text being scanned', which is literally false here: this is the "
    "specimen doing the scanning is aimed at, authored one call away). The "
    "backslash IS the shape under test, so os.sep is the wrong fix in the "
    "strongest available sense -- it would silently rewrite the specimen to "
    "'C:/...' on POSIX and the test would stop exercising the Windows arm "
    "it exists for, while still passing. guard_message_corpus.py's "
    "_wg_settings_json_write_fire() proves it: it monkeypatches the guard's "
    "_is_windows to False precisely so the drive-letter branch is the one "
    "taken, on every host. Note both sites spell the separator as a "
    "concatenation of single-character constants ('C:' + '\\\\' + 'Users') "
    "rather than one literal -- that spelling is an artifact of evading a "
    "TEXT-GREP path-leak detector, and is exactly the false-positive shape "
    "this module's AST approach was built to judge on structure instead."
)

_REASON_MSYS_TRANSLATE_WINDOWS_GUARDED = (
    "translate_msys_path()'s whole body is gated by a bare `if not "
    "_host_is_windows(): return path` early return with no `else:` -- "
    "exactly the KNOWN, NAMED gap in `_is_windows_guarded()` this module's "
    "own failure message and docstring describe (a bare early-return guard "
    "clause is not yet recognized as branch-guarding the sibling "
    "statements that follow it). The flagged `.replace(\"/\", \"\\\\\")` "
    "only executes once that guard has already confirmed the host IS "
    "Windows, building a well-formed native `X:\\...` path from an "
    "MSYS-spelled one for a subsequent native `ntpath.join` -- the correct "
    "construct for that platform, not a POSIX assumption. Adding an "
    "`else:` purely to satisfy the detector is exactly what the gate's own "
    "failure message forbids."
)

_REASON_ENTRYPOINT_INTERPRETER_NONE_IS_POSIX_ONLY = (
    "`os.access(script_path, os.X_OK)` in `_run_one_entrypoint` only runs "
    "when `interpreter is None`, and `_resolve_entrypoint_gate_interpreter` "
    "(the sole producer of that value, called once per `run_entrypoint_gate` "
    "sweep) returns `None` if-and-only-if `os.name != \"nt\"` -- see that "
    "function's own docstring and its `if os.name != \"nt\": return None` "
    "body. The guard is real, just expressed across two functions rather "
    "than as a local branch `_is_windows_guarded()` can see (the same "
    "documented detection gap as a bare early-return, one level removed) -- "
    "this call never executes on Windows, where `os.access(..., os.X_OK)` "
    "would otherwise lie (returns True for any readable file). Restructuring "
    "the call site to satisfy the detector would not change what actually "
    "runs; the invariant already holds."
)

#: Fleet repo keys these exemptions are granted FOR. Values are the
#: `repos.<key>` machine-local registry vocabulary (== `repo_key_for_root`
#: of that repo's canonical clone directory), named here so a typo in a
#: nested key is a NameError at import rather than a silently-inert grant.
REPO_CLAUDE_KLABAUTER = "claude_klabauter"
REPO_EXAMPLE_DOCTRINE_REPO = "example_doctrine_repo"
REPO_EXAMPLE_GAME_WORKBENCH_REPO = "example_game_workbench_repo"


EXEMPTIONS: Dict[str, Dict[str, Dict[str, str]]] = {
    "env_shebang": {
        REPO_EXAMPLE_GAME_WORKBENCH_REPO: {
            "plugin/example-game-repo-control/bin/example-game-repo-control": _EXAMPLE_GAME_REPO_FORWARDER_REASON,
        },
        REPO_CLAUDE_KLABAUTER: {
            "coordinator/bin/detect-staged-rollback.py": _DETECT_STAGED_ROLLBACK_REASON,
            "coordinator/bin/install-claude-klabauter-precommit-hook.py": _INSTALL_CLAUDE_KLABAUTER_PRECOMMIT_HOOK_REASON,
            "coordinator/scripts/setup.py": _CHAIN_WALK_SETUP_SHIM_REASON,
            "coordinator/bin/plan-tasks-resolve": _PLAN_TASKS_RESOLVE_REASON,
            "coordinator/bin/scoped-git-commit": _SCOPED_GIT_COMMIT_REASON,
        },
    },
    "extensionless_exec": {
        REPO_EXAMPLE_GAME_WORKBENCH_REPO: {
            "plugin/example-game-repo-control/bin/example-game-repo-control": _EXAMPLE_GAME_REPO_FORWARDER_REASON,
        },
        REPO_CLAUDE_KLABAUTER: {
            "coordinator/bin/plan-tasks-resolve": _PLAN_TASKS_RESOLVE_REASON,
            "coordinator/bin/scoped-git-commit": _SCOPED_GIT_COMMIT_REASON,
        },
    },
    "mode_100755": {
        REPO_EXAMPLE_GAME_WORKBENCH_REPO: {
            "plugin/example-game-repo-control/bin/example-game-repo-control": _EXAMPLE_GAME_REPO_FORWARDER_REASON,
        },
        REPO_CLAUDE_KLABAUTER: {
            "coordinator/bin/detect-staged-rollback.py": _DETECT_STAGED_ROLLBACK_REASON,
            "coordinator/bin/install-claude-klabauter-precommit-hook.py": _INSTALL_CLAUDE_KLABAUTER_PRECOMMIT_HOOK_REASON,
            "coordinator/scripts/setup.py": _CHAIN_WALK_SETUP_SHIM_REASON,
            "coordinator/bin/plan-tasks-resolve": _PLAN_TASKS_RESOLVE_REASON,
            "coordinator/bin/scoped-git-commit": _SCOPED_GIT_COMMIT_REASON,
        },
    },
    "path_separator": {
        REPO_CLAUDE_KLABAUTER: {
            "coordinator_core/write_guards/_case_fold_path.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/block_derived_global_doctrine_write.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/block_home_dir_memo_delivery.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/block_oss_mirror_memo_delivery.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/guard_memory_store_cap.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/nudge_new_sh_file_naked_python.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_prose_queue_append.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_prose_queue_creation.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/validate_frontmatter_schema_advisory.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/validate_frontmatter_schema_deny.py": _REASON_CANON_STRING,
            "coordinator_core/bash_guards/guard_offer_git_c.py": _REASON_HOST_NATIVE_DRIVE_RESOLVE,
            "coordinator_core/bash_guards/tests/test_advisory_value_registry.py": _REASON_CANON_STRING,
            "coordinator_core/ops/append_integrator_dispositions.py": _REASON_CANON_STRING,
            "coordinator_core/ops/check_auto_memory_drained.py": _REASON_CANON_STRING,
            "coordinator_core/ops/session/fix_concrete_path_citations.py": _REASON_WIN_SYNTAX_IN_TEXT,
            "coordinator_core/ops/session/guard_concrete_path_citations.py": _REASON_WIN_SYNTAX_IN_TEXT,
            "coordinator_core/ops/session/guard_foreign_platform_paths.py": _REASON_CANON_STRING,
            "coordinator_core/ops/session/safe_commit_offer.py": _REASON_CANON_STRING,
            "coordinator_core/install/ensure_venv.py": _REASON_CANON_STRING,
            "coordinator_core/install/uninstall_legs.py": _REASON_CANON_STRING,
            "coordinator_core/baton_assemble/__init__.py": _REASON_CANON_STRING,
            "coordinator_core/diff_scoped_tests.py": _REASON_CANON_STRING,
            "coordinator_core/search/regex_translate.py": _REASON_NOT_A_PATH,
            "coordinator_core/frontmatter/tests/test_handoff_lineage_corpus_dangling_refs.py": _REASON_CANON_STRING,
            "coordinator_core/ops/draft_plan_aging.py": _REASON_CANON_STRING,
            "coordinator_core/ops/ceremony/git_native.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/block_goals_log_hand_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/bash_guards/tests/guard_message_corpus.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator_core/bash_guards/tests/test_subagent_commit_prefilter_and_flags.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator/bin/check-install-doc-payload.py": _REASON_CANON_STRING,
            "coordinator_core/backlog_grind_assemble/readers_mise.py": _REASON_CANON_STRING,
            "coordinator_core/ops/fleet/memo_send.py": _REASON_CANON_STRING,
            "coordinator_core/ops/fleet/tests/test_memo_send.py": _REASON_CANON_STRING,
            "coordinator_core/session/scope.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/nudge_plan_sidecar_family_split.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/tests/test__case_fold_path.py": _REASON_CANON_STRING,
            "coordinator_core/ops/probe_onboarding_currency.py": _REASON_CANON_STRING,
            "coordinator_core/test_resolve_validation_cmd.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator/bin/tests/test_coordinator_registry.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator/tests/test_git_hook_install_foreign_hook_preservation.py": _REASON_CANON_STRING,
            "coordinator_core/bash_guards/_write_bump_sink_shapes.py": _REASON_MSYS_TRANSLATE_WINDOWS_GUARDED,
            "coordinator_core/bash_guards/tests/test_bump_foreign_repo_write.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_bump_foreign_repo_write_c7_findings.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_bump_outside_repo_write.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_bx16_apostrophe_quote_safety.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_bx16_grep_dialect_fidelity.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_bx16_multiprobe_and_headtail_rewrite.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_dispatch_hard_deny_envelope_gate.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_no_handwritten_override_clauses.py": _REASON_CANON_STRING,
            "coordinator_core/bash_guards/tests/test_write_bump_marker.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/bash_guards/tests/test_write_bump_surface_parity.py": _REASON_SHELL_EMBED_FORWARD_SLASH,
            "coordinator_core/cartography/atlas_record.py": _REASON_CANON_STRING,
            "coordinator_core/cartography/chunk_table.py": _REASON_CANON_STRING,
            "coordinator_core/dag.py": _REASON_CANON_STRING,
            "coordinator_core/git/commit_trailers.py": _REASON_CANON_STRING,
            "coordinator_core/install/test_resolve_claude_klabauter.py": _REASON_CANON_STRING,
            "coordinator_core/ops/_relative_link.py": _REASON_CANON_STRING,
            "coordinator_core/ops/ceremony/chunk_commits.py": _REASON_CANON_STRING,
            "coordinator_core/ops/ceremony/scoped_git_commit.py": _REASON_CANON_STRING,
            "coordinator_core/ops/review_trail_write.py": _REASON_CANON_STRING,
            "coordinator_core/ops/session/guard_settings_integrity.py": _REASON_CANON_STRING,
            "coordinator_core/ops/test_coordinator_doe_root.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator_core/ops/tests/test_deliverable_equivalence.py": _REASON_CANON_STRING,
            "coordinator_core/percolate/engine.py": _REASON_CANON_STRING,
            "coordinator_core/session/claimed_plan.py": _REASON_CANON_STRING,
            "coordinator_core/session/path_dialect.py": _REASON_CANON_STRING,
            "coordinator_core/tests/test_async_handler_discipline.py": _REASON_CANON_STRING,
            "coordinator_core/workstream_complete/__init__.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/block_subagent_grant_record_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_subagent_guard_grant_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_handoff_ac_shape.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/nudge_outbox_draft_frontmatter_shape.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/nudge_private_git_fact_resolver.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_shell_shaped_spawn.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/tests/test_ac5_flip_runtime_probes.py": _REASON_CANON_STRING,
            "coordinator_core/write_guards/tests/test_bump_out_of_repo_tool_write.py": _REASON_CANON_STRING,
        },
        REPO_EXAMPLE_DOCTRINE_REPO: {
            # Compares a COMMAND-LINE ARGUMENT against a path-shaped substring to decide whether
            # to strip it. The token may not be a path at all (`-q`, `--no-header`), so
            # PureWindowsPath is the wrong tool here: it would reinterpret a non-path argument as
            # one. Textual comparison, never a filesystem decision.
            "coordinator/bin/stable-suite-run.py": _REASON_NOT_A_PATH,
            # Builds the POSIX-form `$HOME`/`_cc_root` the guard under test reads -- bash's $HOME
            # is POSIX-shaped even under Git Bash, so forward slashes are the CORRECT value, not a
            # portability slip. PureWindowsPath was tried and is actively wrong here: it maps ""
            # to ".", and this file's test_t10_fail_open_zero_stderr_empty_root exercises an EMPTY
            # root, so the swap changed what the guard was handed and broke the test.
            "coordinator/tests/test_cc_root_source_guard.py": _REASON_CANON_STRING,
        },
    },
    "posix_mode_bits": {
        REPO_CLAUDE_KLABAUTER: {
            "coordinator/bin/tests/file-attribution/test_derive_file_attribution.py": _REASON_CHMOD_DIR_GAP,
            "coordinator_core/hooks/test_em_report_altitude.py": _REASON_CHMOD_DIR_GAP,
            "coordinator_core/ops/test_install_meta_repo_precommit_hook_install_all.py": _REASON_CHMOD_EXEC_FOR_SH,
            "coordinator_core/bash_guards/tests/test_write_bump_session_start.py": _REASON_CHMOD_DIR_GAP,
            "coordinator_core/bash_guards/tests/test_write_bump_marker.py": _REASON_CHMOD_DIR_GAP,
            "coordinator_core/tests/test_verify_templates_bin_sync.py": _REASON_CHMOD_MODE_PRESERVATION,
            "coordinator_core/percolate/engine.py": _REASON_ENTRYPOINT_INTERPRETER_NONE_IS_POSIX_ONLY,
        },
    },
    "unresolved_cross_path": {},
    "unclassified": {},
    "symlink_in_index": {},
    "case_collision": {},
    "reserved_filename": {},
    "path_too_long": {},
    "implicit_encoding": {},
}

#: Classes a directory-prefix exclusion is allowed to suppress: everything
#: EXCEPT Tier A. A frozen snapshot tree's files cannot be edited, but a
#: case collision or reserved device name inside one still breaks the whole
#: repo's checkout on Windows -- that is a fact about the repo, not about
#: the file's executability, and "we can't edit the evidence" there means
#: "this tree cannot be tracked here", a call for the tree's owner rather
#: than a carve-out this module grants. See the module docstring's
#: EXEMPT_PREFIXES section.
PREFIX_EXCLUDABLE_CLASSES: Tuple[str, ...] = tuple(
    cls for cls in ALL_CLASSES if cls not in TIER_A_CLASSES
)

#: Directory-scope exclusions: `repo_key -> prefix -> reason`. A tracked
#: path starting with one of its own repo's prefixes is invisible to every
#: class in `PREFIX_EXCLUDABLE_CLASSES` -- the wholesale counterpart to
#: EXEMPTIONS' one-file-at-a-time grant, for trees whose CONTENTS are not
#: this repo's code at all.
#:
#: Read the module docstring's EXEMPT_PREFIXES admission test before adding
#: one: verbatim-preserved foreign/frozen content AND nothing here executes,
#: imports, installs or ships. A merely noisy, legacy, or not-yet-ported
#: directory fails it -- that is debt, and debt belongs in the visible,
#: shrink-only baseline, not behind an invisibility screen.
#:
#: Every prefix is repo-relative, forward-slash spelled, and MUST end in `/`
#: so `state/mirror-native-source/` can never also swallow a sibling named
#: `state/mirror-native-source-scratch/` (`_test_exempt_prefix_shape_is_
#: directory_scoped` enforces it). Repo-scoped for the same reason EXEMPTIONS
#: is: `state/review-trail/` exists in more than one fleet repo and a grant
#: to one must never silently cover another's.
EXEMPT_PREFIXES: Dict[str, Dict[str, str]] = {
    REPO_CLAUDE_KLABAUTER: {
        "dist/mirror-native/": _MIRROR_NATIVE_DESTINATION_REASON,
    },
    REPO_EXAMPLE_DOCTRINE_REPO: {
        "state/review-trail/diffs/m8-baseline/": _M8_REVIEW_TRAIL_SNAPSHOT_REASON,
    },
}


def repo_key_for_root(root) -> str:
    """The fleet `repos.<key>` identity of the repo checked out at `root`.

    Lowercase the clone directory's basename, collapse every non-alnum run
    to `_`, strip the edges — byte-identical to
    `coordinator_core.install.first_run._derive_repo_key`, `cross-repo-memo`'s
    `_receiver_repo_key`, and `coordinator_core.ops.register_discovered_repos`,
    which is what makes `EXEMPTIONS`'s repo keys the SAME names the
    machine-local registry already uses (`repos.example_doctrine_repo`,
    `repos.claude_klabauter`) rather than a second, private repo vocabulary.

    See the module docstring's "Keying" paragraph for the named residual gap
    (a non-canonically-named clone derives a different key and gets none of
    its exemptions — over-report, never under-report).
    """
    name = Path(root).resolve().name
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _exempt_relpaths(cls: str, repo_key: str) -> Dict[str, str]:
    """The `relpath -> reason` exemptions granted for `cls` in the repo
    identified by `repo_key`. Empty for every other repo — an exemption is
    granted to one repo's file, never to a relpath fleet-wide."""
    return EXEMPTIONS.get(cls, {}).get(repo_key, {})


#: Relpath, inside the repo being scanned, of the FILE-based sibling of the
#: in-code `EXEMPT_PREFIXES` dict. See `_file_exempt_prefixes`'s docstring
#: for the mechanism and its relationship to `EXEMPT_PREFIXES`.
_FILE_EXEMPT_PREFIXES_RELPATH = "state/posix-exec-exempt-prefixes.json"


def _file_exempt_prefixes(root) -> Dict[str, str]:
    """Directory-prefix exclusions declared in `<root>/state/posix-exec-
    exempt-prefixes.json` -- the FILE-based sibling of the in-code
    `EXEMPT_PREFIXES` dict, granting the SAME admission test (module
    docstring's EXEMPT_PREFIXES section: verbatim foreign/frozen bytes AND
    nothing under the prefix runs, imports, installs, or ships here) without
    editing this module.

    `EXEMPT_PREFIXES` fits a small, curated set of grants this module's own
    maintainer reviews one at a time across the whole fleet; a caller with an
    entire vendored/third-party subtree to exclude -- a one-off third-party
    import, not a recurring shape worth a dedicated constant in a module it
    does not own -- would otherwise have to either hand-list every file in
    EXEMPTIONS (defeating the coarser mechanism EXEMPT_PREFIXES exists to be)
    or send a change to this module for a tree it has no stake in maintaining.

    Unlike `EXEMPT_PREFIXES`, which is keyed `repo_key -> prefix -> reason`
    because one module instance serves every fleet repo from a single
    in-code dict, this file needs no repo key of its own: `scan()` always
    reads it from the SAME `root` being scanned, so it is already correctly
    scoped to that repo and no other -- a file living in claude-klabauter's
    tree can never be read while scanning a sibling's.

    Malformed or absent input degrades to "no additional exclusions" rather
    than raising: a missing file is the common case (most repos declare
    none), and a corrupt one should widen the visible, scannable surface,
    never silently swallow it. Each entry is validated against the same
    shape bar as `EXEMPT_PREFIXES` (trailing `/`, repo-relative, forward-
    slash spelled, non-escaping, non-empty reason) -- a malformed individual
    entry is dropped rather than poisoning every other entry in the file.
    """
    path = Path(root) / _FILE_EXEMPT_PREFIXES_RELPATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        prefix: reason
        for prefix, reason in data.items()
        if isinstance(prefix, str)
        and prefix.endswith("/")
        and not prefix.startswith("/")
        and "\\" not in prefix
        and ".." not in prefix
        and isinstance(reason, str)
        and reason.strip()
    }


def _exempt_prefixes(repo_key: str, root=None) -> Dict[str, str]:
    """The `prefix -> reason` directory exclusions granted to the repo
    identified by `repo_key`: the in-code `EXEMPT_PREFIXES` grant merged
    with the file-based grant at `root`'s own `_FILE_EXEMPT_PREFIXES_RELPATH`,
    when `root` is given. `root` is optional and defaults to `None` (no file
    consulted, exactly the pre-file-mechanism behaviour) so every caller
    that predates this mechanism -- including direct `is_prefix_excluded`
    call sites in this repo's own test suite -- keeps working unchanged."""
    prefixes = dict(EXEMPT_PREFIXES.get(repo_key, {}))
    if root is not None:
        prefixes.update(_file_exempt_prefixes(root))
    return prefixes


def is_prefix_excluded(relpath: str, repo_key: str, root=None) -> bool:
    """True iff `relpath` lives under one of `repo_key`'s own EXEMPT_PREFIXES
    (in-code) or, when `root` is given, its file-based prefixes too.

    Answers only the prefix question -- whether that exclusion may APPLY to a
    given class is `PREFIX_EXCLUDABLE_CLASSES`'s job (Tier A is never
    suppressed), and every caller checks both.
    """
    prefixes = tuple(_exempt_prefixes(repo_key, root))
    return bool(prefixes) and relpath.startswith(prefixes)




def _git(root, args: List[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root)] + args,
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )
    return proc.stdout


def _tracked_files(root) -> List[str]:
    return [line for line in _git(root, ["ls-files"]).splitlines() if line]


def _mode_755_paths(root) -> List[str]:
    out = _git(root, ["ls-files", "-s"])
    paths = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0] == "100755":
            paths.append(parts[3])
    return paths


def _mode_120000_paths(root) -> List[str]:
    out = _git(root, ["ls-files", "-s"])
    paths = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0] == "120000":
            paths.append(parts[3])
    return paths


def _case_collisions(files: List[str]) -> List[str]:
    """Two or more tracked paths identical except for case -- breaks
    checkout on both Windows and macOS (both default case-insensitive).
    Returns every path that participates in a collision group."""
    by_lower: Dict[str, List[str]] = {}
    for f in files:
        by_lower.setdefault(f.lower(), []).append(f)
    collisions: List[str] = []
    for group in by_lower.values():
        if len(group) > 1:
            collisions.extend(group)
    return sorted(set(collisions))


def _reserved_filename_paths(files: List[str]) -> List[str]:
    """A path with a component matching a Windows reserved device name
    (stem before the first '.', case-insensitive), OR containing a
    character forbidden in a Windows path (`: * ? " < > |`)."""
    hits: List[str] = []
    for f in files:
        if _FORBIDDEN_PATH_CHARS.search(f):
            hits.append(f)
            continue
        for segment in f.split("/"):
            stem = segment.split(".", 1)[0].upper()
            if stem in _RESERVED_DEVICE_NAMES:
                hits.append(f)
                break
    return sorted(set(hits))


def _path_too_long_paths(files: List[str]) -> List[str]:
    return sorted(f for f in files if len(f) > _PATH_TOO_LONG_THRESHOLD)


def _scan_tier_a(root, files: List[str]) -> Dict[str, List[str]]:
    """Scan the four Tier A checkout-breaker classes. Exact git-index/path
    facts only -- no AST, no content regex."""
    raw = {
        "symlink_in_index": _mode_120000_paths(root),
        "case_collision": _case_collisions(files),
        "reserved_filename": _reserved_filename_paths(files),
        "path_too_long": _path_too_long_paths(files),
    }
    repo_key = repo_key_for_root(root)
    return {
        cls: sorted(p for p in raw[cls] if p not in _exempt_relpaths(cls, repo_key))
        for cls in TIER_A_CLASSES
    }


def check_tier_a_zero_tolerance(
    root, precomputed: "Dict[str, List[str]] | None" = None
) -> Tuple[bool, str]:
    """RED iff ANY current Tier A violation exists. No baseline, no
    ratchet -- these are checkout-breakers on a near-zero fleet, so the bar
    is zero, always. EXEMPTIONS is the only escape, same admission test as
    every other class (module docstring).

    `precomputed`, if given, is an ALL_CLASSES-keyed `scan()` result already
    computed by the caller -- lets `main()` share one full-repo scan across
    all three top-level checks instead of each re-deriving `_tracked_files`
    and re-scanning from scratch (review 2026-07-28 Finding 5). Every
    existing caller (including this repo's own test suite) omits it and
    gets the exact prior standalone behavior.
    """
    root = Path(root)
    if precomputed is not None:
        current = {cls: precomputed[cls] for cls in TIER_A_CLASSES}
    else:
        files = _tracked_files(root)
        current = _scan_tier_a(root, files)

    lines: List[str] = []
    ok = True
    for cls in TIER_A_CLASSES:
        paths = current[cls]
        if paths:
            ok = False
            lines.append(f"{cls}: {len(paths)} checkout-breaking violation(s):")
            for p in paths:
                lines.append(f"  - {p}")

    if ok:
        lines.append("OK: no Tier A checkout-breaker violations (zero-tolerance).")
    else:
        lines.append(
            "Fix: Tier A has no baseline -- every violation above must be fixed "
            "or, if a genuine named carve-out, added to EXEMPTIONS in "
            "coordinator_core/ops/check_posix_exec_assumptions.py with a "
            "written reason."
        )

    return ok, "\n".join(lines)


def check_implicit_encoding_zero_tolerance(
    root, precomputed: "Dict[str, List[str]] | None" = None
) -> Tuple[bool, str]:
    """RED iff ANY current `implicit_encoding` violation exists. No
    baseline, no ratchet -- same zero-tolerance contract as Tier A, but a
    SEPARATE failure axis (data corruption on read/write of non-ASCII
    content on Windows, not a checkout-breaker), so it is its own function
    and its own class tuple rather than folded into `TIER_A_CLASSES`.

    `precomputed`, if given, is an ALL_CLASSES-keyed `scan()` result already
    computed by the caller (see `check_tier_a_zero_tolerance`'s docstring
    for why -- same Finding 5 fix). EXEMPTIONS is already applied inside
    `scan()`'s own return, so a precomputed result needs no re-filtering.
    """
    root = Path(root)
    if precomputed is not None:
        paths = sorted(precomputed["implicit_encoding"])
    else:
        files = _tracked_files(root)
        hits: Dict[str, Set[str]] = {
            "path_separator": set(),
            "posix_mode_bits": set(),
            "unresolved_cross_path": set(),
            "unclassified": set(),
            "implicit_encoding": set(),
        }
        for relpath in files:
            if relpath.endswith(".py"):
                _scan_python_file(relpath, root / relpath, hits)

        repo_key = repo_key_for_root(root)
        exempt = _exempt_relpaths("implicit_encoding", repo_key)
        paths = sorted(
            p
            for p in hits["implicit_encoding"]
            if p not in exempt and not is_prefix_excluded(p, repo_key)
        )

    if not paths:
        return True, "OK: no implicit_encoding violations (zero-tolerance)."

    lines = [f"implicit_encoding: {len(paths)} violation(s) (bare open() with no encoding=):"]
    for p in paths:
        lines.append(f"  - {p}")
    lines.append(
        "  Fix: add encoding=\"utf-8\" to each open() call above -- this "
        "class has no baseline, every violation must be fixed or, if a "
        "genuine named carve-out, added to EXEMPTIONS in "
        "coordinator_core/ops/check_posix_exec_assumptions.py with a "
        "written reason."
    )
    return False, "\n".join(lines)


# ---------------------------------------------------------------------------
# Classes 4-6: AST-scanned Python-code patterns.
# ---------------------------------------------------------------------------

_EXEC_BIT_MASK = 0o111  # any owner/group/other exec bit

_CROSS_PATH_PATTERNS = [
    re.compile(r"^/Users/[^/]+/"),
    re.compile(r"^/home/[^/]+/"),
    # Anchored to the ABSOLUTE start of the string (no re.MULTILINE, so `^`
    # cannot re-anchor mid-string) -- this is the fleet's known drive-letter
    # trap: an UNANCHORED `[A-Za-z]:[/\\]` matches the "s:" inside
    # "https://", because re.search() with no `^` will happily match that
    # substring anywhere. Anchoring to true string-start means the string
    # would have to itself BE a one-letter scheme ("s://...") to false-fire,
    # which no real URL scheme is (http, https, ftp, ssh, git, s3, ws, wss
    # are all 2+ characters) -- see test_scan_does_not_flag_url_with_single_
    # letter_looking_scheme for the fixture proving this holds.
    re.compile(r"^[A-Za-z]:[\\/]"),
    re.compile(r"^\\\\"),  # UNC \\server\share
]

_UNCLASSIFIED_CALL_TARGETS = {
    "os": {"fork", "uname", "geteuid", "getegid", "setuid", "setgid"},
    "pwd": {"getpwnam", "getpwuid"},
    "grp": {"getgrnam", "getgrgid"},
}
_UNCLASSIFIED_SIGNAL_NAMES = {"SIGHUP", "SIGUSR1", "SIGUSR2", "SIGCHLD"}


def _docstring_const_ids(tree: ast.AST) -> Set[int]:
    """id() set of Constant nodes that are docstrings (first Expr(Constant)
    statement of a module/class/function body) -- excluded from literal
    content matching so a documented example path is never mistaken for a
    hardcoded runtime assumption."""
    ids: Set[int] = set()
    candidates: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            candidates.append(node)
    for c in candidates:
        body = getattr(c, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _is_stat_exec_ref(node: ast.AST) -> bool:
    """Matches `stat.S_IXUSR`/`S_IXGRP`/`S_IXOTH`/`S_IEXEC` (Attribute) or a
    bare `S_IXUSR`-shaped Name (post `from stat import S_IXUSR`)."""
    if isinstance(node, ast.Attribute):
        return node.attr.startswith("S_IX") or node.attr == "S_IEXEC"
    if isinstance(node, ast.Name):
        return node.id.startswith("S_IX") or node.id == "S_IEXEC"
    return False


def _const_has_exec_bit(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and bool(node.value & _EXEC_BIT_MASK)
    )


def _mode_arg_is_exec(node: ast.AST) -> bool:
    if _const_has_exec_bit(node) or _is_stat_exec_ref(node):
        return True
    return any(_is_stat_exec_ref(sub) for sub in ast.walk(node))


# ---------------------------------------------------------------------------
# Platform-guard recognition (precision fix, 2026-07-28 PM review): code
# that reasons about POSIX permission bits or hardcodes a path separator
# INSIDE a branch that structurally never runs on Windows is not debt -- it
# is the correct cross-platform shape, and flagging it trains authors to
# route AROUND the guard instead of writing it. `posix_mode_bits` and
# `path_separator` both consult `_is_windows_guarded()` before recording a
# hit; `unresolved_cross_path`/`unclassified` do not (a hardcoded path
# literal or POSIX-only call is not made correct by being conditional on
# platform -- if anything a `/tmp/`-literal or an `/Users/...`-literal
# inside a POSIX-only branch is exactly where you'd expect one, and TIER
# A/Tier B classes are exact metadata with no branch context to reason
# about at all).
# ---------------------------------------------------------------------------

_WINDOWS_TOKENS = {"nt", "win32", "windows", "cygwin"}


def _build_parent_map(tree: ast.AST) -> Dict[int, ast.AST]:
    """id(child) -> immediate syntactic parent, for every node in `tree`.
    `ast.iter_child_nodes` flattens list fields (e.g. `If.body`,
    `If.orelse`), so a statement living inside a branch list maps directly
    to the enclosing `If` -- exactly the lookup `_is_windows_guarded` needs."""
    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _is_owner_attr(node: ast.AST, owner: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
    )


def _is_platform_system_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "system"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform"
        and not node.args
    )


def _const_str(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _flip(status: str) -> str:
    return "posix" if status == "windows" else "windows"


def _branch_windows_status(test: ast.AST):
    """Returns 'windows' if `test` being True implies the branch only runs
    on Windows, 'posix' if True implies it only runs on non-Windows, or
    None if the test isn't a recognized platform check. Recognizes
    `os.name`/`sys.platform`/`platform.system()` compared against a
    Windows-identifying constant (`==`/`!=`), `sys.platform.startswith(
    "win"...)`, and a leading `not` on any of those forms."""
    node = test
    negate = False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        negate = True
        node = node.operand

    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left, op, right = node.left, node.ops[0], node.comparators[0]
        is_platform_ref = (
            _is_owner_attr(left, "os", "name")
            or _is_owner_attr(left, "sys", "platform")
            or _is_platform_system_call(left)
        )
        val = _const_str(right)
        if is_platform_ref and val is not None:
            is_windows_val = val.strip().lower() in _WINDOWS_TOKENS
            if isinstance(op, ast.Eq):
                status = "windows" if is_windows_val else "posix"
            elif isinstance(op, ast.NotEq):
                status = "posix" if is_windows_val else "windows"
            else:
                return None
            return _flip(status) if negate else status

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        and _is_owner_attr(node.func.value, "sys", "platform")
        and node.args
    ):
        arg = _const_str(node.args[0])
        if arg is not None and arg.lower().startswith("win"):
            return _flip("windows") if negate else "windows"

    return None


def _is_windows_guarded(node: ast.AST, parents: Dict[int, ast.AST]) -> bool:
    """Walks up from `node` through enclosing `If` statements (elif chains
    are nested `If`s inside `orelse`, handled by the same loop) AND
    enclosing `and`-chains (`os.name != "nt" and ... and os.access(...)` --
    the real shape found in `retire-claude-bin.py`, where the guard and the
    guarded call are short-circuit operands of the SAME boolean expression,
    not a separate nested `If`). Returns True the moment ANY enclosing
    guard structurally means Windows never reaches this node -- one
    sufficient guard is enough, matching how authors actually write this
    code."""
    cur = node
    while True:
        parent = parents.get(id(cur))
        if parent is None:
            return False
        if isinstance(parent, ast.If):
            status = _branch_windows_status(parent.test)
            if status == "posix" and any(cur is b for b in parent.body):
                return True
            if status == "windows" and any(cur is b for b in parent.orelse):
                return True
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
            idx = next((i for i, v in enumerate(parent.values) if v is cur), None)
            if idx is not None and idx > 0:
                # `and` short-circuits left to right -- `cur` only
                # evaluates if every EARLIER operand was truthy, so an
                # earlier operand that IS the platform guard means `cur`
                # never runs on Windows.
                for earlier in parent.values[:idx]:
                    if _branch_windows_status(earlier) == "posix":
                        return True
        cur = parent


def _scan_python_file(relpath: str, abspath: Path, hits: Dict[str, Set[str]]) -> None:
    try:
        src = abspath.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(abspath))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return

    docstring_ids = _docstring_const_ids(tree)
    parent_map = _build_parent_map(tree)

    for node in ast.walk(tree):
        # -- posix_mode_bits: os.access(path, os.X_OK) --------------------
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "access"
        ):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if (
                    (isinstance(arg, ast.Attribute) and arg.attr == "X_OK")
                    or (isinstance(arg, ast.Name) and arg.id == "X_OK")
                ) and not _is_windows_guarded(node, parent_map):
                    hits["posix_mode_bits"].add(relpath)

        # -- posix_mode_bits: os.chmod(path, <mode with exec bit>) -------
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "chmod"
        ):
            mode_args = list(node.args[1:]) + [
                kw.value for kw in node.keywords if kw.arg == "mode"
            ]
            for m in mode_args:
                if _mode_arg_is_exec(m) and not _is_windows_guarded(node, parent_map):
                    hits["posix_mode_bits"].add(relpath)

        # -- posix_mode_bits: <stat_result>.st_mode & <exec-bit-mask> -----
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd):
            operands = (node.left, node.right)
            has_st_mode = any(
                isinstance(o, ast.Attribute) and o.attr == "st_mode" for o in operands
            )
            if has_st_mode:
                for o in operands:
                    if _mode_arg_is_exec(o) and not _is_windows_guarded(node, parent_map):
                        hits["posix_mode_bits"].add(relpath)

        # -- path_separator: .replace("/", "\\") normalization hack ------
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and len(node.args) >= 2
        ):
            a, b = node.args[0], node.args[1]
            a_val = a.value if isinstance(a, ast.Constant) else None
            b_val = b.value if isinstance(b, ast.Constant) else None
            if {a_val, b_val} == {"/", "\\"} and not _is_windows_guarded(node, parent_map):
                hits["path_separator"].add(relpath)

        # -- path_separator: .split("\\") manual path parsing ------------
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "split"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "\\"
            and not _is_windows_guarded(node, parent_map)
        ):
            hits["path_separator"].add(relpath)

        # -- path_separator: literal "\\" used in string concatenation ---
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for o in (node.left, node.right):
                if (
                    isinstance(o, ast.Constant)
                    and o.value == "\\"
                    and id(o) not in docstring_ids
                    and not _is_windows_guarded(node, parent_map)
                ):
                    hits["path_separator"].add(relpath)

        # -- unresolved_cross_path / unclassified: literal string content -
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            s = node.value
            if any(p.search(s) for p in _CROSS_PATH_PATTERNS):
                hits["unresolved_cross_path"].add(relpath)
            if s.startswith("/tmp/"):
                hits["unclassified"].add(relpath)

        # -- unclassified: os.fork / pwd.getpwnam / grp.getgrnam / etc. --
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            targets = _UNCLASSIFIED_CALL_TARGETS.get(node.func.value.id)
            if targets and node.func.attr in targets:
                hits["unclassified"].add(relpath)

        # -- unclassified: signal.SIGHUP / SIGUSR1 / SIGUSR2 / SIGCHLD ---
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _UNCLASSIFIED_SIGNAL_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id == "signal"
        ):
            hits["unclassified"].add(relpath)

        # -- implicit_encoding: bare open() with no encoding=, non-binary --
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            mode = _open_call_mode(node)
            if mode is _MODE_DYNAMIC:
                # Cannot tell binary from text -- see `_open_call_mode`'s
                # docstring; a variable mode never produces a finding
                # rather than guessing "not binary" and false-firing on a
                # caller that genuinely passes a binary mode through.
                pass
            else:
                is_binary = mode is not _MODE_ABSENT and _BINARY_MODE_MARKER in mode
                if not is_binary and not _open_call_has_encoding(node):
                    hits["implicit_encoding"].add(relpath)


def scan(root) -> Dict[str, List[str]]:
    """Scan `root` (a git repo) for all six POSIX-exec-assumption classes
    (the five blocking + the two report-only).

    Returns {class_name: sorted [relpath, ...]} for every name in
    ALL_CLASSES, with EXEMPTIONS already subtracted out — only the
    exemptions granted for `root`'s OWN repo (`repo_key_for_root`), never a
    sibling's entry that happens to share a relpath.
    """
    root = Path(root)
    files = _tracked_files(root)

    env_shebang: List[str] = []
    extensionless_exec: List[str] = []
    ast_hits: Dict[str, Set[str]] = {
        "path_separator": set(),
        "posix_mode_bits": set(),
        "unresolved_cross_path": set(),
        "unclassified": set(),
        "implicit_encoding": set(),
    }

    for relpath in files:
        abspath = root / relpath
        try:
            with open(abspath, "rb") as fh:
                head = fh.read(64)
        except OSError:
            # Not on disk (e.g. a submodule gitlink) -- git ls-files can
            # list paths with no regular-file backing; skip rather than
            # raise, matching check-machine-path-leak.py's tolerance.
            continue

        if head.startswith(b"#!/usr/bin/env"):
            env_shebang.append(relpath)

        base = os.path.basename(relpath)
        if "." not in base and head.startswith(b"#!"):
            extensionless_exec.append(relpath)

        if relpath.endswith(".py"):
            _scan_python_file(relpath, abspath, ast_hits)

    mode_100755 = _mode_755_paths(root)
    tier_a = _scan_tier_a(root, files)

    raw: Dict[str, List[str]] = {
        "env_shebang": env_shebang,
        "extensionless_exec": extensionless_exec,
        "mode_100755": mode_100755,
        "path_separator": sorted(ast_hits["path_separator"]),
        "posix_mode_bits": sorted(ast_hits["posix_mode_bits"]),
        "unresolved_cross_path": sorted(ast_hits["unresolved_cross_path"]),
        "unclassified": sorted(ast_hits["unclassified"]),
        "implicit_encoding": sorted(ast_hits["implicit_encoding"]),
        **tier_a,
    }
    repo_key = repo_key_for_root(root)
    return {
        cls: sorted(
            p
            for p in raw[cls]
            if p not in _exempt_relpaths(cls, repo_key)
            and not (
                cls in PREFIX_EXCLUDABLE_CLASSES
                and is_prefix_excluded(p, repo_key, root)
            )
        )
        for cls in ALL_CLASSES
    }


def load_baseline(baseline_path) -> Dict[str, List[str]]:
    """Load the baseline JSON. Only the BLOCKING classes are persisted --
    report-only classes never appear in the baseline file."""
    path = Path(baseline_path)
    if not path.is_file():
        return {cls: [] for cls in CLASSES}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {cls: list(data.get(cls, [])) for cls in CLASSES}


# Design-as-offers (PM ask, 2026-07-28): for `path_separator` and
# `posix_mode_bits` specifically, LEAD with the known escape for the one
# recognized gap in `_is_windows_guarded()` -- a bare early-return guard
# clause with no `else:` -- so an author who hits that rare false positive
# sees "this is a known gap with a sanctioned exit" first, not a bare
# violation notice that reads as "the guard is broken, route around it."
_GUARD_AWARE_CLASSES = ("path_separator", "posix_mode_bits")

_EARLY_RETURN_ESCAPE_HINT = (
    "  If this fires on code that IS platform-guarded via a bare early-"
    "return clause with no `else:` (e.g. `if sys.platform.startswith"
    "('win'): return p` followed by the guarded code as the next sibling "
    "statement) -- that is a KNOWN, NAMED gap in `_is_windows_guarded()` "
    "(see that function's docstring), not a false alarm to work around by "
    "rewriting working code. Add a named EXEMPTIONS entry citing this gap; "
    "do not add an `else:` purely to satisfy the detector."
)


def _fix_hint(cls: str) -> str:
    generic = (
        f"  Fix: port {cls.replace('_', ' ')} away from the POSIX "
        "assumption (see module docstring for the remediation shape "
        "per class), or -- if this is a genuine, file-specific "
        "carve-out -- add it to EXEMPTIONS in "
        "coordinator_core/ops/check_posix_exec_assumptions.py with a "
        "named reason (never widen the baseline instead)."
    )
    if cls in _GUARD_AWARE_CLASSES:
        return _EARLY_RETURN_ESCAPE_HINT + "\n" + generic
    return generic


def check_against_baseline(
    root, baseline_path, precomputed: "Dict[str, List[str]] | None" = None
) -> Tuple[bool, str]:
    """RED iff any current BLOCKING-class violation is NOT already in the
    baseline. Report-only classes are scanned and summarized but never
    affect `ok`.

    Always prints the total remaining debt count (per blocking class and
    overall), plus a report-only summary, so both stay visible and the
    blocking debt is expected to shrink over time, never silent.

    `precomputed`, if given, is an already-computed `scan()` result -- lets
    `main()` share one full-repo scan across all three top-level checks
    (see `check_tier_a_zero_tolerance`'s docstring; review 2026-07-28
    Finding 5).
    """
    current = precomputed if precomputed is not None else scan(root)
    baseline = load_baseline(baseline_path)

    lines: List[str] = []
    ok = True
    total_current = 0
    total_baseline = 0
    total_new = 0

    for cls in CLASSES:
        cur = set(current[cls])
        base = set(baseline.get(cls, []))
        new = sorted(cur - base)
        total_current += len(cur)
        total_baseline += len(base)
        if new:
            ok = False
            total_new += len(new)
            lines.append(
                f"{cls}: {len(new)} NEW POSIX-exec-assumption violation(s) "
                "not covered by the frozen baseline:"
            )
            for p in new:
                lines.append(f"  - {p}")
            lines.append(_fix_hint(cls))

    # Two distinct counts, printed distinctly on purpose: `total_current` is
    # THIS scan of the live tree; `total_baseline` is the frozen baseline
    # file's own entry count. They diverge whenever the baseline carries
    # stale entries for paths no longer present (deleted/renamed/fixed --
    # see `check_no_stale_baseline_entries`) and/or the live tree carries
    # NEW violations not yet in the baseline (the `ok is False` case above).
    # A prior version of this line printed only `total_current` labelled
    # "baseline-frozen", which a reader could mistake for the baseline
    # file's own count -- it never was one, and the two numbers can differ
    # by hundreds with no explanation in the output. Never collapse these
    # back into one bare number.
    debt_line = (
        f"POSIX-exec-assumption debt: current scan {total_current} "
        f"violation(s), frozen baseline {total_baseline} entry(ies), "
        f"across {len(CLASSES)} blocking classes."
    )
    if total_current != total_baseline:
        total_stale = total_baseline - (total_current - total_new)
        parts = []
        if total_new:
            parts.append(f"{total_new} NEW violation(s) not yet in the baseline")
        if total_stale > 0:
            parts.append(
                f"{total_stale} stale baseline entry(ies) for path(s) the "
                "current scan no longer produces (see "
                "check_no_stale_baseline_entries -- prune them; the "
                "baseline is shrink-only)"
            )
        if parts:
            debt_line += " Difference explained by: " + "; ".join(parts) + "."
        else:
            debt_line += (
                f" Difference of {total_current - total_baseline} unexplained "
                "by NEW violations or stale entries -- investigate before "
                "trusting either number."
            )
    if ok:
        lines.append(f"OK: no new POSIX-exec-assumption violations. {debt_line}")
    else:
        lines.append(debt_line)

    lines.append(_report_only_lines(current))

    return ok, "\n".join(lines)


def _report_only_lines(current: Dict[str, List[str]]) -> str:
    lines = ["Report-only classes (never gate the build):"]
    for cls in REPORT_ONLY_CLASSES:
        paths = current.get(cls, [])
        lines.append(f"  {cls}: {len(paths)} finding(s)")
        for p in paths:
            lines.append(f"    - {p}")
    return "\n".join(lines)


# Metadata key inside the baseline JSON itself: an explicit "re-freeze from
# here" anchor SHA, distinct from any class array. `load_baseline()`/`scan()`
# never read this key (they only ever look up entries by name in `CLASSES`),
# so its presence is fully backward compatible with every other consumer.
#
# Why an explicit override exists alongside the auto-derived introducing
# commit: landing this fix (2026-07-28 review) revealed that BOTH repos'
# real committed baselines had already grown, silently and undetected, many
# times since their true git-history introduction -- exactly the blind spot
# Finding 1 describes, now surfaced by the fix itself. Auto-deriving the
# anchor from the file's original introducing commit would retroactively
# flag that entire pre-fix history as "grown" on day one of enforcement,
# the same bind Tier A's zero-tolerance classes were in before they got
# their own frozen-as-of-2026-07-28 starting line (see module docstring).
# `_FROZEN_AS_OF_SHA_KEY`, once set, re-anchors the ratchet the same way:
# only growth from THIS point forward is enforced, not growth accumulated
# before the mechanism actually worked.
_FROZEN_AS_OF_SHA_KEY = "_frozen_as_of_sha"


def _baseline_anchor_sha(root, baseline_relpath, current: Dict) -> "str | None":
    """Resolve the FIXED commit `assert_baseline_not_grown` diffs against,
    instead of the moving `HEAD` tip (see module docstring § Anchor choice).

    Priority:
      1. An explicit `_frozen_as_of_sha` key in the on-disk baseline JSON
         (`_FROZEN_AS_OF_SHA_KEY`) -- a deliberate, reviewed re-freeze point.
      2. The commit that first introduced `baseline_relpath` to git history
         (`git log --diff-filter=A --follow`), the default for a baseline
         with no explicit override recorded.
    Returns `None` if neither resolves to a real commit (e.g. the file is
    not yet committed at all)."""
    explicit = current.get(_FROZEN_AS_OF_SHA_KEY)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    proc = subprocess.run(
        [
            "git", "-C", str(root), "log", "--diff-filter=A", "--format=%H",
            "--follow", "--", baseline_relpath,
        ],
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )
    if proc.returncode != 0:
        return None
    shas = [line for line in proc.stdout.splitlines() if line]
    return shas[-1] if shas else None


def assert_baseline_not_grown(root, baseline_relpath) -> Tuple[bool, str]:
    """RED iff the baseline file's on-disk content added an entry to any
    BLOCKING class since the FIXED commit that first introduced the file
    (see module docstring § Anchor choice for why this is `git log
    --diff-filter=A`-resolved, not `HEAD`). Report-only classes are never
    persisted into the baseline and so cannot trip this.

    The append-forbidden gate: a maintainer widening the baseline to make a
    new violation pass is the exact anti-pattern this function exists to
    catch, distinct from check_against_baseline()'s job of catching a new
    violation that ISN'T in the baseline at all.

    Growth is measured per class as NET count, not as raw set-difference. A
    rename moves an existing violation from one path to another and appears
    in `added` and `removed` in equal measure; judged by set-difference alone
    it is indistinguishable from a plain append, so the gate refused every
    legitimate rename and left a maintainer choosing between a red suite and
    a dishonest EXEMPTIONS entry for a file that does not merit one. Netting
    admits the rename while keeping the documented invariant exactly as
    stated: the class shrinks as violations are fixed and never grows.

    Known and accepted weakness of netting: removing one genuinely-fixed
    violation while adding one unrelated new violation in the same class also
    nets to zero and passes here. That trade is deliberate — the alternative
    (path-identity tracking) needs rename detection this file has no signal
    for — and it is mitigated by naming every swap explicitly in the OK
    message, so a reviewer reading the gate's output sees the substitution
    rather than a bare green. check_against_baseline() remains the leg that
    catches a violation covered by no entry at all.
    """
    root = Path(root)
    baseline_relpath = str(baseline_relpath)
    current_path = root / baseline_relpath
    if not current_path.is_file():
        return True, (
            f"OK: no baseline file at {baseline_relpath} on disk -- nothing "
            "to check for growth."
        )
    current = json.loads(current_path.read_text(encoding="utf-8"))

    anchor_sha = _baseline_anchor_sha(root, baseline_relpath, current)
    if anchor_sha is None:
        return True, (
            f"OK: {baseline_relpath} not yet committed -- skipping growth "
            "check on first introduction."
        )

    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{anchor_sha}:{baseline_relpath}"],
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )
    if proc.returncode != 0:
        return True, (
            f"OK: {baseline_relpath} not readable at its own introducing "
            f"commit {anchor_sha} -- skipping growth check."
        )
    prior = json.loads(proc.stdout)

    grown: List[Tuple[str, List[str]]] = []
    swapped: List[Tuple[str, List[str], List[str]]] = []
    for cls in CLASSES:
        added = sorted(set(current.get(cls, [])) - set(prior.get(cls, [])))
        removed = sorted(set(prior.get(cls, [])) - set(current.get(cls, [])))
        if len(added) > len(removed):
            grown.append((cls, added))
        elif added:
            swapped.append((cls, added, removed))

    if not grown:
        msg = f"OK: baseline has not grown since its frozen anchor ({anchor_sha})."
        for cls, added, removed in swapped:
            msg += (
                f"\n  {cls}: net-zero substitution -- "
                f"removed {', '.join(removed)}; added {', '.join(added)}. "
                "Named here rather than passing silently: a rename is legitimate, "
                "an unrelated swap hiding behind one is not, and only a reader can "
                "tell them apart."
            )
        return True, msg

    lines = ["BASELINE GREW -- append-forbidden violation:"]
    for cls, paths in grown:
        plural = "y" if len(paths) == 1 else "ies"
        lines.append(
            f"  {cls}: {len(paths)} new baseline entr{plural} added since "
            f"frozen anchor {anchor_sha}:"
        )
        for p in paths:
            lines.append(f"    - {p}")
    lines.append(
        "  Fix the underlying violation instead of widening the baseline. "
        "If this is a genuine, named POSIX-only carve-out, add it to "
        "EXEMPTIONS (with a reason) and remove it from the baseline instead "
        "of leaving it as a grown baseline entry."
    )
    return False, "\n".join(lines)


def check_no_stale_exempt_prefixes(root) -> Tuple[bool, str]:
    """RED iff a prefix declared for `root`'s OWN repo matches no tracked
    file there -- a dead rule that excuses nothing.

    Mirrors `test_no_parity_exemption_is_stale`'s discipline in
    `coordinator_core/test_bin_launcher_parity.py`: a carve-out list that
    outlives what it excused stops being a carve-out and becomes standing
    permission for whatever lands at that path next. A directory-scope
    exclusion makes that failure mode strictly worse than the one-file kind,
    because the successor content need not resemble the content the grant was
    written about at all.

    Fleet-shaped like every other check here: it can only judge the repo it is
    handed, so a sibling's prefixes are checked when that sibling's own test
    tier calls this with ITS root -- never silently from here.
    """
    root = Path(root)
    repo_key = repo_key_for_root(root)
    prefixes = _exempt_prefixes(repo_key, root)
    if not prefixes:
        return True, (
            f"OK: no EXEMPT_PREFIXES declared for repo key {repo_key!r} -- "
            "nothing to check for staleness."
        )

    tracked = _tracked_files(root)
    stale = sorted(p for p in prefixes if not any(f.startswith(p) for f in tracked))
    if not stale:
        return True, (
            f"OK: all {len(prefixes)} EXEMPT_PREFIXES entr"
            f"{'y' if len(prefixes) == 1 else 'ies'} for {repo_key!r} still "
            "match tracked files."
        )

    lines = [
        f"STALE EXEMPT_PREFIXES for repo {repo_key!r}: "
        f"{len(stale)} declared prefix(es) match no tracked file:"
    ]
    for p in stale:
        lines.append(f"  - {p}")
    lines.append(
        "  The tree this prefix excused is gone, so the rule now excuses "
        "nothing and would silently cover whatever lands at that path next. "
        "Delete the entry from EXEMPT_PREFIXES in "
        "coordinator_core/ops/check_posix_exec_assumptions.py -- this list is "
        "shrink-only; re-add it (with a fresh written reason) only if the "
        "preserved tree genuinely returns."
    )
    return False, "\n".join(lines)


def check_no_stale_baseline_entries(
    root, baseline_path, precomputed: "Dict[str, List[str]] | None" = None
) -> Tuple[bool, str]:
    """RED iff the baseline names a path, for a given blocking class, that
    the current scan does not produce for that class -- a dead grandfather
    slot. Deleted, renamed, or fixed files never leave the baseline on
    their own (the ratchet only ever compares NEW-vs-baseline in
    `check_against_baseline`, never prunes in the other direction), so a
    stale entry silently stays a live pass for whatever gets re-added at
    that exact path with that exact violation shape later -- the entry
    excuses nothing real, yet keeps the gate green regardless.

    Mirrors `check_no_stale_exempt_prefixes`'s discipline: a carve-out (or,
    here, a grandfathered debt slot) that outlives what it covered stops
    being what it claims to be and becomes standing permission for whatever
    lands at that path next.

    `precomputed`, if given, is an already-computed `scan()` result -- lets
    `main()` share one full-repo scan across all top-level checks (see
    `check_against_baseline`'s docstring).
    """
    current = precomputed if precomputed is not None else scan(root)
    baseline = load_baseline(baseline_path)

    stale: Dict[str, List[str]] = {}
    for cls in CLASSES:
        cur = set(current[cls])
        base = set(baseline.get(cls, []))
        missing = sorted(base - cur)
        if missing:
            stale[cls] = missing

    if not stale:
        return True, (
            "OK: every baseline entry across all blocking classes still "
            "matches a current violation -- no stale grandfather slots."
        )

    total_stale = sum(len(paths) for paths in stale.values())
    lines = [
        f"STALE BASELINE ENTRIES: {total_stale} entry(ies) across "
        f"{len(stale)} blocking class(es) name a path the current scan no "
        "longer produces for that class:"
    ]
    for cls in CLASSES:
        if cls not in stale:
            continue
        paths = stale[cls]
        lines.append(f"  {cls}: {len(paths)} stale entry(ies):")
        for p in paths:
            lines.append(f"    - {p}")
    lines.append(
        "  The file was deleted, renamed, or the violation was fixed, and "
        "the baseline entry was never pruned -- it is now a dead "
        "grandfather slot: re-adding this exact path with this exact "
        "violation shape would pass silently. Prune the entry from "
        "state/posix-exec-baseline.json; the baseline is shrink-only, "
        "never widen it back."
    )
    return False, "\n".join(lines)


def _default_root() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )
    root = proc.stdout.strip()
    return root or os.getcwd()


def main(argv: List[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="check-posix-exec-assumptions",
        description=(
            "RED-on-existence guard for POSIX-only execution assumptions: "
            "4 Tier A checkout-breaker classes (symlink in index, case "
            "collision, reserved filename, path too long) plus implicit_"
            "encoding (bare open() with no encoding=) at hard zero-"
            "tolerance with no baseline; 5 blocking classes (env-stripped "
            "shebang, extensionless #!-executable, git mode 100755, "
            "hardcoded path separator, runtime POSIX-mode-bit reasoning) "
            "ratcheted against a frozen per-repo baseline; plus 2 "
            "report-only classes (unresolved cross-machine path literal, "
            "unclassified residual)."
        ),
    )
    ap.add_argument(
        "--root",
        default=None,
        help="Repo root to scan (default: git rev-parse --show-toplevel of cwd).",
    )
    ap.add_argument(
        "--baseline",
        default=None,
        help="Baseline JSON path (default: <root>/state/posix-exec-baseline.json).",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit one machine-readable JSON object to stdout instead of the "
            "human-readable text report -- for CI tooling consuming this "
            "guard's result programmatically without text-scraping stdout. "
            "Same underlying checks and exit code either way; --json only "
            "changes the render."
        ),
    )
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(_default_root())
    baseline_path = (
        Path(args.baseline) if args.baseline else root / "state" / "posix-exec-baseline.json"
    )
    if not baseline_path.is_file():
        print(
            f"check-posix-exec-assumptions: no baseline at {baseline_path} -- "
            "treating as empty (every current violation reports as NEW)",
            file=sys.stderr,
        )

    # One full-repo scan, shared across all three checks below (review
    # 2026-07-28 Finding 5) -- check_against_baseline / check_tier_a_zero_
    # tolerance / check_implicit_encoding_zero_tolerance each used to
    # independently re-derive _tracked_files() and re-scan every file from
    # scratch, three full passes over the same tree for output `scan()`
    # already computes once.
    current_scan = scan(root)

    ok, msg = check_against_baseline(root, baseline_path, precomputed=current_scan)

    try:
        baseline_rel = baseline_path.relative_to(root)
    except ValueError:
        baseline_rel = baseline_path
    grown_ok, grown_msg = assert_baseline_not_grown(root, baseline_rel)

    tier_a_ok, tier_a_msg = check_tier_a_zero_tolerance(root, precomputed=current_scan)

    implicit_encoding_ok, implicit_encoding_msg = check_implicit_encoding_zero_tolerance(
        root, precomputed=current_scan
    )

    prefixes_ok, prefixes_msg = check_no_stale_exempt_prefixes(root)

    stale_baseline_ok, stale_baseline_msg = check_no_stale_baseline_entries(
        root, baseline_path, precomputed=current_scan
    )

    overall_ok = (
        ok
        and grown_ok
        and tier_a_ok
        and implicit_encoding_ok
        and prefixes_ok
        and stale_baseline_ok
    )

    if args.json:
        # `scan` is the ALL_CLASSES-keyed current result, included wholesale
        # so a consumer never has to re-run the scan itself to get per-class
        # relpath lists -- the same data the human-readable branch derives
        # its per-class debt counts from, just structured instead of prose.
        print(
            json.dumps(
                {
                    "ok": overall_ok,
                    "checks": {
                        "baseline": {"ok": ok, "message": msg},
                        "baseline_growth": {"ok": grown_ok, "message": grown_msg},
                        "tier_a": {"ok": tier_a_ok, "message": tier_a_msg},
                        "implicit_encoding": {
                            "ok": implicit_encoding_ok,
                            "message": implicit_encoding_msg,
                        },
                        "stale_exempt_prefixes": {
                            "ok": prefixes_ok,
                            "message": prefixes_msg,
                        },
                        "stale_baseline_entries": {
                            "ok": stale_baseline_ok,
                            "message": stale_baseline_msg,
                        },
                    },
                    "scan": current_scan,
                },
                indent=2,
            )
        )
    else:
        print(msg)
        print(grown_msg)
        print(tier_a_msg)
        print(implicit_encoding_msg)
        print(prefixes_msg)
        print(stale_baseline_msg)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
