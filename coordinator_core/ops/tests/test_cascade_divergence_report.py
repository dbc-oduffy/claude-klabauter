"""
coordinator_core.ops.tests.test_cascade_divergence_report — C2 test surface
for `deliverable.cascade_divergence_report`
(docs/plans/2026-09-23-cascade-write-provenance.md).

Every fixture is a hand-built tmp_path corpus with a plain `.git` DIRECTORY
(never a real `git init`) — `main_worktree_root`/`locked_rmw`'s lock sidecar
resolution is pure path logic (see those functions' own docstrings), so no
test here needs an actual git repository. This file spawns NO process,
matching the op's own zero-spawn contract (case i below asserts it directly).

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_cascade_divergence_report.py -q
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import yaml

import coordinator_core.ops.cascade_divergence_report as report_mod

pytestmark = pytest.mark.cadence


def _init_repo(root: Path) -> Path:
    """A plain `.git` DIRECTORY — never a real `git init` (no process spawn).
    Returns the common dir (`root / ".git"`), the shape `_handler` requires."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    return root / ".git"


def _write_plan(
    root: Path,
    name: str,
    *,
    status: str = "implemented",
    deliverable_id: str = "",
    sizing_object: str = "",
    under_archive_specs: bool = False,
) -> Path:
    lines = ["---", f"status: {status}"]
    if deliverable_id:
        lines.append(f"deliverable_id: {deliverable_id}")
    if sizing_object:
        lines.append(f"sizing_object: {sizing_object}")
    lines += ["---", "", "# body", ""]
    text = "\n".join(lines)
    if under_archive_specs:
        target_dir = root / "archive" / "specs" / "2026-09"
    else:
        target_dir = root / "docs" / "plans"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_sizing(
    root: Path,
    name: str,
    *,
    status: str = "routed",
    deliverable_id: str = "dlv-example-000000",
    extra: str = "",
) -> Path:
    """A schema-valid whole-document sizing-object YAML body (per the vendored
    sizing-object schema's `required` list), mirroring
    `test_deliverable_cascade_kinds._sizing_body`."""
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
        f"deliverable_id: {deliverable_id}",
    ]
    text = "\n".join(lines) + "\n" + extra
    path = root / "state" / "sizings" / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_handoff(
    root: Path,
    name: str,
    *,
    deployment_state: str = "in_flight",
    deliverable_id: str = "dlv-example-000000",
    kind: str = "",
) -> Path:
    lines = ["---", f"deployment_state: {deployment_state}", f"deliverable_id: {deliverable_id}"]
    if kind:
        lines.append(f"kind: {kind}")
    lines += ["---", "", "# body", ""]
    path = root / "state" / "handoffs" / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _run(repo_root: Path) -> dict:
    return asyncio.run(report_mod._handler({}, repo_root))


# ---------------------------------------------------------------------------
# (a) The incident fixture: advanced then whole-file reverted to pre-advance bytes
# ---------------------------------------------------------------------------


def test_incident_fixture_reverted_sizing_is_reported(tmp_path):
    """C4 (a sibling chunk of this same plan) is the one that teaches
    `_advance_one_sizing` to stamp `advanced_by`/`advanced_at` — it is not in
    THIS chunk's write set (see the plan's `depends_on`). This fixture
    reproduces the incident's OBSERVABLE shape by hand instead: a sizing
    carrying the two provenance fields (as C4's write would leave them), then
    whole-file-reverted to bytes with neither — exactly the "advanced then
    stale-write-reverted" incident the bug row names, independent of which
    code path stamped the fields in the first place."""
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-incident-000000", sizing_object="state/sizings/s.yaml")
    sizing = _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-incident-000000")
    pre_bytes = sizing.read_bytes()
    assert b"advanced_by" not in pre_bytes

    advanced_text = sizing.read_text(encoding="utf-8").replace(
        "status: routed", "status: shipped"
    ) + "advanced_by: dlv-incident-000000\nadvanced_at: '2026-09-01T00:00:00Z'\n"
    sizing.write_text(advanced_text, encoding="utf-8")
    assert b"advanced_by" in sizing.read_bytes()

    # Whole-file revert to pre-advance bytes.
    sizing.write_bytes(pre_bytes)

    result = _run(common_dir)
    assert result["exit_code"] == 0
    assert result["scan_incomplete"] is False
    divs = [d for d in result["divergences"] if d["kind"] == "sizing"]
    assert len(divs) == 1
    entry = divs[0]
    assert entry["path"] == str(sizing)
    assert entry["terminal_source"] == str(repo / "docs" / "plans" / "p.md")
    assert entry["deliverable_id_matches"] is True
    assert entry["provenance"] is None  # revert erased the fields


# ---------------------------------------------------------------------------
# (b) An already-shipped sizing is not reported
# ---------------------------------------------------------------------------


