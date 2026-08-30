"""
coordinator_core.plugin_health.fleet_reachability — FAIL-LOUD delete-safety
gate for claude-klabauter `coordinator/bin/` oracles the DoE-claude fleet still
consumes.

Purpose: catch the `c79e66cd` regression class at delete-time — claude-klabauter
deleted `lint-frontmatter.js` while DoE-claude skills still cited
`bin/lint-frontmatter` — instead of relying on an operator to remember to
grep the fleet before every delete. This is the north-star "discharge, don't
enumerate" answer (DoE-claude coordinator/docs/wiki/invisible-doctrine.md):
a durable artifact that fails a test at delete-time, not a directive an
operator is trusted to run by hand.

Sibling of, NOT an extension of, `plugin_health.forwarder_drift`.
`forwarder_drift` checks a different axis entirely — installed-state drift
(derived-vs-installed forwarder names on THIS machine, WARN-only, surfaced at
consumer `/workday-start`) — and reads clean immediately after any fresh
install, including one that just reinstalled after a delete. That is exactly
the blind spot this module exists to close: `forwarder_drift` cannot see
whether a deleted oracle was still fleet-demanded, only whether the CURRENT
`coordinator/bin/` and the CURRENT install agree with each other. This module
reuses `forwarder_drift`'s enumeration primitive (importing
`coordinator_core.install.substrate._derive_agent_helper_target_map` as the
live single source of truth for "which oracles exist"), never its
WARN-only/installed-state assertion.

Axes compared:
  - Supply: claude-klabauter oracle names, derived from a LIVE scan of claude-klabauter's
    fleet-invocable oracle surface via `_derive_agent_helper_target_map` —
    never `git ls-files` (a stale index or an uncommitted delete would both
    lie). That surface is a FIXED, FINITE set of three directories, not a
    repo-wide glob (2026-07-27 widening; see `plugin_health.oracle_surface`
    — the single definition of this surface, also consumed by
    `bin_inventory_gate.py` — for why exactly these three and no more):
      - `coordinator/bin/` (the original supply side, ~700 CLIs)
      - `<repo-root>/bin/` (e.g. `claude-klabauter-doctor-probe.py`, `shell-init-guard.py`
        — root-level oracles with the same `.cmd`-Windows-launcher-twin shape
        as `coordinator/bin/`, confirmed by their `.cmd` siblings)
      - `coordinator/lib/` (e.g. `resolve-coordinator-clone.py` — installed via
        a different family than the forwarder loop, see
        `_AGENT_HELPER_RESERVED_NAMES`, but still a real fleet-cited oracle)
    Each is included because it independently satisfies the same test: a
    file DoE actually cites via `bin/<name>` lives there AND carries the
    `.cmd`/`.ps1` Windows-launcher-twin tell that marks it invoked as an
    executable, not read as a library-internal helper. A directory is NOT
    added to this list merely because it contains a `.py` file whose stem
    happens to match some cited token — see the module docstring's own
    "Documented residual blind spots" for what a name with no oracle in any
    of these three directories means (a genuine unmet demand, not a
    reason to widen the scan further).
  - Demand: DoE-claude fleet consumers, derived from a regex sweep of
    `coordinator/{skills,commands,hooks,pipelines}` `.md` files for the
    `bin/<name>` invocation form, NARROWED to genuine live-invocation
    demand by two independent structural filters (2026-07-27 widening —
    see `_is_excluded_from_invocation_surface` and
    `_is_namespace_qualified_citation` for the false positives each one
    closes): the file itself must not be test/fixture/archive/changelog
    /plan-doc shaped, and the citation itself must be NAMESPACE-QUALIFIED
    — a literal `coordinator/bin/<name>` or `templates/bin/<name>` prose
    form, or the settings-home-forwarder-seam expansion (`${COORDINATOR_
    SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<name>`) — see
    `_DOE_BIN_TOKEN_RE`. A bare `bin/<name>` with no namespace qualifier is
    deliberately NOT treated as demand (see `_is_namespace_qualified_
    citation`'s own docstring for why: it is structurally indistinguishable
    from a per-consumer-repo convention or a retired-artifact backlink).

Both sides are NORMALIZED to a common stem before diffing (strip a trailing
`.py`/`.js`/`.sh`, matching `_derive_agent_helper_target_map`'s own
`.py`-strip convention extended to the extensions DoE fences actually cite):
a fence citing `bin/query-records` and a live `query-records.js` oracle are
the SAME oracle for reachability purposes — this gate answers "does an
oracle by this name exist in some form", not "does the extension match".
Flag-level/extension-exactness drift is an explicitly named blind spot below,
not silently swallowed.

Assertion: `missing = normalized_doe_demand - normalized_claude_klabauter_oracles`.
FAIL LOUD (`ok=False`) if `missing` is non-empty — never WARN. This is the
one deliberate contract divergence from `forwarder_drift`: that module's
WARN-only contract exists because ITS drift is expected to self-heal on the
next install pass; a fleet-consumed oracle with no surviving claude-klabauter entry
does not self-heal — it is a live breakage the next consumer install hits.

Skip-masking guard: a clean `skipped=True, ok=True` result when DoE-claude is
unresolvable (the OSS-consumer / no-DoE-checkout persona — genuinely nothing
to compare) is NOT itself a failure. But a skip on a machine where
`repos.doe_claude` IS registered would silently reproduce the exact blind
spot this gate exists to close — `assert_registered_implies_no_skip()` below
is the CI-facing assertion that catches that shape; see
`test_fleet_reachability.py` for its wiring.

Documented residual blind spots (do not pretend coverage beyond these):
  - A DoE fence ADDED after this gate last ran (i.e. between a claude-klabauter delete
    landing and a later DoE-side citation) is invisible to a gate run BEFORE
    that fence existed — this gate is a delete-time check, not a continuous
    monitor. An optional `/workday-start` WARN net over the same primitive is
    out of scope here.
  - Non-fence DoE consumers — a Python module, a Workflow script, or any
    caller that does not cite `bin/<name>` in Markdown prose inside the
    swept directories — are invisible to a regex sweep by construction. The
    sweep is deliberately restricted to `*.md` (see `_SWEEPABLE_SUFFIX`) for
    the same reason: a `.py` test fixture embedding a `coordinator/bin/`
    path literal is not a fence citation, even though it would otherwise
    match the token regex.
  - Flag-level drift: an oracle name present on both sides with a dropped or
    renamed FLAG is a different gate (this one is name-reachability only).
  - Extension-exactness is deliberately NOT asserted (see normalization
    above) — a fence pinned to the literal `.js` form of an oracle that has
    since become `.py`-only still reads as reachable here, even though the
    literal invocation may differ.
  - A demand name resolved via `plugin_health.relocation_ledger` (see
    `_ledger_explains_missing`) is reported OK even when the ledger's
    `"moved"` entry points at a DIFFERENT repo, not claude-klabauter's own
    `coordinator/bin/` — this gate answers "is the demand explained", not
    "does claude-klabauter itself still serve it"; `relocation_ledger`'s own
    `check_relocation_ledger_integrity` is what verifies a `"moved"` entry's
    `new_path` actually still exists.
  - A demand name with NO ledger entry and no live claude-klabauter oracle in ANY of
    the three scanned directories is a genuine unmet fleet demand — this
    gate does not fabricate a disposition for it, and never will (see this
    module's own dispatch history: 2026-07-27 triage explicitly declined to
    manufacture a `"retired"` entry for `resolve-coordinator-clone` absent
    independent confirmation it was ever recorded as such — that same name
    was later confirmed to have a genuine live oracle at `coordinator/lib/
    resolve-coordinator-clone.py`, which the fix in this same dispatch
    surfaces by widening the scan rather than by touching the ledger).
  - A genuine live claude-klabauter oracle cited ONLY in bare `bin/<name>` form
    anywhere in the swept surface — no `coordinator/bin/`, `templates/bin/`,
    or settings-home-forwarder-seam citation of it exists — is invisible to
    this gate after the 2026-07-27 namespace-qualification narrowing.
    `claude-klabauter-doctor-probe` is the confirmed live instance today
    (`workday-start.md`'s "written by claude-klabauter's `bin/claude-klabauter-doctor-probe.py`"
    is its only DoE citation, and it is bare). Accepted trade: the
    alternative — treating every bare `bin/` mention as demand — is what
    produced the `check-fixture-sync` / `ensure-coordinator-venv` /
    `coordinator-handoff-archive` false positives this same pass fixes, and
    a bare mention cannot be told apart from those without interpreting
    prose. Closing this residual requires DoE to cite real oracles via one
    of the two qualified forms, not a widening of this gate's own regex.

Invocation surfaces: `pytest coordinator_core/plugin_health/tests/
test_fleet_reachability.py` (delete-time gate, per this module's docstring
purpose) or, for a live-state check outside pytest, `python3 -m
coordinator_core.plugin_health.fleet_reachability` (this module's own
`main()`, which checks real disk state and exits nonzero on a real miss). No
JSON-RPC op is registered for this module — unlike sibling `forwarder_drift`
(`@register_op("plugin_health.forwarder_drift")`, WARN-only, surfaced at
`/workday-start`), this gate is FAIL-LOUD delete-time-only by design, not a
continuous WARN probe, so it is not wired into the workday-start op-registry
surface. (Review: code-reviewer — this paragraph names the invocation
surfaces explicitly so a future reader does not have to infer them from the
bare `if __name__ == "__main__"` entry point.)

Spec backlink: pln-python-ize-claude-klabauter-bin-oracles--218413 D3
(original gate). Widened 2026-07-27 (commit 411f80ac's own follow-up finding —
"the remaining set is not yet the true one") to scan `<repo-root>/bin/` and
`coordinator/lib/` alongside `coordinator/bin/`: the live set was narrower
than claude-klabauter's real fleet-exposed oracle surface, so the gate was reporting
`claude-klabauter-doctor-probe`, `shell-init-guard`, and `resolve-coordinator-clone` as
missing when each has a genuine on-disk oracle outside `coordinator/bin/`.

Further narrowed 2026-07-27 (commit b1bc5789's own follow-up, same day) to cut
the remaining false-positive surface the scan-side fix left standing:
`check-fixture-sync`, `coordinator-handoff-archive`, and
`ensure-coordinator-venv` were reported missing despite none of them being a
genuine demand on claude-klabauter. Two independent structural filters close this —
`_is_excluded_from_invocation_surface` (file-class: drop test/fixture
/archive/changelog/plan-doc `.md` files from the sweep before token-matching)
and `_is_namespace_qualified_citation` (token-class: require a
`coordinator/bin/`, `templates/bin/`, or settings-home-forwarder-seam prefix
before treating a `bin/<name>` match as demand, dropping bare mentions that
are structurally indistinguishable from a per-consumer-repo convention or a
retired-artifact backlink). See "Demand" above and the two functions' own
docstrings for the verified-against-DoE's-tree false positives each one
closes, and the residual-blind-spots list for the accepted coverage trade
(`claude-klabauter-doctor-probe`'s bare-only citation).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.machine_resolver import registry_get

_PROG = "fleet-reachability"

# Directory names `_derive_agent_helper_target_map` excludes from its own
# scan (see that function's docstring) — a DoE fence citing `bin/lib/...`,
# `bin/tests/...`, etc. is citing a subdirectory, never a bare-name CLI, so
# these tokens are dropped from the DoE demand set rather than compared as
# oracle names.
_NON_ORACLE_SUBDIR_NAMES = {
    "lib",
    "fixtures",
    "tests",
    "test-fixtures",
    "repomap",
    "install-health",
    "__pycache__",
}

# The settings-home bin invocation form DoE fences cite, in both its bare
# (`bin/query-records`) and extensioned (`bin/query-records.js`) shapes, plus
# the plain `coordinator/bin/<name>` form skills reference in prose. A
# leading alnum requirement excludes prose ellipses (`bin/...`) and
# placeholder tokens (`bin/<cli>`) without a dedicated denylist. Trailing
# alnum is required too (Review: code-reviewer — a bare, unfenced citation at
# the end of a prose sentence, e.g. "See bin/query-records.", would otherwise
# capture the sentence-terminating "." into the token; the trailing-alnum
# requirement excludes it from the match instead of relying on `_normalize()`
# to strip it after the fact).
_DOE_BIN_TOKEN_RE = re.compile(r"\bbin/([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")

# Only Markdown prose is a fence-citation surface (see module docstring's
# "Demand" bullet — settings-home-forwarder-seam and bare `coordinator/bin/`
# citations both live in skill/command/hook/pipeline Markdown). A `.py` test
# fixture that happens to embed a `coordinator/bin/<name>.sh` path literal
# (e.g. a regression-guard docstring or an assertion payload) is DoE-internal
# test scaffolding, never a fenced demand on claude-klabauter's oracle surface — sweep
# only `.md` so that class of file cannot contribute a spurious token.
_SWEEPABLE_SUFFIX = ".md"

# Namespace-qualifying markers: a `bin/<name>` match is a genuine demand on
# CLAUDE-KLABAUTER's oracle surface only when it is immediately preceded by one of
# these two literal path segments (a real reference to claude-klabauter's own
# `coordinator/bin/` or the OSS `templates/bin/` mirror) -- see
# `_is_namespace_qualified_citation` below for the settings-home-forwarder
# -seam form (`}/bin/<name>`), the third qualifying shape.
#
# 2026-07-27 finding (see spec backlink below): a BARE `bin/<name>` with
# nothing namespace-specific before it is NOT reliably a claude-klabauter-oracle
# demand -- it is exactly as often a generic convention DoE's own prose
# addresses to the READER'S OWN repo (`workday-start.md`'s "Repos with
# paired cross-repo writers ship a `bin/check-fixture-sync.sh`" -- a
# per-consumer-repo file, never a claude-klabauter oracle) or a retired-artifact
# backlink (`install.md`'s "`bin/ensure-coordinator-venv.sh` no longer
# exists", `handoff-archival.md`'s "formerly `bin/coordinator-handoff-
# archive.sh`"). Both shapes are structurally identical to a genuine bare
# citation of a live claude-klabauter oracle (`workday-start.md`'s own "written by
# claude-klabauter's `bin/claude-klabauter-doctor-probe.py`") -- there is no regex-visible
# feature that tells these two classes apart without interpreting the
# surrounding prose, which this gate's own doctrine forbids (see module
# docstring's north-star "discharge test" -- a heuristic that mis-resolves
# silently is worse than an honest gap). Requiring namespace qualification
# is a deliberate precision-over-recall trade: a real oracle cited ONLY in
# bare form, with no qualified citation anywhere in the swept surface, is
# now invisible to this gate. `claude-klabauter-doctor-probe` is the concrete
# instance of that residual gap today (see module docstring's blind-spot
# list) -- accepted because the alternative (treating every bare `bin/`
# mention as demand) is what produced three false positives in the same
# 2026-07-27 fleet-reachability report and forced a retracted memo to DoE.
_QUALIFYING_PATH_MARKERS = ("coordinator/", "templates/")


def _is_namespace_qualified_citation(text: str, bin_start: int) -> bool:
    """True when the `bin/` at `text[bin_start:]` is genuinely
    namespace-qualified -- immediately preceded by a literal
    `coordinator/` or `templates/` path segment (with a real word/path
    boundary before that segment, so `othercoordinator/bin/` cannot
    masquerade as a `coordinator/` citation), or by the settings-home
    -forwarder-seam expansion's closing `}/` (e.g. `${COORDINATOR_SETTINGS_
    HOME:-$HOME/.coordinator-claude-settings}/bin/<name>`).

    Callers MUST still apply `_is_system_path_citation` as a separate
    exclusion -- `${HOME}/bin/<name>` (a recognized non-oracle marker) ends
    in the identical `}/` shape as the settings-home-forwarder-seam form,
    so this function alone cannot tell them apart; the two checks are
    complementary, not redundant (see `_doe_demand_tokens`)."""
    for marker in _QUALIFYING_PATH_MARKERS:
        marker_start = bin_start - len(marker)
        if marker_start < 0 or text[marker_start:bin_start] != marker:
            continue
        boundary_ok = marker_start == 0 or not (
            text[marker_start - 1].isalnum() or text[marker_start - 1] in "_-"
        )
        if boundary_ok:
            return True
    return bin_start >= 2 and text[bin_start - 2 : bin_start] == "}/"


# A `bin/<name>` match immediately followed by a run of `-`/`_`/`.` then a
# `*` stopped short because of a glob wildcard the token character class
# (deliberately) does not consume — e.g. `bin/verify-*-sync.sh` truncates to
# a captured "verify" (the regex backtracks over the trailing "-" to end on
# an alnum, leaving "-*-sync.sh" unconsumed). That is a family/convention
# reference ("everything matching this glob"), not a citation of one
# specific oracle name, so it must not be reported as an atomic demand in
# its own right — the actual named CLIs the glob describes are always cited
# literally elsewhere and are swept there instead.
_GLOB_TRUNCATION_RE = re.compile(r"[-_.]*\*")

# Review: code-reviewer — `.cmd` added: every `.py`/extensionless oracle in
# `coordinator/bin/` gets a generated `.cmd` Windows-launcher twin
# (`coordinator/bin/gen-launcher-shim.py`); a DoE fence citing the literal
# `bin/<name>.cmd` form would otherwise normalize to `<name>.cmd` and never
# match the `.py`-stripped claude-klabauter stem, a false-positive FAIL.
_KNOWN_ORACLE_EXTENSIONS = (".py", ".js", ".sh", ".cmd")

_SWEEP_SUBDIRS = ("skills", "commands", "hooks", "pipelines")

# Path-shape exclusions from the demand sweep: a `.md` file living at one of
# these structural locations is evidence ABOUT DoE's own tree (a test
# fixture, a retirement-guard's own assertion, a changelog entry, a working
# plan doc), never a live invocation surface a fleet consumer actually
# follows. 2026-07-27 finding: the oracle's own dispatch brief named
# `coordinator/tests/test_no_residual_divergence.py` -- a retirement guard
# asserting NO SKILL.md still cites the already-deleted
# `coordinator-handoff-archive.sh` -- as a false-positive source; that
# specific file is `.py` (already outside `_SWEEPABLE_SUFFIX`) and outside
# these four sweep subdirs, so it was never actually the live culprit (the
# real match traced to a "formerly `bin/coordinator-handoff-archive.sh`"
# backlink in `pipelines/update-docs/handoff-archival.md`, resolved by the
# namespace-qualification fix above instead). This exclusion set is kept as
# a second, independent structural discriminator regardless -- confirmed
# real occurrences on DoE's own tree: `coordinator/hooks/tests/
# block-destructive-rm.security-review.md` (a code-review artifact, not a
# hook DoE ever tells an agent to invoke) and
# `coordinator/pipelines/artifact-distillation/tests/phase3d-fixtures/**`
# (pipeline test fixtures).
#
# Deliberately NOT a filename-substring match on "review" (e.g. `*-review*`)
# despite that being the brief's own suggested exclusion class: DoE's own
# tree has genuine, live, fleet-invoked skill/command entrypoints named
# exactly that shape -- `commands/parallel-code-review.md`,
# `commands/enrich-and-review.md`, `skills/review/SKILL.md` -- and a
# substring filter would silently drop real demand from those, which is
# the "gate that cannot fail" overshoot this module's docstring warns
# against. The demonstrated review-artifact false positive
# (`block-destructive-rm.security-review.md`) is already caught by the
# `tests` path-segment rule below, without needing a name heuristic that
# cannot distinguish "a review of a change" from "a skill named review".
_EXCLUDED_PATH_SEGMENTS = frozenset({"tests", "test", "archive", "archived"})


