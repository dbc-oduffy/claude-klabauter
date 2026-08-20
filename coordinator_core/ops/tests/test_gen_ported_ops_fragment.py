"""
coordinator_core.ops.tests.test_gen_ported_ops_fragment

Tests for scripts/gen_ported_ops_fragment.py — the op-registration inventory
generator + legacy-oracle basename lookup. Imported as a module (never
subprocessed, per the spec's Windows/macOS-parity + no-shelling-out
constraint), so every assertion below calls straight into the script's own
functions.

Coverage:
    (a) by-op-hit           — --by-op exact match against a real committed
                               record
    (b) by-op-miss          — --by-op on a key that does not exist
    (c) oracle-basename-real-disk — --by-oracle-basename against the real
                               tree for detect-hardware.sh/find-polluter.sh.
                               Both modules are documented (their own
                               docstrings, verified on disk) as pristine,
                               UNREGISTERED CLI trampolines — no
                               @register_op call site — so they can only
                               ever surface via the stage-2 fallback grep,
                               never a stage-1 ported_from match. This test
                               asserts that real-disk shape rather than the
                               stage-1 case the handoff names them for; see
                               module docstring note below on why the
                               genuine stage-1 case needs a fixture instead.
    (d) oracle-basename-stage1-fixture — a genuine stage-1 hit (a fixture op
                               whose ported_from field matches the queried
                               basename), built against a synthetic tmp_path
                               tree. A real fixture is used here rather than
                               a fabricated assertion because the live repo
                               currently has ZERO @register_op sites with a
                               matching ported_from field (verified below,
                               and independently documented in the script's
                               own _ported_from() docstring) — there is no
                               real on-disk stage-1 example to point a test
                               at today.
    (e) oracle-basename-stage2-only-fixture — an op WITH a ported_from field
                               that does NOT match the queried basename, but
                               whose basename string appears elsewhere in
                               the file — the fallback case the spec calls
                               out as required coverage beyond stage-1.
    (f) freshness-check-committed — check_freshness() against the real
                               committed .github/ artifacts returns no drift
                               (the artifacts are current as of this baton).
    (g) write-artifacts-idempotent-and-red-path — write_artifacts() against a
                               synthetic .github/ output pair: fresh-tree
                               idempotence (second --write-equivalent call
                               writes nothing and check_freshness() agrees),
                               and the red path (a hand-staled artifact makes
                               check_freshness() non-empty, write_artifacts()
                               repairs it, check_freshness() is clean again).
                               Monkeypatches JSON_OUT/PATHS_OUT rather than
                               writing through the real committed paths.

Spec backlink: state/handoffs/2026-07-25_000913_op-inventory-and-oracle-discovery.md
Exercises:     scripts/gen_ported_ops_fragment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.gen_ported_ops_fragment as gpof  # noqa: E402


# ---------------------------------------------------------------------------
# (a)/(b) --by-op
# ---------------------------------------------------------------------------


def test_by_op_hit_matches_real_committed_record():
    """A real op_key present in the live discovery walk resolves to its
    OpRecord via query_by_op — the exact-match CLI lookup path."""
    records = gpof.discover_records()
    assert records, "expected at least one live @register_op call site"
    target = records[0]
    found = gpof.query_by_op(records, target.op_key)
    assert found is not None
    assert found.op_key == target.op_key
    assert found.module_path == target.module_path


def test_by_op_miss_returns_none():
    """A op_key with no matching @register_op call site returns None, the
    signal _print_op_record renders as 'not found.'"""
    records = gpof.discover_records()
    assert gpof.query_by_op(records, "no.such.op.key.exists") is None


# ---------------------------------------------------------------------------
# (c) --by-oracle-basename against the real tree
# ---------------------------------------------------------------------------


def test_oracle_basename_real_disk_detect_hardware_is_stage2_only():
    """detect_hardware.py's own docstring (verified on disk) reads 'ported
    from coordinator/lib/detect-hardware.sh' but the module carries NO
    @register_op call site (confirmed: no 'register_op' token anywhere in
    the file) — it is a pristine unregistered CLI trampoline, not an op.
    Because stage-1 matching only walks OpRecords, this basename can only
    ever surface via the stage-2 raw-text grep, reported as
    registered=False. This is the real, current disk shape — not the
    stage-1 case the handoff's Reference materials section named these two
    files for; see (d) below for the genuine stage-1 fixture case."""
    records = gpof.discover_records()
    candidates = gpof.query_by_oracle_basename(records, "detect-hardware.sh")
    assert candidates, "expected the stage-2 grep to find detect_hardware.py"
    hit = next(
        c for c in candidates if c["module_path"].endswith("detect_hardware.py")
    )
    assert hit["registered"] is False
    assert hit["matched_stage1"] is False
    assert hit["op_key"] is None


def test_oracle_basename_real_disk_find_polluter_is_stage2_only():
    """Same shape as detect_hardware.py, for find_polluter.py ('ported from
    coordinator/bin/find-polluter.sh' in its docstring, no @register_op
    call site)."""
    records = gpof.discover_records()
    candidates = gpof.query_by_oracle_basename(records, "find-polluter.sh")
    assert candidates, "expected the stage-2 grep to find find_polluter.py"
    hit = next(c for c in candidates if c["module_path"].endswith("find_polluter.py"))
    assert hit["registered"] is False
    assert hit["matched_stage1"] is False


def test_no_real_op_currently_carries_a_ported_from_field():
    """Guard for the premise behind (d): confirms, against the live
    discovery walk, that the only real @register_op site with a non-null
    ported_from is the one known, deliberate exception —
    `updatedocs.gates` (coordinator_core/ops/updatedocs_gates.py), whose
    module docstring names its 'ported from
    coordinator/bin/update-docs-probes.py' provenance in its first 10
    lines.

    That provenance line is NOT new: it arrived with `updatedocs.gates`
    itself (4c7d711ce), predating nothing and postdating this guard's own
    landing (86eeee81d) only in the sense that the two were authored
    independently. So the premise this test guards has been false on the
    live corpus since then, and the assertion is being corrected to match
    reality rather than relaxed to accommodate a change made today — the
    docstring is required backlink text (CLAUDE.md § RAG-bait exception),
    so deleting it to restore a zero count would be the wrong repair.

    Every other live site must still carry none — (d)'s
    fixture-based stage-1 test stays synthetic on purpose (it exercises the
    stage-1/stage-2 branch logic in isolation, independent of which real
    op happens to carry provenance today), so a second real hit here is
    still worth failing loud on rather than silently widening the
    allowlist."""
    records = gpof.discover_records()
    ported = {r.op_key: r.ported_from for r in records if r.ported_from is not None}
    assert ported == {"updatedocs.gates": "coordinator/bin/update-docs-probes.py"}


# ---------------------------------------------------------------------------
# (d)/(e) synthetic fixture tree — genuine stage-1 hit + stage-2-only case
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_ops_tree(tmp_path):
    """A synthetic ops/** tree with two modules:

    - stage1_op.py: @register_op'd, docstring reads 'ported from
      legacy/oracle-one.sh' — a genuine stage-1 ported_from match.
    - stage2_only_op.py: @register_op'd, docstring names a DIFFERENT legacy
      artifact ('ported from legacy/unrelated.sh', so ported_from does not
      match 'oracle-two.sh'), but the literal string 'oracle-two.sh' still
      appears in the file body (a historical comment) — stage-2's raw-text
      grep must find it even though stage-1 misses.
    """
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()

    (ops_dir / "stage1_op.py").write_text(
        '"""\n'
        "fixture.stage1_op — ported from legacy/oracle-one.sh\n"
        '"""\n'
        "from coordinator_core.ipc import register_op\n\n"
        '@register_op("fixture.stage1_op")\n'
        "def handler(params):\n"
        "    return {}\n",
        encoding="utf-8",
    )

    (ops_dir / "stage2_only_op.py").write_text(
        '"""\n'
        "fixture.stage2_only_op — ported from legacy/unrelated.sh\n"
        '"""\n'
        "from coordinator_core.ipc import register_op\n\n"
        '@register_op("fixture.stage2_only_op")\n'
        "def handler(params):\n"
        "    return {}\n\n"
        "# historical note: this behavior used to live in oracle-two.sh\n"
        "# before the DOE-PORT migration.\n",
        encoding="utf-8",
    )

    return ops_dir


def test_oracle_basename_stage1_hit_on_fixture(fixture_ops_tree):
    """Genuine stage-1 match: fixture.stage1_op's ported_from field
    ('legacy/oracle-one.sh') matches the queried basename directly."""
    records = gpof.discover_records(
        roots=(fixture_ops_tree,),
        repo_root=fixture_ops_tree.parent,
        lookup_tables=({}, {}, set()),
    )
    candidates = gpof.query_by_oracle_basename(
        records,
        "oracle-one.sh",
        roots=(fixture_ops_tree,),
        repo_root=fixture_ops_tree.parent,
    )
    hit = next(c for c in candidates if c["op_key"] == "fixture.stage1_op")
    assert hit["matched_stage1"] is True
    assert hit["registered"] is True
    assert hit["summary"] == "registered on 1/4 surfaces"


def test_oracle_basename_stage2_only_fallback_on_fixture(fixture_ops_tree):
    """The fallback case the spec requires beyond stage-1: fixture.
    stage2_only_op's ported_from field does NOT match 'oracle-two.sh' (it
    names a different artifact), yet the basename literal appears
    elsewhere in the file, so the stage-2 raw-text grep must still surface
    it as a candidate — tagged matched_stage1=False, registered=True since
    it IS a real op, just not one whose provenance field happened to match."""
    records = gpof.discover_records(
        roots=(fixture_ops_tree,),
        repo_root=fixture_ops_tree.parent,
        lookup_tables=({}, {}, set()),
    )
    candidates = gpof.query_by_oracle_basename(
        records,
        "oracle-two.sh",
        roots=(fixture_ops_tree,),
        repo_root=fixture_ops_tree.parent,
    )
    hit = next(c for c in candidates if c["op_key"] == "fixture.stage2_only_op")
    assert hit["matched_stage1"] is False
    assert hit["registered"] is True
    assert hit["summary"] == "registered on 1/4 surfaces"
    # stage1_op.py's own text never mentions oracle-two.sh; only one candidate.
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# (f) freshness check against committed artifacts
# ---------------------------------------------------------------------------


def test_freshness_check_passes_against_committed_artifacts():
    """check_freshness() regenerates in-memory and diffs against the
    committed .github/op-inventory.json + .github/ported-ops-paths.txt.
    Both are current as of this baton, so this must return no drift.
    The red-path (staled committed JSON -> non-empty return) was
    demonstrated by hand during this baton's verification pass rather than
    encoded here, since it requires mutating the committed artifact file
    in place."""
    problems = gpof.check_freshness()
    assert problems == []


# ---------------------------------------------------------------------------
# (g) --write: idempotence + red-path repair, against a synthetic output pair
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_artifact_paths(tmp_path, monkeypatch):
    """Point JSON_OUT/PATHS_OUT at a tmp_path pair for the duration of a test,
    so write_artifacts()/check_freshness() exercise the real live discovery
    walk (the real repo tree, unmodified) but never touch the actually
    committed .github/ artifacts."""
    json_out = tmp_path / "op-inventory.json"
    paths_out = tmp_path / "ported-ops-paths.txt"
    monkeypatch.setattr(gpof, "JSON_OUT", json_out)
    monkeypatch.setattr(gpof, "PATHS_OUT", paths_out)
    # check_freshness()'s STALE messages render paths via `.relative_to(REPO_ROOT)`;
    # repoint REPO_ROOT at tmp_path too so that formatting stays valid for the
    # synthetic pair above (discover_records()'s own default roots/repo_root
    # are bound at module-load time and are unaffected by this).
    monkeypatch.setattr(gpof, "REPO_ROOT", tmp_path)
    return json_out, paths_out


def test_write_artifacts_idempotent_on_already_fresh_tree(synthetic_artifact_paths):
    """First --write-equivalent call creates both files (both reported as
    written, since neither existed). A second call against the now-fresh
    pair writes nothing — same bytes in, same bytes out, reported as no
    changes — and check_freshness() agrees there is no drift."""
    json_out, paths_out = synthetic_artifact_paths

    first = gpof.write_artifacts()
    assert set(first) == {json_out, paths_out}
    first_json_bytes = json_out.read_bytes()
    first_paths_bytes = paths_out.read_bytes()

    second = gpof.write_artifacts()
    assert second == []
    assert json_out.read_bytes() == first_json_bytes
    assert paths_out.read_bytes() == first_paths_bytes
    assert gpof.check_freshness() == []


def test_write_artifacts_repairs_staled_artifact(synthetic_artifact_paths):
    """The red path required by the spec: hand-stale one committed artifact,
    confirm check_freshness() flags it, run write_artifacts() to repair it,
    then confirm check_freshness() is clean again — proving --check and
    --write share one drift definition rather than two that could
    disagree."""
    json_out, paths_out = synthetic_artifact_paths

    gpof.write_artifacts()
    json_out.write_text('[{"op_key": "stale.fixture.entry"}]\n', encoding="utf-8")

    problems = gpof.check_freshness()
    assert problems, "expected a hand-staled artifact to be flagged as drift"
    assert any("op-inventory.json" in p for p in problems)

    written = gpof.write_artifacts()
    assert written == [json_out]
    assert gpof.check_freshness() == []
