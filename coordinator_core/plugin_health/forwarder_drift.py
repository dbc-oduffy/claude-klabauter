r"""
coordinator_core.plugin_health.forwarder_drift — staleness probe for
generated agent-helper bin/ forwarders. WARN-only for the UNCITED population;
FAIL-LOUD for the CITED-but-missing population (see CITED-VS-UNCITED SPLIT
below, amended 2026-08-12) — two populations, two severities, not one
uniform contract.

Purpose: install-substrate's `_derive_agent_helper_target_map`
(coordinator_core/install/substrate.py) computes the installed-forwarder name
set from a LIVE scan of claude-klabauter's own `coordinator/bin/` directory, but only at
INSTALL TIME. Nothing re-runs that scan between install runs — a CLI landing
in (or removed from) `coordinator/bin/` produces no signal until the next full
install. The 2026-07-23 incident: `gen-settings-hooks` and
`run-platform-localize` shipped in `coordinator/bin/` with no installed
forwarder in either bin/ location, because no install had run since they
landed — the first ceremony to invoke either name rc=127'd with a generic
"command not found", not a message naming the actual cause (a stale install).

Why this lives here, not `coordinator/bin/install-health/`: every
`_NATIVE_LEGS` install-health leg (`coordinator_core.ops.install_health_run`)
runs ONLY during an install pass, immediately after `_install_bin_resolvers`
has just regenerated every forwarder from a fresh scan — derived and
installed are trivially equal at that exact moment BY CONSTRUCTION. The drift
this module detects can only appear BETWEEN install runs, when
`coordinator/bin/` changes underneath an already-completed install. That is
exactly the cadence this module's sibling, `plugin_health.drift`, already
serves — a standalone, non-blocking probe surfaced daily at /workday-start
Step 1.10 Addon Health, not an install-time gate. install-health-run.py's own
contract is also FAIL-LOUD-on-aggregate ("install is incomplete" — see that
module's docstring); a leg that must never fail the run it is registered in
would be a contradiction of that contract, not an instance of it. This module
follows the drift.py home and its contract instead for the population that
argument is actually about: install-health legs where derived==installed BY
CONSTRUCTION. That argument is scoped to that population — it predates, and
does not extend to, the CITED-but-missing population added 2026-07-31/
sharpened 2026-08-12 (see CITED-VS-UNCITED SPLIT below). A cited CLI with no
forwarder is not expected transient state between installs; it is a live
prompt surface instructing an EM to invoke something that cannot resolve.
This module never raises regardless of population — but it is no longer
uniformly WARN-only; see the Contract block and CITED-VS-UNCITED SPLIT below
for the two-population split this docstring now records.

Contract:
  - Never raises, in either population. Any resolution failure (claude-klabauter root
    unresolvable) is a clean SKIP (`skipped=True`, `ok=True`), never a fail
    — that persona (an OSS consumer with no claude-klabauter checkout, or any repo/
    machine where repos.claude_klabauter isn't registered) legitimately has
    nothing to compare. AC6: the CITED-population fail-loud path below fires
    ONLY on a positively-computed non-empty `cited_missing` set — never on
    "could not determine".
  - UNCITED population: WARN, never FAIL — `main()`/the CLI stay exit 0.
    Uncited drift is exactly "an install ran before the current
    coordinator/bin/ contents", ordinary transient lag, not an install
    defect; making this population fail-loud would be the permanent-noise
    failure this module's own history (the retired `~/.claude/bin` mirror
    leg, see below) already learned the hard way.
  - CITED-but-missing population: FAIL-LOUD — `check-forwarder-drift.py`
    exits non-zero (see CITED-VS-UNCITED SPLIT below and that CLI's own
    `Exit codes:` block) when `ForwarderDriftResult.cited_missing` is
    non-empty. This is a distinct population from the install-health legs
    the WARN-only argument above was written for: nothing regenerates a
    forwarder for a CLI a prompt surface currently cites, so this is not
    "derived==installed by construction" drift — it is a live, currently-
    broken invocation path.
  - Reports BOTH directions for the settings-home `bin/` (authoritative —
    gets the PATH nudge, the only location `_install_bin_resolvers` still
    writes): DERIVED-BUT-NOT-INSTALLED (a stale install — the 2026-07-23
    incident shape: a CLI landed, no forwarder was ever written) and
    INSTALLED-BUT-NOT-DERIVED (an orphaned forwarder for a CLI that no
    longer exists in `coordinator/bin/` — the same rc=127 failure mode, in
    the opposite direction).
  - The retired `~/.claude/bin` compat mirror is checked in the
    INSTALLED-BUT-NOT-DERIVED direction ONLY (stale leftover files are a
    real, reconcilable fact). Its DERIVED-BUT-NOT-INSTALLED direction is
    deliberately suppressed (`_diff_one_location(..., check_missing=False)`):
    that mirror's producer was retired 2026-07-24 (0fc30697) and nothing
    populates it any more, so checking that direction there reports EVERY
    derived CLI as "missing" on EVERY run, forever — permanent, unfixable
    noise (71 names on the machine this fix was authored on) that buried
    the one real, actionable gap this module exists to catch (the missing
    `review-assemble` forwarder in settings-home/bin, stranded since
    2026-07-26 — the CLI's own claiming session died before shipping its
    forwarder chunk, per `docs/plans/2026-07-26-review-skill-computed-residue.md`
    — and undetected until 2026-07-27). A "reports drift" module whose own
    secondary leg drifts unconditionally on every invocation is not
    reporting drift, it is reporting existence — the distinction this fix
    restores.
  - Every drifted name is named explicitly. A report that says "drift
    detected" without naming what drifted is nearly worthless at this scale
    (~300 forwarders; the incident was 2 names out of ~300).

CITED-VS-UNCITED SPLIT (2026-07-31 sharpening; exit contract split
2026-08-12) — a missing settings-home/bin forwarder is reported (and now
gated) in one of two registers depending on whether any DoE-claude prompt
surface actually tells an agent to invoke it:
  - UNCITED: nothing in the corpus names this CLI. Today's framing is
    correct as-is — this is exactly "an install ran before the current
    coordinator/bin/ contents", ordinary transient lag, remedied by a
    re-install, never an escalation. Stays WARN-only, exit 0.
  - CITED: a prompt surface (a skill, command, agent, snippet, or pipeline
    doc) names this exact settings-home/bin/<name> invocation. That
    invocation FAILS — a shell prints `bash: <path>: No such file or
    directory` on stderr before rc=127 (not silent output-wise) — but
    nothing GATES on that exit code (bash treats 127 as an ordinary nonzero
    exit; no branch on that code means the caller proceeds as if the step
    ran). The defect is that nothing gates, not that nothing prints. For a
    step a skill calls a *blocking gate* (`check-auto-memory-drained`,
    cited by `/workstream-complete`) or cites at a ceremony boundary
    (`safe-commit-offer`, cited by `/handoff` and `/quick-wrap`), the
    missing forwarder is not transient lag an operator can shrug off until
    the next install — it is a gate silently not running, indistinguishable
    in its own output from a gate that ran and passed. This population is
    both reported with a distinctly louder line naming every citing surface
    AND (2026-08-12) exposed as the machine-readable `cited_missing` field
    (see `ForwarderDriftResult`) that `check-forwarder-drift.py` gates on —
    non-empty `cited_missing` fails the CLI's exit code; the UNCITED
    population never does.

  Names are drawn from DoE-claude's `coordinator/{agents,skills,commands,
  snippets,pipelines}/**/*.md` (excluding `tests/`/`fixtures/` segments,
  same five trees and same exemption as
  `coordinator/hooks/scripts/_prompt_surface_citations.py::PROMPT_SURFACE_DIRS`
  in that repo). Re-derived here, not imported: claude-klabauter must not depend on
  DoE-claude's test tree (a sibling repo that may not even be checked out
  on this machine — see below), so `_ENTRYPOINT_RE` and the five-tree scan
  are a deliberate, small, standalone copy of the shape
  `coordinator/tests/test_prompt_surfaces_cite_installed_entrypoints.py`
  (DoE-claude, committed 2026-07-31, b507bf1a2076) already validated against
  the corpus — including that module's own documented non-greedy-`.*?`
  subtlety: `[^}]*` cannot cross the inner `}` of the
  `${CLAUDE_HOME:-$HOME}` fallback spelling and silently matches nothing on
  that shape, so `_ENTRYPOINT_RE` uses `.*?` for the same reason that
  module does.

  DoE-claude may legitimately be absent on this machine (OSS consumer, CI,
  a machine with no `repos.doe_claude` registered) — resolved via the same
  registry seam `coordinator_core.ops.coordinator_doe_root.
  coordinator_doe_root()` uses elsewhere (never a hardcoded path), and any
  resolution failure degrades this module to TODAY's undifferentiated
  advisory wording, never a hard failure — same never-raises contract as
  the rest of this module.

  EXIT CODE (amended 2026-08-12; verified this session — no consumer keys on
  this CLI's exit code programmatically, so this flip is additive, not a
  breaking contract change) — `main()` now exits 1 when `cited_missing` is
  non-empty, 0 in every other case (uncited-only drift, orphan-only drift,
  skip, clean). `main()`'s caller, `/workday-start` Step 1.10 Addon Health,
  consumes this probe's stdout advisorily and does not branch on its exit
  code today, so this flip changes nothing for that caller's current
  behavior while giving any future or external consumer (a script, a
  different ceremony step) a real signal to key on instead of parsing
  stdout. The UNCITED population's exit code is unchanged at 0 — see
  Contract block above.

EXTENSION axis (added 2026-08-30): the NAME axis asks whether an installed
forwarder exists for a derived CLI name; the CONTENT axis (in the CLI
trampoline, `coordinator/bin/check-forwarder-drift.py`) asks whether installed
bytes match source. Neither asks the third question: does the EXTENSION a
citing prompt surface tells an EM to type match what the install actually
wrote? The 2026-08-27 door cutover made `<cli>.exe` the spelling for every
settings-home CLI on Windows, retaining `.cmd` for only six pre-engine
bootstrap resolvers (`claude-home`, `coordinator-settings-home`,
`example-game-repo-control`, `machine-local`, `platform-localize`,
`resolve-coordinator-clone`). Both existing axes stayed green through it:
presence and byte-equality were still true for every installed file; only the
extension named in doctrine was wrong. The failing invocation reports
`command-not-found` naming the CLI, which reads as "this CLI does not exist"
rather than "this CLI exists under a different spelling". This axis is scanned
only via the Windows-specific Shape W citation shape
(`$env:COORDINATOR_SETTINGS_HOME\bin\<name>`, see `_ENTRYPOINT_W_RE`) — the
POSIX `${COORDINATOR_SETTINGS_HOME:-...}/bin/<name>` shape `_ENTRYPOINT_RE`
matches has no notion of a Windows extension to get wrong. HOST GATE: the
oracle for this axis is THIS machine's own settings-home/bin — a POSIX
install writes extensionless launchers and no `.exe` at all, so a POSIX host
would false-fire on every Shape W citation purely from how POSIX installs;
Shape W is Windows-only by construction (its own rung 0 fires only on a
PowerShell host), so only a Windows install can adjudicate it, and this axis
degrades to a clean skip on any other host (see `_check_extension_axis`).
Unlike the CONTENT axis (always advisory, never gates — see that function's
docstring in the CLI trampoline), a non-empty `extension_mismatch` DOES gate
`check-forwarder-drift.py`'s exit code, the same CITED-population fail-loud
posture as `cited_missing` above: a live prompt surface telling an EM to type
a path that cannot resolve is not expected transient lag.

Forwarder identification is CONTENT-based (a fixed marker line every
`_write_agent_forwarder`-generated file carries), not name-based:
`_derive_agent_helper_target_map`'s exclusion/stem-dedup rules (reserved
names, `.cmd`/`.ps1` twins, data suffixes, stem-dedup) are substrate.py's own
evolving install policy — re-deriving which INSTALLED files belong to the
forwarder family here would duplicate that policy and silently drift out of
sync with it whenever it changes. Matching on the marker line is decoupled
from that policy entirely; it only needs `_write_agent_forwarder`'s template
to keep emitting the same first-line comment.

Self-registration: importing this module calls register_op("plugin_health.
forwarder_drift", ...), mirroring plugin_health.drift's own pattern.

Resolution ladder (in order, first hit wins — see individual resolvers):
  1. Caller-supplied path (explicit override, e.g. from a test or a future
     CLI flag).
  2. Env var: COORDINATOR_SETTINGS_HOME / CLAUDE_HOME / HOME (via
     coordinator_core._settings_home.settings_home(), the same bootstrap-safe
     resolver install-substrate itself uses) for the settings-home bin/; the
     same env pair, unmodified, for the ~/.claude/bin compat mirror.
  3. CLAUDE_KLABAUTER_ROOT env var / <settings-home>/machine-local/.claude-klabauter-live-root pointer
     / machine-local registry (`repos.claude_klabauter`) — via
     coordinator_core.engine_root.coordinator_engine_root(), for the
     coordinator/bin/ scan root.
  4. Unresolvable at any required rung -> clean skip, never a fail.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-claude-klabauter-pickup-assemble-heads-up.md
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.ipc import register_op
from coordinator_core.engine_root import coordinator_engine_root
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_PROG = "forwarder-drift"

# The literal first-line-of-body comment `_write_agent_forwarder`
# (coordinator_core/install/substrate.py) emits into every generated
# forwarder — see module docstring for why content is matched instead of
# re-deriving substrate.py's own name-exclusion policy here.
_FORWARDER_MARKER = "# coordinator-claude bin forwarder for "

# Minimum remedy (AC5): `python3 -m coordinator_core.install.substrate`
# requires CLAUDE_PLUGIN_ROOT pointed at the DoE-claude clone's coordinator/
# dir and fails loud without it (no self-derivation) — that is the whole
# forwarder-regeneration step, without the maximalist orchestrator's broader
# scope (settings.json hook blocks, shell shims, the claude-doe wrapper, the
# venv), which is over-broad advice for forwarder-only drift on a machine
# running dozens of concurrent sessions. /coordinator:install
# (coordinator/scripts/install-maximalist.py) remains the guided superset,
# not the only named option. Note for whoever reads this: `--check-only`
# stops at the FIRST divergence and never reaches the forwarder step, so it
# cannot preview this drift.
_REMEDY = (
    "run `python3 -m coordinator_core.install.substrate` (requires CLAUDE_PLUGIN_ROOT set to the "
    "DoE-claude coordinator/ dir) as the minimum remedy, or /coordinator:install "
    "(coordinator/scripts/install-maximalist.py) as the guided superset, to regenerate forwarders"
)

# Corrected 2026-08-14 (percolate-push memo — 10 real forwarders, including
# percolate-push, sat undetected because the prior wording ("expected
# transient state between installs") assumes something re-installs on a
# cadence; a machine running the engine straight out of claude-klabauter's own live
# working tree (this fleet's dev-box default — see CLAUDE.md's Load norm)
# never does, so on THAT machine the same drift is not transient at all, it
# is permanent until an operator runs the remedy by hand. The severity split
# itself is unchanged (still WARN/exit-0 for UNCITED, still fail-loud for
# CITED-but-missing) — only the rationale text, which this line states
# plainly once rather than asserting a cadence this fleet does not have.
_ADVISORY_LINE = (
    f"[info] {_PROG}: UNCITED drift here is advisory-only, exit 0 always. On a machine that "
    "re-installs periodically this is ordinary transient lag between installs; on one running the "
    "engine straight out of a live sibling working tree (no periodic install ever closes it) the "
    "same drift persists until someone runs the remedy by hand — nothing else surfaces it. "
    "CITED-but-missing drift is different: it fails the CLI's exit code (see module docstring's "
    f"CITED-VS-UNCITED SPLIT). Remedy in both cases: {_REMEDY}."
)

# The ~/.claude/bin compat mirror's producer was retired 2026-07-24 (0fc30697,
# owns-zero Gate 6) — this leg's label says so explicitly so a reader does not
# read its counts as live drift against an actively-maintained location.
_COMPAT_BIN_LABEL = "~/.claude/bin (legacy-mirror residue — compat producer retired 2026-07-24)"

# Deliberate standalone copy of DoE-claude's
# `coordinator/tests/test_prompt_surfaces_cite_installed_entrypoints.py::_ENTRYPOINT_RE`
# (committed 2026-07-31, b507bf1a2076) — see module docstring's CITED-VS-UNCITED
# SPLIT section for why this is copied, not imported. Non-greedy `.*?` so this
# crosses the inner `}` of the `${CLAUDE_HOME:-$HOME}` fallback form; `[^}]*`
# silently fails to match that shape (verified against the corpus by the
# module this was copied from).
_ENTRYPOINT_RE = re.compile(r"\$\{COORDINATOR_SETTINGS_HOME:-.*?\}/bin/([A-Za-z0-9_.-]+)")

# Shape W (rung 0 of DoE-claude's `coordinator/snippets/resolve-coordinator-bin.md`
# precedence ladder) — PowerShell has no `${VAR:-default}` fallback syntax, so a
# Windows-authored citation spells the settings-home bin/ lookup as
# `$env:COORDINATOR_SETTINGS_HOME\bin\<name>` instead, a shape `_ENTRYPOINT_RE`
# above is structurally incapable of matching (no `${...}`, no POSIX `/bin/`
# separator guaranteed). Accepts either `\` or `/` as the separator since a doc
# author may write either. See module docstring's EXTENSION axis section for
# why this citation population, not the NAME axis' bare-name population,
# is what makes the third axis possible: this citation retains the extension
# the caller was actually told to type.
_ENTRYPOINT_W_RE = re.compile(r"\$env:COORDINATOR_SETTINGS_HOME[\\/]bin[\\/]([A-Za-z0-9_.-]+)")

# Extensions the extension axis treats as a plausible, strippable suffix when
# splitting a cited spelling into (base, extension) — see `_check_extension_axis`.
# Several CLI names legitimately contain dots (data-suffix style names) and must
# not have an arbitrary trailing dot-segment treated as an extension.
_PLAUSIBLE_EXTENSIONS = (".exe", ".cmd", ".ps1", ".py", ".sh")

# Same five trees, same tests/fixtures exemption, as DoE-claude's
# `coordinator/hooks/scripts/_prompt_surface_citations.py::PROMPT_SURFACE_DIRS`
# — re-derived rather than imported for the same reason as `_ENTRYPOINT_RE`.
_DOE_PROMPT_SURFACE_SUBDIRS = ("agents", "skills", "commands", "snippets", "pipelines")
_DOE_EXEMPT_PATH_SEGMENTS = frozenset({"tests", "fixtures"})


@dataclass
class ForwarderDriftResult:
    """ok=True -> no drift (or a clean skip); ok=False -> drift detected in at
    least one checked location. `skipped=True` means no comparison was
    possible at all (claude-klabauter root unresolvable) — distinct from a clean
    zero-drift result, though both carry ok=True. `lines` are [ok]/[warn]/
    [skip] stdout-shaped messages in emission order; `stderr_lines` is always
    empty today (kept for shape-parity with plugin_health.drift.DriftResult
    and to leave room for a future diagnostic-only channel).

    `cited_missing` (new 2026-08-12, AC1) is the machine-readable form of the
    CITED-VS-UNCITED SPLIT (see module docstring): `{settings-home-bin CLI
    name: [citing-site, ...]}` for every missing settings-home/bin forwarder
    that is ALSO named by a live DoE-claude prompt-surface invocation. Empty
    dict in every other case — uncited-only drift, orphan-only drift, skip,
    or clean. This is the field `check-forwarder-drift.py` gates on
    (non-empty -> non-zero exit); everything else about this result stays
    advisory. Distinct from the rendered `[warn] ... cited at: ...` lines in
    `lines`, which remain the human-readable form of the same fact."""

    ok: bool
    skipped: bool = False
    lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)
    cited_missing: Dict[str, List[str]] = field(default_factory=dict)

    # `extension_mismatch` (new — EXTENSION axis) is the third axis'
    # machine-readable output: `{cited spelling (extension included): [citing
    # site, ...]}` for every Shape W citation whose exact spelling is NOT
    # installed at settings-home/bin, while a sibling with the same base name
    # (a different extension) IS installed there. Same CITED population and
    # same fail-loud posture as `cited_missing` above — a live prompt surface
    # instructing an EM to invoke a path that cannot resolve, just caught on
    # the extension rather than the presence axis. Empty in every other case:
    # a clean/skip result, a non-Windows host (this axis' oracle is THIS
    # machine's settings-home/bin — see `_check_extension_axis`), an
    # unresolvable DoE-claude root, or a cited base name with no installed
    # sibling at all (that gap is the NAME axis' `cited_missing` population,
    # not this one's).
    extension_mismatch: Dict[str, List[str]] = field(default_factory=dict)


def _resolve_agent_bin() -> Optional[Path]:
    try:
        engine_root = coordinator_engine_root()
    except RuntimeError:
        return None
    candidate = Path(engine_root) / "coordinator" / "bin"
    return candidate if candidate.is_dir() else None


def _resolve_settings_bin() -> Path:
    return settings_home() / "bin"


def _resolve_compat_bin() -> Path:
    return home_dir() / ".claude" / "bin"


def _resolve_doe_root() -> Optional[Path]:
    """DoE-claude's repo root, via the same registry seam
    `coordinator_core.ops.coordinator_doe_root` uses elsewhere — never a
    hardcoded path. `coordinator_doe_root()` itself never raises (folds every
    rung's failure to None, see that module's docstring); this wrapper adds
    only the is-a-directory gate. Returns None on any unresolvable/absent
    state (OSS consumer, CI, no `repos.doe_claude` registered) — the caller
    degrades to the undifferentiated advisory wording in that case, never a
    hard failure (see module docstring's CITED-VS-UNCITED SPLIT)."""
    root = coordinator_doe_root()
    if not root:
        return None
    candidate = Path(root)
    return candidate if candidate.is_dir() else None


def _cited_entrypoint_sites(doe_root: Path) -> Dict[str, List[str]]:
    """{settings-home-bin CLI name: [ "<rel-path-from-doe-root>:<line>", ... ]}
    for every settings-home `bin/<name>` citation — the
    `${COORDINATOR_SETTINGS_HOME:-...}` expansion form — across
    DoE-claude's five prompt-surface trees (see module docstring). Best-effort
    per file — an unreadable file is skipped, never a hard failure."""
    sites: Dict[str, List[str]] = {}
    for subdir in _DOE_PROMPT_SURFACE_SUBDIRS:
        root_dir = doe_root / "coordinator" / subdir
        if not root_dir.is_dir():
            continue
        for path in sorted(root_dir.rglob("*.md")):
            try:
                rel = path.relative_to(doe_root)
            except ValueError:
                continue
            if _DOE_EXEMPT_PATH_SEGMENTS.intersection(rel.parts[:-1]):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.split("\n"), start=1):
                for match in _ENTRYPOINT_RE.finditer(line):
                    sites.setdefault(match.group(1), []).append(f"{rel.as_posix()}:{line_no}")
    return sites


