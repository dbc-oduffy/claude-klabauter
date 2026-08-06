"""
coordinator_core.plugin_health.forwarder_drift — WARN-only staleness probe for
generated agent-helper bin/ forwarders.

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
follows the drift.py home and its contract instead: WARN, never FAIL, never
raise.

Contract:
  - Never raises. Any resolution failure (claude-klabauter root unresolvable) is a
    clean SKIP (`skipped=True`, `ok=True`), never a fail — that persona (an
    OSS consumer with no claude-klabauter checkout, or any repo/machine where
    repos.claude_klabauter isn't registered) legitimately has nothing to
    compare.
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

CITED-VS-UNCITED SPLIT (2026-07-31 sharpening) — a missing settings-home/bin
forwarder is reported in one of two registers depending on whether any
Example-doctrine-repo prompt surface actually tells an agent to invoke it:
  - UNCITED: nothing in the corpus names this CLI. Today's framing is
    correct as-is — this is exactly "an install ran before the current
    coordinator/bin/ contents", ordinary transient lag, remedied by a
    re-install, never an escalation.
  - CITED: a prompt surface (a skill, command, agent, snippet, or pipeline
    doc) names this exact settings-home/bin/<name> invocation. That
    invocation rc=127's SILENTLY (bash treats 127 as an ordinary nonzero
    exit; no branch on that code means the caller proceeds as if the step
    ran). For a step a skill calls a *blocking gate*
    (`check-auto-memory-drained`, cited by `/workstream-complete`) or cites
    at a ceremony boundary (`safe-commit-offer`, cited by `/handoff` and
    `/quick-wrap`), the missing forwarder is not transient lag an operator
    can shrug off until the next install — it is a gate silently not
    running, indistinguishable in its own output from a gate that ran and
    passed. This case gets a distinctly louder line naming every citing
    surface, so the fact does not stay buried in "advisory, exit 0 always"
    framing that was written for the OTHER case.

  Names are drawn from example-doctrine-repo's `coordinator/{agents,skills,commands,
  snippets,pipelines}/**/*.md` (excluding `tests/`/`fixtures/` segments,
  same five trees and same exemption as
  `coordinator/hooks/scripts/_prompt_surface_citations.py::PROMPT_SURFACE_DIRS`
  in that repo). Re-derived here, not imported: claude-klabauter must not depend on
  example-doctrine-repo's test tree (a sibling repo that may not even be checked out
  on this machine — see below), so `_ENTRYPOINT_RE` and the five-tree scan
  are a deliberate, small, standalone copy of the shape
  `coordinator/tests/test_prompt_surfaces_cite_installed_entrypoints.py`
  (example-doctrine-repo, committed 2026-07-31, b507bf1a2076) already validated against
  the corpus — including that module's own documented non-greedy-`.*?`
  subtlety: `[^}]*` cannot cross the inner `}` of the
  `${CLAUDE_HOME:-$HOME}` fallback spelling and silently matches nothing on
  that shape, so `_ENTRYPOINT_RE` uses `.*?` for the same reason that
  module does.

  example-doctrine-repo may legitimately be absent on this machine (OSS consumer, CI,
  a machine with no `repos.example_doctrine_repo` registered) — resolved via the same
  registry seam `coordinator_core.ops.coordinator_doe_root.
  coordinator_doe_root()` uses elsewhere (never a hardcoded path), and any
  resolution failure degrades this module to TODAY's undifferentiated
  advisory wording, never a hard failure — same never-raises contract as
  the rest of this module.

  EXIT CODE — deliberately UNCHANGED at "exit 0 always" even for the CITED
  case. `main()`'s caller (`/workday-start` Step 1.10 Addon Health)
  consumes this probe as advisory; making the cited case fail would need a
  matching contract change on that consuming step, which lives in
  example-doctrine-repo and is out of this dispatch's scope to edit. A louder in-band
  line that a human (or a skill parsing this probe's own output) actually
  reads is the right lever here, not a silent contract break on a step that
  currently has no branch for a non-zero exit from this probe. If a
  cited-CLI 127 ever needs to hard-block a ceremony, that is a coordinated
  two-repo change (this module's exit contract + the consuming step's
  handling of it), not a unilateral flip here.

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
  3. CLAUDE_KLABAUTER_ROOT env var / <settings-home>/machine-local/.claude-klabauter-root pointer
     / machine-local registry (`repos.claude_klabauter`) — via
     coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root(), for the
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
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_PROG = "forwarder-drift"

# The literal first-line-of-body comment `_write_agent_forwarder`
# (coordinator_core/install/substrate.py) emits into every generated
# forwarder — see module docstring for why content is matched instead of
# re-deriving substrate.py's own name-exclusion policy here.
_FORWARDER_MARKER = "# coordinator-claude bin forwarder for "

_REMEDY = "re-run /coordinator:install (coordinator/scripts/install-maximalist.py) to regenerate forwarders"

_ADVISORY_LINE = (
    f"[info] {_PROG}: advisory-only, exit 0 always — drift here means an install ran BEFORE the "
    "current coordinator/bin/ contents (expected transient state between installs, not an install "
    f"defect). The remedy is a re-install ({_REMEDY}), never an escalation."
)

# The ~/.claude/bin compat mirror's producer was retired 2026-07-24 (0fc30697,
# owns-zero Gate 6) — this leg's label says so explicitly so a reader does not
# read its counts as live drift against an actively-maintained location.
_COMPAT_BIN_LABEL = "~/.claude/bin (legacy-mirror residue — compat producer retired 2026-07-24)"

# Deliberate standalone copy of example-doctrine-repo's
# `coordinator/tests/test_prompt_surfaces_cite_installed_entrypoints.py::_ENTRYPOINT_RE`
# (committed 2026-07-31, b507bf1a2076) — see module docstring's CITED-VS-UNCITED
# SPLIT section for why this is copied, not imported. Non-greedy `.*?` so this
# crosses the inner `}` of the `${CLAUDE_HOME:-$HOME}` fallback form; `[^}]*`
# silently fails to match that shape (verified against the corpus by the
# module this was copied from).
_ENTRYPOINT_RE = re.compile(r"\$\{COORDINATOR_SETTINGS_HOME:-.*?\}/bin/([A-Za-z0-9_.-]+)")

# Same five trees, same tests/fixtures exemption, as example-doctrine-repo's
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
    and to leave room for a future diagnostic-only channel)."""

    ok: bool
    skipped: bool = False
    lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)


