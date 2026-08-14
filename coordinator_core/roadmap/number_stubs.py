"""
coordinator_core.roadmap.number_stubs — authoring-helper CLI for roadmap stub
topological linearization.

Purpose: thin CLI wrapper over ``topo_number`` (constructive) and
``check_dependency_order`` (verification) from ``coordinator_core.roadmap.graph``.
Provides two modes:

  Default mode  -- read a small edges file the author writes from clusters.md
                   BEFORE stubs exist; print a label -> (stub number, sprint,
                   wave) mapping table for the author to transcribe into stub
                   frontmatter. Stub numbers are emitted zero-padded to >=2
                   digits (width = max(2, digits of the highest stub number in
                   the set), e.g. 04, 12).

  --check mode  -- verify that stubs ALREADY ON DISK for a given roadmap_id have
                   dependency-monotone (sprint, wave) ordering.

  --state mode  -- EXTENSION (2026-07-24, not ported from the JS oracle). Resolve
                   the per-stub awaiting_gate/ready_to_fire/gate_dependency
                   readiness state for all stubs on disk for a given roadmap_id,
                   read straight from frontmatter, and print it. Replaces the
                   operator-navigated state-branch prose in
                   coordinator/skills/roadmap-planning/SKILL.md Step 3.1 — this
                   resolver only enumerates and prints; it does NOT automate the
                   gate-meaningfulness y/n/clarify judgment (Step 3.2), which
                   stays a human/skill-side decision on purpose.
                   Spec backlink: docs/plans/2026-07-24-computed-skills-b5-
                   planning-cluster.md § C12.

Port source: coordinator/bin/roadmap-number-stubs.js (DoE-claude), 387 LoC.
Domain vocabulary: stub, sprint, wave, topological linearization, blocked_by edge,
dependency-monotone, (sprint, wave) slot, roadmap_id, spinoff-roadmap.
Spec backlink: docs/plans/2026-06-28-roadmap-stub-numbering-dependency-order.md § C2
Port backlink: BIG_PORT Wave B, item roadmap-pair.

Invocation:
    python3 -m coordinator_core.roadmap.number_stubs <edges-file>
    python3 -m coordinator_core.roadmap.number_stubs --check <run-id>
    python3 -m coordinator_core.roadmap.number_stubs --state <run-id>

Exit codes (faithful to the oracle CLI):
    0 -- success (mapping printed, or --check passed / found nothing to check, or
         --state printed a readiness table -- including an empty one).
    1 -- dependency cycle detected (default mode) OR --check found violations
         (missing-sprint / number-order / same-or-inverted-slot / unresolved-edge
         / edge-source-divergence / cycle).
    2 -- CLI usage error (no args, missing edges file, unreadable/empty/malformed
         edges file, missing --check/--state <run-id>, malformed run-id, unknown
         flag) OR --check/--state mode's resolve_root() environment failure (not
         inside a git worktree / git unavailable) OR --check/--state mode could
         not establish roadmap state (both the live and archived roadmap-baton
         queries raised, or one raised and the other corpus's result set was
         empty -- an empty combined result set is only reportable as "nothing
         to check"/"no stubs found" when both queries actually succeeded).

Negative-spec / documented scope adaptations from the oracle:
  - ``resolve_root()`` deliberately does NOT reproduce the oracle's ``__dirname``
    -relative fallback heuristic (``path.resolve(__dirname, '../../../../')``) --
    that heuristic assumed a fixed directory depth between the script and the DoE
    plugin's ``bin/`` folder, a relationship that has no analog once the CLI is a
    claude-klabauter-resident Python module (``__file__`` lives under
    ``claude-klabauter/coordinator_core/roadmap/``, unrelated to any consumer repo's
    layout). The fallback branch only ever fired in a detached/non-git invocation
    context, which is not the documented invocation shape (the roadmap-planning
    skill always runs from a git-tracked repo) -- this port fails loud instead of
    silently resolving to a meaningless path. This is a scope adaptation forced by
    the port's changed location, not a silent behavior drop.
  - ``run_check_mode`` does NOT shell out to ``query-records.js`` / ``schema.js``
    (and therefore drops the oracle's own existence-guard-and-exit(2) for those two
    files) -- it calls ``coordinator_core.ops.ceremony.records_query.query_records``
    in-process instead, mirroring the same facade-seam simplification already
    established by ``coordinator_core.roadmap.audit`` (the audit-roadmap.sh port) for
    the identical enumerate-handoff-and-handoff-archived-by-where-clause query. There
    is no legacy-fallback path to port because there is no separate subprocess seam
    to be absent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.roadmap.graph import (
    RoadmapCycleError,
    check_dependency_order,
    topo_number,
)

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# `kind in (...)` term covering the canonical `roadmap-baton` value plus any
# still-live retired pre-rename spelling(s) — derived at import time from
# `baton_class.kind_values_for_canonical`, never a hand-authored pair. See
# that function's docstring and
# `coordinator_core/tests/test_baton_class_is_the_only_membership_set.py`.
_ROADMAP_BATON_KIND_WHERE = "kind in ({})".format(
    ",".join(kind_values_for_canonical("roadmap-baton"))
)

# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def resolve_root() -> str:
    """Resolve the invoking repo's root via ``git rev-parse --show-toplevel``.

    See module negative-spec above for why the oracle's ``__dirname``-relative
    fallback is not reproduced here.

    Raises:
        RuntimeError: not inside a git worktree (or git is unavailable).
    """
    out = show_toplevel()
    if not out:
        raise RuntimeError(
            "not inside a git worktree (git rev-parse --show-toplevel failed) -- "
            "run from a git-tracked repo"
        )
    return out


# ---------------------------------------------------------------------------
# Edges-file parsing
# ---------------------------------------------------------------------------


class _EdgesJsonEntryError(ValueError):
    """A JSON-form edges-file entry lacked a string ``from``/``to`` field.

    Carries the offending array index so callers can compose their own
    error-message policy (``parse_edges_file`` exits loud; the reconciliation
    path in ``_reconcile_edges_file_count`` degrades to "nothing to
    reconcile").
    """

    def __init__(self, index: int) -> None:
        self.index = index
        super().__init__(
            f'JSON entry {index} missing string "from" or "to" field'
        )


def _parse_edges_json_form(trimmed: str) -> List[Tuple[str, str]]:
    """Parse the JSON-array/object edges-file grammar into ``(from, to)``
    tuples. Shared token-splitting core for ``parse_edges_file`` and
    ``_reconcile_edges_file_count`` — each caller owns its own
    error-handling policy (exit-1 vs. return-None) around this.

    Raises:
        json.JSONDecodeError: *trimmed* is not valid JSON.
        _EdgesJsonEntryError: an entry lacks string ``from``/``to`` fields.
    """
    parsed = json.loads(trimmed)
    arr = parsed if isinstance(parsed, list) else [parsed]
    edges: List[Tuple[str, str]] = []
    for i, e in enumerate(arr):
        if not isinstance(e, dict) or not isinstance(e.get("from"), str) or not isinstance(
            e.get("to"), str
        ):
            raise _EdgesJsonEntryError(i)
        edges.append((e["from"], e["to"]))
    return edges


def _split_edge_line(line: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Split one already-stripped, non-blank, non-comment line-form edges-file
    line on ``<-``. Shared token-splitting core for ``parse_edges_file`` and
    ``_reconcile_edges_file_count``.

    Returns:
        ``None`` if the line has no ``<-`` (candidate isolated node).
        ``(lhs_or_None, rhs_or_None)`` if it does — either side is ``None``
        when empty after trimming (a malformed edge line); callers decide
        whether that is a warning-and-skip or a silent skip.
    """
    arrow_idx = line.find("<-")
    if arrow_idx == -1:
        return None
    lhs = line[:arrow_idx].strip() or None
    rhs = line[arrow_idx + 2 :].strip() or None
    return (lhs, rhs)


def parse_edges_file(content: str) -> Dict[str, List[Any]]:
    """Parse an edges file.

    Accepts two forms:
      (a) JSON array of ``{from, to}`` objects -- detected by a successful
          ``json.loads``.
      (b) Line form: ``"A <- B"`` meaning "A blocked_by B" (-> ``{from:'A',
          to:'B'}``). Blank lines and lines starting with ``#`` are ignored. A
          line with no ``<-`` and non-empty content is treated as an isolated
          node.

    Byte-parity port of roadmap-number-stubs.js's ``parseEdgesFile``.

    Returns:
        ``{"edges": [{"from": str, "to": str}], "isolatedNodes": [str]}``.
    """
    trimmed = content.strip()

    if trimmed.startswith("[") or trimmed.startswith("{"):
        try:
            edge_tuples = _parse_edges_json_form(trimmed)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"ERROR: file looks like JSON but failed to parse: {exc}\n")
            sys.exit(1)
        except _EdgesJsonEntryError as exc:
            sys.stderr.write(f"ERROR: file looks like JSON but failed to parse: {exc}\n")
            sys.exit(1)
        edges = [{"from": frm, "to": to} for frm, to in edge_tuples]
        return {"edges": edges, "isolatedNodes": []}

    edges = []
    isolated_nodes = []
    lines = content.split("\n")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        split = _split_edge_line(line)
        if split is None:
            isolated_nodes.append(line)
            continue
        lhs, rhs = split
        if not lhs or not rhs:
            sys.stderr.write(
                f'WARNING: malformed edge line {i + 1}: "{raw}" — skipped\n'
            )
            continue
        edges.append({"from": lhs, "to": rhs})
    return {"edges": edges, "isolatedNodes": isolated_nodes}