def _cited_shape_w_sites(doe_root: Path) -> Dict[str, List[str]]:
    """{cited spelling verbatim, extension included (e.g. "app-session.cmd"):
    [ "<rel-path-from-doe-root>:<line>", ... ]} for every Shape W citation
    (`$env:COORDINATOR_SETTINGS_HOME\\bin\\<name>` / `/bin/<name>`) across the
    same five DoE-claude prompt-surface trees `_cited_entrypoint_sites` scans,
    with the same tests/fixtures exemption and the same best-effort per-file
    `OSError` skip.

    Keyed on the CITED SPELLING, extension included — unlike
    `_cited_entrypoint_sites`'s bare-name key, the extension is the whole
    subject of the EXTENSION axis this function feeds (see module docstring).

    Trailing `.` characters are stripped from the capture: markdown prose
    routinely ends a sentence immediately after a backtick-quoted path (e.g.
    `` `...\\bin\\workweek-complete-brief.exe`. `` — the regex has no notion
    of sentence boundaries, so the caller strips the artifact instead."""
    sites: Dict[str, List[str]] = {}
    for subdir in _DOE_PROMPT_SURFACE_SUBDIRS:
        root_dir = doe_root / "coordinator" / subdir
        if not root_dir.is_dir():
            continue
        for path in sorted(root_dir.rglob("*.md")):
            try:
                rel = path.relative_to(doe_root)
            except ValueError:
                continue
            if _DOE_EXEMPT_PATH_SEGMENTS.intersection(rel.parts[:-1]):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.split("\n"), start=1):
                for match in _ENTRYPOINT_W_RE.finditer(line):
                    spelling = match.group(1).rstrip(".")
                    if not spelling:
                        continue
                    sites.setdefault(spelling, []).append(f"{rel.as_posix()}:{line_no}")
    return sites


