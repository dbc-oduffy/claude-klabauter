"""
Tests for coordinator_core.claims_emit — the atomic claims.json + sidecar
writer.

Spec backlink:
  archive/specs/2026-08/2026-08-06-claims-emit-writer-atomic-pair.md — chunk C4.
  (Review: coordinator:code-reviewer — plan was archived by
  fleet.archive_completed_plans before this diff's slice landed; the
  docs/plans/ path 404s.)

Covers AC1-AC10. AC11 (the sidecar-stays-out-of-glob tripwire) is pinned
separately in coordinator_core/ops/tests/test_claims_sidecar_stays_out_of_glob.py.
"""
from __future__ import annotations

import json
import os

import pytest

from coordinator_core import claims_emit
from coordinator_core.frontmatter import schema_drift_watch

VALID_CLAIM = {
    "id": "c1",
    "claim_text": "Widgets are load-bearing.",
    "confidence": "HIGH",
}

VALID_RAN_AT = "2026-08-06T12:00:00+00:00"
VALID_PIPELINE = "linux-x86_64"
VALID_PRODUCER = "some-producer"


def _emit(tmp_path, claims=None, **overrides):
    kwargs = dict(
        claims=[VALID_CLAIM] if claims is None else claims,
        producer=VALID_PRODUCER,
        ran_at=VALID_RAN_AT,
        pipeline=VALID_PIPELINE,
        out_stem=str(tmp_path / "run1"),
    )
    kwargs.update(overrides)
    return claims_emit.emit_claims(**kwargs)


def test_ac1_writes_exactly_two_files_and_nothing_else(tmp_path):
    before = set(tmp_path.iterdir())
    assert before == set()

    rc = _emit(tmp_path)

    assert rc == claims_emit.EXIT_OK
    after = set(tmp_path.iterdir())
    expected = {tmp_path / "run1.claims.json", tmp_path / "run1.claims.meta.json"}
    assert after == expected


def test_ac2_claims_file_is_bare_top_level_array(tmp_path):
    rc = _emit(tmp_path)
    assert rc == claims_emit.EXIT_OK

    with open(tmp_path / "run1.claims.json", encoding="utf-8") as f:
        loaded = json.load(f)
    assert isinstance(loaded, list)


def test_ac3_valid_record_passes_per_record_schema_validation(tmp_path):
    rc = _emit(tmp_path, claims=[VALID_CLAIM])
    assert rc == claims_emit.EXIT_OK


def test_ac3_invalid_record_rejected_with_producer_failure_exit(tmp_path):
    bad_claim = {"id": "c1", "confidence": "NOT-A-REAL-CONFIDENCE-LEVEL"}
    rc = _emit(tmp_path, claims=[bad_claim])
    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    # No partial pair should be left behind by a rejected record either.
    assert not (tmp_path / "run1.claims.json").exists()
    assert not (tmp_path / "run1.claims.meta.json").exists()


def test_ac4_sidecar_is_object_with_at_least_ran_at_and_pipeline(tmp_path):
    rc = _emit(tmp_path)
    assert rc == claims_emit.EXIT_OK

    with open(tmp_path / "run1.claims.meta.json", encoding="utf-8") as f:
        sidecar = json.load(f)
    assert isinstance(sidecar, dict)
    assert sidecar["ran_at"] == VALID_RAN_AT
    assert sidecar["pipeline"] == VALID_PIPELINE


@pytest.mark.parametrize(
    "bad_ran_at",
    [
        "2026-08-06T12:00:00",  # naive - no offset
        "2026-08-06",  # date-only
        "",  # empty
    ],
    ids=["naive", "date-only", "empty"],
)
def test_ac5_ran_at_rejected_variants_exit_2(tmp_path, bad_ran_at):
    rc = _emit(tmp_path, ran_at=bad_ran_at)
    assert rc == claims_emit.EXIT_INVALID_INVOCATION
    assert not (tmp_path / "run1.claims.json").exists()
    assert not (tmp_path / "run1.claims.meta.json").exists()