def _is_excluded_from_invocation_surface(rel_path: Path) -> bool:
    """True when `rel_path` (relative to DoE-claude's repo root) is a
    non-invocation-surface file class per `_EXCLUDED_PATH_SEGMENTS`'s own
    docstring -- a test/fixture directory, an archived directory, a
    CHANGELOG, or a `docs/plans/` working doc -- rather than live prose a
    fleet consumer actually follows."""
    parts = [part.lower() for part in rel_path.parts]
    if _EXCLUDED_PATH_SEGMENTS.intersection(parts):
        return True
    if rel_path.name.upper().startswith("CHANGELOG"):
        return True
    return any(parts[i] == "docs" and parts[i + 1] == "plans" for i in range(len(parts) - 1))


# Characters that, immediately before an absolute path's leading `/`,
# confirm it really IS an absolute-path opening (a shebang `!`, a quote or
# backtick delimiting a literal path in prose, whitespace, a home-directory
# `~`, or nothing at all — start of the swept text). None of these can
# appear immediately before the `/` in a genuine relative citation
# (`coordinator/bin/`, `templates/bin/`, or a settings-home `}/bin/`
# expansion all have an alnum, `-`, `_`, `.`, or `}` there instead) — see
# `_is_system_path_citation`.
_ABS_PATH_OPEN_BOUNDARY_CHARS = frozenset(" \t\r\n\"'`!(<>~")

