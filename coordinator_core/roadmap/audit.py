"""
coordinator_core.roadmap.audit — Phase 2 close cross-file audits for
coordinator:roadmap-planning.

Purpose: closes the gap that ``bin/lint-frontmatter.js`` cannot enforce per-file:
rules that compare multiple stubs in the active set, or cross-reference a stub
against ``state/roadmap/<run-id>/pm-gates.md``. Runs 5 independent audits and
accumulates ALL failures before exiting (never short-circuits on the first
failure) — mirrors the bash oracle's ``fail()``/``pass()`` accumulate-don't-abort
posture.

Port of: audit-roadmap.sh (DoE b5a4192c, 2026-07-20), 441 LoC, 5 audits.
Spec backlink: docs/plans/2026-05-08-roadmap-skill-and-handoff-lifecycle.md § Phase 5
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3e

Audit 1 — stub-coverage: count of MERGE+KEEP verdicts in reconciliation.md must
  equal count of stubs on disk (live + archived) with this roadmap_id.
Audit 2 — at most one ``ready_to_fire`` stub per (roadmap_id, sprint, wave) —
  wave is sprint-LOCAL, so the uniqueness key is (sprint, wave), not wave alone.
Audit 3 — every stub with ``gate_dependency`` starting ``"PM "`` must have a
  matching ``stub_id`` row in pm-gates.md.
Audit 4 — every ``pending`` row in pm-gates.md must be referenced by at least
  one stub (inverse of Audit 3).
Audit 5 — dependency-order invariant: for every edge A blocked_by B (B ships
  first), ``number(B) < number(A)`` and ``(sprint(B), wave(B)) <_lex
  (sprint(A), wave(A))`` (strict; equal slot is a violation). Missing sprint on
  either endpoint fails loud. Edges to absent stub_ids are unresolved (not
  silently dropped). Cycles fail loud.

Sprint-scoped mode (C4, ``run_audit(..., sprint_id=...)`` / CLI ``--sprint``)
— stubs now arrive one sprint at a time (docs/plans/2026-08-21-engine-half-
of-the-roadmap-sprint-spine-split.md), so a whole-roadmap Audit 1/3/5 run
before the LAST sprint has landed reports false violations against sprints
whose spine descriptor legitimately has no stubs on disk yet
(spine.schema.json's ABSENT-vs-``[]`` ``sprints[].stubs`` distinction). This
mode reads ``state/roadmap/<run-id>/SPINE.md`` (``read_spine``) to scope
Audit 1's coverage count and Audit 3's pm-gates cross-reference to one named
sprint's own cluster (``sprints[].stubs``), sourcing both ``reconciliation.md``
and ``pm-gates.md`` from that sprint's own ``sprint-<ordinal>/`` directory
(mirroring C11's per-sprint ``OVERVIEW.md`` homing) rather than the roadmap
root. Audit 5 resolves cross-sprint edges by reading the spine record ALONE
(``check_cross_sprint_edge_order`` over ``sprints[].ordinal`` and
``cross_sprint_edges[]``) — no dependency on the descriptor-altitude edge
entity C5b resolves, so this mode is buildable, and usable, without C5b's
answer once C3b's ``spine.schema.json`` lands. Audits 2 (ready_to_fire
uniqueness) and 4 (pending-row reference) are whole-roadmap-only and are not
run in sprint-scoped mode — the C4 body names only 1/3/5.

DATA_ROOT resolution — ``--root`` flag wins; else derived from the per-repo
state root (Rule 5, no ``--central``) for the CURRENT working directory's git
repo: meta-repo (``~/.claude``) routes to claude-klabauter's ``state/``, dirname'd back
to the claude-klabauter repo root; any other (sibling) repo uses its own repo root.
Explicitly NO ``$CLAUDE_KLABAUTER_ROOT`` env-var precedence branch here — an
ambient/leaked ``CLAUDE_KLABAUTER_ROOT`` from an unrelated caller re-rooting this audit
is the exact dead-gate bug class this script was fixed to avoid (bash
oracle comment, preserved). Callers that need to force a specific root use
``--root <dir>``, which is unambiguous opt-in.

PMG/RECON path fix (found during golden-diff verification, not in the recipe) —
the bash oracle computes ``PMG``/``RECON`` via a SEPARATE ``coordinator_state_root()``
call keyed off the CALLER'S CWD, made *after* ``--root`` is parsed and exported
to ``CLAUDE_KLABAUTER_ROOT``. Because Rule 5's non-meta-repo branch never reads
``$CLAUDE_KLABAUTER_ROOT``, this second call silently ignores an explicit ``--root`` flag
whenever the caller's cwd is a DIFFERENT repo than ``--root`` — e.g.
``(cwd=claude-klabauter) audit-roadmap.sh <id> --root /path/to/other-repo`` resolves
``PMG``/``RECON`` under claude-klabauter's own ``state/roadmap/``, not
``/path/to/other-repo/state/roadmap/`` — a real, empirically-confirmed
divergence (verified during this port's golden-diff pass), invisible to every
existing bash regression test because they all run with cwd == the ``--root``
value. This port computes ``PMG``/``RECON`` DIRECTLY from ``DATA_ROOT``
(``DATA_ROOT/state/roadmap/<run-id>/...``) — always self-consistent with
whatever root was resolved (flag or default), fixing this latent bug by
construction. Byte-identical to the bash oracle in every tested/intended usage
(no ``--root``, or ``--root`` matching cwd's repo) — this is a genuine
correctness fix in the untested edge case, not a silent behavior change; see
the code-review discipline this port applies (break-class bash bugs on
migration-bound surfaces route to the Python port, fix-in-port).

Facade seam — the bash oracle threads ``DATA_ROOT`` to ``cc_records_query``
via ``export CLAUDE_KLABAUTER_ROOT="$DATA_ROOT"`` (a subprocess-boundary crossing). This
port has no such boundary: ``DATA_ROOT`` is passed as an explicit
``worktree_root`` argument to
``coordinator_core.ops.ceremony.records_query.query_records`` (the in-process
records-query helper), never through an environment variable. The bash
oracle's State-1/State-2/State-3 tri-state (seam-absent-legacy-fallback /
seam-present-working / seam-present-transport-broken) does not apply: there is
no separate transport to fail over — an in-process function call either
returns results or raises. ``_abort_on_state3``'s FAIL-LOUD POSTURE is
preserved as-is: any unexpected exception from ``query_records`` aborts the
whole audit with exit code 3 (never silently swallowed into an empty result
set — the old dead-gate class this bash fix targeted).

Frontmatter parsing — the bash oracle shells ``node -e`` per stub file to
parse frontmatter via ``schema.js``'s ``parseFrontmatter``. This port reuses
``query_records``'s already-parsed ``frontmatter`` dict per result (a full
stdlib-YAML parse via ``coordinator_core.dag._read_meta``, not a per-audit
re-read) — strictly more robust than the bash oracle's scoped node parse.
Negative-spec: a stub file with a frontmatter block that fails to parse is
silently excluded from ``query_records``'s result set (documented on that
helper) rather than surfaced as the bash oracle's explicit ``PARSE_ERROR``
sentinel (Audit 2) — this divergence is accepted as a simplification, not
preserved as a bug, per the port's authorization to simplify subprocess-boundary
plumbing (see facade seam note above); a stub with genuinely malformed YAML
frontmatter is an authoring bug this port surfaces differently (silently
absent from the audit, not a distinct PARSE_ERROR count) rather than not at
all.

Negative-spec:
  - Does NOT resolve or fall back to ``query-records.js`` (the bash oracle's
    State-1 legacy-node-fallback path) — this port's facade call IS the
    equivalent of "seam present and working" unconditionally; there is no
    legacy fallback to port because there is no separate seam to be absent.
  - Does NOT implement a ``--central`` state-root mode — Rule 5 only (matches
    the bash oracle, which never passes ``--central`` to
    ``coordinator_state_root``).
  - state-root resolution below (``_state_root``) is a MINIMAL, LOCAL
    reimplementation of ``coordinator-state-root.sh``'s Rule 5, duplicated
    from the same small pattern already inlined in
    ``coordinator_core.orientation.regenerate_cache._state_root`` /
    ``coordinator_core.ops.queue_append._claude_klabauter_root`` (that module carries
    its own "de-dup into a shared module" TODO) rather than importing those
    private names cross-module. A canonical
    ``coordinator_core.state_root.coordinator_state_root()`` seam does not
    exist yet on disk as of this port (grepped at build time) — if/when one
    lands, this function should be replaced with a call to it rather than
    kept as a fourth duplicate.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core._settings_home import machine_local_dir, settings_home
from coordinator_core.engine_root import coordinator_engine_root_env
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.roadmap.spine import (  # noqa: F401 -- re-exported
    read_spine,
)
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.win_portability import same_path

# `kind in (...)` term covering the canonical `roadmap-baton` value plus any
# still-live retired pre-rename spelling(s) — derived at import time from
# `baton_class.kind_values_for_canonical`, never a hand-authored pair. See
# that function's docstring and
# `coordinator_core/tests/test_baton_class_is_the_only_membership_set.py`.
# Every `where=` string in this module that filters on roadmap-baton `kind`
# uses this term instead of a literal `kind=spinoff-roadmap`.
_ROADMAP_BATON_KIND_WHERE = "kind in ({})".format(
    ",".join(kind_values_for_canonical("roadmap-baton"))
)

# ---------------------------------------------------------------------------
# state-root resolution (local minimal Rule-5 port — see module docstring)
# ---------------------------------------------------------------------------

_CLAUDE_HOME_ENV = "CLAUDE_HOME"


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Resolve `_machine_local.py`: MACHINE_LOCAL_IMPL override, then the
    settings-home copy, then the legacy ~/.claude/bin fallback. Mirrors
    coordinator_core.pyresolve._machine_local_impl's settings-home-first
    ordering."""
    override = os.environ.get("MACHINE_LOCAL_IMPL")
    if override:
        return override

    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl

    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    impl = _machine_local_impl()
    if not os.path.exists(impl):
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root_pointer_file() -> Optional[str]:
    """Rung-1.5 fast path: read ``<settings-home>/machine-local/.claude-klabauter-live-root``
    directly, no subprocess spawn.

    Review: code-reviewer (P2) — the oracle (coordinator-claude-klabauter-root.sh, rung
    1.5) checks this pointer file BEFORE the machine-local subprocess ladder,
    documented as a Windows hook-latency fix (per-invoke resolution avoiding a
    bash subprocess spawn). This port skipped straight to the subprocess-based
    ``_machine_local_get``, always paying the spawn cost. Mirrored here as a
    plain file read (no subprocess), falling through to rung 2 on absence.
    """
    try:
        ptr = machine_local_dir() / ".claude-klabauter-live-root"
        if ptr.is_file():
            val = ptr.read_text(encoding="utf-8").strip()
            if val:
                return val
    except OSError:
        return None
    return None


