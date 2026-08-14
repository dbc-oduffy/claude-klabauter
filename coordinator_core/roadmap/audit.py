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
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.git.repo_root import show_toplevel
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
_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"


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
    """Rung-1.5 fast path: read ``<settings-home>/machine-local/.claude-klabauter-root``
    directly, no subprocess spawn.

    Review: code-reviewer (P2) — the oracle (coordinator-claude-klabauter-root.sh, rung
    1.5) checks this pointer file BEFORE the machine-local subprocess ladder,
    documented as a Windows hook-latency fix (per-invoke resolution avoiding a
    bash subprocess spawn). This port skipped straight to the subprocess-based
    ``_machine_local_get``, always paying the spawn cost. Mirrored here as a
    plain file read (no subprocess), falling through to rung 2 on absence.
    """
    try:
        ptr = machine_local_dir() / ".claude-klabauter-root"
        if ptr.is_file():
            val = ptr.read_text(encoding="utf-8").strip()
            if val:
                return val
    except OSError:
        return None
    return None


def _claude_klabauter_root() -> Optional[str]:
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
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


def _audit1_stub_coverage(r: _Reporter, run_id: str, data_root: Path, recon_path: Path) -> None:
    if not recon_path.is_file():
        r.fail(f"reconciliation.md not found at {recon_path} — Phase 1 incomplete")
        return

    text = recon_path.read_text(encoding="utf-8")
    keep_count = _count_verdict(text, "KEEP")
    merge_count = _count_verdict(text, "MERGE")
    expected = keep_count + merge_count

    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"
    live_count = len(query_records("handoff", data_root, where=where))
    arch_count = len(query_records("handoff-archived", data_root, where=where))
    stub_count = live_count + arch_count

    if stub_count == 0 and expected == 0:
        r.fail(
            f"Stub-coverage: 0 stubs on disk AND 0 KEEP/MERGE verdicts parsed from "
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
            f"Stub-coverage mismatch: {stub_count} stubs on disk ({live_count} live + "
            f"{arch_count} archived), {expected} expected (KEEP={keep_count} + "
            f"MERGE={merge_count}). See {recon_path}."
        )
    else:
        r.passed(
            f"Stub-coverage: {stub_count} stubs ({live_count} live + {arch_count} "
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
    r: _Reporter, run_id: str, data_root: Path, pmg_path: Path
) -> None:
    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id} AND deployment_state=awaiting_gate"
    results = query_records("handoff", data_root, where=where)

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
            f"pm-gates.md missing at {pmg_path} — found PM-prefixed gate_dependency "
            f"on stub_ids: {pm_stub_ids_joined}"
        )
        return

    pmg_content = pmg_path.read_text(encoding="utf-8")
    any_missing = False
    for stub in pm_stub_ids:
        if stub not in pmg_content:
            r.fail(
                f"Stub {stub} has gate_dependency starting 'PM ' but is not "
                f"cross-referenced in pm-gates.md"
            )
            any_missing = True

    # Review: code-reviewer (P2) — the oracle gates this PASS line on the
    # GLOBAL accumulator (`[ "$EXIT_CODE" -eq 0 ] && pass ...`),
    # suppressing it once any prior audit has already failed. Gate on
    # r.exit_code (not just this audit's local any_missing) for stdout-shape
    # parity with the oracle.
    if not any_missing and r.exit_code == 0:
        r.passed(f"pm-gates.md cross-references: all {pm_stub_ids_joined} present.")


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


def run_audit(run_id: str, data_root: Path, state_root: Path) -> Tuple[int, List[str], List[str]]:
    """Run all 5 audits for *run_id*, accumulating failures. Never raises for
    an expected audit-fail — only an unexpected query error propagates.

    Returns (exit_code, stdout_lines, stderr_lines).
    """
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
    """CLI entry: ``audit-roadmap <run-id> [--root <dir>]``."""
    if not argv:
        print("Usage: audit-roadmap <run-id> [--root <dir>]", file=sys.stderr)
        print(
            "  Audits the roadmap with roadmap_id=<run-id> for Phase 2 close gates.",
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
        exit_code, stdout_lines, stderr_lines = run_audit(run_id, data_root, state_root)
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