# Fixed-width path segments that, directly preceding a `/bin/` slash, name a
# canonical non-coordinator directory the way `/usr/` does — a generic
# system dir (`/usr/bin/`) or a generic user/home dir (`$HOME/bin/`,
# `${HOME}/bin/`), never `coordinator/bin/`. These are canonical shell/unix
# vocabulary (on par with recognizing `usr` as a system prefix), not
# per-CLI-name noise entries — the same distinction the module's own
# de-bash carve-out doctrine draws between "the interpreter/shell itself"
# and an arbitrary artifact name.
_KNOWN_NON_ORACLE_DIR_MARKERS = ("/usr/", "${HOME}/", "$HOME/")


def _is_system_path_citation(text: str, bin_start: int) -> bool:
    """True when the `bin/` at `text[bin_start:]` is actually part of an
    ABSOLUTE unix system path (`/bin/<name>`, `/usr/bin/<name>`) or a
    generic user-bin path (`~/bin/<name>`, `$HOME/bin/<name>`) — a shebang
    line, or prose naming a real OS/PATH binary like `/bin/bash` or
    `~/bin/scc` — not a citation of a claude-klabauter `coordinator/bin/` oracle.

    Structural, not name-based: this rejects the SHAPE of these path
    prefixes wherever they occur, so it generalizes to any future binary
    mentioned this way (`/bin/zsh`, `/usr/bin/python3`, `~/bin/ripgrep`,
    ...) rather than needing a new denylist entry per noisy name. Both
    legitimate citation shapes this gate's docstring documents — bare
    `bin/<name>` (nothing at all before it) and `coordinator/bin/<name>` /
    `templates/bin/<name>` / a settings-home `${...}/bin/<name>` expansion
    (an alnum, `-`, `_`, `.`, or `}` directly before the slash) — never have
    a path-boundary character (quote, backtick, whitespace, `!`, `~`, or
    start-of-text) immediately before that slash; only a genuine
    system/home path does.
    """
    root_idx: Optional[int] = None
    for marker in _KNOWN_NON_ORACLE_DIR_MARKERS:
        if bin_start >= len(marker) and text[bin_start - len(marker) : bin_start] == marker:
            root_idx = bin_start - len(marker)
            break
    if root_idx is None:
        if bin_start >= 1 and text[bin_start - 1] == "/":
            root_idx = bin_start - 1
        else:
            return False  # no leading slash at all -- bare `bin/...`, never a system path
    if root_idx == 0:
        return True  # the marker/slash opens the swept text itself
    return text[root_idx - 1] in _ABS_PATH_OPEN_BOUNDARY_CHARS


