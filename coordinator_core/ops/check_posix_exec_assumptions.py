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
    fixed: `_is_windows_guarded()` (below) recognizes a nested `If` guard,
    the harder short-circuit `and`-chain shape (the actual
    `retire-claude-bin.py` case), and (2026-08-13) a bare, `else:`-less
    early-return guard clause, with positive-control fixtures proving the
    fix doesn't blanket-suppress detection entirely. Demoting once the
    cause is fixed would be consistency for its own sake at real
    enforcement cost -- report-only classes get skimmed and ignored. The
    residual gap is narrower now: a windows-test the detector does not
    recognize as one (only os.name/sys.platform/platform.system() forms
    are), which surfaces as an immediately-resolvable EXEMPTIONS case
    rather than a silent trap -- see the per-class failure-message text in
    `check_against_baseline`.
  - **Tier C, narrowed and promoted** (part of `CLASSES`) —
    `unresolved_cross_path`, promoted to BLOCKING 2026-08-13 (eng-director
    ruling, `state/audits/2026-08-13-the Director of Engineering-carveout-and-cross-path-ruling.md`
    § Q2.1) after its WIDE definition was re-measured and found defective,
    not merely unread: a live re-run against this tree produced 178
    findings, of which **127 (~71%) matched only the class's own
    `<drive-letter>:[\\/]` arm** and were, in the main, this repo's own
    Windows-portability layer being flagged for existing --
    `pyresolve.py`'s `os.path.join("C:\\Program Files", ...)` interpreter-
    discovery ladder, `break_glass.py`'s `_TMP_SHAPE_MARKERS` Windows
    temp-path SYNTAX RECOGNITION. That is the identical defect shape that
    got `path_separator`/`posix_mode_bits` demoted then promoted back on
    2026-07-28 -- a blocking class this noisy trains authors to route
    around the guard, the module's own stated reason for that July
    precision fix. **The drive-letter arm is dropped entirely** (a
    precision decision, not a scope reduction -- see the class's own
    Report-only-turned-blocking entry below for the measurement and the
    negative-spec bullet closing the re-widening path). The surviving
    home-rooted arm is further narrowed to CURRENT-MACHINE operator-home
    literals only, reusing (not reinventing) the discrimination
    `coordinator/bin/check-machine-path-leak.py` already draws for
    `working-repos.yaml` ("current-machine-home-rooted only") -- see
    `_current_machine_home()` below. After both narrowings the tracked
    population is a small, fixable drain, not a debt corpus, so this class
    is gated at ZERO from day one like `implicit_encoding` and Tier A --
    **no baseline entry for it, ever** (a baseline here would be the
    grandfather-list failure this same ruling's evidence names: a baseline
    that sat unmoved from the day it was frozen).
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
    (34 files in claude-klabauter, 1 in coordinator-claude), fixed outright on
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
    etc., an equivalent short-circuit `and`-chain, or a bare, `else:`-less
    early-return guard clause) that structurally never runs on Windows —
    that is correct cross-platform code, not debt, and flagging it would
    train authors to route around the guard (`_is_windows_guarded`,
    precision fix 2026-07-28 after `retire-claude-bin.py:186` false-fired on
    the nested-`If` shape, widened 2026-08-13 to also recognize the bare
    early-return shape). This fix, plus positive-control fixtures proving it
    doesn't blanket-suppress detection, is why both classes are BLOCKING
    here rather than demoted: they were briefly report-only the same day,
    then promoted back once the cause was fixed (see the CLASSES
    declaration's own comment for the full history). A windows-test the
    detector does not recognize as one (only os.name/sys.platform/
    platform.system() forms are) is the residual gap — see that function's
    own docstring for exactly what it does and doesn't see; hitting it
    surfaces as an EXEMPTIONS-resolvable failure message, not a silent
    trap.

  - `unresolved_cross_path` — BLOCKING, zero-tolerance (no baseline, ever),
                            promoted 2026-08-13 from a WIDE, report-only
                            definition (2026-08-13 eng-director ruling,
                            `state/audits/2026-08-13-the Director of Engineering-carveout-and-
                            cross-path-ruling.md` § Q2.1). The WIDE
                            definition this class used to carry was: a
                            tracked `.py` file containing a hardcoded
                            cross-machine/cross-drive path literal
                            (`/Users/<name>/...`, `/home/<name>/...`, OR
                            `<drive-letter>:\\...`) that does not resolve
                            via `${COORDINATOR_SETTINGS_HOME:-$HOME/
                            .coordinator-claude-settings}` or the
                            machine-local `repos.*` registry. A live
                            re-measurement against this tree found that
                            definition DEFECTIVE, not merely unread: 178
                            findings, of which **127 (~71%) matched only
                            the `<drive-letter>:[\\/]` arm** and were, in
                            the main, this repo's own Windows-portability
                            layer being flagged for existing --
                            `pyresolve.py`'s Windows interpreter-discovery
                            ladder (`os.path.join("C:\\Program Files",
                            ...)`), `break_glass.py`'s
                            `_TMP_SHAPE_MARKERS` Windows temp-path SYNTAX
                            RECOGNITION. That is the identical false-
                            positive shape that got `path_separator`/
                            `posix_mode_bits` demoted-then-repromoted on
                            2026-07-28: a blocking class this noisy trains
                            authors to route around the guard, the same
                            reason cited for that July precision fix.

                            THE NARROWED DEFINITION (current, live): the
                            `<drive-letter>:[\\/]` arm is DROPPED
                            ENTIRELY -- a precision decision, not a scope
                            reduction, evidenced by the 127/178
                            measurement above. The surviving home-rooted
                            arm (`/Users/<name>/...`, `/home/<name>/...`)
                            is narrowed FURTHER to literals rooted at the
                            CURRENT MACHINE's own operator home directory
                            only -- a synthetic `/Users/alice` in a
                            fixture is test data, not a leak; a literal
                            matching THIS machine's `$HOME` (or
                            `os.path.expanduser("~")`/`%USERPROFILE%`
                            fallback) breaks on every other machine and on
                            Windows. This reuses, rather than reinvents,
                            the exact discrimination
                            `coordinator/bin/check-machine-path-leak.py`
                            already draws for `working-repos.yaml`
                            ("current-machine-home-rooted only") -- see
                            `_current_machine_home()` below; that script
                            is a hyphenated `coordinator/bin/` CLI
                            (fleet-wide convention: invoked as a
                            subprocess, never imported -- see its own test
                            suite), so its logic is REUSED by mirroring
                            its two-line computation with a citation, not
                            by a fragile `importlib.util` load of a
                            dash-named script. After both narrowings the
                            live population is small enough to fix
                            outright (twelve tracked files at ruling
                            time), so this class is gated at ZERO like
                            `implicit_encoding` and Tier A -- no baseline
                            entry for it, ever; see `CLASSES`' own comment
                            for why a baseline here would reproduce this
                            plan's own thesis.

                            NEGATIVE-SPEC: re-widening this class's
                            literal-matching back to include a
                            `<drive-letter>:[\\/]` arm re-flags this
                            repo's own correct Windows-portability code
                            (the 127/178 measurement above) -- it is not a
                            safe restoration of coverage, it is
                            reintroducing the exact defect this narrowing
                            fixes. Scanned via AST on `.py` files only,
                            deliberately narrower than a repo-wide text-grep
                            (see Reconciliation with check-machine-path-
                            leak.py below) -- markdown/wiki prose citing an
                            example path is out of scope by construction, not
                            merely tolerated (docstrings are excluded from
                            literal matching, see Negative-spec below).