def test_already_shipped_sizing_is_not_reported(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-shipped-000000", sizing_object="state/sizings/s.yaml")
    _write_sizing(repo, "s.yaml", status="shipped", deliverable_id="dlv-shipped-000000")

    result = _run(common_dir)
    assert result["divergences"] == []
    assert result["refused"] == []
    assert result["sources_scanned"]["sizings_read"] == 1


# ---------------------------------------------------------------------------
# (c) A candidate the predicate refuses (live-session claim; separately, superseded)
# ---------------------------------------------------------------------------


def test_live_claimed_sizing_is_refused_not_divergent(tmp_path):
    import coordinator_core.claim_state as claim_state_mod
    import coordinator_core.ops.deliverable_cascade as cascade_mod

    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-claim-000000", sizing_object="state/sizings/s.yaml")
    sizing = _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-claim-000000")

    session_id = "33333333-3333-3333-3333-333333333333"
    claim_dir = claim_state_mod.handoff_claim_dir(common_dir, Path("state/sizings/s.yaml"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-10T10:00:00Z", encoding="utf-8")

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True), \
        mock.patch.object(cascade_mod, "resolve_live_session_ids", return_value={session_id}):
        result = _run(common_dir)

    assert result["divergences"] == []
    assert len(result["refused"]) == 1
    refusal = result["refused"][0]
    assert refusal["path"] == str(sizing)
    assert "claimed by live session" in refusal["reason"]


def test_superseded_sizing_is_refused_not_divergent(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-superseded-000000", sizing_object="state/sizings/s.yaml")
    _write_sizing(repo, "s.yaml", status="superseded", deliverable_id="dlv-superseded-000000")

    result = _run(common_dir)
    assert result["divergences"] == []
    assert len(result["refused"]) == 1
    assert "live-and-advanceable" in result["refused"][0]["reason"]


# ---------------------------------------------------------------------------
# (d) A back-pointer whose deliverable_id differs is reported with deliverable_id_matches: false
# ---------------------------------------------------------------------------


def test_mismatched_deliverable_id_reports_matches_false(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-plan-000000", sizing_object="state/sizings/s.yaml")
    sizing = _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-different-000000")

    result = _run(common_dir)
    divs = [d for d in result["divergences"] if d["kind"] == "sizing"]
    assert len(divs) == 1
    assert divs[0]["path"] == str(sizing)
    assert divs[0]["deliverable_id_matches"] is False


# ---------------------------------------------------------------------------
# (e) A non-terminal handoff with the implemented plan's id is reported;
#     a spinoff lands in refused (leg d), not divergences
# ---------------------------------------------------------------------------


def test_non_terminal_handoff_matching_implemented_plan_is_reported(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-handoff-000000")
    handoff = _write_handoff(repo, "h.md", deployment_state="in_flight", deliverable_id="dlv-handoff-000000")

    result = _run(common_dir)
    divs = [d for d in result["divergences"] if d["kind"] == "handoff"]
    assert len(divs) == 1
    assert divs[0]["path"] == str(handoff)
    assert divs[0]["deliverable_id_matches"] is True
    assert divs[0]["terminal_source"] == str(repo / "docs" / "plans" / "p.md")


def test_spinoff_handoff_is_refused_via_leg_d(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-spinoff-000000")
    _write_handoff(
        repo, "h.md", deployment_state="in_flight", deliverable_id="dlv-spinoff-000000", kind="spinoff"
    )

    result = _run(common_dir)
    assert [d for d in result["divergences"] if d["kind"] == "handoff"] == []
    refused = [r for r in result["refused"] if r["kind"] == "handoff"]
    assert len(refused) == 1
    assert "spinoff" in refused[0]["reason"]


# ---------------------------------------------------------------------------
# (f) A field-level revert: provenance present, status non-terminal
# ---------------------------------------------------------------------------


def test_field_level_revert_reports_provenance(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-field-000000", sizing_object="state/sizings/s.yaml")
    _write_sizing(
        repo,
        "s.yaml",
        status="routed",
        deliverable_id="dlv-field-000000",
        extra="advanced_by: dlv-field-000000\nadvanced_at: '2026-09-01T00:00:00Z'\n",
    )

    result = _run(common_dir)
    divs = [d for d in result["divergences"] if d["kind"] == "sizing"]
    assert len(divs) == 1
    assert divs[0]["provenance"] == {
        "advanced_by": "dlv-field-000000",
        "advanced_at": "2026-09-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# (g) An archived plan under archive/specs/ is a source
# ---------------------------------------------------------------------------


def test_archived_plan_under_archive_specs_is_a_source(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(
        repo,
        "p.md",
        deliverable_id="dlv-archived-000000",
        sizing_object="state/sizings/s.yaml",
        under_archive_specs=True,
    )
    sizing = _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-archived-000000")

    result = _run(common_dir)
    assert result["sources_scanned"]["plans"] == 1
    assert result["sources_scanned"]["implemented_plans"] == 1
    divs = [d for d in result["divergences"] if d["kind"] == "sizing"]
    assert len(divs) == 1
    assert divs[0]["path"] == str(sizing)


# ---------------------------------------------------------------------------
# (h) A malformed sizing sets scan_incomplete and does not abort the scan
# ---------------------------------------------------------------------------


def test_malformed_sizing_sets_scan_incomplete_without_aborting(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p1.md", deliverable_id="dlv-bad-000000", sizing_object="state/sizings/bad.yaml")
    (repo / "state" / "sizings" / "bad.yaml").write_text("not: [a, mapping", encoding="utf-8")
    _write_plan(repo, "p2.md", deliverable_id="dlv-good-000000", sizing_object="state/sizings/good.yaml")
    good = _write_sizing(repo, "good.yaml", status="routed", deliverable_id="dlv-good-000000")

    result = _run(common_dir)
    assert result["scan_incomplete"] is True
    divs = [d for d in result["divergences"] if d["kind"] == "sizing"]
    assert len(divs) == 1
    assert divs[0]["path"] == str(good)


# ---------------------------------------------------------------------------
# (i) A zero-spawn assertion
# ---------------------------------------------------------------------------


def test_report_spawns_no_process(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(repo, "p.md", deliverable_id="dlv-nospawn-000000", sizing_object="state/sizings/s.yaml")
    _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-nospawn-000000")
    _write_handoff(repo, "h.md", deployment_state="in_flight", deliverable_id="dlv-nospawn-000000")

    def _raise(*args, **kwargs):
        raise AssertionError("subprocess.Popen must never be invoked by this op")

    with mock.patch.object(subprocess, "Popen", _raise):
        result = _run(common_dir)

    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# (j) No write: corpus bytes identical before and after
# ---------------------------------------------------------------------------


def test_report_writes_nothing(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    plan = _write_plan(repo, "p.md", deliverable_id="dlv-nowrite-000000", sizing_object="state/sizings/s.yaml")
    sizing = _write_sizing(repo, "s.yaml", status="routed", deliverable_id="dlv-nowrite-000000")
    handoff = _write_handoff(repo, "h.md", deployment_state="in_flight", deliverable_id="dlv-nowrite-000000")

    before = {p: p.read_bytes() for p in (plan, sizing, handoff)}
    _run(common_dir)
    after = {p: p.read_bytes() for p in (plan, sizing, handoff)}

    assert before == after


# ---------------------------------------------------------------------------
# (k) A back-pointer with no live file is counted and not reported; archive/sizings/ untouched
# ---------------------------------------------------------------------------


def test_non_live_back_pointer_counted_and_archive_sizings_untouched(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)
    _write_plan(
        repo,
        "p.md",
        deliverable_id="dlv-terminal-000000",
        sizing_object="state/sizings/does-not-exist.yaml",
    )

    archive_sizings = repo / "archive" / "sizings" / "2026-08"
    archive_sizings.mkdir(parents=True, exist_ok=True)
    decoy = archive_sizings / "does-not-exist.yaml"
    decoy.write_text("status: shipped\ndeliverable_id: dlv-terminal-000000\n", encoding="utf-8")
    decoy_before = decoy.read_bytes()

    result = _run(common_dir)
    assert result["sources_scanned"]["back_pointers_not_live"] == 1
    assert result["sources_scanned"]["sizings_read"] == 0
    assert result["divergences"] == []
    assert result["refused"] == []
    assert decoy.read_bytes() == decoy_before


# ---------------------------------------------------------------------------
# (l) Partition: every candidate appears in exactly one of divergences and refused
# ---------------------------------------------------------------------------


def test_partition_every_candidate_in_exactly_one_list(tmp_path):
    repo = tmp_path / "repo"
    common_dir = _init_repo(repo)

    _write_plan(repo, "p1.md", deliverable_id="dlv-mix-a", sizing_object="state/sizings/a.yaml")
    _write_sizing(repo, "a.yaml", status="routed", deliverable_id="dlv-mix-a")

    _write_plan(repo, "p2.md", deliverable_id="dlv-mix-b", sizing_object="state/sizings/b.yaml")
    _write_sizing(repo, "b.yaml", status="superseded", deliverable_id="dlv-mix-b")

    _write_plan(repo, "p3.md", deliverable_id="dlv-mix-c")
    _write_handoff(repo, "c.md", deployment_state="in_flight", deliverable_id="dlv-mix-c")

    _write_plan(repo, "p4.md", deliverable_id="dlv-mix-d")
    _write_handoff(repo, "d.md", deployment_state="in_flight", deliverable_id="dlv-mix-d", kind="spinoff")

    result = _run(common_dir)
    div_paths = {(d["kind"], d["path"]) for d in result["divergences"]}
    refused_paths = {(r["kind"], r["path"]) for r in result["refused"]}
    assert div_paths.isdisjoint(refused_paths)

    total_candidates = 4  # a (candidate), b (candidate refused), c (candidate), d (candidate refused)
    assert len(div_paths) + len(refused_paths) == total_candidates
    assert ("sizing", str(repo / "state" / "sizings" / "a.yaml")) in div_paths
    assert ("sizing", str(repo / "state" / "sizings" / "b.yaml")) in refused_paths
    assert ("handoff", str(repo / "state" / "handoffs" / "c.md")) in div_paths
    assert ("handoff", str(repo / "state" / "handoffs" / "d.md")) in refused_paths


# ---------------------------------------------------------------------------
# repo_root required
# ---------------------------------------------------------------------------


def test_missing_repo_root_returns_exit_code_1():
    result = asyncio.run(report_mod._handler({}, None))
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]