def _normalize(name: str) -> str:
    """Strip a trailing known oracle extension and lowercase — the common
    stem both the claude-klabauter-derived name and the DoE-cited token are compared
    on. See module docstring's "Both sides are NORMALIZED" section for why
    extension-exactness is deliberately not part of this gate's contract."""
    for ext in _KNOWN_ORACLE_EXTENSIONS:
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name.lower()


@dataclass
class FleetReachabilityResult:
    """ok=True -> no fleet-consumed oracle is missing (or a clean skip);
    ok=False -> at least one DoE-cited `bin/<name>` has no surviving claude-klabauter
    oracle. `skipped=True` means no comparison was possible (claude-klabauter root
    and/or DoE-claude root unresolvable) — distinct from a clean zero-missing
    result. `missing` carries the un-normalized DoE-cited tokens (for
    readability) whose normalized stem has no claude-klabauter match; empty when
    `ok=True`.

    `demand_count` (Review: code-reviewer, Finding 1) is `len(doe_demand)` —
    the total number of namespace-qualified fleet citations the sweep found
    before diffing against claude-klabauter's live oracle surface, on a NON-skipped
    run (left at its default `0` when `skipped=True`, since nothing was
    swept). It exists so `ok=True` is never indistinguishable from a vacuous
    sweep: `missing == []` is satisfied both by a genuine ~140-citation clean
    pass AND by a demand-side filter regression that zeroed `doe_demand` out
    entirely, and the module docstring's own north star ("a heuristic that
    mis-resolves silently is worse than an honest gap") requires that second
    case be a checkable, asserted fact rather than something visible only in
    a printed line a human happens to read."""

    ok: bool
    skipped: bool = False
    missing: List[str] = field(default_factory=list)
    demand_count: int = 0
    lines: List[str] = field(default_factory=list)