# ---------------------------------------------------------------------------
# Node-set derivation
# ---------------------------------------------------------------------------


def derive_nodes(edges: List[Dict[str, str]], isolated_nodes: List[str]) -> List[str]:
    """Derive the full node set from edges (union of all labels) plus any
    explicit isolated nodes. Preserves first-seen order for stable tie-breaking.
    Byte-parity port of roadmap-number-stubs.js's ``deriveNodes``.
    """
    seen = set()
    nodes: List[str] = []

    def add(label: str) -> None:
        if label not in seen:
            seen.add(label)
            nodes.append(label)

    for e in edges:
        add(e["to"])  # dependency (ships first) — add before dependent for topo order
        add(e["from"])  # dependent
    for n in isolated_nodes:
        add(n)
    return nodes


# ---------------------------------------------------------------------------
# Default mode — constructive linearization
# ---------------------------------------------------------------------------


def run_default_mode(edges_file_path: str) -> None:
    """Run topo_number on the parsed edges and print the mapping table.

    Table format (one row per label, topological order — dependencies before
    dependents): ``<label>  <padded-num>  <sprint>  <wave>``.
    """
    try:
        content = Path(edges_file_path).read_text(encoding="utf-8")
    except OSError as err:
        sys.stderr.write(f'ERROR: cannot read edges file "{edges_file_path}": {err}\n')
        sys.exit(2)

    parsed = parse_edges_file(content)
    edges = parsed["edges"]
    isolated_nodes = parsed["isolatedNodes"]
    nodes = derive_nodes(edges, isolated_nodes)

    if not nodes:
        sys.stderr.write(
            "ERROR: no nodes derived from edges file — is the file empty or malformed?\n"
        )
        sys.exit(2)

    try:
        result = topo_number(nodes, edges)
    except RoadmapCycleError as err:
        sys.stderr.write(
            f"ERROR: dependency cycle detected — cycle nodes: {', '.join(err.cycle)}\n"
        )
        sys.stderr.write("Fix the cycle before numbering stubs.\n")
        sys.exit(1)

    sys.stdout.write("# Roadmap stub linearization\n")
    sys.stdout.write("# Transcribe these values into stub frontmatter (number, sprint, wave).\n")
    sys.stdout.write("# Dependencies appear before their dependents.\n")
    sys.stdout.write(
        "# Stub numbers are zero-padded to ≥2 digits — use the padded form in "
        "frontmatter and filenames.\n"
    )
    sys.stdout.write("#\n")
    sys.stdout.write("# label                    stub#  sprint  wave\n")
    sys.stdout.write("# " + "-" * 52 + "\n")

    order = result["order"]
    number = result["number"]
    sprint_wave = result["sprintWave"]

    max_num = max(number[label] for label in order)
    width = max(2, len(str(max_num)))
    pad_label = 26
    for label in order:
        num = number[label]
        sw = sprint_wave[label]
        num_pad = str(num).zfill(width)
        label_pad = label + " " * (pad_label - len(label)) if len(label) < pad_label else label
        sys.stdout.write(f"{label_pad}  {num_pad}      {sw['sprint']}       {sw['wave']}\n")