def _split_cited_base(spelling: str) -> str:
    """Strip a plausible extension (see `_PLAUSIBLE_EXTENSIONS`) from a cited
    spelling to get its base name — used to find installed siblings under a
    different extension. A trailing dot-segment that is NOT one of the
    plausible extensions is left alone: several CLI names legitimately
    contain dots and must not be truncated on an arbitrary suffix."""
    for ext in _PLAUSIBLE_EXTENSIONS:
        if spelling.endswith(ext):
            return spelling[: -len(ext)]
    return spelling


def _is_windows_host() -> bool:
    """Host predicate for the EXTENSION axis, factored out as a seam so a
    test can select the POSIX branch without patching `os.name` globally —
    pathlib reads `os.name` to choose its concrete Path class, so patching
    it module-wide makes every subsequent Path operation on a Windows host
    raise UnsupportedOperation."""
    return os.name == "nt"


def _check_extension_axis(
    doe_root: Optional[Path], settings_bin: Path
) -> "tuple[List[str], Dict[str, List[str]]]":
    """EXTENSION axis (see module docstring) — does the extension a Shape W
    citation tells an EM to type match what is actually installed at
    settings-home/bin, for the exact base name it names?

    HOST GATE, first and non-negotiable: this axis' oracle is THIS machine's
    own settings-home/bin — a POSIX install writes extensionless launchers
    (no `.exe` at all), so every Shape W citation would false-fire on a POSIX
    host purely because of how POSIX installs, not because of any real
    mismatch. Shape W is Windows-only by construction (rung 0 of the
    precedence ladder fires only on a PowerShell host — see
    `resolve-coordinator-bin.md`), so only a Windows install can adjudicate
    it; a non-Windows host (or a caller unable to resolve DoE-claude, or a
    `settings_bin` that isn't an installed directory) degrades to a clean
    skip, never a fail — same never-raises contract as the rest of this
    module.

    Verdict per cited spelling, against the set of file names actually
    present in `settings_bin`:
      - cited spelling present verbatim -> OK (this is what keeps the six
        legitimate pre-engine `.cmd` bootstrap resolvers green; the install
        itself is the oracle, nothing here hardcodes that list).
      - cited spelling absent, and no sibling of the same base name (base,
        base.exe, base.cmd) is installed either -> not this axis' business;
        that gap belongs to the NAME axis' `cited_missing` population (or is
        prose that never names a real invocation at all, e.g. a wildcard
        prefix) — skipped silently.
      - cited spelling absent, but a sibling of the same base name IS
        installed -> EXTENSION MISMATCH, recorded.
    """
    if not _is_windows_host():
        return (
            [f"[skip] {_PROG} (extension axis): non-Windows host — Shape W is a Windows-only citation shape, nothing to check here"],
            {},
        )
    if doe_root is None:
        return (
            [f"[skip] {_PROG} (extension axis): DoE-claude root unresolvable — nothing to compare"],
            {},
        )
    if not settings_bin.is_dir():
        return (
            [f"[skip] {_PROG} (extension axis): '{settings_bin}' does not exist — no install at this location yet"],
            {},
        )

    installed_names = {entry.name for entry in settings_bin.iterdir() if entry.is_file()}
    cited_sites = _cited_shape_w_sites(doe_root)

    mismatch: Dict[str, List[str]] = {}
    for spelling in sorted(cited_sites):
        if spelling in installed_names:
            continue
        base = _split_cited_base(spelling)
        siblings_present = any(
            candidate in installed_names for candidate in (base, f"{base}.exe", f"{base}.cmd")
        )
        if not siblings_present:
            continue
        mismatch[spelling] = sorted(cited_sites[spelling])

    if not mismatch:
        return ([f"[ok] {_PROG} (extension axis): {len(cited_sites)} Shape W citation(s) checked, 0 extension mismatches"], {})

    lines: List[str] = []
    for spelling in sorted(mismatch):
        base = _split_cited_base(spelling)
        # `.exe` first, deliberately: on Windows the extensionless python
        # forwarder sits beside the launcher, so a bare-`base` remedy would
        # name the forwarder — which Shape W does not invoke (rung 0 names
        # the `.exe` for every settings-home CLI, no per-CLI exception).
        installed_spelling = next(
            (candidate for candidate in (f"{base}.exe", f"{base}.cmd", base) if candidate in installed_names),
            base,
        )
        sites_str = ", ".join(mismatch[spelling])
        lines.append(
            f"[warn] {_PROG} (extension axis): '{spelling}' is cited but settings-home/bin installs "
            f"'{installed_spelling}' instead — rewrite the citation to '{installed_spelling}'. cited at: {sites_str}"
        )
    return lines, mismatch