def _claude_klabauter_oracle_names(oracle_dirs: List[Path]) -> Set[str]:
    """Union the live oracle-name set across every directory in
    `oracle_dirs` (the fixed, finite fleet-exposed surface — see module
    docstring's "Axes compared: Supply" section for the inclusion rule and
    why this is never a repo-wide glob), then `_normalize()` each name for
    this gate's own extension-agnostic comparison.

    The raw scan itself (which directories, which scanner, reserved-name
    restoration) is NOT reimplemented here — it is
    `plugin_health.oracle_surface.live_oracle_names()`, the single
    definition of claude-klabauter's oracle surface `bin_inventory_gate.py` also
    consumes, so the two disappearance-detecting gates can never
    independently drift on what "currently exists" means. See that
    module's own docstring for the shared-surface rationale."""
    from coordinator_core.plugin_health.oracle_surface import live_oracle_names

    return {_normalize(name) for name in live_oracle_names(oracle_dirs)}


def _doe_demand_tokens(doe_root: Path) -> Set[str]:
    """Regex-sweep DoE-claude's coordinator/{skills,commands,hooks,pipelines}
    for `bin/<name>` citations, normalized to a common stem. Non-existent
    swept subdirs are skipped (a leaner DoE checkout is not an error).

    Two independent structural filters narrow the sweep to genuine
    fleet-invocation demand (see each filter's own docstring for the
    2026-07-27 false positives each one closes):
      - `_is_excluded_from_invocation_surface` drops whole FILES that are
        test/fixture/archive/changelog/plan-doc shaped, before any token
        matching happens.
      - `_is_namespace_qualified_citation` drops individual TOKEN matches
        that are not genuinely namespace-qualified (bare `bin/<name>` with
        no `coordinator/`/`templates/`/settings-home-forwarder-seam prefix)."""
    tokens: Set[str] = set()
    coordinator_dir = doe_root / "coordinator"
    for subdir in _SWEEP_SUBDIRS:
        root = coordinator_dir / subdir
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{_SWEEPABLE_SUFFIX}"):
            if not path.is_file():
                continue
            if _is_excluded_from_invocation_surface(path.relative_to(doe_root)):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _DOE_BIN_TOKEN_RE.finditer(text):
                if _is_system_path_citation(text, match.start()):
                    continue
                if not _is_namespace_qualified_citation(text, match.start()):
                    continue
                if _GLOB_TRUNCATION_RE.match(text, match.end()):
                    continue
                raw = match.group(1)
                normalized = _normalize(raw)
                if not normalized or normalized in _NON_ORACLE_SUBDIR_NAMES:
                    continue
                tokens.add(normalized)
    return tokens