# ---------------------------------------------------------------------------
# --check mode — dependency-order verification against stubs on disk
# ---------------------------------------------------------------------------


def _coerce_number(value: Any) -> Optional[float]:
    """``Number(value)``-equivalent coercion: preserves ``None``, else parses
    numerics, else NaN (never raises) — mirrors the JS oracle's implicit
    ``Number(...)`` coercion and Python nan-comparisons-are-always-False parity
    with JS NaN comparisons (see coordinator_core.roadmap.audit's
    ``_coerce_int_or_nan`` for the identical rationale, ported independently
    here for this module's self-containment).
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")


def _reconcile_edges_file_count(
    root: Path, run_id: str, stubs: List[Dict[str, Any]]
) -> Optional[int]:
    """Count edges declared in *run_id*'s on-disk edges file that resolve
    against stub_ids already materialized in *stubs*, for ``--check`` mode's
    edge-source-divergence reconciliation (AC1/AC4).

    Deliberately does NOT call ``parse_edges_file`` — that function
    ``sys.exit(1)``s on JSON-looking-but-malformed input, which from
    ``run_check_mode`` would hard-exit the whole process indistinguishable
    from "violations found" and bypass the query-failure exit-code
    discipline. A malformed, unreadable, absent, or ambiguous (N>1 glob
    match) edges file is therefore always treated as "nothing to reconcile"
    here — never a process exit.

    Spec backlink: pln-roadmap-dependency-graph-close-6192fb § C1.

    Returns:
        ``None`` if there is nothing to reconcile against (no file, multiple
        candidate files, unreadable file, or malformed content) — the caller
        must not claim a divergence in that case. Otherwise the count of
        declared edges whose both endpoints resolve to a ``stub_id`` already
        present in *stubs*.
    """
    edges_dir = root / "state" / "roadmap" / run_id
    matches = sorted(edges_dir.glob("*edges*.txt")) if edges_dir.is_dir() else []

    if not matches:
        return None

    if len(matches) > 1:
        sys.stdout.write(
            f"NOTE: {len(matches)} candidate edges files found under "
            f"{edges_dir} — cannot establish which is authoritative; "
            f"skipping edges.txt reconciliation.\n"
        )
        return None

    try:
        content = matches[0].read_text(encoding="utf-8")
    except OSError:
        return None

    stub_ids = {s["stub_id"] for s in stubs}
    trimmed = content.strip()
    raw_edges: List[Tuple[str, str]] = []

    if trimmed.startswith("[") or trimmed.startswith("{"):
        try:
            raw_edges = _parse_edges_json_form(trimmed)
        except (json.JSONDecodeError, _EdgesJsonEntryError):
            sys.stdout.write(
                f"NOTE: edges file {matches[0]} looks like JSON but could not "
                f"be parsed — skipping edges.txt reconciliation.\n"
            )
            return None
    else:
        for raw in content.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            split = _split_edge_line(line)
            if split is None:
                continue
            lhs, rhs = split
            if not lhs or not rhs:
                continue
            raw_edges.append((lhs, rhs))

    return sum(1 for frm, to in raw_edges if frm in stub_ids and to in stub_ids)


def _query_roadmap_baton_records(
    root: Path, where: str
) -> Tuple[List[Dict[str, Any]], bool, bool]:
    """Run the live + archived roadmap-baton ``query_records`` calls, degrading
    each to ``[]`` on exception (continue-with-what-we-have) while tracking
    whether each one raised.

    Review: staff-eng F3 — a query raising is NOT the same fact as a query
    returning zero rows. Both queries degrading to [] on any exception (kept
    below, for continue-with-what-we-have on a single-corpus failure) used to
    be indistinguishable from a genuinely empty, successfully-queried corpus.
    Tracking the raise itself lets ``_roadmap_state_error_exit`` tell "checked
    and clean" apart from "could not establish."

    Shared by ``run_check_mode`` and ``run_state_mode`` — see
    ``_roadmap_state_error_exit`` for the paired decision helper.
    Spec backlink: pln-roadmap-dependency-graph-close-6192fb § C7.

    Returns:
        ``(live + arch, live_raised, arch_raised)``.
    """
    live: List[Dict[str, Any]] = []
    live_raised = False
    try:
        live = query_records("handoff", root, where=where)
    except Exception as err:  # noqa: BLE001 — preserve continue-with-what-we-have
        live_raised = True
        sys.stderr.write(f"WARNING: query-records live query failed: {err}\n")

    arch: List[Dict[str, Any]] = []
    arch_raised = False
    try:
        arch = query_records("handoff-archived", root, where=where)
    except Exception as err:  # noqa: BLE001
        arch_raised = True
        sys.stderr.write(f"WARNING: query-records archived query failed: {err}\n")

    return live + arch, live_raised, arch_raised


def _roadmap_state_error_exit(
    live_raised: bool, arch_raised: bool, all_results: List[Dict[str, Any]]
) -> Optional[int]:
    """Decide whether the paired ``query_records`` calls establish roadmap
    state well enough to proceed.

    A query raising is not interchangeable with a query returning zero rows:
    if both raised, or if exactly one raised and the combined result set is
    empty (an empty result from only the surviving corpus does not mean "no
    stubs" — the failed corpus might have held them), roadmap state could not
    be established and the caller must exit(2) rather than treat it as a
    clean empty result.

    Shared by ``run_check_mode`` and ``run_state_mode``.
    Spec backlink: pln-roadmap-dependency-graph-close-6192fb § C7.

    Returns:
        ``2`` if roadmap state could not be established (having already
        written the ``ERROR:`` lines to stderr), else ``None`` — the caller
        should proceed.
    """
    if live_raised and arch_raised:
        sys.stderr.write("ERROR: could not establish roadmap state\n")
        sys.stderr.write(
            "  both the live and archived roadmap-baton queries raised — see WARNING "
            "lines above\n"
        )
        return 2

    if not all_results and (live_raised or arch_raised):
        failed_corpus = "live" if live_raised else "archived"
        sys.stderr.write("ERROR: could not establish roadmap state\n")
        sys.stderr.write(
            f"  the {failed_corpus} roadmap-baton query raised and the other corpus "
            f"returned no results — an empty combined result set here does not mean "
            f"\"no stubs\"\n"
        )
        return 2

    return None


def run_check_mode(run_id: str) -> int:
    """Enumerate stubs for *run_id* (live + archived spinoff-roadmap stubs),
    build the stubs array, and call ``check_dependency_order``. Returns the
    process exit code (0 pass / nothing-to-check, 1 violations found).
    """
    # Review: code-reviewer — F3, resolve_root()'s RuntimeError previously
    # propagated uncaught as a raw traceback instead of the clean
    # ERROR:-message + explicit-exit-code contract every other failure path
    # in this module follows.
    try:
        root = Path(resolve_root())
    except RuntimeError as err:
        sys.stderr.write(f"ERROR: {err}\n")
        return 2
    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"

    all_results, live_raised, arch_raised = _query_roadmap_baton_records(root, where)

    err_exit = _roadmap_state_error_exit(live_raised, arch_raised, all_results)
    if err_exit is not None:
        return err_exit

    if not all_results:
        sys.stdout.write(f"No roadmap-baton (spinoff-roadmap) stubs found for roadmap_id={run_id}\n")
        sys.stdout.write("(Nothing to check — create stubs first, then re-run --check.)\n")
        return 0

    if live_raised or arch_raised:
        missing_corpus = "live" if live_raised else "archived"
        sys.stderr.write(
            f"WARNING: results are partial — the {missing_corpus} roadmap-baton query "
            f"raised; this check only covers the corpus that succeeded\n"
        )

    stubs: List[Dict[str, Any]] = []
    for rec in all_results:
        fm = rec.get("frontmatter") or {}
        blocked_by_raw = fm.get("blocked_by")
        if isinstance(blocked_by_raw, list):
            blocked_by = list(blocked_by_raw)
        elif blocked_by_raw:
            blocked_by = [blocked_by_raw]
        else:
            blocked_by = []
        stubs.append(
            {
                "stub_id": fm.get("stub_id") or "",
                "number": _coerce_number(fm.get("number")),
                "sprint": _coerce_number(fm.get("sprint")),
                "wave": _coerce_number(fm.get("wave")),
                "blocked_by": blocked_by,
            }
        )

    # Review: code-reviewer — Finding 2. This check's whole purpose after this
    # diff is that it loses the ability to pass vacuously; reconciling
    # edges.txt against a stub_id set built from a partial corpus and then
    # printing OK would be that same vacuous pass wearing a different hat, so
    # skip the reconciliation outright (treat as "nothing to reconcile")
    # rather than widen the WARNING text and reconcile anyway.
    if live_raised or arch_raised:
        sys.stdout.write(
            "NOTE: skipping edges.txt reconciliation — results are partial "
            "(see WARNING above); the resolved stub_id set is incomplete, so "
            "an edge-source-divergence conclusion drawn against it would not "
            "be trustworthy.\n"
        )
        edges_txt_count = None
    else:
        edges_txt_count = _reconcile_edges_file_count(root, run_id, stubs)
    if edges_txt_count is not None:
        carried_count = sum(len(s["blocked_by"]) for s in stubs)
        if edges_txt_count > carried_count:
            sys.stderr.write(
                f"FAIL [edge-source-divergence]: edges.txt declares {edges_txt_count} "
                f"edge(s) between resolved stubs but stub frontmatter carries only "
                f"{carried_count} — the dependency graph is frontmatter-only; write "
                f"the reciprocal blocked_by via roadmap.link_stubs\n"
            )
            return 1

    result = check_dependency_order(stubs)

    if result["ok"]:
        sys.stdout.write(f"OK: dependency order is valid for roadmap_id={run_id}\n")
        sys.stdout.write(
            f"  {len(stubs)} stub(s) checked; all (sprint, wave) slots are "
            f"dependency-monotone.\n"
        )
        if edges_txt_count is not None:
            sys.stdout.write(
                f"  {edges_txt_count} edge(s) read from edges.txt for reconciliation "
                f"only — stub frontmatter blocked_by is the ONLY edge source this "
                f"check enforces; dependencies recorded elsewhere (e.g. edges.txt) "
                f"are not enforced.\n"
            )
        return 0

    if result["cycle"]:
        sys.stderr.write(
            f"FAIL: dependency cycle detected among stub_ids: {', '.join(result['cycle'])}\n"
        )

    for v in result["violations"]:
        if v["reason"] == "missing-sprint":
            sys.stderr.write(
                f"FAIL [missing-sprint]: edge {v['from']} blocked_by {v['to']} — "
                f"sprint absent on {v['which']}\n"
            )
        elif v["reason"] == "number-order":
            sys.stderr.write(
                f"FAIL [number-order]: {v['from']} (#{v['numberA']}) blocked_by "
                f"{v['to']} (#{v['numberB']}) — dependency must have a lower number\n"
            )
        elif v["reason"] == "same-or-inverted-slot":
            sys.stderr.write(
                f"FAIL [same-or-inverted-slot]: {v['from']} "
                f"(sprint={v['slotA']['sprint']}, wave={v['slotA']['wave']}) blocked_by "
                f"{v['to']} (sprint={v['slotB']['sprint']}, wave={v['slotB']['wave']}) — "
                f"dependency must occupy an earlier (sprint, wave) slot\n"
            )
        else:
            sys.stderr.write(f"FAIL [{v['reason']}]: {v['from']} → {v['to']}\n")

    for u in result["unresolved"]:
        sys.stderr.write(
            f"FAIL [unresolved-edge]: {u['from']} blocked_by {u['to']} — no stub "
            f"with that stub_id found in roadmap_id={run_id}\n"
        )

    return 1


# ---------------------------------------------------------------------------
# --state mode — awaiting_gate/ready_to_fire/gate_dependency readiness resolver
# ---------------------------------------------------------------------------


def run_state_mode(run_id: str) -> int:
    """Enumerate stubs for *run_id* (live + archived spinoff-roadmap stubs) and
    print each stub's ``deployment_state`` and ``gate_dependency`` (when
    ``awaiting_gate``), sorted by ``(sprint, wave, number)``.

    EXTENSION of the roadmap-number-stubs CLI family (not ported from the JS
    oracle) — see module docstring "--state mode" section. This is a pure
    read-and-print: it resolves readiness from frontmatter already on disk, it
    does NOT transition any stub's ``deployment_state`` and does NOT run the
    gate-meaningfulness y/n/clarify judgment (that stays skill-side, per Step
    3.2 of ``roadmap-planning/SKILL.md``).

    Returns the process exit code: 0 on success (including the empty-set case
    where both queries succeeded), 2 if ``resolve_root()`` fails (not inside a
    git worktree) or roadmap state could not be established (see
    ``_roadmap_state_error_exit``).
    """
    try:
        root = Path(resolve_root())
    except RuntimeError as err:
        sys.stderr.write(f"ERROR: {err}\n")
        return 2

    where = f"{_ROADMAP_BATON_KIND_WHERE} AND roadmap_id={run_id}"

    all_results, live_raised, arch_raised = _query_roadmap_baton_records(root, where)

    err_exit = _roadmap_state_error_exit(live_raised, arch_raised, all_results)
    if err_exit is not None:
        return err_exit

    if not all_results:
        sys.stdout.write(f"No roadmap-baton (spinoff-roadmap) stubs found for roadmap_id={run_id}\n")
        return 0

    if live_raised or arch_raised:
        missing_corpus = "live" if live_raised else "archived"
        sys.stderr.write(
            f"WARNING: results are partial — the {missing_corpus} roadmap-baton query "
            f"raised; this state resolution only covers the corpus that succeeded\n"
        )

    rows: List[Dict[str, Any]] = []
    for rec in all_results:
        fm = rec.get("frontmatter") or {}
        rows.append(
            {
                "stub_id": fm.get("stub_id") or "",
                "deployment_state": fm.get("deployment_state") or "",
                "sprint": _coerce_number(fm.get("sprint")),
                "wave": _coerce_number(fm.get("wave")),
                "gate_dependency": fm.get("gate_dependency") or "",
            }
        )

    def _sort_key(row: Dict[str, Any]) -> Tuple[float, float, str]:
        sprint = row["sprint"]
        wave = row["wave"]
        sprint_key = sprint if isinstance(sprint, (int, float)) else float("inf")
        wave_key = wave if isinstance(wave, (int, float)) else float("inf")
        return (sprint_key, wave_key, str(row["stub_id"]))

    rows.sort(key=_sort_key)

    sys.stdout.write(f"# Roadmap state-branch readiness — roadmap_id={run_id}\n")
    sys.stdout.write(
        "# stub_id                    deployment_state  sprint  wave  gate_dependency\n"
    )
    sys.stdout.write("# " + "-" * 70 + "\n")
    for row in rows:
        sprint_disp = "-" if row["sprint"] is None else str(row["sprint"])
        wave_disp = "-" if row["wave"] is None else str(row["wave"])
        gate_disp = row["gate_dependency"] if row["deployment_state"] == "awaiting_gate" else ""
        stub_pad = 26
        stub_label = row["stub_id"] + " " * (stub_pad - len(row["stub_id"])) if len(
            row["stub_id"]
        ) < stub_pad else row["stub_id"]
        state_pad = 17
        state_label = row["deployment_state"] + " " * (
            state_pad - len(row["deployment_state"])
        ) if len(row["deployment_state"]) < state_pad else row["deployment_state"]
        sys.stdout.write(
            f"{stub_label}  {state_label} {sprint_disp:<6}  {wave_disp:<4}  {gate_disp}\n"
        )

    ready_count = sum(1 for row in rows if row["deployment_state"] == "ready_to_fire")
    gated_count = sum(1 for row in rows if row["deployment_state"] == "awaiting_gate")
    sys.stdout.write(
        f"\n{len(rows)} stub(s): {ready_count} ready_to_fire, {gated_count} awaiting_gate.\n"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_USAGE = (
    "Usage:\n"
    "  roadmap-number-stubs <edges-file>      # linearize from edges file\n"
    "  roadmap-number-stubs --check <run-id>  # verify stubs on disk\n"
    "  roadmap-number-stubs --state <run-id>  # print per-stub readiness state\n"
)


def main(argv: List[str]) -> int:
    """Parse *argv* (already stripped of the program name, i.e. ``sys.argv[1:]``)
    and dispatch to the appropriate mode. Byte-parity port of
    roadmap-number-stubs.js's ``main``.
    """
    args = argv

    if not args:
        sys.stderr.write(_USAGE)
        return 2

    if args[0] in ("--help", "-h"):
        sys.stdout.write(_USAGE)
        return 0

    if args[0] == "--check":
        run_id = args[1] if len(args) > 1 else None
        if not run_id:
            sys.stderr.write("ERROR: --check requires a <run-id> argument\n")
            return 2
        if not _RUN_ID_RE.match(run_id):
            sys.stderr.write(
                f'ERROR: <run-id> must match ^[a-z0-9][a-z0-9-]*$ (got: "{run_id}")\n'
            )
            return 2
        return run_check_mode(run_id)

    if args[0] == "--state":
        run_id = args[1] if len(args) > 1 else None
        if not run_id:
            sys.stderr.write("ERROR: --state requires a <run-id> argument\n")
            return 2
        if not _RUN_ID_RE.match(run_id):
            sys.stderr.write(
                f'ERROR: <run-id> must match ^[a-z0-9][a-z0-9-]*$ (got: "{run_id}")\n'
            )
            return 2
        return run_state_mode(run_id)

    if args[0].startswith("--"):
        sys.stderr.write(f'ERROR: unknown flag "{args[0]}"\n')
        sys.stderr.write(_USAGE)
        return 2

    edges_file_path = args[0]
    run_default_mode(edges_file_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