@pytest.mark.parametrize(
    "bad_pipeline",
    ["", "   ", None],
    ids=["empty", "whitespace", "none"],
)
def test_ac6_blank_pipeline_exits_2(tmp_path, bad_pipeline):
    rc = _emit(tmp_path, pipeline=bad_pipeline)
    assert rc == claims_emit.EXIT_INVALID_INVOCATION
    # Review: coordinator:code-reviewer — pin non-existence of both files for
    # every blank-pipeline variant (not just the "" case, see the sibling
    # test below), so a regression that writes a partial pair before hitting
    # the pipeline check would fail loudly here.
    assert not (tmp_path / "run1.claims.json").exists()
    assert not (tmp_path / "run1.claims.meta.json").exists()


def test_ac6_blank_pipeline_never_becomes_producer_slug(tmp_path):
    # If a blank pipeline were silently defaulted to the producer slug, this
    # invocation would succeed (exit 0) and the sidecar would carry
    # pipeline == producer. Assert the negative directly, not just the exit
    # code, so a future refactor that both defaults AND happens to still
    # exit 2 in some other case cannot slip past a positive-only check.
    rc = claims_emit.emit_claims(
        claims=[VALID_CLAIM],
        producer=VALID_PRODUCER,
        ran_at=VALID_RAN_AT,
        pipeline="",
        out_stem=str(tmp_path / "run1"),
    )
    assert rc == claims_emit.EXIT_INVALID_INVOCATION
    meta_path = tmp_path / "run1.claims.meta.json"
    assert not meta_path.exists()


def test_ac7_exit_0_on_success(tmp_path):
    assert _emit(tmp_path) == claims_emit.EXIT_OK


def test_ac7_exit_1_on_producer_side_failure(tmp_path):
    bad_claim = {"confidence": "GARBAGE-VALUE"}
    assert _emit(tmp_path, claims=[bad_claim]) == claims_emit.EXIT_PRODUCER_FAILURE


def test_ac7_exit_2_on_invalid_invocation(tmp_path):
    assert _emit(tmp_path, producer="") == claims_emit.EXIT_INVALID_INVOCATION


def test_ac7_schema_failure_is_exit_1_not_exit_2(tmp_path):
    # The exit-code split is deliberate and load-bearing: a per-record schema
    # validation failure on an otherwise well-formed invocation is exit 1
    # (producer-side), never exit 2 (invocation-side) — pin the split
    # explicitly so a later refactor cannot quietly collapse the two.
    bad_claim = {"confidence": "GARBAGE-VALUE"}
    rc = _emit(tmp_path, claims=[bad_claim])
    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    assert rc != claims_emit.EXIT_INVALID_INVOCATION


def test_ac8_mid_pair_failure_leaves_neither_file_and_no_temps(tmp_path, monkeypatch):
    claims_path = tmp_path / "run1.claims.json"
    meta_path = tmp_path / "run1.claims.meta.json"
    real_replace = os.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        call_count["n"] += 1
        # Review: coordinator:code-reviewer — assert the semantic identity of
        # each ordinal call (not just count it), so a future reorder inside
        # _write_atomic_pair fails this test loudly at the wrong-step
        # assertion rather than silently pinning a different step than the
        # one this test's name claims to exercise.
        if call_count["n"] == 1:
            assert str(dst) == str(claims_path)
        elif call_count["n"] == 2:
            assert str(dst) == str(meta_path)
            raise OSError("simulated failure on second os.replace")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims_emit.os, "replace", flaky_replace)

    rc = _emit(tmp_path)

    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    assert call_count["n"] == 2
    assert not (tmp_path / "run1.claims.json").exists()
    assert not (tmp_path / "run1.claims.meta.json").exists()
    # No temp files left behind either.
    leftovers = [p for p in tmp_path.iterdir()]
    assert leftovers == []