def _ledger_explains_missing(normalized_name: str, ledger_path: Optional[Path]) -> Optional[str]:
    """Consult `plugin_health.relocation_ledger.find_relocation` for a
    normalized name this gate would otherwise report missing. Returns a
    one-line explanation if the ledger has a `"retired"` (deliberately gone,
    no oracle expected) or `"moved"` (resolves elsewhere) entry for it, or
    `None` if the ledger has nothing to say — in which case the name is a
    genuine unresolved demand, not noise.

    Reuses `relocation_ledger`'s own loader/query API (never a second JSON
    reader — see this module's dispatch brief and `relocation_ledger`'s own
    docstring for why a third parser would be the wrong shape). Tries the
    bare name plus each `_KNOWN_ORACLE_EXTENSIONS` suffix because the ledger
    stores `old_path` with whatever extension the retired/moved artifact
    actually had (e.g. `bin/resolve-coordinator-clone.sh`), while this
    gate's own demand tokens are already extension-stripped."""
    from coordinator_core.plugin_health.relocation_ledger import find_relocation

    for ext in ("",) + _KNOWN_ORACLE_EXTENSIONS:
        try:
            entry = find_relocation(f"bin/{normalized_name}{ext}", ledger_path=ledger_path)
        except RuntimeError:
            # `ledger_path` was omitted and claude-klabauter's own root is unresolvable
            # on this machine/harness (e.g. the pytest suite-root home
            # quarantine hiding the registry — see this file's sibling test
            # module for the same shape). No ledger to consult is a skip for
            # THIS lookup, not a gate failure; the caller's own agent_bin/
            # doe_root resolution already handles the analogous top-level
            # unresolvable case the same way.
            return None
        if entry is not None:
            return entry.describe()
    return None