Tier D / residual — report-only classes (scanned, counted, and listed every
run, but NEVER fail `check_against_baseline` or `assert_baseline_not_grown`
— see Report-only contract below):

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
invoked from any sibling repo's own test tier (e.g. Coordinator-claude) against
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
this function (this repo's and coordinator-claude's own real-tree pytest suites) run
`assert_baseline_not_grown` as a plain pytest assertion, necessarily AFTER
any widening commit has already landed — neither repo wires this into a
git pre-commit hook (staged-vs-parent-HEAD is the only diff shape where
`HEAD` would be meaningful), and requiring one would only hold on machines
that have actually run the installer, which does not fit this fleet's
shared-branch, many-concurrent-session shape. Anchoring to the file's own
introducing commit instead means the diff is against something that never
moves, so it keeps firing no matter how many commits land afterward.
**Named residual gap** (mirrors this module's other honestly-documented
detector gaps, e.g. `_is_windows_guarded`'s unrecognized-test-form case
above): an
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

A THIRD admission test governs a grant the two-leg-pair test cannot reach:
**permanent artifact-shape irreducibility** (2026-08-13 eng-director
ruling, `state/audits/2026-08-13-the Director of Engineering-carveout-and-cross-path-ruling.md`
§ Q1.c). This is not a fourth carve-out concept -- it is the artifact-side
RESTATEMENT of the rule `docs/reference/shell-out-carve-outs.md` already
carries for call-sites: its classes (c)/(d)/(e) are sanctioned because
"their subject is the interpreter or shell itself." Where a FILE's own
subject is the interpreter or shell that would have to run its port, the
two-leg-pair test's "a Windows-invocable counterpart exists" bar has no
content to deliver -- porting is not an available action, so the override
is inapplicable by construction, not overridden. A grant under this test
needs all four items below, or no grant, same discipline as every other
EXEMPTIONS bar in this module:

  1. **The mechanism, stated concretely, not a bucket label.** "bootstrap-
     circular" is a label; "resolves PYTHON_BIN before any Python exists
     to run" is a mechanism.
  2. **Permanence, not contingency.** The port must be impossible by the
     artifact's OWN nature, not impossible merely while some peer artifact
     happens to stay shell-shaped -- a row whose impossibility depends on a
     peer artifact's current shape is migration debt, not this test, and
     belongs in a baseline instead.
  3. **The Windows leg, named -- or explicitly stated absent.** Either the
     artifact that carries this capability on Windows, or a plain
     statement that none exists. Windows is P0: no Windows leg on a hot
     path is break-class, and no register entry here saves it.
  4. **A live caller, named -- or an explicit statement that none was
     found in this repo.**

Elimination-target clause, required alongside the four items above --
without it this test becomes a permanent home for anything that merely
runs early. Where porting is unavailable by construction, hot-path status
does NOT authorize the artifact to persist: it sets the PRIORITY of
eliminating the need for it by other means -- resolving the interpreter
(or probing the shell) at install time and writing the resolved answer, so
no runtime shim runs at all. Hot-path + unportable is a standing reduction
target with an owner, not a settled row -- the same "standing reduction
target" teeth class (b) in `shell-out-carve-outs.md` already carries for
local git hooks.