def _claude_klabauter_root() -> Optional[str]:
    override = (coordinator_engine_root_env(__name__) or "").strip()
    if override:
        return override
    val = _claude_klabauter_root_pointer_file()
    if val:
        return val
    val = _machine_local_get("repos.claude_klabauter")
    return val if val else None


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Promoted from realpath-only to
    samefile-then-fallback semantics: broader (junction-aware) equality is
    correct here since this call site only checks "is repo_root the meta-repo
    home", where a junction-aliased home must compare equal."""
    return same_path(a, b)


def _state_root(repo_root: Path) -> Path:
    """Resolve the per-repo state root (coordinator-state-root.sh Rule 5, no --central).

    meta-repo (~/.claude) -> claude-klabauter/state; sibling repo -> repo_root/state.
    Fails loud (RuntimeError) when repo_root is the meta-repo and CLAUDE_KLABAUTER_ROOT
    is unresolvable.
    """
    if _same_path(str(repo_root), _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            raise RuntimeError(
                "audit-roadmap: repo_root is the meta-repo but CLAUDE_KLABAUTER_ROOT is "
                "unresolvable (no CLAUDE_KLABAUTER_ROOT env, no repos.claude_klabauter "
                "machine-local entry)"
            )
        return Path(claude_klabauter_root) / "state"
    return repo_root / "state"


def resolve_repo_root(cwd: Optional[Path] = None) -> Path:
    """``git rev-parse --show-toplevel`` from *cwd*.

    Review: code-reviewer (P1) — the oracle (coordinator-state-root.sh Rule 5)
    FAILS LOUD when the git root is unresolvable ("never silently
    pick either branch"); this previously fell back to *cwd* unchanged, which
    would silently mis-root DATA_ROOT and let audits 2/4/5 vacuously PASS on
    zero query results with no signal a rooting failure occurred — the exact
    dead-gate class this script exists to prevent.
    """
    cwd = cwd or Path.cwd()
    out = show_toplevel(str(cwd))
    if out:
        return Path(out)
    raise RuntimeError(
        f"audit-roadmap: git rev-parse --show-toplevel failed or returned empty "
        f"for cwd={cwd}. Not inside a git repository. Remediation: run from "
        f"within a git repository."
    )


def resolve_data_root(root_flag: Optional[str], cwd: Optional[Path] = None) -> Path:
    """Resolve DATA_ROOT: ``--root`` flag wins; else dirname(state_root(cwd's repo))."""
    if root_flag:
        return Path(root_flag)
    repo_root = resolve_repo_root(cwd)
    return _state_root(repo_root).parent


# ---------------------------------------------------------------------------
# run-id validation — Review: code-reviewer (F4/P2), preserved exactly.
# ---------------------------------------------------------------------------

_RUN_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def validate_run_id(run_id: str) -> Optional[str]:
    """Return an error message if run_id is invalid, else None.

    Review: code-reviewer (nit) — a real, beneficial divergence from the oracle,
    undocumented until now. The oracle's ``grep -qE`` matches PER LINE, so a
    run_id with an embedded newline could smuggle content past validation (one
    line matches even though the full string doesn't). ``re.match`` without
    ``re.MULTILINE`` anchors ``$`` to true end-of-string, correctly rejecting
    such input — a strict improvement, not a bug to preserve.
    """
    if not _RUN_ID_RE.match(run_id):
        return f'ERROR: <run-id> must match ^[a-z0-9][a-z0-9-]*$ (got: "{run_id}")'
    return None


# ---------------------------------------------------------------------------
# Verdict-count regex (Audit 1) — byte-parity port of the bash oracle's
# 2nd-OR-3rd-cell grep with prose fallback.
# ---------------------------------------------------------------------------

_VERDICT_TABLE_RE: Dict[str, "re.Pattern[str]"] = {
    "KEEP": re.compile(r'^\|[^|]*\|\s*\*\*KEEP\*\*|^\|[^|]*\|[^|]*\|\s*\*\*KEEP\*\*'),
    "MERGE": re.compile(r'^\|[^|]*\|\s*\*\*MERGE\*\*|^\|[^|]*\|[^|]*\|\s*\*\*MERGE\*\*'),
}
_VERDICT_PROSE_RE: Dict[str, "re.Pattern[str]"] = {
    "KEEP": re.compile(r'verdict:\s*keep\b', re.IGNORECASE),
    "MERGE": re.compile(r'verdict:\s*merge\b', re.IGNORECASE),
}


def _count_verdict(text: str, kind: str) -> int:
    """Count matching LINES (grep -cE semantics — one count per matching line,
    not per regex hit) for the KEEP/MERGE table-cell shape, falling back to the
    prose "Verdict: KEEP"/"Verdict: MERGE" form if the table shape matches 0 rows."""
    table_re = _VERDICT_TABLE_RE[kind]
    count = sum(1 for line in text.splitlines() if table_re.search(line))
    if count == 0:
        prose_re = _VERDICT_PROSE_RE[kind]
        count = sum(1 for line in text.splitlines() if prose_re.search(line))
    return count


# ---------------------------------------------------------------------------
# Post-reconciliation stub declarations (Audit 1 scoping).
# ---------------------------------------------------------------------------

POST_RECONCILIATION_FILENAME = "post-reconciliation-stubs.md"

_DECLARED_STUB_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+$')


def parse_post_reconciliation_stubs(text: str) -> Dict[str, str]:
    """Parse a roadmap's ``post-reconciliation-stubs.md`` sibling into
    ``{stub_id: provenance}``.

    Negative spec — this is a scoping declaration, NOT a suppression list. A row
    only exempts its stub if it carries a non-empty provenance cell saying where
    the stub came from; a bare stub_id with an empty reason is ignored, so an
    undocumented exemption cannot quietly widen the coverage bar. Audit 1
    additionally fails on a declared stub_id that is not on disk, so the file
    cannot rot into a standing waiver for stubs that no longer exist.

    Row shape (pm-gates.md-style pipe table, header and separator rows skipped
    by the stub_id pattern not matching):

        | stub_id | minted | why it postdates reconciliation |
        |---------|--------|---------------------------------|
        | foo-04  | 2026-07-04 | expansion cohort, clusters.md Cluster 7 |
    """
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        stub_id = cells[0].strip("`*")
        provenance = cells[-1]
        if not _DECLARED_STUB_RE.match(stub_id) or not provenance:
            continue
        out[stub_id] = provenance
    return out


# ---------------------------------------------------------------------------
# Pending-row parse (Audit 4) — port of the awk -F'|' extraction.
# ---------------------------------------------------------------------------

_PENDING_STUB_RE = re.compile(r'^[a-z]+-[0-9]+$')
_INTERNAL_WS_RE = re.compile(r'[ \t]+')


def _parse_pending_stubs(pmg_text: str) -> List[str]:
    """awk -F'|' '/pending/ { gsub(/[ \\t]+/,"",$3); if ($3 ~ /^[a-z]+-[0-9]+$/) print $3 }'.

    Review: code-reviewer (P2) — the oracle's ``gsub(/[ \t]+/,"",$3)`` strips
    EVERY run of spaces/tabs anywhere in the cell, not just leading/trailing.
    ``.strip()`` only trimmed edges, so a pending row with internal spacing
    (e.g. ``| foo - 3 |``) matched the oracle's post-gsub regex but silently
    failed Python's, dropping that row from Audit 4. Use a full-string
    whitespace strip (``_INTERNAL_WS_RE.sub("", ...)``) for byte-parity.
    """
    out: List[str] = []
    for line in pmg_text.splitlines():
        if "pending" not in line:
            continue
        fields = line.split("|")
        if len(fields) < 3:
            continue
        val = _INTERNAL_WS_RE.sub("", fields[2])
        if _PENDING_STUB_RE.match(val):
            out.append(val)
    return out


# ---------------------------------------------------------------------------
# checkDependencyOrder — port of coordinator/bin/lib/roadmap-graph.js
# ---------------------------------------------------------------------------

_TRAILING_NUM_RE = re.compile(r'[-_](\d+)$')


def _resolve_number(stub: Dict[str, Any]) -> Optional[float]:
    # Review: code-reviewer (P1) — ``number`` may already be a coerced
    # float("nan") from _build_stub_descriptors (non-numeric frontmatter
    # value); re-wrapping in int() here would crash on nan. Values coming
    # through this dict are already int/float/None-typed by the descriptor
    # builder, so return as-is rather than re-converting.
    num = stub.get("number")
    if num is not None:
        return num
    m = _TRAILING_NUM_RE.search(str(stub.get("stub_id", "")))
    return int(m.group(1)) if m else None


def check_dependency_order(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Verify stub numbers and (sprint, wave) slots are dependency-monotone
    across all declared blocked_by edges. Byte-parity port of
    roadmap-graph.js's ``checkDependencyOrder`` — see that file's docstring
    for the full invariant description.

    Returns {"ok": bool, "violations": [...], "unresolved": [...], "cycle": list|None}.
    """
    violations: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    stub_map: Dict[str, Dict[str, Any]] = {}
    for stub in stubs:
        entry = dict(stub)
        entry["_num"] = _resolve_number(stub)
        stub_map[stub["stub_id"]] = entry

    for stub in stubs:
        a = stub_map[stub["stub_id"]]
        blocked_by = stub.get("blocked_by") or []

        for dep_id in blocked_by:
            if dep_id not in stub_map:
                unresolved.append(
                    {"from": stub["stub_id"], "to": dep_id, "reason": "unresolved-edge"}
                )
                continue

            b = stub_map[dep_id]

            a_missing_s = a.get("sprint") is None
            b_missing_s = b.get("sprint") is None
            if a_missing_s or b_missing_s:
                violations.append(
                    {
                        "from": a["stub_id"],
                        "to": b["stub_id"],
                        "reason": "missing-sprint",
                        "which": "both" if (a_missing_s and b_missing_s) else ("A" if a_missing_s else "B"),
                    }
                )
                continue

            if a["_num"] is not None and b["_num"] is not None:
                if not (b["_num"] < a["_num"]):
                    violations.append(
                        {
                            "from": a["stub_id"],
                            "to": b["stub_id"],
                            "reason": "number-order",
                            "numberA": a["_num"],
                            "numberB": b["_num"],
                        }
                    )

            a_has_w = a.get("wave") is not None
            b_has_w = b.get("wave") is not None
            if a_has_w and b_has_w:
                slot_ok = b["sprint"] < a["sprint"] or (
                    b["sprint"] == a["sprint"] and b["wave"] < a["wave"]
                )
                if not slot_ok:
                    violations.append(
                        {
                            "from": a["stub_id"],
                            "to": b["stub_id"],
                            "reason": "same-or-inverted-slot",
                            "slotA": {"sprint": a["sprint"], "wave": a["wave"]},
                            "slotB": {"sprint": b["sprint"], "wave": b["wave"]},
                        }
                    )

    cycle = _detect_cycles(stubs, stub_map)

    ok = not violations and not unresolved and cycle is None
    return {"ok": ok, "violations": violations, "unresolved": unresolved, "cycle": cycle}


def _detect_cycles(
    stubs: List[Dict[str, Any]], stub_map: Dict[str, Dict[str, Any]]
) -> Optional[List[str]]:
    """DFS three-colour cycle detection over the blocked_by graph (provided set only)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {stub["stub_id"]: WHITE for stub in stubs}
    cycle_found: Optional[List[str]] = None

    def dfs(node_id: str, path: List[str]) -> None:
        nonlocal cycle_found
        if cycle_found is not None:
            return
        color[node_id] = GRAY
        path.append(node_id)

        stub = stub_map[node_id]
        for dep_id in (stub.get("blocked_by") or []):
            if dep_id not in stub_map:
                continue
            if cycle_found is not None:
                return
            c = color.get(dep_id)
            if c == GRAY:
                cycle_start = path.index(dep_id)
                cycle_found = list(path[cycle_start:])
                return
            if c == WHITE:
                dfs(dep_id, path)

        path.pop()
        color[node_id] = BLACK

    for stub in stubs:
        if cycle_found is not None:
            break
        if color.get(stub["stub_id"]) == WHITE:
            dfs(stub["stub_id"], [])

    return cycle_found


# ---------------------------------------------------------------------------
# Sprint-scoped mode (C4) — reads state/roadmap/<run-id>/SPINE.md
# (spine.schema.json, C3b) to answer "what is this sprint's own cluster
# subset" and "how do cross-sprint gates order" WITHOUT waiting on C5b's
# descriptor-altitude blocks-sprint edge entity. Stubs now arrive one sprint
# at a time, so a whole-roadmap Audit 1/3/5 run before the LAST sprint has
# landed reports false violations against sprints that legitimately have no
# stubs on disk yet (spine.schema.json's ABSENT-vs-empty-array `stubs[]`
# distinction). This mode instead scopes coverage/gate/order checks to one
# named sprint's own descriptor.
# ---------------------------------------------------------------------------




def _find_sprint_descriptor(spine: Dict[str, Any], sprint_id: str) -> Optional[Dict[str, Any]]:
    for sprint in spine.get("sprints") or []:
        if isinstance(sprint, dict) and sprint.get("id") == sprint_id:
            return sprint
    return None


def check_cross_sprint_edge_order(spine: Dict[str, Any]) -> Dict[str, Any]:
    """Verify `spine['cross_sprint_edges']` is ordinal-monotone against
    `spine['sprints'][].ordinal` — the sprint-descriptor-altitude analogue
    of `check_dependency_order`, read entirely from the spine record.

    Deliberately does NOT consult any stub's `blocked_by` or a
    descriptor-altitude edge entity (that is C5b's, still gated on DoE) —
    every input here comes from the spine's own `sprints[]`/
    `cross_sprint_edges[]`, per the module's C4 mandate.

    Returns {"ok": bool, "violations": [...], "unresolved": [...], "cycle": list|None}.
    An edge naming a sprint id absent from `sprints[]` is "unresolved"
    (never silently dropped); an edge whose `from` ordinal is not strictly
    less than its `to` ordinal is a "violation"; a cycle among edges is
    reported separately.
    """
    ordinal_by_id: Dict[str, Any] = {}
    for sprint in spine.get("sprints") or []:
        if isinstance(sprint, dict) and isinstance(sprint.get("id"), str):
            ordinal_by_id[sprint["id"]] = sprint.get("ordinal")

    violations: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    adjacency: Dict[str, List[str]] = {}

    for edge in spine.get("cross_sprint_edges") or []:
        if not isinstance(edge, dict):
            continue
        frm = edge.get("from")
        to = edge.get("to")
        if frm not in ordinal_by_id:
            unresolved.append({"from": frm, "to": to, "which": "from"})
            continue
        if to not in ordinal_by_id:
            unresolved.append({"from": frm, "to": to, "which": "to"})
            continue
        if not (ordinal_by_id[frm] < ordinal_by_id[to]):
            violations.append(
                {
                    "from": frm,
                    "to": to,
                    "ordinalFrom": ordinal_by_id[frm],
                    "ordinalTo": ordinal_by_id[to],
                }
            )
        adjacency.setdefault(frm, []).append(to)

    # Three-colour DFS cycle detection over the cross_sprint_edges adjacency
    # — same shape as `_detect_cycles`, scoped to sprint ids instead of stub
    # ids since this graph's nodes are sprint descriptors.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {sid: WHITE for sid in ordinal_by_id}
    cycle_found: Optional[List[str]] = None

    def dfs(node_id: str, path: List[str]) -> None:
        nonlocal cycle_found
        if cycle_found is not None:
            return
        color[node_id] = GRAY
        path.append(node_id)
        for nxt in adjacency.get(node_id, []):
            if cycle_found is not None:
                return
            c = color.get(nxt)
            if c == GRAY:
                start = path.index(nxt)
                cycle_found = list(path[start:])
                return
            if c == WHITE:
                dfs(nxt, path)
        path.pop()
        color[node_id] = BLACK

    for sid in ordinal_by_id:
        if cycle_found is not None:
            break
        if color[sid] == WHITE:
            dfs(sid, [])

    ok = not violations and not unresolved and cycle_found is None
    return {"ok": ok, "violations": violations, "unresolved": unresolved, "cycle": cycle_found}


# ---------------------------------------------------------------------------
# Audit runner
# ---------------------------------------------------------------------------


class _Reporter:
    """Accumulates pass/fail lines exactly as the bash oracle's fail()/pass()."""

    def __init__(self) -> None:
        self.exit_code = 0
        self.stdout_lines: List[str] = []
        self.stderr_lines: List[str] = []

    def fail(self, msg: str) -> None:
        self.stderr_lines.append(f"FAIL: {msg}")
        self.exit_code = 1

    def passed(self, msg: str) -> None:
        self.stdout_lines.append(f"PASS: {msg}")


def _audit1_stub_coverage(
    r: _Reporter,
    run_id: str,
    data_root: Path,
    recon_path: Path,
    stub_filter: Optional[set] = None,
    scope_label: Optional[str] = None,
) -> None:
    """*stub_filter*/*scope_label* are the C4 sprint-scoped mode's hook: when
    *stub_filter* is given (a set of stub_ids), coverage is counted against
    that subset only — the sprint's own cluster, per its spine descriptor's
    `stubs[]` — rather than every stub sharing *run_id*. Both default to
    None, which reproduces the original whole-roadmap behaviour byte-for-byte
    (message text unchanged; every existing whole-roadmap test pins this)."""
    label = f" sprint={scope_label}" if scope_label else ""
    if not recon_path.is_file():
        if scope_label:
            r.fail(
                f"reconciliation.md not found at {recon_path} for sprint={scope_label} "
                f"— sprint-planning has not authored this sprint's reconciliation yet"
            )
        else:
            r.fail(f"reconciliation.md not found at {recon_path} — Phase 1 incomplete")
        return

    text = recon_path.read_text(encoding="utf-8")
    keep_count = _count_verdict(text, "KEEP")
    merge_count = _count_verdict(text, "MERGE")
    # A MERGE verdict folds its cluster into an existing KEEP cluster and mints
    # NO stub of its own — the reconciliation corpus says so in its own words
    # (claude-klabauter-strangler-2026-07-04 line 17; op-proportionality
    # § "Verdict-time MERGE — the four, and why each produces no stub of its
    # own"). Counting MERGE into `expected` inflated the bar by one per merged
    # cluster and made the pre-2026-08-29 PASS on claude-klabauter-strangler coincidental:
    # 4 live stubs happened to equal a wrong expected of 4, and neither number
    # was the truth. MERGE stays parsed and reported so a reconciliation file
    # that changes the convention fails loudly rather than silently.
    expected = keep_count

    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"
    live_records = query_records("handoff", data_root, where=where)
    arch_records = query_records("handoff-archived", data_root, where=where)

    if stub_filter is not None:
        live_records = [
            rec
            for rec in live_records
            if str(rec.get("frontmatter", {}).get("stub_id")) in stub_filter
        ]
        arch_records = [
            rec
            for rec in arch_records
            if str(rec.get("frontmatter", {}).get("stub_id")) in stub_filter
        ]

    live_count = len(live_records)
    arch_count = len(arch_records)

    # Coverage is a property of the STUB, not of the records carrying it. Under
    # DR-172 a succession deliberately leaves two records sharing one `stub_id`
    # — the archived predecessor and the live successor — permanently. Summing
    # record counts therefore over-counts by one per succession and fails this
    # audit forever after the first one, blocking Phase 3 dispatch on a roadmap
    # whose graph is sound. Count distinct stub_ids instead.
    # `stub_id` lives under the record's `frontmatter` mapping, not at top level —
    # same access path `_build_stub_descriptors` uses.
    all_records = [*live_records, *arch_records]
    stub_ids = {
        str(rec["frontmatter"]["stub_id"])
        for rec in all_records
        if rec.get("frontmatter", {}).get("stub_id")
    }
    # A record carrying no stub_id cannot be deduplicated against anything, so it
    # counts as its own unit — preserving the pre-DR-172 arithmetic for malformed
    # records rather than silently dropping them from coverage.
    untagged_count = sum(
        1 for rec in all_records if not rec.get("frontmatter", {}).get("stub_id")
    )
    stub_count = len(stub_ids) + untagged_count
    record_count = len(all_records)

    # A long-lived roadmap can legitimately mint stubs AFTER its Phase-1
    # reconciliation pass — an expansion cohort minted against a later
    # clusters.md section, or a stub sub-split into a family. Neither has a
    # verdict row, and neither can honestly be given one after the fact.
    # `post-reconciliation-stubs.md` is where that growth is declared, per stub,
    # with its provenance; declared stubs come out of the coverage count so the
    # equality below stays an equality for everything the pass actually covered.
    post_recon_path = recon_path.parent / POST_RECONCILIATION_FILENAME
    declared: Dict[str, str] = {}
    if post_recon_path.is_file():
        declared = parse_post_reconciliation_stubs(
            post_recon_path.read_text(encoding="utf-8")
        )
    if stub_filter is not None:
        declared = {k: v for k, v in declared.items() if k in stub_filter}

    orphaned = sorted(set(declared) - stub_ids)
    if orphaned:
        r.fail(
            f"Stub-coverage{label}: {post_recon_path} declares post-reconciliation "
            f"stub_id(s) [{' '.join(orphaned)}] that are not on disk for "
            f"roadmap_id={run_id} — a declaration outlived its stub and is now a "
            f"standing waiver. Remove the row or restore the stub."
        )
        return

    exempt_count = len(declared)
    stub_count -= exempt_count
    if exempt_count:
        label = f"{label} ({exempt_count} post-reconciliation stub(s) declared)"

    if stub_count == 0 and expected == 0:
        r.fail(
            f"Stub-coverage{label}: 0 stubs on disk AND 0 KEEP/MERGE verdicts parsed from "
            f"{recon_path} — this is the dead-gate signature (a real roadmap never "
            f"legitimately has both sides zero at Phase 2 close). Check DATA_ROOT "
            f"rooting and the reconciliation.md verdict table format — the verdict "
            f"cell must be BOLDED (`**KEEP**` / `**MERGE**`) in the 2nd or 3rd "
            f"column; a bare `KEEP` matches zero rows and is the most common way "
            f"to reach both-sides-zero. The prose fallback shape is "
            f"`Verdict: KEEP` / `Verdict: MERGE` (case-insensitive)."
        )
    elif stub_count != expected:
        r.fail(
            f"Stub-coverage{label} mismatch: {stub_count} stubs on disk across "
            f"{record_count} record(s) ({live_count} live + {arch_count} "
            f"archived), {expected} expected (KEEP={keep_count}; MERGE={merge_count} "
            f"folds into a KEEP cluster and mints no stub). "
            f"If the excess stubs postdate the reconciliation pass, declare them in "
            f"{post_recon_path}. See {recon_path}."
        )
    else:
        r.passed(
            f"Stub-coverage{label}: {stub_count} stubs across "
            f"{record_count} record(s) ({live_count} live + {arch_count} "
            f"archived) match {expected} verdicts (KEEP={keep_count}, MERGE={merge_count})."
        )


def _audit2_ready_to_fire_uniqueness(r: _Reporter, run_id: str, data_root: Path) -> None:
    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id} AND deployment_state=ready_to_fire"
    results = query_records("handoff", data_root, where=where)
    if not results:
        return

    slots: List[str] = []
    for rec in results:
        fm = rec["frontmatter"]
        sprint = fm.get("sprint")
        wave = fm.get("wave")
        s = "NO_SPRINT" if sprint is None else str(sprint)
        w = "NO_WAVE" if wave is None else str(wave)
        slots.append(f"s{s}:w{w}")

    seen: Dict[str, int] = {}
    for s in slots:
        seen[s] = seen.get(s, 0) + 1
    dupes = sorted(k for k, c in seen.items() if c > 1)

    if dupes:
        r.fail(
            f"Multiple ready_to_fire stubs in the same (roadmap_id, sprint, wave): "
            f"slots [{chr(10).join(dupes)}]. At most one ready_to_fire per (sprint, "
            f"wave) allowed."
        )
    else:
        r.passed(
            f"ready_to_fire uniqueness: {len(results)} ready stubs across distinct "
            f"(sprint, wave) slots."
        )


def _audit3_pm_gates_cross_reference(
    r: _Reporter,
    run_id: str,
    data_root: Path,
    pmg_path: Path,
    stub_filter: Optional[set] = None,
    scope_label: Optional[str] = None,
) -> None:
    """*stub_filter*/*scope_label* mirror `_audit1_stub_coverage`'s C4 hook:
    when *stub_filter* is given, only awaiting_gate stubs in that sprint's
    own cluster are cross-referenced against *pmg_path* (the sprint's own
    pm-gates.md, per the C4 body's "pm-gates.md moves to sprint-planning").
    Both default to None, reproducing the whole-roadmap behaviour unchanged."""
    label = f" (sprint={scope_label})" if scope_label else ""
    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id} AND deployment_state=awaiting_gate"
    results = query_records("handoff", data_root, where=where)
    if stub_filter is not None:
        results = [
            rec
            for rec in results
            if str(rec.get("frontmatter", {}).get("stub_id")) in stub_filter
        ]

    pm_stub_ids: List[str] = []
    for rec in results:
        fm = rec["frontmatter"]
        gd = str(fm.get("gate_dependency") or "")
        if gd.startswith("PM "):
            sid = fm.get("stub_id")
            if sid:
                pm_stub_ids.append(str(sid))

    if not pm_stub_ids:
        return

    # Bash-parity note: the oracle's PM_STUB_IDS accumulator starts empty and
    # appends " ${id}" per match, so its final value carries a LEADING space
    # (" id1 id2 id3") — interpolated after a literal ": ", this produces a
    # double space before the first id in both messages below. Preserved
    # byte-exactly (golden-diff parity), not "fixed", per port discipline.
    pm_stub_ids_joined = " " + " ".join(pm_stub_ids)

    if not pmg_path.is_file():
        r.fail(
            f"pm-gates.md missing at {pmg_path}{label} — found PM-prefixed gate_dependency "
            f"on stub_ids: {pm_stub_ids_joined}"
        )
        return

    pmg_content = pmg_path.read_text(encoding="utf-8")
    any_missing = False
    for stub in pm_stub_ids:
        if stub not in pmg_content:
            r.fail(
                f"Stub {stub} has gate_dependency starting 'PM ' but is not "
                f"cross-referenced in pm-gates.md{label}"
            )
            any_missing = True

    # Review: code-reviewer (P2) — the oracle gates this PASS line on the
    # GLOBAL accumulator (`[ "$EXIT_CODE" -eq 0 ] && pass ...`),
    # suppressing it once any prior audit has already failed. Gate on
    # r.exit_code (not just this audit's local any_missing) for stdout-shape
    # parity with the oracle.
    if not any_missing and r.exit_code == 0:
        r.passed(f"pm-gates.md cross-references{label}: all {pm_stub_ids_joined} present.")