def _resolve_doe_root() -> Optional[Path]:
    pointer = read_doe_root_pointer()
    if not pointer:
        return None
    candidate = Path(pointer)
    return candidate if candidate.is_dir() else None


def check_fleet_reachability(
    *,
    agent_bin: Optional[Path] = None,
    extra_oracle_dirs: Optional[List[Path]] = None,
    doe_root: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
) -> FleetReachabilityResult:
    """Core gate — used by both `main()` (CLI) and the delete-time pytest
    (`test_fleet_reachability.py`). `agent_bin` / `extra_oracle_dirs` /
    `doe_root` / `ledger_path` are explicit overrides for tests; a caller
    that omits any of them gets the real resolution ladder (claude-klabauter's own
    `coordinator_claude_klabauter_root()`, DoE's `read_doe_root_pointer()`, and
    `relocation_ledger`'s own `default_ledger_path()` — all registry-first,
    see each resolver's own docstring).

    `extra_oracle_dirs` is deliberately independent of `agent_bin`: a test
    that overrides `agent_bin` alone (the pre-2026-07-27 shape every
    existing fixture in this module's test file uses) gets NO extra
    directories scanned unless it also passes `extra_oracle_dirs` explicitly
    — this keeps every such fixture's live set exactly as narrow as it was
    before this gate's supply side was widened, so a synthetic test can't be
    silently widened by this machine's real `<repo-root>/bin/` or
    `coordinator/lib/` contents. Only the fully-real invocation (neither
    override given) auto-derives both via `plugin_health.oracle_surface`
    (`resolve_agent_bin()` / `resolve_extra_oracle_dirs()`), the single
    definition of this surface shared with `bin_inventory_gate.py`.

    A name that would otherwise be reported missing is first checked against
    `plugin_health.relocation_ledger` (see `_ledger_explains_missing`): a
    `"retired"` entry means the artifact is deliberately gone (no oracle
    expected, not a regression); a `"moved"` entry means it resolves under a
    different name/repo. Either way it is NOT a fleet-reachability failure —
    only a name the ledger has nothing to say about is a genuine unmet
    demand."""
    from coordinator_core.plugin_health.oracle_surface import (
        resolve_agent_bin,
        resolve_extra_oracle_dirs,
    )

    if agent_bin is not None:
        resolved_agent_bin: Optional[Path] = agent_bin
        resolved_extra_dirs = extra_oracle_dirs if extra_oracle_dirs is not None else []
    else:
        resolved_agent_bin = resolve_agent_bin()
        resolved_extra_dirs = extra_oracle_dirs if extra_oracle_dirs is not None else resolve_extra_oracle_dirs()

    if resolved_agent_bin is None:
        return FleetReachabilityResult(
            ok=True,
            skipped=True,
            lines=[f"[skip] {_PROG}: claude-klabauter root (or its coordinator/bin/) unresolvable — nothing to compare"],
        )

    resolved_doe_root = doe_root if doe_root is not None else _resolve_doe_root()
    if resolved_doe_root is None:
        # foreign-identity: NOT-REACHABLE — plugin_health fleet delete-safety
        # gate, operator/gate-invoked, not ambient to a example-retrieval-repo EM (audit
        # row 22, fleet_reachability.py:612,642)
        return FleetReachabilityResult(
            ok=True,
            skipped=True,
            lines=[f"[skip] {_PROG}: DoE-claude root unresolvable — nothing to compare"],
        )

    claude_klabauter_oracles = _claude_klabauter_oracle_names([resolved_agent_bin] + resolved_extra_dirs)
    doe_demand = _doe_demand_tokens(resolved_doe_root)
    candidate_missing = sorted(doe_demand - claude_klabauter_oracles)

    missing_normalized: List[str] = []
    ledger_notes: List[str] = []
    for name in candidate_missing:
        explanation = _ledger_explains_missing(name, ledger_path)
        if explanation is not None:
            ledger_notes.append(f"[ok] {_PROG}: {name!r} not live, but ledger-explained — {explanation}")
            continue
        missing_normalized.append(name)

    if not missing_normalized:
        if not doe_demand:
            # Review: code-reviewer, Finding 1 — a zero-citation sweep is NOT
            # the same fact as a genuine clean pass, and must not print
            # identically to one. `demand_count=0` makes the distinction a
            # checkable field (see `assert_registered_implies_no_skip`'s own
            # live counterpart, `test_live_tree_reachability_ok_on_this_
            # machine_when_registered`, for where a real run asserts a floor
            # on this count); the `[warn]` line makes it loud on stdout too.
            return FleetReachabilityResult(
                ok=True,
                demand_count=0,
                lines=[
                    f"[warn] {_PROG}: 0 fleet-cited oracle(s) found in the swept surface — nothing "
                    "was compared; a genuinely clean sweep of a real DoE-claude tree always finds "
                    "some citations, so this is reported distinctly rather than as an identical-"
                    "looking [ok]"
                ]
                + ledger_notes,
            )
        return FleetReachabilityResult(
            ok=True,
            demand_count=len(doe_demand),
            lines=[f"[ok] {_PROG}: {len(doe_demand)} fleet-cited oracle(s) all have a surviving claude-klabauter oracle"]
            + ledger_notes,
        )

    return FleetReachabilityResult(
        ok=False,
        missing=missing_normalized,
        demand_count=len(doe_demand),
        lines=[
            f"[fail] {_PROG}: {len(missing_normalized)} fleet-consumed oracle(s) have no surviving "
            f"claude-klabauter oracle — {', '.join(missing_normalized)}"
        ]
        + ledger_notes,
    )


def assert_registered_implies_no_skip() -> None:
    """CI-facing skip-masking guard (see module docstring). Raises
    AssertionError if `repos.doe_claude` IS registered on this machine but
    `check_fleet_reachability()` still skipped — a silent skip on a machine
    that CAN resolve DoE-claude would reproduce this gate's own blind spot.
    No-ops (does not raise, does not skip a caller's own pytest) when
    `repos.doe_claude` is not registered — that persona legitimately has
    nothing to compare, and asserting non-skip there would be the OSS/
    no-DoE-checkout false positive this guard must not introduce."""
    if not registry_get("repos.doe_claude"):
        return
    result = check_fleet_reachability()
    if result.skipped:
        raise AssertionError(
            f"{_PROG}: skipped despite repos.doe_claude being registered — "
            "this reproduces the exact skip-masking blind spot the gate exists to close"
        )


def main(argv: List[str]) -> int:
    del argv  # no flags accepted today
    result = check_fleet_reachability()
    for line in result.lines:
        print(line, file=sys.stderr if not result.ok else sys.stdout)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
