"""coordinator/bin/tests/test_cartography_chunk_table_consume_gate_smoke.py — the
hermetic smoke test of `coordinator/bin/survey-consume-gate.py`, the
`/architecture-survey` Phase-0.5 `cartography.chunk_table` consume-gate.

WHY THIS EXISTS
    Every defect this guards is consumer-side: the producing op conformed to its
    wire contract while the consumer mis-read a conformant payload. Contract
    conformance cannot catch that; running the consumer against a recorded
    payload can.

TRANSPORT — real script, not a hand-ported mirror
    `survey-consume-gate.py` is a pure-stdlib script this process loads via
    `importlib.util.spec_from_file_location` (hyphenated filename) and drives
    directly: `_run_cartography_extraction()` is the actual implementation.
    Only the two I/O seams are stubbed — the op subprocess call (`_invoke_op`)
    and the artifact read (`_read_chunk_table_artifact`). The four consumer-side
    checks, the census-shaped mapping and the `oversized` mapping all run as the
    real code path.

HERMETIC
    The recorded fixture is `fixtures/cartography_chunk_table_recorded_artifact.json`
    (captured shape, not a live op call); negative cases prove each invariant
    rejects the defect shape it exists to catch. The live-op integration leg
    needs the DoE hook plane's engine-root resolver and is not carried here.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
RECORDED_ARTIFACT_PATH = FIXTURES_DIR / "cartography_chunk_table_recorded_artifact.json"
GATE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "survey-consume-gate.py"

# Matched to the recorded fixture's `systems` field.
CENSUS_BUCKETS = [
    {"bucketId": "alpha", "dirs": ["src/alpha"]},
    {"bucketId": "beta", "dirs": ["src/beta"]},
]

#: Sentinel distinguishing "no `oversized` key in the emitted reply at all"
#: from "an `oversized` key present with an empty/None value" — the AC4
#: unavailable-signal case depends on the key being absent, not falsy.
_NO_OVERSIZED_KEY = object()


def _load_recorded_fixture() -> dict:
    return json.loads(RECORDED_ARTIFACT_PATH.read_text(encoding="utf-8"))


def _load_gate_module() -> Any:
    spec = importlib.util.spec_from_file_location("survey_consume_gate", GATE_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survey_consume_gate"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_real_gate(
    mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    read_back: dict[str, Any],
    reported_counts: dict[str, Any],
    census_buckets: list[dict[str, Any]],
    oversized: Any = _NO_OVERSIZED_KEY,
) -> dict[str, Any]:
    """Drives the REAL `_run_cartography_extraction()` against a recorded
    artifact, stubbing only the op-subprocess and artifact-read seams —
    everything downstream (the four consumer-side checks, the census-shaped
    mapping, the `oversized` mapping) runs as the real, unmirrored script."""

    def fake_invoke_op(claude_klabauter_root, op, params):
        assert op == "cartography.chunk_table"
        emitted: dict[str, Any] = {
            "chunk_table_path": "fixture/leg-a-hermetic/chunk_table.json",
            "counts": reported_counts,
        }
        if oversized is not _NO_OVERSIZED_KEY:
            emitted["oversized"] = oversized
        return 0, emitted, None

    monkeypatch.setattr(mod, "_invoke_op", fake_invoke_op)
    monkeypatch.setattr(mod, "_read_chunk_table_artifact", lambda repo_root, chunk_table_path: read_back)

    config = {
        "repo_root": "unused-hermetic-repo-root",
        "claude_klabauter_root": "unused-hermetic-claude-klabauter-live-root",
        "run_id": "leg-a-hermetic",
        "census_buckets": census_buckets,
    }
    return mod._run_cartography_extraction(config)


@pytest.fixture
def gate_mod() -> Any:
    return _load_gate_module()


class TestLegAHermetic:
    """Recorded fixture only, unconditionally green, driven through the real
    `_run_cartography_extraction()`."""

    def test_positive_case_accepts_the_recorded_artifact(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        result = _run_real_gate(gate_mod, monkeypatch, fixture, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is True
        bucket_ids = sorted(entry["bucket_id"] for entry in result["censusShapedResults"])
        assert bucket_ids == ["alpha", "beta"]

    def test_uses_bucketed_total_not_tracked_total(self, gate_mod, monkeypatch):
        # Sanity precondition: the fixture's tracked_total and
        # bucketed_total genuinely differ, so a consumer that compared
        # against tracked_total by mistake would fail this fixture
        # differently than one comparing against bucketed_total.
        fixture = _load_recorded_fixture()
        assert fixture["counts"]["tracked_total"] != fixture["counts"]["bucketed_total"]

        # Falsifier: a broken consumer using tracked_total in place of
        # bucketed_total would reject this VALID artifact (a false
        # negative) because tracked_total (6) never equals the read-back
        # file-count sum (5). Prove the real check does NOT do that — it
        # passes against the real bucketed_total.
        broken_reported_counts = dict(fixture["counts"])
        broken_reported_counts["bucketed_total"] = broken_reported_counts["tracked_total"]
        result = _run_real_gate(gate_mod, monkeypatch, fixture, broken_reported_counts, CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "truncation cross-check" in result["declined_reason"]

    def test_rejects_truncated_bucket_sum_mismatch(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        truncated = copy.deepcopy(fixture)
        # Drop a file from the read-back payload without correcting the
        # independently-reported counts.bucketed_total — the exact
        # ~515KB-truncated-to-{}-with-ok:true failure mode this cross-check
        # exists to catch.
        truncated["buckets"]["alpha"]["files"].pop()
        result = _run_real_gate(gate_mod, monkeypatch, truncated, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "truncation cross-check" in result["declined_reason"]

    def test_rejects_directory_keyed_bucket_ids(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        renamed = copy.deepcopy(fixture)
        # Key the payload by directory name instead of the supplied bucket
        # id — a producer/consumer shape mismatch a naive "trust the keys"
        # consumer would silently accept.
        renamed["buckets"]["src/alpha"] = renamed["buckets"].pop("alpha")
        counts = dict(fixture["counts"])
        result = _run_real_gate(gate_mod, monkeypatch, renamed, counts, CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "does not match supplied CENSUS_BUCKETS" in result["declined_reason"]

    def test_rejects_file_outside_bucket_prefixes(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        contaminated = copy.deepcopy(fixture)
        contaminated["buckets"]["alpha"]["files"].append("src/beta/intruder.py")
        counts = dict(fixture["counts"])
        counts["bucketed_total"] = counts["bucketed_total"] + 1
        result = _run_real_gate(gate_mod, monkeypatch, contaminated, counts, CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "outside its supplied dirs prefixes" in result["declined_reason"]

    def test_rejects_forward_schema_version(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        forward = copy.deepcopy(fixture)
        forward["schema_version"] = gate_mod.CHUNK_TABLE_SCHEMA_VERSION + 1
        result = _run_real_gate(gate_mod, monkeypatch, forward, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "newer than this consumer supports" in result["declined_reason"]

    # ---- the `oversized` mapping -----------------------------------------

    def test_oversized_list_maps_true_onto_exactly_those_paths(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        oversized_path = fixture["buckets"]["alpha"]["files"][0]
        result = _run_real_gate(
            gate_mod, monkeypatch, fixture, fixture["counts"], CENSUS_BUCKETS, oversized=[oversized_path]
        )
        assert result["ok"] is True
        assert result["oversizedSignalAvailable"] is True
        assert result["oversizedCount"] == 1
        flagged = {
            entry["path"]
            for bucket in result["censusShapedResults"]
            for entry in bucket["files"]
            if entry["oversized"]
        }
        assert flagged == {oversized_path}

    def test_missing_oversized_key_reports_signal_unavailable(self, gate_mod, monkeypatch):
        fixture = _load_recorded_fixture()
        result = _run_real_gate(gate_mod, monkeypatch, fixture, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is True
        assert result["oversizedSignalAvailable"] is False
        assert result["oversizedCount"] == 0
        assert all(
            entry["oversized"] is False
            for bucket in result["censusShapedResults"]
            for entry in bucket["files"]
        )

    # ---- stdout reply vs. a well-formed artifact -------------------------

    def test_stdout_counts_disagreeing_with_a_well_formed_artifact_fails_truncation_cross_check(
        self, gate_mod, monkeypatch
    ):
        fixture = _load_recorded_fixture()
        disagreeing_counts = dict(fixture["counts"])
        disagreeing_counts["bucketed_total"] = disagreeing_counts["bucketed_total"] + 1
        result = _run_real_gate(gate_mod, monkeypatch, fixture, disagreeing_counts, CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "truncation cross-check" in result["declined_reason"]

    def test_the_original_fifteen_empty_buckets_shape_is_caught(self, gate_mod, monkeypatch):
        """The literal cockpit failure — buckets present but emptied out
        while the independently-reported count still claims the full
        artifact — is caught by the truncation cross-check."""
        fixture = _load_recorded_fixture()
        emptied = copy.deepcopy(fixture)
        emptied["buckets"]["alpha"]["files"] = []
        result = _run_real_gate(gate_mod, monkeypatch, emptied, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "truncation cross-check" in result["declined_reason"]

    def test_rejects_a_header_reporting_no_buckets_at_all(self, gate_mod, monkeypatch):
        """An artifact with an empty bucket map must refuse, not proceed to a
        schema-valid empty atlas: the read-back sum over an empty bucket map is
        0, which the truncation cross-check catches against any non-zero
        independently-reported bucketed_total."""
        fixture = _load_recorded_fixture()
        empty = copy.deepcopy(fixture)
        empty["buckets"] = {}
        result = _run_real_gate(gate_mod, monkeypatch, empty, fixture["counts"], CENSUS_BUCKETS)
        assert result["ok"] is False
        assert "truncation cross-check" in result["declined_reason"]


class TestCoverageFloorMeasurement:
    """The fifth consumer-side measurement — inventoried count vs. on-disk count
    per census bucket. Report-only: computes/logs `below_floor`, never declines.
    """

    def test_hermetic_ratio_and_below_floor_computation(self, gate_mod, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "alpha" / "b.py").write_text("x", encoding="utf-8")
        (tmp_path / "alpha" / "c.py").write_text("x", encoding="utf-8")
        (tmp_path / "alpha" / "d.py").write_text("x", encoding="utf-8")

        census_buckets = [{"bucketId": "alpha", "dirs": ["alpha"]}]
        census_shaped_results = [{"bucket_id": "alpha", "files": [{"path": "alpha/a.py"}]}]

        result = gate_mod._measure_bucket_coverage(str(tmp_path), census_buckets, census_shaped_results, 0.5)
        assert result["buckets"]["alpha"]["inventoried"] == 1
        assert result["buckets"]["alpha"]["on_disk"] == 4
        assert result["overall_ratio"] == pytest.approx(0.25)
        assert result["below_floor"] is True

    def test_report_only_never_declines_run_gate(self, gate_mod, monkeypatch, tmp_path):
        # A thin bucket (below any sane floor) must still leave the overall
        # gate ok — the measurement reports, it does not decide.
        (tmp_path / "alpha").mkdir()
        for i in range(10):
            (tmp_path / "alpha" / f"f{i}.py").write_text("x", encoding="utf-8")

        fixture = copy.deepcopy(_load_recorded_fixture())
        fixture["buckets"] = {"alpha": {"files": ["alpha/f0.py"]}}
        counts = dict(fixture["counts"])
        counts["bucketed_total"] = 1

        census_buckets = [{"bucketId": "alpha", "dirs": ["alpha"]}]
        result = _run_real_gate(gate_mod, monkeypatch, fixture, counts, census_buckets)
        assert result["ok"] is True

        coverage = gate_mod._measure_bucket_coverage(str(tmp_path), census_buckets, result["censusShapedResults"], 0.5)
        assert coverage["below_floor"] is True