def _audit4_pending_rows_reference_stubs(
    r: _Reporter, run_id: str, data_root: Path, pmg_path: Path
) -> None:
    if not pmg_path.is_file():
        return

    pmg_text = pmg_path.read_text(encoding="utf-8")
    pending_stubs = _parse_pending_stubs(pmg_text)
    if not pending_stubs:
        return

    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"
    live = query_records("handoff", data_root, where=where)
    arch = query_records("handoff-archived", data_root, where=where)
    all_stubs = {
        str(rec["frontmatter"].get("stub_id"))
        for rec in (live + arch)
        if rec["frontmatter"].get("stub_id")
    }

    for stub in pending_stubs:
        if stub not in all_stubs:
            r.fail(
                f"pm-gates.md has pending row for {stub} but no stub with that "
                f"stub_id exists in roadmap_id={run_id}"
            )


def _coerce_int_or_nan(value: Any) -> Optional[float]:
    """``int(value)`` if possible, else ``float("nan")`` for a non-numeric value.

    Review: code-reviewer (P1) — a malformed frontmatter value (e.g.
    ``number: TBD``) previously raised an uncaught ValueError here, which
    propagated to main()'s blanket except -> exit 3 "internal error",
    misclassifying an authoring typo as a tooling failure. The oracle's JS
    coerces via ``Number(...)`` -> NaN, which flows into the ordinary
    comparison logic and surfaces as a normal exit-1 dependency-order
    violation; ``nan`` mirrors that (nan comparisons are always False in
    Python too, same as JS).
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return float("nan")


def _build_stub_descriptors(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    stubs: List[Dict[str, Any]] = []
    for rec in results:
        fm = rec["frontmatter"]
        stub_id = fm.get("stub_id")
        if not stub_id:
            continue
        number = fm.get("number")
        sprint = fm.get("sprint")
        wave = fm.get("wave")
        blocked_by = fm.get("blocked_by")
        if isinstance(blocked_by, list):
            blocked_by = [str(b) for b in blocked_by]
        elif blocked_by:
            blocked_by = [str(blocked_by)]
        else:
            blocked_by = []
        stubs.append(
            {
                "stub_id": str(stub_id),
                "number": _coerce_int_or_nan(number),
                "sprint": _coerce_int_or_nan(sprint),
                "wave": _coerce_int_or_nan(wave),
                "blocked_by": blocked_by,
            }
        )
    return stubs


def _audit5_dependency_order(r: _Reporter, run_id: str, data_root: Path) -> None:
    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"
    live = query_records("handoff", data_root, where=where)
    arch = query_records("handoff-archived", data_root, where=where)
    all_results = live + arch

    if not all_results:
        r.passed(
            f"Audit 5: no roadmap-baton (spinoff-roadmap) stubs found for roadmap_id={run_id} — "
            f"dependency-order check skipped."
        )
        return

    stubs = _build_stub_descriptors(all_results)
    if not stubs:
        r.passed(
            "Audit 5: stubs found but none carried a parseable stub_id — "
            "dependency-order check skipped."
        )
        return

    result = check_dependency_order(stubs)
    stub_count = len(stubs)
    edge_count = sum(len(s["blocked_by"]) for s in stubs)

    if result["ok"]:
        r.passed(
            f"Audit 5: dependency-order invariant holds for roadmap_id={run_id} "
            f"({edge_count} edges checked across {stub_count} stubs)."
        )
        return

    for v in result["violations"]:
        if v["reason"] == "missing-sprint":
            r.fail(
                f"Audit 5: dependency-order violation — {v['from']} blocked_by "
                f"{v['to']} but sprint missing on {v['which']} endpoint"
            )
        elif v["reason"] == "number-order":
            r.fail(
                f"Audit 5: dependency-order violation — {v['from']} (N={v['numberA']}) "
                f"blocked_by {v['to']} (N={v['numberB']}) but number(dep) >= "
                f"number(dependent); expected number({v['to']}) < number({v['from']})"
            )
        elif v["reason"] == "same-or-inverted-slot":
            r.fail(
                f"Audit 5: dependency-order violation — {v['from']} "
                f"(sprint={v['slotA']['sprint']},wave={v['slotA']['wave']}) blocked_by "
                f"{v['to']} (sprint={v['slotB']['sprint']},wave={v['slotB']['wave']}) "
                f"but (sprint,wave) slot of dep is not strictly less than dependent"
            )
        else:
            r.fail(
                f"Audit 5: dependency-order violation — {v['from']} blocked_by "
                f"{v['to']} ({v['reason']})"
            )

    for u in result["unresolved"]:
        r.fail(
            f"Audit 5: unresolved blocked_by edge — {u['from']} depends on "
            f"{u['to']} which is not in the roadmap_id={run_id} stub set"
        )

    if result["cycle"]:
        r.fail(f"Audit 5: dependency cycle detected among stubs: {' → '.join(result['cycle'])}")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _audit5_cross_sprint_edge_order(
    r: _Reporter, spine: Dict[str, Any], run_id: str, sprint_id: str
) -> None:
    """C4 sprint-scoped Audit 5 — resolves cross-sprint edges by reading
    *spine* (`SPINE.md`'s parsed frontmatter) ALONE. No dependency on the
    descriptor-altitude edge entity C5b resolves: `spine['cross_sprint_edges']`
    is spine.schema.json's own required field, already present once C3b
    lands, so this is buildable without C5b's answer."""
    result = check_cross_sprint_edge_order(spine)
    edge_count = len(spine.get("cross_sprint_edges") or [])

    if result["ok"]:
        r.passed(
            f"Audit 5 (sprint-scoped, sprint={sprint_id}): cross-sprint edge order holds "
            f"for roadmap_id={run_id} ({edge_count} cross_sprint_edges checked against "
            f"spine sprints[] ordinal)."
        )
        return

    for v in result["violations"]:
        r.fail(
            f"Audit 5 (sprint-scoped, sprint={sprint_id}): cross-sprint edge violation — "
            f"sprint {v['from']!r} (ordinal={v['ordinalFrom']}) blocks sprint {v['to']!r} "
            f"(ordinal={v['ordinalTo']}) but ordinal(from) is not strictly less than "
            f"ordinal(to)"
        )

    for u in result["unresolved"]:
        r.fail(
            f"Audit 5 (sprint-scoped, sprint={sprint_id}): cross_sprint_edges entry "
            f"{u['from']!r} -> {u['to']!r} names an unresolved sprint id on its {u['which']!r} "
            f"side — not present in the spine's sprints[] for roadmap_id={run_id}"
        )

    if result["cycle"]:
        r.fail(
            f"Audit 5 (sprint-scoped, sprint={sprint_id}): cross-sprint edge cycle detected "
            f"among sprint descriptors: {' → '.join(result['cycle'])}"
        )


def _sprint_scoped_fail_summary(
    r: _Reporter, run_id: str, sprint_id: str
) -> Tuple[int, List[str], List[str]]:
    r.stdout_lines.append("")
    r.stdout_lines.append(
        f"audit-roadmap: one or more checks FAILED for roadmap_id={run_id} "
        f"sprint={sprint_id} — Phase 3 dispatch is blocked"
    )
    return r.exit_code, r.stdout_lines, r.stderr_lines


def _run_audit_sprint_scoped(
    run_id: str, data_root: Path, state_root: Path, sprint_id: str
) -> Tuple[int, List[str], List[str]]:
    """C4 sprint-scoped mode: Audit 1 counts KEEP+MERGE against the named
    sprint's own cluster subset (`spine['sprints'][].stubs`), Audit 3
    cross-references pm-gates.md WITHIN the sprint (both files now sourced
    from `state/roadmap/<run-id>/sprint-<ordinal>/`, mirroring C11's
    per-sprint `OVERVIEW.md` homing), and Audit 5 resolves cross-sprint
    edges from the spine record alone (`_audit5_cross_sprint_edge_order`).
    Audits 2 and 4 are whole-roadmap-only (ready_to_fire uniqueness and
    pm-gates pending-row reference are not named in the C4 body's sprint-
    scoped list) and are not run here.
    """
    r = _Reporter()
    spine_path = state_root / "roadmap" / run_id / "SPINE.md"
    spine = read_spine(spine_path)
    if spine is None:
        r.fail(
            f"SPINE.md not found or did not parse as a kind: roadmap-spine record at "
            f"{spine_path} — sprint-scoped audit requires the roadmap's sprint spine (C3b)"
        )
        return _sprint_scoped_fail_summary(r, run_id, sprint_id)

    sprint = _find_sprint_descriptor(spine, sprint_id)
    if sprint is None:
        known_ids = sorted(
            s.get("id")
            for s in (spine.get("sprints") or [])
            if isinstance(s, dict) and isinstance(s.get("id"), str)
        )
        r.fail(
            f"sprint_id={sprint_id!r} not found in {spine_path}'s sprints[] for "
            f"roadmap_id={run_id} (known sprint ids: {known_ids})"
        )
        return _sprint_scoped_fail_summary(r, run_id, sprint_id)

    ordinal = sprint.get("ordinal")
    sprint_dir = state_root / "roadmap" / run_id / f"sprint-{ordinal}"
    pmg_path = sprint_dir / "pm-gates.md"
    recon_path = sprint_dir / "reconciliation.md"
    # ABSENT is not `[]` — spine.schema.json fixes these as different facts and
    # the audit must not collapse them. Absent means `sprint-planning` has not run
    # for this sprint, so reconciliation.md and pm-gates.md legitimately do not
    # exist and Audits 1/3 have nothing to check: running them anyway lands the
    # both-sides-zero "dead-gate signature" fail, a FALSE violation of exactly the
    # kind this sprint-scoped mode exists to remove. `[]` means it DID run and
    # authored no stubs — a finding, and the dead-gate fail is then the correct
    # verdict. Audit 5 runs either way: cross-sprint edges live on the spine
    # record and are answerable whether or not any sprint has been planned.
    raw_stubs = sprint.get("stubs")
    if raw_stubs is None:
        r.passed(
            f"Stub-coverage[{sprint_id}]: skipped — sprints[].stubs is ABSENT, so "
            f"sprint-planning has not run for this sprint and its stubs are "
            f"legitimately not on disk. An authored-but-empty `stubs: []` is a "
            f"different fact and is NOT skipped."
        )
    else:
        sprint_stub_ids = {str(s) for s in raw_stubs}
        _audit1_stub_coverage(
            r, run_id, data_root, recon_path, stub_filter=sprint_stub_ids, scope_label=sprint_id
        )
        _audit3_pm_gates_cross_reference(
            r, run_id, data_root, pmg_path, stub_filter=sprint_stub_ids, scope_label=sprint_id
        )
    _audit5_cross_sprint_edge_order(r, spine, run_id, sprint_id)

    r.stdout_lines.append("")
    if r.exit_code == 0:
        r.stdout_lines.append(
            f"audit-roadmap: all checks passed for roadmap_id={run_id} sprint={sprint_id}"
        )
    else:
        r.stdout_lines.append(
            f"audit-roadmap: one or more checks FAILED for roadmap_id={run_id} "
            f"sprint={sprint_id} — Phase 3 dispatch is blocked"
        )

    return r.exit_code, r.stdout_lines, r.stderr_lines


def run_audit(
    run_id: str, data_root: Path, state_root: Path, sprint_id: Optional[str] = None
) -> Tuple[int, List[str], List[str]]:
    """Run the audits for *run_id*, accumulating failures. Never raises for
    an expected audit-fail — only an unexpected query error propagates.

    *sprint_id* is the C4 sprint-scoped mode's entry point: when given,
    delegates to `_run_audit_sprint_scoped` instead of running the
    whole-roadmap 5-audit set. Default None reproduces the original
    whole-roadmap behaviour unchanged.

    Returns (exit_code, stdout_lines, stderr_lines).
    """
    if sprint_id is not None:
        return _run_audit_sprint_scoped(run_id, data_root, state_root, sprint_id)

    pmg_path = state_root / "roadmap" / run_id / "pm-gates.md"
    recon_path = state_root / "roadmap" / run_id / "reconciliation.md"

    r = _Reporter()

    _audit1_stub_coverage(r, run_id, data_root, recon_path)
    _audit2_ready_to_fire_uniqueness(r, run_id, data_root)
    _audit3_pm_gates_cross_reference(r, run_id, data_root, pmg_path)
    _audit4_pending_rows_reference_stubs(r, run_id, data_root, pmg_path)
    _audit5_dependency_order(r, run_id, data_root)

    r.stdout_lines.append("")
    if r.exit_code == 0:
        r.stdout_lines.append(f"audit-roadmap: all checks passed for roadmap_id={run_id}")
    else:
        r.stdout_lines.append(
            f"audit-roadmap: one or more checks FAILED for roadmap_id={run_id} — "
            f"Phase 3 dispatch is blocked"
        )

    return r.exit_code, r.stdout_lines, r.stderr_lines


def main(argv: List[str]) -> int:
    """CLI entry: ``audit-roadmap <run-id> [--root <dir>] [--sprint <sprint-id>]``.

    ``--sprint`` selects the C4 sprint-scoped mode (Audits 1/3/5 scoped to
    one sprint descriptor's own cluster, reading `SPINE.md`) instead of the
    whole-roadmap 5-audit set.
    """
    if not argv:
        print("Usage: audit-roadmap <run-id> [--root <dir>] [--sprint <sprint-id>]", file=sys.stderr)
        print(
            "  Audits the roadmap with roadmap_id=<run-id> for Phase 2 close gates.",
            file=sys.stderr,
        )
        print(
            "  --sprint scopes Audits 1/3/5 to one sprint descriptor's own cluster "
            "(spine.schema.json's sprints[]).",
            file=sys.stderr,
        )
        return 2

    run_id = argv[0]
    err = validate_run_id(run_id)
    if err:
        print(err, file=sys.stderr)
        return 2

    rest = argv[1:]
    root_flag: Optional[str] = None
    sprint_flag: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--root":
            if i + 1 >= len(rest) or not rest[i + 1]:
                print("ERROR: --root requires a directory argument", file=sys.stderr)
                return 2
            root_flag = rest[i + 1]
            i += 2
            continue
        if tok == "--sprint":
            if i + 1 >= len(rest) or not rest[i + 1]:
                print("ERROR: --sprint requires a sprint-id argument", file=sys.stderr)
                return 2
            sprint_flag = rest[i + 1]
            i += 2
            continue
        print(f"ERROR: unexpected argument: {tok}", file=sys.stderr)
        return 2

    # Review: code-reviewer (P2) — resolve_data_root/RuntimeError is a
    # foreseeable USAGE/CONFIG error (no CLAUDE_KLABAUTER_ROOT env, no
    # repos.claude_klabauter machine-local entry), not this module's own
    # documented exit-3 "unexpected internal error (records-query failure)"
    # contract. The oracle's equivalent (coordinator_claude_klabauter_root rung 3) exits
    # 1 under `set -euo pipefail`, no special framing. Catch this rooting
    # failure separately from run_audit's exit-3 "hard error" path below.
    try:
        data_root = resolve_data_root(root_flag)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        state_root = data_root / "state"
        exit_code, stdout_lines, stderr_lines = run_audit(
            run_id, data_root, state_root, sprint_id=sprint_flag
        )
    except Exception as exc:  # noqa: BLE001 — fail-loud on any unexpected query/IO error
        print(
            f"FAIL: audit-roadmap: hard error while auditing roadmap_id={run_id} — "
            f"{exc.__class__.__name__}: {exc}; aborting to avoid dead-gate silent skip",
            file=sys.stderr,
        )
        return 3

    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