def test_ac9_output_path_isolation_diff_is_exactly_the_two_files(tmp_path):
    sibling_dir = tmp_path / "untouched-sibling"
    sibling_dir.mkdir()
    (sibling_dir / "leave-me-alone.txt").write_text("hi", encoding="utf-8")

    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    rc = _emit(tmp_path)
    assert rc == claims_emit.EXIT_OK

    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    added = set(after) - set(before)
    changed = {p for p in set(after) & set(before) if after[p] != before[p]}
    removed = set(before) - set(after)

    assert added == {tmp_path / "run1.claims.json", tmp_path / "run1.claims.meta.json"}
    assert changed == set()
    assert removed == set()


def test_ac10_vendored_schema_appears_in_vendored_schema_paths():
    paths = schema_drift_watch.vendored_schema_paths()
    names = {p.name for p in paths}
    assert "research-claim.schema.json" in names


def test_a_failed_re_run_leaves_the_pre_existing_pair_intact(tmp_path, monkeypatch):
    # A failed emission must be a no-op on disk. A re-run over a stem that
    # already carries a good pair from a prior successful run, where the
    # SECOND os.replace (sidecar) fails, must restore the pre-existing
    # claims.json byte-for-byte rather than destroying it — leaving a
    # claims-less stale sidecar is strictly worse than either a clean no-op
    # or a clean full-destroy.
    first_rc = _emit(tmp_path)
    assert first_rc == claims_emit.EXIT_OK
    claims_path = tmp_path / "run1.claims.json"
    meta_path = tmp_path / "run1.claims.meta.json"
    assert claims_path.exists()
    assert meta_path.exists()
    original_claims_bytes = claims_path.read_bytes()
    original_meta_bytes = meta_path.read_bytes()

    real_replace = os.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        call_count["n"] += 1
        # Order for a re-run over a fully-occupied stem: (1) back up the
        # existing claims.json, (2) back up the existing meta.json, (3) land
        # the new claims.json, (4) land the new sidecar — fail on the last.
        # Review: coordinator:code-reviewer — assert semantic identity per
        # call (src for the backup steps, whose dst is an unpredictable temp
        # name; dst for the landing steps) so a reorder fails loudly here
        # instead of silently mistargeting.
        if call_count["n"] == 1:
            assert str(src) == str(claims_path)
        elif call_count["n"] == 2:
            assert str(src) == str(meta_path)
        elif call_count["n"] == 3:
            assert str(dst) == str(claims_path)
        elif call_count["n"] == 4:
            assert str(dst) == str(meta_path)
            raise OSError("simulated failure on second os.replace, re-run case")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims_emit.os, "replace", flaky_replace)

    second_rc = _emit(tmp_path, claims=[{**VALID_CLAIM, "id": "different-claim-should-not-land"}])

    assert second_rc == claims_emit.EXIT_PRODUCER_FAILURE
    # Both pre-existing files survive, restored byte-for-byte — not just
    # present, but unchanged from before the failed re-run.
    assert claims_path.exists()
    assert meta_path.exists()
    assert claims_path.read_bytes() == original_claims_bytes
    assert meta_path.read_bytes() == original_meta_bytes
    # No temp/backup files left behind either.
    leftovers = [p for p in tmp_path.iterdir() if p not in (claims_path, meta_path)]
    assert leftovers == []