def _installed_forwarder_names(bin_dir: Path) -> Set[str]:
    """Content-based scan (see module docstring) — every regular, non-.cmd/
    .ps1 file in `bin_dir` whose head carries `_FORWARDER_MARKER`.

    The read is deliberately bounded to the first 512 BYTES (binary, matched
    against the marker encoded as bytes), not a `Path.read_text()` slice: this
    directory holds hundreds of multi-hundred-KB native launcher images (375
    `.exe` forwarders averaging 173.5 KB on the machine this bound was
    measured on), and `read_text()[:512]` still decodes the ENTIRE file before
    discarding everything past the first 512 characters — ~63 MB read and
    UTF-8-decoded per run, ~580ms of the ~720ms this function used to cost,
    for information contained entirely in the first 62 bytes. Safety of the
    512-byte window: `_FORWARDER_MARKER` is pure ASCII, and across all 390
    marker-carrying files in the live install its maximum END offset is byte
    62 — a 512-byte window has ~8x headroom, and because the marker is ASCII
    the byte-window-vs-character-window distinction cannot bite. A future
    edit back to `read_text()` is the regression this comment exists to
    prevent."""
    if not bin_dir.is_dir():
        return set()
    marker = _FORWARDER_MARKER.encode("utf-8")
    names: Set[str] = set()
    for entry in bin_dir.iterdir():
        if entry.is_dir() or entry.suffix in (".cmd", ".ps1"):
            continue
        try:
            with open(entry, "rb") as fh:
                head = fh.read(512)
        except OSError:
            continue
        if marker in head:
            names.add(entry.name)
    return names


