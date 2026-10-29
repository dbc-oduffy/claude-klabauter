"""The claim-index lookup-cost probe counts index reads, sink folds and bytes on a fixture corpus."""
import os
from datetime import datetime

from coordinator_core.benchmarks import claim_index_lookup_cost as cost
from coordinator_core.session import claim_index, touch_record


def _touch(base, sid, path):
    sink = os.path.join(str(base), sid, "touch-record.jsonl")
    ts = datetime.fromisoformat("2026-08-08T10:00:00+00:00").timestamp()
    touch_record.append_event(
        sink, session_id=sid, agent_id=None, verb="T", path=path, timestamp=ts
    )


def _corpus(tmp_path):
    base = str(tmp_path)
    for sid in ("sess-a", "sess-b", "sess-c"):
        _touch(base, sid, "shared/hot.py")
    _touch(base, "sess-a", "only/a.py")
    return base


def test_cold_lookup_folds_every_claimant_sink_and_warm_folds_none(tmp_path):
    base = _corpus(tmp_path)
    claim_index.lookup(["shared/hot.py"], sessions_dir=base)

    cold = cost.measure_lookup(base, ["shared/hot.py"], cold=True)
    warm = cost.measure_lookup(base, ["shared/hot.py"], cold=False)

    assert cold["claimants"] == 3 and cold["complete"]
    assert cold["sinks_folded"] == 3
    assert cold["fold_cache_misses"] >= 3 and cold["fold_bytes_read"] > 0
    assert cold["index_file_reads"] >= 1
    assert warm["fold_cache_misses"] == 0 and warm["fold_bytes_read"] == 0


def test_unclaimed_path_folds_no_sink(tmp_path):
    base = _corpus(tmp_path)
    claim_index.lookup(["only/a.py"], sessions_dir=base)

    out = cost.measure_lookup(base, ["never/claimed.py"], cold=True)

    assert out["claimants"] == 0 and out["sinks_folded"] == 0


def test_instrument_restores_the_seams(tmp_path):
    fold = touch_record.read_stream_claims_and_held_kind
    names = claim_index._read_index_names
    with cost.instrument(cost.Probe()):
        assert touch_record.read_stream_claims_and_held_kind is not fold
    assert touch_record.read_stream_claims_and_held_kind is fold
    assert claim_index._read_index_names is names


def test_concurrent_cold_lookups_report_per_thread_folds(tmp_path):
    base = _corpus(tmp_path)
    claim_index.lookup(["shared/hot.py"], sessions_dir=base)

    out = cost.measure_concurrent(base, ["shared/hot.py"], threads=4)

    assert out["threads"] == 4
    assert out["sinks_folded"] == 3
    assert out["fold_calls"] >= 3 * 4
    assert out["max_misses_on_one_sink"] >= 1


def test_run_and_census_on_fixture(tmp_path):
    base = _corpus(tmp_path)

    report = cost.run(base, None, 2, 2)

    assert report["census"]["live_sinks"] == 3
    assert report["census"]["record_bytes"] > 0
    assert report["paths"][0] == "shared/hot.py"
    assert "concurrent_cold" in report