Keying (repo-scoped, closed 2026-08-03 — was the coordinator-claude memo of
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
      fleet-wide (see coordinator-claude `coordinator.local.md` P0 bash-kill
      campaign) and the remaining count is near zero; adding a shell-syntax
      parser for a near-extinct substrate was not worth the added false-
      positive surface. A `.py`-only scope is stated as a real limitation,
      not implied to be complete coverage of every interpreter in the tree.

Spec backlink: coordinator-claude coordinator/docs/wiki/foreign-platform-path-guard.md
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
# That defect is now FIXED (`_is_windows_guarded()` below, covering a
# nested `If` guard, the harder short-circuit `and`-chain shape actually
# found in `retire-claude-bin.py`, and (2026-08-13) a bare, `else:`-less
# early-return guard clause), with positive-control fixtures proving the
# fix doesn't blanket-suppress (an inverted branch still fires, an
# ungated call still fires) -- that evidence is what earns keeping this
# BLOCKING rather than demoting: demoting once the cause is fixed would be
# consistency for its own sake at the cost of real enforcement (report-only
# classes get skimmed and ignored). The residual gap -- a windows-test the
# detector does not recognize as one, e.g. a project-local wrapper function
# rather than os.name/sys.platform/platform.system() -- is narrow and, when
# hit, surfaces as a guard failure an author can immediately resolve via
# EXEMPTIONS (see the per-class failure-message text in
# `check_against_baseline`), not a silent trap.
CLASSES: Tuple[str, ...] = (
    "env_shebang",
    "extensionless_exec",
    "mode_100755",
    "path_separator",
    "posix_mode_bits",
    "unresolved_cross_path",
)

# Report-only classes: scanned and printed every run, never gate the build,
# never persisted into the baseline JSON, never counted by the append-
# forbidden growth check.
#
# `unresolved_cross_path` is NOT here -- it was promoted to BLOCKING (above)
# 2026-08-13 after being narrowed (module docstring, Q2.1 ruling). It stays
# gated at zero with NO baseline entry, same discipline as
# `implicit_encoding`/Tier A, not ratcheted like the rest of `CLASSES`; a
# test asserts its absence from this tuple so a future demotion back to
# report-only fails red instead of passing quietly (see
# `test_unresolved_cross_path_is_not_report_only`).
REPORT_ONLY_CLASSES: Tuple[str, ...] = (
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
# count (34 files in claude-klabauter, 1 file in coordinator-claude, all fixed
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
    "coordinator_core.ops.setup_chain_walker` (asked of coordinator-claude-em "
    "2026-08-03 via the doe-contract-stale-surfaces memo, item 4) — delete "
    "this entry then rather than letting it outlive the forwarder."
)

_M8_REVIEW_TRAIL_SNAPSHOT_REASON = (
    "FROZEN REVIEW-EVIDENCE SNAPSHOT, not live code. Everything under this "
    "prefix is a verbatim point-in-time copy of the claude-klabauter engine's own "
    "bash_guards/write_guards modules, checked into coordinator-claude's review trail so a "
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
    "_host_is_windows(): return path` early return with no `else:` -- the "
    "bare-early-return SHAPE is recognized by `_is_windows_guarded()` "
    "(2026-08-13), but its TEST is not: `_host_is_windows()` is a "
    "project-local wrapper function, not one of the recognized "
    "os.name/sys.platform/platform.system() forms `_branch_windows_status()` "
    "matches on, so this site is still invisible to the detector -- the "
    "residual, NAMED gap that function's own docstring now describes. The "
    "flagged `.replace(\"/\", \"\\\\\")` only executes once that guard has "
    "already confirmed the host IS Windows, building a well-formed native "
    "`X:\\...` path from an MSYS-spelled one for a subsequent native "
    "`ntpath.join` -- the correct construct for that platform, not a POSIX "
    "assumption. Rewriting the guard's test to a recognized form purely to "
    "satisfy the detector, or adding an `else:`, is exactly what the gate's "
    "own failure message forbids."
)

_REASON_PS51_TARGET_SYNTAX = (
    "_build_ps_command() escapes a path to backslash form before embedding "
    "it in a single-quoted PowerShell literal handed to a `powershell.exe` "
    "subprocess -- the string's destination interpreter is Windows PowerShell "
    "5.1, unconditionally, regardless of which host is running this Python "
    "process. os.sep reflects the host running THIS code, not the syntax the "
    "downstream interpreter parses; PS 5.1 itself accepts forward slashes "
    "fine (own comment), so this exists purely for byte-parity with the "
    "predecessor bash oracle's Windows-native path form, not correctness. "
    "The construct is still not a local filesystem operation -- no open()/ "
    "os.path call ever sees the backslashed value -- so os.sep would be "
    "orthogonal to what it does, not a stronger fix."
)

_REASON_CHMOD_HOOK_POSIX_EXEC = (
    "Sets the POSIX exec bit (0o755) on a generated git-hook script whose "
    "own first line is a hardcoded `#!/bin/sh` shebang -- git invokes hooks "
    "by direct execution (shebang dispatch), which requires the exec bit on "
    "POSIX. On Windows the bit is a harmless no-op (NTFS has no POSIX mode "
    "bits; os.chmod's exec argument is silently ignored) and Windows git's "
    "own hook-execution path does not consult it, so this call is neither "
    "wrong nor a decision Windows lies about -- it is dead weight there, not "
    "a portability bug. Nothing here reads the bit back as a decision input "
    "(the FIX-class shape); it only sets the one mode the hook needs to fire "
    "on the platform where the mode matters at all."
)

_REASON_CHMOD_RMTREE_UNBLOCK = (
    "os.chmod(target_path, 0o777) runs inside shutil.rmtree's onerror "
    "callback to clear a read-only file/directory that is blocking "
    "deletion, then immediately retries the delete. This is a write-only "
    "state-setting op, never a decision input: nothing reads the bit back "
    "afterward, so the FIX-class concern (Windows silently lying about a "
    "value a decision depends on) does not apply -- os.chmod's write side "
    "works correctly on both platforms (on Windows it clears the "
    "FILE_ATTRIBUTE_READONLY flag, the documented stdlib workaround for "
    "this exact rmtree failure mode). Trimming the exec bits out of the "
    "literal is not a real port either: target_path may be a directory "
    "here (onerror fires for both file and directory removal failures), "
    "and a directory needs its execute bit set on POSIX to be traversed/ "
    "emptied at all -- narrowing to 0o666 would silently reintroduce the "
    "same rmtree failure for a read-only directory tree. The broad literal "
    "is the correct, minimal construct for a callback that does not know "
    "in advance which of the two node types it was called for."
)

_REASON_ENTRYPOINT_INTERPRETER_NONE_IS_POSIX_ONLY = (
    "`os.access(script_path, os.X_OK)` in `_run_one_entrypoint` only runs "
    "when `interpreter is None`, and `_resolve_entrypoint_gate_interpreter` "
    "(the sole producer of that value, called once per `run_entrypoint_gate` "
    "sweep) returns `None` if-and-only-if `os.name != \"nt\"` -- see that "
    "function's own docstring and its `if os.name != \"nt\": return None` "
    "body. The guard is real, just expressed across two functions rather "
    "than as a local branch `_is_windows_guarded()` can see -- that "
    "function only walks the enclosing FUNCTION's own AST (an inline `If`, "
    "an `and`-chain, or a same-function bare early-return), never a "
    "second function's return-value contract, so a cross-function guard "
    "like this one is structurally out of its reach regardless of shape -- "
    "this call never executes on Windows, where `os.access(..., os.X_OK)` "
    "would otherwise lie (returns True for any readable file). Restructuring "
    "the call site to satisfy the detector would not change what actually "
    "runs; the invariant already holds."
)

_REASON_GEN_SETTINGS_HOOKS_C7 = (
    "Two distinct path_separator constructs, both carve-outs, in "
    "gen_settings_hooks.py: (1) `_DRIVE_LETTER_RE = re.compile(r\"[A-Za-z]:"
    "[\\\\/]\")`, consulted only by `_assert_portable_command`'s structural "
    "backstop that a resolved coordinator_root never leaked a raw Windows "
    "drive-letter form into an emitted hook command -- this parses Windows "
    "path SYNTAX appearing in scanned command text, never opens a "
    "filesystem path (REASON_WIN_SYNTAX_IN_TEXT's territory). (2) "
    "`coordinator_root = coordinator_root.replace(\"\\\\\", \"/\")`: "
    "Review: an earlier draft of this reason claimed the normalized value "
    "is never opened -- false. `run()` calls `os.path.isdir(coordinator_root)` "
    "on the very next statement after this normalization, a real filesystem "
    "stat on the literal-normalized string. The carve-out still holds, on "
    "different grounds: `os.path.isdir` (and the Windows filesystem API it "
    "wraps) accepts forward-slash paths interchangeably with backslash, so "
    "the normalization does not break that call. It is also required "
    "independently for `coordinator_root`'s OTHER consumers in the same "
    "function -- it is resolved once per run and then interpolated into "
    "emitted hook command strings (via `_rewrite_cpr`) that are themselves "
    "invoked through POSIX sh even under Git-Bash on Windows (own comment: "
    "'mirrors bash's belt-and-suspenders normalisation ... canonical fix "
    "lives at the machine-local cmd_get emission point, this is "
    "defense-in-depth') -- forward-slash form is the CORRECT form for that "
    "downstream shell dialect regardless of host, the same territory "
    "REASON_CANON_STRING/REASON_SHELL_EMBED_FORWARD_SLASH cover elsewhere. "
    "os.sep would reflect the host running this generator, not the shell "
    "dialect the emitted command is later parsed by -- wrong for both the "
    "`isdir` check (though tolerated there) and the shell-interpolation "
    "consumers (not tolerated there)."
)

_REASON_GEN_SETTINGS_HOOKS_TEST_C7 = (
    "Two distinct path_separator constructs, both carve-outs, mirroring the "
    "module under test: (1) `_DRIVE_LETTER_RE`, a copy of the production "
    "regression-detection regex used by this test module's own assertions "
    "that no Windows drive-letter form leaked into generated settings.json "
    "-- parses Windows path SYNTAX in scanned output text, not a filesystem "
    "path (REASON_WIN_SYNTAX_IN_TEXT). (2) "
    "`backslashed = str(coordinator_root).replace(\"/\", \"\\\\\")` in "
    "test_windows_backslash_coordinator_root_is_normalised: constructs a "
    "Windows-shaped FIXTURE value fed to `generate()` to exercise the "
    "backslash-normalization arm directly -- the backslash is the shape "
    "under test, so os.sep would silently rewrite the specimen back to "
    "forward-slash form on POSIX and the test would stop exercising the "
    "arm it exists for (REASON_WIN_SYNTAX_AS_FIXTURE's territory)."
)

_REASON_INSTALL_ONE_EXEC_BIT_SKIPIF_GAP = (
    "The two flagged `dst.stat().st_mode & stat.S_IXUSR` sites (in "
    "test_install_one_identical_dst_still_applies_exec_bit and "
    "test_install_one_diverging_dst_force_overwrite_applies_exec_bit) are "
    "both inside functions decorated `@_EXEC_BIT_SKIP` -- a module-level "
    "`pytest.mark.skipif(os.name == \"nt\", reason=...)` (own docstring: "
    "'NTFS has no POSIX exec bit ... this exec-bit-only assertion has no "
    "Windows analogue'). This is a real, structural Windows guard, but at "
    "the pytest-decorator level -- `_is_windows_guarded()` walks enclosing "
    "`If`/`and`-chain AST nodes only, per its own docstring, and does not "
    "see a decorator attached to the enclosing FunctionDef. The sibling "
    "assertions at the SAME construct shape, guarded inline instead "
    "(`if os.name != \"nt\": assert dst.stat().st_mode & stat.S_IXUSR`, "
    "lines ~281/~339) are correctly recognized and do not fire -- proving "
    "the gap is specifically the decorator shape, not the construct itself. "
    "Rewriting these two sites to use the inline-guard shape purely to "
    "satisfy the detector would be the same class of cosmetic churn the "
    "module's own guidance forbids for the bare-early-return gap; the "
    "decorator already fully guards the code on the platform where the "
    "assertion is meaningless."
)

_REASON_CONFIG_PATH_FOREIGN_AUTHORED = (
    "Normalizes a config-supplied path value (plugin manifest "
    "`live_path`/`source_path`) that may have been authored on either "
    "platform to forward-slash form before resolving it via `pathlib.Path` "
    "on this host. os.sep only reflects the host running this code, not "
    "the platform the config was written on, and would leave a "
    "foreign-authored backslash unrecognized by `PurePosixPath` parsing on "
    "a POSIX host; forward-slash is the one separator `pathlib.Path` "
    "accepts natively on every platform, making this the correct "
    "normalize-before-resolve idiom."
)

_REASON_CHMOD_RELATIVE_INVARIANT = (
    "Sets an arbitrary starting file mode via `os.chmod` only to assert an "
    "atomic-write preserves it -- a before/after relative invariant, not a "
    "specific POSIX bit pattern. On a platform with no real POSIX mode "
    "bits (`os.stat().st_mode & 0o777` always the same degenerate value "
    "regardless of `chmod`'s argument), both sides of the comparison read "
    "that same value and the assertion still holds; this is a platform "
    "semantic gap no code change can close, not a POSIX assumption in the "
    "test."
)

_RESOLVE_PYTHON_SH_IRREDUCIBILITY_REASON = (
    "coordinator/lib/resolve-python.sh -- granted under the third "
    "EXEMPTIONS admission test (permanent artifact-shape irreducibility, "
    "2026-08-13 eng-director ruling), not the two-leg-pair test above: "
    "porting this file to Python is literally circular, since its entire "
    "job is resolving PYTHON_BIN/PYTHON_ARGS before any Python interpreter "
    "exists to run a ported replacement. Mechanism: resolves the Python "
    "interpreter itself (python.org install-dir probe, PATH via `command "
    "-v` rejecting WindowsApps, `py`/`pyw` launcher fallback) -- nothing to "
    "invoke a port with. Permanence: the impossibility is intrinsic to "
    "what the file DOES, not contingent on any peer artifact staying "
    "shell-shaped. Windows leg: this file IS its own Windows leg -- its "
    "body branches on Windows-specific resolution logic (python.org "
    "install dirs, the `py` launcher) and runs under the POSIX-compatible "
    "shell (git-bash/MSYS) any Windows caller of a sourced bash lib "
    "already requires; there is no separate counterpart artifact because "
    "resolution spans both platforms in one script by design. Live "
    "caller: zero in either plane, verified 2026-08-13 -- none inside "
    "claude-klabauter by grep, and coordinator-claude's own sweep found the single "
    "coordinator-claude-plane consumer (the OSS coordinator-update skill) "
    "repointed onto the COORDINATOR_PYTHON contract in their d16272a9e, "
    "with no interface constraint asserted on this file. Per the ruling's "
    "Q1.b(ii) the caller question was never load-bearing for this grant "
    "either way. Elimination-target: hot-path "
    "status (if confirmed) does not authorize this file to persist -- it "
    "sets the priority of resolving the interpreter at install time and "
    "writing the resolved path, so no runtime shim runs at all."
)

_INVOKING_SHELL_BASH4_PROBE_IRREDUCIBILITY_REASON = (
    "coordinator/scripts/lib/invoking-shell-bash4-probe.sh -- granted "
    "under the third EXEMPTIONS admission test (permanent artifact-shape "
    "irreducibility), the behaviour-under-test twin of resolve-python.sh's "
    "bootstrap-circularity: porting it to Python would replace the exact "
    "thing it exists to detect. Mechanism: detects whether the INVOKING "
    "shell is bash>=4 and emits loud remediation when it is not -- its own "
    "docstring states it must parse and run correctly under bash 3.2 and "
    "plain /bin/sh, because that failure mode is the specimen. Permanence: "
    "the invoking shell IS the subject under test; a Python port could "
    "only ever observe its OWN interpreter, never the shell that invoked "
    "it, so the impossibility is intrinsic, not contingent on any peer "
    "artifact. Windows leg: this file is its own Windows leg -- the same "
    "/bin/sh-compatible probe runs under whatever POSIX shell (git-bash) "
    "a Windows caller's sourcing chain already provides; no separate "
    "counterpart exists because the probe's whole point is to be callable "
    "from any invoking shell. Live caller: none found by grep inside "
    "claude-klabauter; per its own docstring and the 2026-08-13 triage it is "
    "consumed by /pickup's coordinator-claude-plane wiring, outside this "
    "repo's Tier-3 reach -- not load-bearing for this grant per the "
    "ruling's Q1.b(ii). Elimination-target: hot-path status does not "
    "authorize persistence -- the priority is resolving the invoking-shell "
    "check earlier (e.g. at install time) so fewer runtime probes are "
    "needed, mirroring class (b)'s standing reduction-target teeth."
)

_TEST_BIN_SH_POLYGLOT_DIRECT_INVOCATION_IRREDUCIBILITY_REASON = (
    "coordinator/bin/tests/test-bin-sh-polyglot-direct-invocation.sh -- "
    "granted under the third EXEMPTIONS admission test (permanent "
    "artifact-shape irreducibility), behaviour-under-test bucket: it "
    "verifies cross-repo-memo's POST-RETIREMENT invocation contract (own "
    "docstring -- the sh/python polyglot trampoline this suite once "
    "exercised was itself retired 2026-07-21) by spawning real "
    "interpreters and asserting which one actually runs; the harness's own "
    "subject is direct-interpreter invocation, so it stays a shell-invoked "
    "scaffold by design rather than something a Python port would "
    "preserve unchanged. Mechanism: constructs a scratch PATH exposing "
    "only a python3 symlink and asserts cross-repo-memo --help resolves "
    "it directly, no sh trampoline. Windows leg / hot-path: NONE NEEDED -- "
    "this file's own line 2 self-declares non-hot-path ('interpreter "
    "spawns run in the CI/local test harness, never the Windows "
    "interactive coordinator hot-path'), confirmed by 2026-08-13 triage as "
    "the one row with positive in-file evidence rather than inference. "
    "Live caller: this repo's own test tier invokes it directly; it is "
    "not sourced or consumed elsewhere."
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
            "coordinator/lib/resolve-python.sh": _RESOLVE_PYTHON_SH_IRREDUCIBILITY_REASON,
            "coordinator/bin/tests/test-bin-sh-polyglot-direct-invocation.sh": _TEST_BIN_SH_POLYGLOT_DIRECT_INVOCATION_IRREDUCIBILITY_REASON,
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
            "coordinator/lib/resolve-python.sh": _RESOLVE_PYTHON_SH_IRREDUCIBILITY_REASON,
            "coordinator/scripts/lib/invoking-shell-bash4-probe.sh": _INVOKING_SHELL_BASH4_PROBE_IRREDUCIBILITY_REASON,
            "coordinator/bin/tests/test-bin-sh-polyglot-direct-invocation.sh": _TEST_BIN_SH_POLYGLOT_DIRECT_INVOCATION_IRREDUCIBILITY_REASON,
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
            # C7-guards (2026-08-13): `_normalize`/`_normalize_path`/`_collapse_slashes`
            # helpers in every one of these `check()`-shaped write guards normalize
            # `tool_input.file_path` (or `notebook_path`) -- a string supplied by the
            # editing tool that may already contain either separator depending on the
            # invoking platform -- to forward-slash form purely so the guard's own
            # regex/glob/prefix match runs consistently. None of these hand the
            # normalized value to `open()`/`os.path`/`Path` I/O; `os.sep` would only
            # recognize this host's native separator and miss a payload written with
            # the other one. Same idiom already exempted under this reason for
            # `nudge_new_sh_file_naked_python.py`/`nudge_prose_queue_append.py`/etc.
            "coordinator_core/write_guards/block_completion_monolith_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_consumed_handoff_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_cutover_phase_hand_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_dev_side_mirror_wiki.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_em_hand_edit_pending_review_integration.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_memo_status_hand_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_priority_ledger_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_subagent_archive_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_subagent_plan_body_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_tracker_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/block_unauthorized_claude_md_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_baton_body_bar.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_improvement_queue_write.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_tasks_state_folder_split.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_terminal_artifact_edit.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/write_guards/nudge_windows_subprocess_popup.py": _REASON_TOOL_INPUT_PATH,
            # `_rel()` test helper normalizes a `Path.relative_to()` result -- a
            # resolved-path string -- to forward-slash form purely for a test
            # assertion's string comparison, never a live I/O path; same shape as
            # `test_bump_out_of_repo_tool_write.py` above.
            "coordinator_core/write_guards/tests/test_block_cutover_phase_hand_edit.py": _REASON_CANON_STRING,
            # `_normalize_windows_argv0_head_path_with_spaces`/`_normalize_windows_git_argv0`
            # rewrite a Windows-drive-letter/root-rooted, backslash-separated argv0
            # path -- recognized as Windows path SYNTAX appearing in the raw bash
            # command TEXT being scanned, before tokenization -- to forward-slash form
            # so the allowlist's basename-identity check recognizes it. The command
            # string is never opened as a filesystem path by this guard; only parsed
            # and pattern-matched. Same shape as `_REASON_WIN_SYNTAX_IN_TEXT`'s existing
            # `fix_concrete_path_citations.py`/`guard_concrete_path_citations.py` entries.
            "coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py": _REASON_WIN_SYNTAX_IN_TEXT,
            # Same argv0-rewrite idiom as above (3 sites), plus one additional site
            # (`_git_root_relative_path_denies_repo_root`) that canonicalizes a
            # repo-relative dirty-file-path STRING (from `git diff --name-only`) and
            # a `git_root` string for a literal-equality/containment comparison via
            # `posixpath` -- never resolved against this host's real filesystem via
            # `os.path`/`Path` I/O. Both shapes are host-independent string handling,
            # not a live filesystem decision.
            "coordinator_core/bash_guards/block_subagent_commit.py": _REASON_WIN_SYNTAX_IN_TEXT,
            # Same argv0-rewrite idiom, 2 sites (git/coordinator-safe-commit-family
            # and shell/python-interpreter-family basenames) -- Windows path syntax
            # appearing in scanned command text, never opened by this guard.
            "coordinator_core/bash_guards/block_subagent_destructive_action.py": _REASON_WIN_SYNTAX_IN_TEXT,
            # `_base`/`_norm_path` normalize a shlex-split command-line ARGUMENT
            # (an executable basename, or a testpaths-comparison token) that may have
            # been typed with either separator by whichever agent/OS composed the
            # dispatched bash command, to forward-slash form for string comparison
            # against this module's own configured testpaths/interpreter-name sets.
            # The one call site that reaches real I/O (`os.path.isdir(os.path.join(cwd,
            # norm))`) is unaffected: Windows' filesystem API accepts a forward-slash
            # `norm` component in `os.path.join`/`os.path.isdir` interchangeably with
            # a backslash one, so the canonicalization does not break that lookup --
            # it is purely for the preceding string-equality/prefix comparison against
            # testpaths.
            "coordinator_core/bash_guards/check_test_suite_invocation.py": _REASON_CANON_STRING,
            # Multiple sites, all the same underlying shape: (1) `_abs_path` resolves
            # a path via native `os.path.join`/`Path.resolve()` (correct, host-native
            # separator throughout) and ONLY THEN forward-slash-normalizes the
            # resulting STRING for a later substring/containment comparison (deny-
            # message text, `/coordinator-sessions/`-prefix check) -- the resolve
            # itself never depends on the literal; (2) `os.path.basename(tok.replace(
            # "\\", "/"))` extracts a basename from an already-tokenized argv0/env
            # command token that may carry either separator; (3) `out_gd.strip()
            # .replace("\\", "/")` canonicalizes `git rev-parse --git-dir` subprocess
            # TEXT OUTPUT before a `/worktrees/` substring check. None of these three
            # shapes builds a path this process hands to `open()`/`os.path` I/O using
            # the literal as the separator -- each is string canonicalization for
            # comparison, matching `_REASON_CANON_STRING`'s own listed "resolved-path
            # fallback" and command-token categories.
            "coordinator_core/bash_guards/dispatch_checks.py": _REASON_CANON_STRING,
            # C7-ops (2026-08-13): 17 files under coordinator_core/ops/, all
            # string-canonicalization-for-comparison shapes -- never a hardcoded
            # separator building a path this process hands to open()/os.path/Path
            # I/O. `records_query.py` is the one exception (regex escape emission,
            # REASON_NOT_A_PATH); `verify_ps51_clean.py` is another (emits
            # Windows path syntax for a powershell.exe subprocess's own literal
            # syntax, REASON_PS51_TARGET_SYNTAX). `cruft_sweep.py`'s
            # `_has_git_boundary`/`_has_negative_spec_component` sites (a live
            # os.walk() Path's own components) WERE ported to
            # `PurePath(path).parts`; its third site, `_is_pruned_child`, was
            # tried the same way and reverted -- its own test
            # (`test_is_pruned_child_recognizes_both_separators`) feeds a
            # Windows-form string on any host and asserts it still matches,
            # which `PurePath` (host-native) cannot do -- so it stays the
            # explicit both-separator check, exempted below as REASON_CANON_STRING.
            "coordinator_core/ops/bootstrap_orchestrate.py": _REASON_CANON_STRING,
            "coordinator_core/ops/bootstrap_repo.py": _REASON_CANON_STRING,
            "coordinator_core/ops/central_run_due.py": _REASON_CANON_STRING,
            "coordinator_core/ops/ceremony/commit_exec_bit.py": _REASON_CANON_STRING,
            "coordinator_core/ops/ceremony/renderers.py": _REASON_CANON_STRING,
            "coordinator_core/ops/check_windows_ssh_binary.py": _REASON_CANON_STRING,
            # Multiple sites: a dedup-key normalizer for Claude Code's own
            # activity-log path strings (may be native-Windows or POSIX form,
            # authored by a different process/platform than this one) plus a
            # decode helper reconstructing a Windows drive-letter path STRING
            # from this repo's own encoded project-dir naming scheme -- neither
            # ever reaches open()/os.path I/O with the literal as the separator.
            "coordinator_core/ops/discover_working_repos.py": _REASON_CANON_STRING,
            "coordinator_core/ops/emit/normalizers.py": _REASON_CANON_STRING,
            "coordinator_core/ops/list_reverse_drift_cmds.py": _REASON_CANON_STRING,
            # `git ls-files <rel_dir>` pathspecs are POSIX-form by git's own
            # convention regardless of host; `os.path.relpath(...).replace(...)`
            # canonicalizes the argument for that consumer, and the sibling
            # `rel` sites canonicalize for error-text/result-dict comparison --
            # neither is a hardcoded-separator os.path/Path I/O build.
            "coordinator_core/ops/normalize_claimed_frontmatter.py": _REASON_CANON_STRING,
            "coordinator_core/ops/records_query.py": _REASON_NOT_A_PATH,
            "coordinator_core/ops/setup_rag_decision.py": _REASON_CANON_STRING,
            "coordinator_core/ops/test_discover_working_repos.py": _REASON_CANON_STRING,
            "coordinator_core/ops/tests/test_handoff_author_fork.py": _REASON_CANON_STRING,
            "coordinator_core/ops/verify_ps51_clean.py": _REASON_PS51_TARGET_SYNTAX,
            "coordinator_core/ops/cruft_sweep.py": _REASON_CANON_STRING,
            # C7-install (2026-08-13): coordinator_core/install/ cluster, 7 of
            # 10 files. All path_separator hits here canonicalize a caller-
            # or env-sourced path STRING (shape (a)) or scan/mock Windows
            # path SYNTAX (shape (b)) -- none build a hardcoded-separator
            # path this process hands to open()/os.path/Path I/O.
            "coordinator_core/install/check_install_singularity.py": _REASON_CANON_STRING,
            "coordinator_core/install/gen_settings_hooks.py": _REASON_GEN_SETTINGS_HOOKS_C7,
            "coordinator_core/install/scaffold_structure.py": _REASON_CANON_STRING,
            "coordinator_core/install/test_check_install_singularity.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator_core/install/test_gen_settings_hooks.py": _REASON_GEN_SETTINGS_HOOKS_TEST_C7,
            "coordinator_core/install/tests/test_scaffold_structure.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            "coordinator_core/install/tests/test_substrate_migrate.py": _REASON_WIN_SYNTAX_AS_FIXTURE,
            # C7-coordinator (2026-08-13): encode_project_path() replicates Claude
            # Code's own directory-naming encoding (every '/', '\\', ':', '.'
            # becomes '-') so a Windows-authored coordinator_root produces the
            # same transcript-directory name as a forward-slash one -- see the
            # function's own Review comment naming the 2026-07-28 2566-row
            # silent-attribution-loss incident this guards against. os.sep is
            # the wrong fix: Claude Code's encoding treats BOTH separators as
            # literal characters to collapse, regardless of the host running
            # this code.
            "coordinator/bin/derive-file-attribution.py": _REASON_CANON_STRING,
            # `_sh_path()`'s own docstring: normalizes a path for interpolation
            # into a POSIX-`sh` git-hook body -- the hook file is always run
            # through `sh` (git's hook-execution model) regardless of which
            # host authored the Python-side value, so the destination's
            # separator convention is fixed at forward-slash independent of
            # os.sep. Paths used for actual Python filesystem operations in
            # this same module are explicitly called out as NOT going through
            # this helper.
            "coordinator/bin/lib/git_hook_install.py": _REASON_CANON_STRING,
            # canonicalize_wiki_target()'s own docstring: collapses the several
            # equivalent input shapes different routers emit for "the same"
            # wiki target -- including a backslash-separated variant -- to one
            # canonical string so two callers' YAML/dedupe keys agree. Not a
            # filesystem path; os.sep is orthogonal to a router-emitted
            # semantic identifier.
            "coordinator/bin/lib/target_wiki_canon.py": _REASON_CANON_STRING,
            # `_normalise_path()`'s own docstring: normalizes for CROSS-PLATFORM
            # matching between a CLI-supplied query path and attribution rows
            # that may have been recorded on a different host/OS than the one
            # running this query. os.sep only recognizes the querying host's
            # own convention, missing a row recorded with the other one.
            "coordinator/bin/query-file-attribution.py": _REASON_CANON_STRING,
            # Every `.replace("\\\\", "/")` site normalizes a repo-relative
            # path STRING before a comparison/regex match (`.repomapignore`
            # patterns, UE Build.cs module-prefix matching, include-graph
            # in-degree lookups) -- never a live `open()`/`os.path` join. The
            # source of these strings is a MIX within a single run: `git
            # ls-files` output (always forward-slash, even on Windows) when
            # git is available, falling back to `Path.relative_to()` (native
            # separator) when it is not (see `get_git_tracked_files`'s
            # caller). os.sep would only recognize the current host's own
            # convention and miss the git-ls-files-sourced form even on the
            # SAME Windows host where both code paths coexist.
            "coordinator/bin/repomap/generate-repomap.py": _REASON_CANON_STRING,
            # Two sites: (1) `os.path.relpath(memo_path, tmpdir)` normalized
            # before matching a hardcoded, forward-slash-spelled glob regex
            # (`cross-repo/inbox/[0-9]*.md`) -- the regex's syntax is fixed
            # regardless of host, so os.sep (which would only emit the SAME
            # host's native separator the relpath is already in) cannot make
            # the comparison target match; (2) `git diff-tree --name-only`
            # output normalized before set-membership comparison, same idiom
            # as `diff_scoped_tests.py`'s existing REASON_CANON_STRING entry.
            "coordinator/bin/test_cross_repo_memo_roundtrip.py": _REASON_CANON_STRING,
            # `_testpaths_forward_slash()`'s own docstring: normalizes
            # `[tool.pytest.ini_options] testpaths` values read from
            # `pyproject.toml` -- a recorded config string, not a live
            # filesystem path this process opens -- to forward-slash form for
            # comparison against the tracked-file set.
            "coordinator/bin/tests/test_testpaths_location_guard.py": _REASON_CANON_STRING,
            # `_xplatform_patterns()`/`_win_home_patterns()`/`_drive_letter_patterns()`
            # build REGEX PATTERNS matching Windows-drive-letter home-path
            # SYNTAX (a drive letter, colon, backslash-separated Users path,
            # JSON-escaped and raw forms) appearing as literal text in a
            # scanned publish-log/artifact -- mirroring
            # publish.sh's own bash pattern derivation (module docstring).
            # These backslashes are the syntax being recognized, not a
            # filesystem operation; os.sep would corrupt the very Windows
            # shape this audit exists to detect.
            "coordinator/lib/percolate/phase4_audit.py": _REASON_WIN_SYNTAX_IN_TEXT,
            # `_resolve_machine_local_bin()`: normalizes the `MACHINE_LOCAL_BIN`
            # env var -- which may be set with either separator convention
            # depending on the invoking shell/platform -- purely to run a
            # `'/../' in normalized` traversal-syntax SECURITY check. The
            # unmodified `env_bin` (not the normalized value) is what gets
            # returned and later used for real filesystem operations; os.sep
            # would miss a `..\\` traversal attempt spelled with the other
            # separator on the same host.
            "coordinator/lib/percolate/resolve_target.py": _REASON_CANON_STRING,
            # `str(live_dir).replace("\\\\", "/")` compares this test's own
            # live_dir against text written inside `direct_url.json` by the
            # venv-install tool under test (uv/pip) -- that tool's own output
            # convention is not controlled by this test's host, so os.sep
            # (this host's convention) cannot substitute for tolerating
            # either form the tool may have written.
            "coordinator/tests/test_refresh_plugin_live_install_integration.py": _REASON_CANON_STRING,
            # Normalizes `proc.stdout` -- the scaffold CLI's own printed path,
            # a cross-process TEXT payload whose separator convention is set
            # by that subprocess, not by this test's os.sep -- before joining
            # it onto `repo` to locate the scaffolded file.
            "coordinator/tests/test_review_findings_scaffold.py": _REASON_CANON_STRING,
            # C7-rest-a (2026-08-13): 11 path_separator carve-outs, mostly
            # canonicalizing a recorded/frontmatter/tool-input path string
            # for comparison, never a live filesystem resolve.
            #
            # candidates.py is the one entry here that is a carve-out ON TOP OF a
            # port, not instead of one. Its `_parent_dir()` previously called
            # os.path.dirname() on the RAW frontmatter string before normalizing,
            # so on a POSIX host a Windows-authored value had no separator for
            # dirname to split and silently returned an empty parent dir. That
            # was a live cross-platform bug and it was ported (normalize first,
            # then rsplit) with a 5-case regression test. The residual
            # `.replace("\\", "/")` the detector still trips on IS the fix: the
            # value is a recorded frontmatter field of unknown authoring
            # platform, so os.sep -- this host's separator -- is the wrong tool
            # by construction. Porting further would reintroduce the defect.
            "coordinator_core/clustering/candidates.py": _REASON_CANON_STRING,
            "coordinator_core/cartography/churn.py": _REASON_CANON_STRING,
            "coordinator_core/cartography/file_index.py": _REASON_CANON_STRING,
            "coordinator_core/frontmatter/schema_validate.py": _REASON_CANON_STRING,
            "coordinator_core/hooks/nudge_em_code_dispatch.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/hooks/nudge_unauthorized_handoff.py": _REASON_TOOL_INPUT_PATH,
            "coordinator_core/orientation/regenerate_cache.py": _REASON_CANON_STRING,
            "coordinator_core/plugin_health/bin_inventory_gate.py": _REASON_CANON_STRING,
            # 3 functions, 5 `.replace("\\", "/")` sites (lines 343-344,
            # 480-481, 606) -- each immediately wrapped in Path(...) and
            # resolved (.is_dir()) against THIS host's filesystem. The
            # config value (plugin manifest live_path/source_path) may have
            # been authored on either platform.
            "coordinator_core/plugin_health/drift.py": _REASON_CONFIG_PATH_FOREIGN_AUTHORED,
            "coordinator_core/plugin_health/relocation_ledger.py": _REASON_CANON_STRING,
            "coordinator_core/reconcile/commit_reality.py": _REASON_CANON_STRING,
            "coordinator_core/reconcile/gate_eval.py": _REASON_CANON_STRING,
            # C7-rest-b2 (2026-08-13): 11 path_separator carve-outs (the
            # 12th finding in this cluster is posix_mode_bits, see below).
            "coordinator_core/resolution/test_facade.py": _REASON_CANON_STRING,
            "coordinator_core/review_assemble/residue.py": _REASON_CANON_STRING,
            "coordinator_core/snippet_sync/tests/test_verify.py": _REASON_CANON_STRING,
            "coordinator_core/test_baton_assemble.py": _REASON_CANON_STRING,
            "coordinator_core/test_trusted_root_guard.py": _REASON_CANON_STRING,
            "coordinator_core/tests/test_hooks_bookkeeping.py": _REASON_CANON_STRING,
            "coordinator_core/tests/test_no_bash_dependency.py": _REASON_WIN_SYNTAX_IN_TEXT,
            "coordinator_core/tests/test_win_portability.py": _REASON_CANON_STRING,
            "coordinator_core/text/query_record_display.py": _REASON_CANON_STRING,
            "coordinator_core/trusted_root_guard.py": _REASON_CANON_STRING,
            "coordinator_core/win_portability.py": _REASON_CANON_STRING,
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
            "coordinator/bin/refresh-plugin-live-install.py": _REASON_CHMOD_RMTREE_UNBLOCK,
            # C7-install (2026-08-13): shape (c), the one place an
            # EXEMPTIONS entry is the FIRST choice -- a pytest.mark.skipif
            # decorator gap _is_windows_guarded() cannot see. See reason
            # text for the two flagged sites and their inline-guarded
            # siblings that already suppress correctly.
            "coordinator_core/install/test_install_one_overwrite_policy.py": _REASON_INSTALL_ONE_EXEC_BIT_SKIPIF_GAP,
            # C7-ops (2026-08-13): git installs/execs these `#!/bin/sh` hooks by
            # shebang dispatch, which needs the exec bit on POSIX only.
            "coordinator_core/ops/install_meta_repo_precommit_hook.py": _REASON_CHMOD_HOOK_POSIX_EXEC,
            "coordinator_core/ops/install_publish_repo_precommit_hook.py": _REASON_CHMOD_HOOK_POSIX_EXEC,
            # `test_write_preserves_executable_permission_bit`'s own comment
            # already states the Windows-degrade rationale (mode always 0o666
            # there); same shape as test_verify_templates_bin_sync.py above.
            "coordinator_core/ops/test_backfill_deliverable_spine.py": _REASON_CHMOD_MODE_PRESERVATION,
            # Asserts the exec bit survives install_claude_doe_wrapper.py's
            # `cp -p`-equivalent copy (module docstring: preserves the doc
            # oracle's `cp -p` semantics verbatim) -- a POSIX-only mode-bit
            # value assertion, same shape as REASON_CHMOD_MODE_PRESERVATION's
            # existing entries.
            "coordinator_core/ops/test_install_claude_doe_wrapper.py": _REASON_CHMOD_MODE_PRESERVATION,
            # fake_bin stub-gate scripts are invoked by the `#!/bin/sh`-executed
            # hook via its own PATH lookup by filename (direct exec, not
            # `sh <script>`), so they need the exec bit on the POSIX-only
            # invocation this test file already depends on end to end -- same
            # shape as test_install_meta_repo_precommit_hook_install_all.py.
            "coordinator_core/ops/test_install_meta_repo_precommit_hook.py": _REASON_CHMOD_EXEC_FOR_SH,
            "coordinator_core/ops/test_install_post_sync_hooks.py": _REASON_CHMOD_EXEC_FOR_SH,
            "coordinator_core/ops/test_verify_coverage.py": _REASON_CHMOD_DIR_GAP,
            # C7-rest-a (2026-08-13): sets an arbitrary starting mode via
            # os.chmod only to assert atomic_write preserves it -- a
            # before/after relative invariant, not an absolute POSIX octal.
            "coordinator_core/hooks/test_platform_localize.py": _REASON_CHMOD_RELATIVE_INVARIANT,
            # C7-rest-b2 (2026-08-13): twelve os.chmod(script, 0o755) sites
            # setting the exec bit on a generated #!/usr/bin/env bash
            # drop-in script that install_health_run.main() invokes via
            # resolve_by_shebang + subprocess.call -- POSIX-only invocation.
            "coordinator_core/tests/test_install_health_run.py": _REASON_CHMOD_EXEC_FOR_SH,
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
_FROZEN_FIXTURE_BYTES_REASON = (
    "FROZEN FIXTURE BYTES -- the third EXEMPTIONS admission test's "
    "'frozen fixture bytes' bucket (permanent artifact-shape "
    "irreducibility, 2026-08-13 eng-director ruling), granted here via "
    "EXEMPT_PREFIXES per the drain plan's own citation of this exact "
    "bucket as EXEMPT_PREFIXES' textbook case. Covers two golden/input "
    "fixture-tree pairs: coordinator_core/percolate/tests/fixtures/"
    "doe-golden/ + doe-input-tree/, and coordinator_core/publish/tests/"
    "fixtures/golden-tree/sub/ + input-tree/sub/. Mechanism: each pair's "
    "golden-tree diff test asserts an input tree, once transformed, "
    "equals its golden tree byte-for-byte -- the .sh fixture's shebang "
    "and mode bits are themselves part of the bytes under assertion, so "
    "editing them would falsify the comparison rather than fix anything. "
    "Permanence: not contingent on any peer artifact's shape -- a "
    "fixture's job IS to be a fixed specimen. Windows leg: none, and none "
    "is needed -- nothing under either prefix is executed, imported, or "
    "installed by this repo or by anything this repo installs, so the "
    "portability question has no referent here. Live caller: the sibling "
    "percolate/publish diff-test suites read these trees as fixed input "
    "data; they never execute the .sh files inside them."
)

EXEMPT_PREFIXES: Dict[str, Dict[str, str]] = {
    REPO_CLAUDE_KLABAUTER: {
        "dist/mirror-native/": _MIRROR_NATIVE_DESTINATION_REASON,
        "coordinator_core/percolate/tests/fixtures/doe-golden/": _FROZEN_FIXTURE_BYTES_REASON,
        "coordinator_core/percolate/tests/fixtures/doe-input-tree/": _FROZEN_FIXTURE_BYTES_REASON,
        "coordinator_core/publish/tests/fixtures/golden-tree/sub/": _FROZEN_FIXTURE_BYTES_REASON,
        "coordinator_core/publish/tests/fixtures/input-tree/sub/": _FROZEN_FIXTURE_BYTES_REASON,
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

# Home-rooted candidate shape ONLY -- the `<drive-letter>:[\\/]` and UNC
# (`\\\\server\share`) arms this class used to carry were DROPPED 2026-08-13
# (module docstring, `unresolved_cross_path` entry, Q2.1 ruling): a live
# re-measurement found the drive-letter arm alone responsible for 127/178
# findings, almost entirely this repo's own correctly-written Windows-
# portability code (`pyresolve.py`'s interpreter-discovery ladder,
# `break_glass.py`'s Windows temp-path syntax recognition) being flagged
# for existing. A candidate match here is necessary but not sufficient --
# `_current_machine_home()` below narrows it further to THIS machine's own
# operator-home literal before it becomes a finding, so a synthetic
# `/Users/alice` fixture string never fires.
_CROSS_PATH_PATTERNS = [
    re.compile(r"^/Users/[^/]+/"),
    re.compile(r"^/home/[^/]+/"),
]


def _current_machine_home() -> str:
    """The current machine's operator-home path, for narrowing a candidate
    home-rooted literal (matched by `_CROSS_PATH_PATTERNS` above) down to
    ONE that actually leaks THIS machine's identity, vs. synthetic fixture
    text like `/Users/alice` that merely has the right shape.

    Deliberately mirrors -- does not reinvent -- the discrimination
    `coordinator/bin/check-machine-path-leak.py`'s `main()` already draws
    for `working-repos.yaml` ("current-machine-home-rooted only"):
    `current_home = os.environ.get("HOME") or os.path.expanduser("~")`,
    which honors `$HOME` where set and falls back to
    `os.path.expanduser("~")` (which itself honors `%USERPROFILE%` on
    stock Windows) rather than silently no-oping the check off-POSIX. That
    script is a hyphenated `coordinator/bin/` CLI, invoked as a subprocess
    everywhere else in this fleet (including its own test suite) rather
    than imported -- importing a dash-named script via `importlib.util`
    here would be a second, more fragile discrimination mechanism, not a
    reuse of the first, so the two-line computation is mirrored instead
    with this citation rather than duplicated silently."""
    return os.environ.get("HOME") or os.path.expanduser("~")

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


def _block_always_exits(body: "List[ast.stmt]") -> bool:
    """True if `body`'s LAST statement unconditionally leaves the enclosing
    function/process -- a bare `return`/`return <value>`, a `raise`, or a
    call to `sys.exit(...)`/`os._exit(...)`. Consulted only to prove that
    the statements FOLLOWING a bare, `else:`-less `if <windows-test>:
    <body>` guard clause are unreachable when the test is true, so this is
    deliberately conservative: any other trailing shape (a loop, a bare
    `if` with just one arm, a plain expression) returns False rather than
    guess. A false positive here would silently hide a genuinely
    POSIX-assuming site behind a guard that doesn't actually exit."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        call = last.value
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in ("sys", "os")
            and call.func.attr in ("exit", "_exit")
        ):
            return True
    return False


def _bare_exit_guard_precedes(cur: ast.AST, parent: ast.AST) -> bool:
    """True if `cur` is a statement in one of `parent`'s statement-list
    fields (`body`, `orelse`, `finalbody`, ...) and an EARLIER sibling in
    that same list is a bare `if <windows-test>: <exits>` clause with no
    `else:` whose test is true only on Windows and whose body always exits
    (`_block_always_exits`) -- the shape `if sys.platform.startswith("win"):
    return p` followed by the guarded code as the next sibling statement.
    Since the guard clause has no `else:`, `cur` is only reached once the
    test evaluated False, i.e. on a non-Windows host."""
    for _field, value in ast.iter_fields(parent):
        if not isinstance(value, list) or not value:
            continue
        idx = next((i for i, v in enumerate(value) if v is cur), None)
        if idx is None:
            continue
        for sib in value[:idx]:
            if (
                isinstance(sib, ast.If)
                and not sib.orelse
                and _branch_windows_status(sib.test) == "windows"
                and _block_always_exits(sib.body)
            ):
                return True
    return False


def _is_windows_guarded(node: ast.AST, parents: Dict[int, ast.AST]) -> bool:
    """Walks up from `node` through enclosing `If` statements (elif chains
    are nested `If`s inside `orelse`, handled by the same loop), enclosing
    `and`-chains (`os.name != "nt" and ... and os.access(...)` -- the real
    shape found in `retire-claude-bin.py`, where the guard and the guarded
    call are short-circuit operands of the SAME boolean expression, not a
    separate nested `If`), AND a preceding, `else:`-less bare early-return
    guard clause in the same statement list (`if sys.platform.startswith
    ("win"): return p` followed by the guarded code as the next sibling
    statement, recognized via `_bare_exit_guard_precedes` /
    `_block_always_exits`). Returns True the moment ANY enclosing guard
    structurally means Windows never reaches this node -- one sufficient
    guard is enough, matching how authors actually write this code.

    A guard whose TEST is not one of `_branch_windows_status`'s recognized
    forms (`os.name`/`sys.platform`/`platform.system()` compared against a
    Windows-identifying constant, or `sys.platform.startswith("win"...)`) is
    NOT recognized here even in the bare-early-return shape -- e.g. a
    project-local wrapper like `if not _host_is_windows(): return` is
    invisible to this function, deliberately: this module reuses its one
    existing notion of "a windows test" rather than inventing a second, and
    a false negative (treating unguarded code as guarded) is worse than
    under-recognizing a genuinely guarded site, which merely routes its
    author to EXEMPTIONS."""
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
        if _bare_exit_guard_precedes(cur, parent):
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
    current_home = _current_machine_home()

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
            if (
                any(p.search(s) for p in _CROSS_PATH_PATTERNS)
                and current_home
                and s.startswith(current_home)
            ):
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
# `posix_mode_bits` specifically, LEAD with the known escape for the
# residual gap in `_is_windows_guarded()` -- a windows-test it does not
# recognize as one (a project-local wrapper function, not
# os.name/sys.platform/platform.system()) -- so an author who hits that
# rare false positive sees "this is a known gap with a sanctioned exit"
# first, not a bare violation notice that reads as "the guard is broken,
# route around it." A bare early-return guard clause with no `else:` IS
# now recognized (2026-08-13), as is the recognized-test-inside-it shape;
# see `_is_windows_guarded()`'s own docstring for exactly what is and
# isn't seen.
_GUARD_AWARE_CLASSES = ("path_separator", "posix_mode_bits")

_EARLY_RETURN_ESCAPE_HINT = (
    "  A bare early-return guard clause with no `else:` (e.g. `if "
    "sys.platform.startswith('win'): return p` followed by the guarded "
    "code as the next sibling statement) IS recognized by "
    "`_is_windows_guarded()` (2026-08-13) -- if this fires on code shaped "
    "like that, the guard's TEST is the more likely culprit: only "
    "os.name/sys.platform/platform.system() compared against a "
    "Windows-identifying constant, or sys.platform.startswith('win'...), "
    "are recognized as a windows-test, not a project-local wrapper "
    "function (see that function's own docstring). If the test really is "
    "one of those recognized forms and this still fires, that is a "
    "genuine detector gap -- add a named EXEMPTIONS entry describing it. "
    "Do not add an `else:` purely to satisfy the detector -- that was "
    "never the remedy and still isn't."
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