def _derive_names(agent_bin: Path) -> Set[str]:
    # Lazy import: substrate.py is a live install-surface module (currently
    # mid-refactor in this repo at the time this probe was written) — imported
    # at call time, the same way _install_bin_resolvers' own call site does,
    # rather than at module load, so an in-flight edit there can never break
    # THIS module's import merely by being imported alongside it.
    from coordinator_core.install.substrate import _derive_agent_helper_target_map

    return set(_derive_agent_helper_target_map(agent_bin))


def _diff_one_location(
    label: str,
    bin_dir: Path,
    derived: Set[str],
    *,
    check_missing: bool = True,
    cited_sites: Optional[Dict[str, List[str]]] = None,
) -> ForwarderDriftResult:
    """``check_missing=False`` suppresses the derived-but-not-installed
    direction for a location that no producer writes to any more (the
    retired ``~/.claude/bin`` compat mirror — see ``_COMPAT_BIN_LABEL``).
    Without this, that leg reports EVERY derived CLI as "missing" on EVERY
    run forever (its producer was retired 2026-07-24, so nothing ever
    populates it again), which is permanent, unfixable noise — 71 names on
    this repo's own machine at the time of this fix — that buried the one
    real, actionable settings-home/bin gap (the missing `review-assemble`
    forwarder, 2026-07-27) inside a wall of unactionable text every single
    `/workday-start` run. The orphan direction (installed-but-not-derived)
    stays live for the compat mirror: a stale leftover file there is a real,
    reconcilable fact even though the mirror gets no new writes.

    ``cited_sites`` (see module docstring's CITED-VS-UNCITED SPLIT) is the
    ``{name: [site, ...]}`` map from `_cited_entrypoint_sites`, or None when
    DoE-claude was unresolvable on this machine — in which case every missing
    name renders in the plain, undifferentiated register (today's wording),
    since there is nothing to differentiate against."""
    installed = _installed_forwarder_names(bin_dir)
    missing = sorted(derived - installed) if check_missing else []  # stale install (incident shape)
    orphaned = sorted(installed - derived)  # deleted CLI, forwarder left behind

    if not missing and not orphaned:
        if check_missing:
            ok_line = f"[ok] {_PROG} ({label}): {len(derived)} derived == {len(installed)} installed"
        else:
            # No "derived == installed" claim here — derived and installed
            # counts are expected to diverge at a retired, orphan-only-checked
            # location (nothing populates it any more), so asserting equality
            # would be as misleading as the suppressed missing-check itself.
            ok_line = f"[ok] {_PROG} ({label}): {len(installed)} installed, 0 orphaned (missing-check not applicable)"
        return ForwarderDriftResult(ok=True, lines=[ok_line])

    lines: List[str] = []
    cited_missing_sites: Dict[str, List[str]] = {}
    if missing:
        cited_missing = [n for n in missing if cited_sites and n in cited_sites] if cited_sites is not None else []
        uncited_missing = [n for n in missing if n not in cited_missing]
        if uncited_missing:
            lines.append(
                f"[warn] {_PROG} ({label}): {len(uncited_missing)} CLI(s) in coordinator/bin/ have no "
                f"installed forwarder — {_REMEDY}: {', '.join(uncited_missing)}"
            )
        if cited_missing:
            for name in cited_missing:
                sites = sorted(cited_sites[name])
                cited_missing_sites[name] = sites
                sites_str = ", ".join(sites)
                lines.append(
                    f"[warn] {_PROG} ({label}): CLI(s) in coordinator/bin/ have no installed forwarder "
                    "AND are cited by a live prompt-surface invocation — this is NOT expected transient "
                    "install lag: the invocation fails and NOTHING GATES ON IT, so any gate or step "
                    f"depending on it is being SKIPPED, not passed. {_REMEDY}. bin/{name} cited at: {sites_str}"
                )
    if orphaned:
        lines.append(
            f"[warn] {_PROG} ({label}): {len(orphaned)} installed forwarder(s) have no matching CLI "
            f"in coordinator/bin/ (orphaned) — {_REMEDY} to reconcile: {', '.join(orphaned)}"
        )
    return ForwarderDriftResult(ok=False, lines=lines, cited_missing=cited_missing_sites)