def _resolve_agent_bin() -> Optional[Path]:
    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError:
        return None
    candidate = Path(claude_klabauter_root) / "coordinator" / "bin"
    return candidate if candidate.is_dir() else None


def _resolve_settings_bin() -> Path:
    return settings_home() / "bin"


def _resolve_compat_bin() -> Path:
    return home_dir() / ".claude" / "bin"


def _resolve_doe_root() -> Optional[Path]:
    """example-doctrine-repo's repo root, via the same registry seam
    `coordinator_core.ops.coordinator_doe_root` uses elsewhere — never a
    hardcoded path. `coordinator_doe_root()` itself never raises (folds every
    rung's failure to None, see that module's docstring); this wrapper adds
    only the is-a-directory gate. Returns None on any unresolvable/absent
    state (OSS consumer, CI, no `repos.example_doctrine_repo` registered) — the caller
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
    example-doctrine-repo's five prompt-surface trees (see module docstring). Best-effort
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


def _installed_forwarder_names(bin_dir: Path) -> Set[str]:
    """Content-based scan (see module docstring) — every regular, non-.cmd/
    .ps1 file in `bin_dir` whose head carries `_FORWARDER_MARKER`."""
    if not bin_dir.is_dir():
        return set()
    names: Set[str] = set()
    for entry in bin_dir.iterdir():
        if entry.is_dir() or entry.suffix in (".cmd", ".ps1"):
            continue
        try:
            head = entry.read_text(encoding="utf-8", errors="ignore")[:512]
        except OSError:
            continue
        if _FORWARDER_MARKER in head:
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
    example-doctrine-repo was unresolvable on this machine — in which case every missing
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
                sites = ", ".join(sorted(cited_sites[name]))
                lines.append(
                    f"[warn] {_PROG} ({label}): CLI(s) in coordinator/bin/ have no installed forwarder "
                    "AND are cited by a live prompt-surface invocation — this is NOT expected transient "
                    "install lag: every invocation exits 127 SILENTLY (no output), so any gate or step "
                    f"depending on it is being SKIPPED, not passed. {_REMEDY}. bin/{name} cited at: {sites}"
                )
    if orphaned:
        lines.append(
            f"[warn] {_PROG} ({label}): {len(orphaned)} installed forwarder(s) have no matching CLI "
            f"in coordinator/bin/ (orphaned) — {_REMEDY} to reconcile: {', '.join(orphaned)}"
        )
    return ForwarderDriftResult(ok=False, lines=lines)


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
    for the CITED-VS-UNCITED SPLIT — omitted, it resolves example-doctrine-repo via the
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
    locations = [
        ("settings-home/bin", settings_bin if settings_bin is not None else _resolve_settings_bin(), True, cited_sites),
        (_COMPAT_BIN_LABEL, compat_bin if compat_bin is not None else _resolve_compat_bin(), False, None),
    ]

    lines: List[str] = [_ADVISORY_LINE]
    stderr_lines: List[str] = []
    any_drift = False
    for label, bin_dir, check_missing, location_cited_sites in locations:
        if not bin_dir.is_dir():
            lines.append(f"[skip] {_PROG} ({label}): '{bin_dir}' does not exist — no install at this location yet")
            continue
        result = _diff_one_location(
            label, bin_dir, derived, check_missing=check_missing, cited_sites=location_cited_sites
        )
        lines.extend(result.lines)
        stderr_lines.extend(result.stderr_lines)
        if not result.ok:
            any_drift = True

    return ForwarderDriftResult(ok=not any_drift, lines=lines, stderr_lines=stderr_lines)


def main(argv: List[str]) -> int:
    del argv  # no flags accepted today
    result = check_forwarder_drift()
    for line in result.lines:
        print(line)
    for line in result.stderr_lines:
        print(line, file=sys.stderr)
    # Advisory only — never gates (see module docstring: WARN, not FAIL).
    return 0


@register_op("plugin_health.forwarder_drift")
async def _plugin_health_forwarder_drift(params: dict, repo_root=None) -> dict:
    """JSON-RPC "plugin_health.forwarder_drift" handler.

    Params: none today. `repo_root` is accepted for handler-signature parity
    but IGNORED — this op inspects the operator's OWN settings-home/claude-klabauter
    install state, not the caller's repo (same "none"-scope class as
    plugin_health.drift / engine.drift).

    Returns {"ok": bool, "skipped": bool, "lines": [...], "stderr_lines": [...]}.
    `ok=False` means drift was found — the caller decides what to do with
    that; this op itself never raises and never signals a hard failure.
    """
    del params
    result = check_forwarder_drift()
    return {
        "ok": result.ok,
        "skipped": result.skipped,
        "lines": result.lines,
        "stderr_lines": result.stderr_lines,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