def test_a_failed_restore_never_destroys_the_caller_s_only_surviving_copy(tmp_path, monkeypatch):
    # The unwind's own failure path. Regression pin for the A-writer-module P1
    # finding: a shared `finally` once discarded the backup unconditionally, so
    # a restore that itself raised left the backup deleted AND the destination
    # unrestored — destroying the last surviving copy of the caller's original.
    # The invariant is that a backup may only be discarded once its original has
    # been restored, or once the emission has fully succeeded; a restore failure
    # must surface loudly and leave the bytes on disk.
    first_rc = _emit(tmp_path)
    assert first_rc == claims_emit.EXIT_OK
    claims_path = tmp_path / "run1.claims.json"
    meta_path = tmp_path / "run1.claims.meta.json"
    original_claims_bytes = claims_path.read_bytes()

    real_replace = os.replace
    call_count = {"n": 0}

    def doubly_flaky_replace(src, dst, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 4:
            # Landing the new sidecar fails, entering the unwind.
            assert str(dst) == str(meta_path)
            raise OSError("simulated failure landing the sidecar")
        if call_count["n"] == 5:
            # The unwind's restore of the backed-up claims.json fails too.
            assert str(dst) == str(claims_path)
            raise OSError("simulated failure restoring the claims backup")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims_emit.os, "replace", doubly_flaky_replace)

    rc = _emit(tmp_path, claims=[{**VALID_CLAIM, "id": "should-not-land"}])

    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    # The original bytes must still exist SOMEWHERE on disk — restored to the
    # destination if that worked, else surviving as the untouched backup. What
    # must never happen is that they exist nowhere.
    survivors = [
        p for p in tmp_path.iterdir()
        if p.is_file() and p.read_bytes() == original_claims_bytes
    ]
    assert survivors, (
        "the caller's original claims.json was destroyed by a failed restore — "
        "no file under the stem's directory still carries its bytes"
    )


def test_failed_re_run_restores_when_only_claims_json_pre_exists(tmp_path, monkeypatch):
    claims_path = tmp_path / "run1.claims.json"
    meta_path = tmp_path / "run1.claims.meta.json"
    original_claims_bytes = b'[{"pre-existing": "claims-only"}]\n'
    claims_path.write_bytes(original_claims_bytes)
    assert not meta_path.exists()

    real_replace = os.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        call_count["n"] += 1
        # Order when only claims.json pre-exists: (1) back it up, (2) land
        # the new claims.json, (3) land the new sidecar — fail on the last.
        # Review: coordinator:code-reviewer — assert semantic identity per
        # call so a reorder fails loudly here rather than silently
        # mistargeting.
        if call_count["n"] == 1:
            assert str(src) == str(claims_path)
        elif call_count["n"] == 2:
            assert str(dst) == str(claims_path)
        elif call_count["n"] == 3:
            assert str(dst) == str(meta_path)
            raise OSError("simulated failure landing the sidecar")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims_emit.os, "replace", flaky_replace)

    rc = _emit(tmp_path)

    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    # The pre-existing claims.json is restored byte-for-byte.
    assert claims_path.exists()
    assert claims_path.read_bytes() == original_claims_bytes
    # meta.json never pre-existed, and must not exist after a failed run.
    assert not meta_path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p != claims_path]
    assert leftovers == []


def test_failed_re_run_restores_when_only_meta_json_pre_exists(tmp_path, monkeypatch):
    claims_path = tmp_path / "run1.claims.json"
    meta_path = tmp_path / "run1.claims.meta.json"
    original_meta_bytes = b'{"pre-existing": "meta-only"}\n'
    meta_path.write_bytes(original_meta_bytes)
    assert not claims_path.exists()

    real_replace = os.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        call_count["n"] += 1
        # Order when only meta.json pre-exists: (1) back it up, (2) land the
        # new claims.json, (3) land the new sidecar — fail on the last.
        # Review: coordinator:code-reviewer — assert semantic identity per
        # call so a reorder fails loudly here rather than silently
        # mistargeting.
        if call_count["n"] == 1:
            assert str(src) == str(meta_path)
        elif call_count["n"] == 2:
            assert str(dst) == str(claims_path)
        elif call_count["n"] == 3:
            assert str(dst) == str(meta_path)
            raise OSError("simulated failure landing the sidecar")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims_emit.os, "replace", flaky_replace)

    rc = _emit(tmp_path)

    assert rc == claims_emit.EXIT_PRODUCER_FAILURE
    # claims.json never pre-existed, and must not exist after a failed run.
    assert not claims_path.exists()
    # The pre-existing meta.json is restored byte-for-byte.
    assert meta_path.exists()
    assert meta_path.read_bytes() == original_meta_bytes
    leftovers = [p for p in tmp_path.iterdir() if p != meta_path]
    assert leftovers == []