def check_forwarder_drift(
    *,
    settings_bin: Optional[Path] = None,
    compat_bin: Optional[Path] = None,
    agent_bin: Optional[Path] = None,
    doe_root: Optional[Path] = None,
) -> ForwarderDriftResult:
    """Core scan — used by both `main()` (CLI) and the `plugin_health.
    forwarder_drift` op. Every param is an explicit override for tests; a
    caller that omits one gets the real resolution ladder (see module
    docstring). ``doe_root`` (new 2026-07-31) overrides `_resolve_doe_root`
    for the CITED-VS-UNCITED SPLIT — omitted, it resolves DoE-claude via the
    registry seam; unresolvable there degrades to the undifferentiated
    wording (see `_diff_one_location`), never a hard failure."""
    resolved_agent_bin = agent_bin if agent_bin is not None else _resolve_agent_bin()
    if resolved_agent_bin is None:
        return ForwarderDriftResult(
            ok=True,
            skipped=True,
            lines=[
                _ADVISORY_LINE,
                f"[skip] {_PROG}: claude-klabauter root (or its coordinator/bin/) unresolvable — "
                "nothing to compare (expected on an OSS consumer install with no claude-klabauter "
                "checkout, or a machine with no repos.claude_klabauter registered)",
            ],
        )

    derived = _derive_names(resolved_agent_bin)

    # CITED-VS-UNCITED SPLIT (see module docstring) — best-effort, only for
    # settings-home/bin (the compat mirror's missing direction is already
    # unconditionally suppressed, so it has no "missing" list to split).
    resolved_doe_root = doe_root if doe_root is not None else _resolve_doe_root()
    cited_sites = _cited_entrypoint_sites(resolved_doe_root) if resolved_doe_root is not None else None

    # `check_missing` is False only for the retired compat mirror — see
    # `_diff_one_location`'s docstring for why the derived-but-not-installed
    # direction is permanent, unactionable noise there.
    resolved_settings_bin = settings_bin if settings_bin is not None else _resolve_settings_bin()
    locations = [
        ("settings-home/bin", resolved_settings_bin, True, cited_sites),
        (_COMPAT_BIN_LABEL, compat_bin if compat_bin is not None else _resolve_compat_bin(), False, None),
    ]

    lines: List[str] = [_ADVISORY_LINE]
    stderr_lines: List[str] = []
    any_drift = False
    cited_missing: Dict[str, List[str]] = {}
    for label, bin_dir, check_missing, location_cited_sites in locations:
        if not bin_dir.is_dir():
            lines.append(f"[skip] {_PROG} ({label}): '{bin_dir}' does not exist — no install at this location yet")
            continue
        result = _diff_one_location(
            label, bin_dir, derived, check_missing=check_missing, cited_sites=location_cited_sites
        )
        lines.extend(result.lines)
        stderr_lines.extend(result.stderr_lines)
        cited_missing.update(result.cited_missing)
        if not result.ok:
            any_drift = True

    extension_lines, extension_mismatch = _check_extension_axis(resolved_doe_root, resolved_settings_bin)
    lines.extend(extension_lines)
    if extension_mismatch:
        any_drift = True

    return ForwarderDriftResult(
        ok=not any_drift,
        lines=lines,
        stderr_lines=stderr_lines,
        cited_missing=cited_missing,
        extension_mismatch=extension_mismatch,
    )


def main(argv: List[str]) -> int:
    del argv  # no flags accepted today
    result = check_forwarder_drift()
    for line in result.lines:
        print(line)
    for line in result.stderr_lines:
        print(line, file=sys.stderr)
    # Two CITED populations gate the exit code (see module docstring's
    # CITED-VS-UNCITED SPLIT and the EXTENSION axis section): a non-empty
    # `cited_missing` (NAME axis) or `extension_mismatch` (EXTENSION axis) is
    # a live prompt surface instructing an EM to invoke something that cannot
    # resolve — that is not the advisory, "expected transient lag" population
    # the WARN-only contract was written for, so it gates. Every other
    # outcome (uncited-only drift, orphan-only drift, skip, clean) stays
    # exit 0.
    return 1 if (result.cited_missing or result.extension_mismatch) else 0


@register_op("plugin_health.forwarder_drift")
async def _plugin_health_forwarder_drift(params: dict, repo_root=None) -> dict:
    """JSON-RPC "plugin_health.forwarder_drift" handler.

    Params: none today. `repo_root` is accepted for handler-signature parity
    but IGNORED — this op inspects the operator's OWN settings-home/claude-klabauter
    install state, not the caller's repo (same "none"-scope class as
    plugin_health.drift / engine.drift).

    Returns {"ok": bool, "skipped": bool, "lines": [...], "stderr_lines": [...],
    "cited_missing": {name: [site, ...]}, "extension_mismatch": {cited
    spelling: [site, ...]}}. `ok=False` means drift was found — the caller
    decides what to do with that; this op itself never raises and never
    signals a hard failure. `cited_missing` is the AC1 machine-readable field
    (see `ForwarderDriftResult.cited_missing`'s docstring); non-empty only for
    the cited-but-missing (NAME axis) population, never for uncited/orphan
    drift, a skip, or a clean result. `extension_mismatch` is the EXTENSION
    axis' equivalent (see `ForwarderDriftResult.extension_mismatch`'s
    docstring) — non-empty only when a Windows Shape W citation names an
    extension that is not what settings-home/bin actually installed for that
    base name; always empty on a non-Windows host.
    """
    del params
    result = check_forwarder_drift()
    return {
        "ok": result.ok,
        "skipped": result.skipped,
        "lines": result.lines,
        "stderr_lines": result.stderr_lines,
        "cited_missing": result.cited_missing,
        "extension_mismatch": result.extension_mismatch,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
