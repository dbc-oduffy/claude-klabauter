"""Cadence-gate coverage — the ``EmitContext.full_enrichment`` tier split (Levers 2 and 3).

Gates the four properties the gate has to hold, plus the one invariant that makes it safe:

  1. Default-FULL — a caller that says nothing gets FRESH data. Regressing this default to
     cheap reintroduces a silent-staleness trap for no measured saving (both real callers are
     already full); the cheap tier is an explicit opt-in. See ``EmitContext.full_enrichment``
     for why this reversed on 2026-07-29.
  2. ``emit.cadence`` and ``artifact.emit`` — both on the full tier, one explicitly.
  3. Skipped stage REUSES the previously-emitted value rather than synthesising one.
  4. Skipped stage with NO prior emission falls through to a real compute (``MUST_COMPUTE``)
     or writes the contract-sanctioned null (``NULL_SANCTIONED``), per stage.
  5. The never-manufacture-an-empty-collection invariant, asserted against
     ``skipped_stage`` directly — the one place the rule lives.

Spec backlink: emit() cost-lever work, 2026-07-29 (Levers 2/3, and the shared invariant).
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.emit import skipped_stage
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import file_attribution


def _make_ctx(root: Path, *, full_enrichment: bool = False) -> EmitContext:
    """A test ctx on the CHEAP tier by default — the opposite of the dataclass default.

    Deliberate and stated, because it inverts production: most tests in this file exercise the
    skip path, so the helper opts into it explicitly rather than each test repeating the
    keyword. The two default-tier tests construct ``EmitContext`` / call ``resolve_context``
    directly instead of going through here, so this inversion cannot mask a regression in the
    real default.
    """
    return EmitContext(
        repo_root=root,
        coordinator_root=root,
        central_state_root=root / "state",
        git_branch="test-branch",
        git_sha="d" * 40,
        git_sha_short="dddddddd",
        observed_at="2026-07-29T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
        full_enrichment=full_enrichment,
    )


def _write_prior_emission(root: Path, payload: dict) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "cockpit-emission.json").write_text(json.dumps(payload), encoding="utf-8")


def _fresh(root: Path, **kwargs) -> EmitContext:
    """A ctx with the per-process prior-emission memo cleared (tests share one interpreter)."""
    skipped_stage.clear_prior_emission_cache()
    return _make_ctx(root, **kwargs)


# ------------------------------------------------------------- 1. default-FULL (the safe default)
def test_emit_context_defaults_to_the_full_tier(tmp_path: Path) -> None:
    """An unknown/new caller must inherit FRESH data, never silently-reused data.

    This pins the 2026-07-29 reversal, and the direction matters more than the value. The
    field shipped defaulting to False for a few hours, so that a caller which said nothing
    inherited the fast emit. That was a trap rather than a saving: both real
    ``envelope.emit`` callers are on the full tier, so default-cheap bought nothing measurable
    while letting a future caller emit reused/stale values with no signal — the exact defect
    this work's most serious confirmed finding was (``artifact.emit`` silently demoted). A
    forgotten flag must cost time, which anyone notices, not truth, which nobody does.
    """
    ctx = EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path / "state",
        git_branch="b",
        git_sha="e" * 40,
        git_sha_short="eeeeeeee",
        observed_at="2026-07-29T00:00:00Z",
        hostname="h",
        repo_name="o/r",
    )
    assert ctx.full_enrichment is True


def test_resolve_context_defaults_to_the_full_tier(tmp_path: Path) -> None:
    """``resolve_context`` must hand back the SAFE tier, not the cheap one.

    Same reasoning as the sibling above — asserted separately because a resolver is exactly
    where a "cheap by default for speed" edit would plausibly land, and it is the seam every
    op goes through.

    Behavioral, not a source grep: an earlier version asserted the literal substring
    ``"full_enrichment: bool = False"`` in context.py, which a whitespace-only reformat would
    have failed and a mention in a comment would have passed.
    Review: code-reviewer (Finding 6).
    """
    from coordinator_core.ops.emit.envelope import resolve_context

    assert resolve_context(tmp_path).full_enrichment is True


# --------------------------------------------------------------------- 2. emit.cadence opts in
def test_emit_cadence_opts_into_the_full_tier(tmp_path: Path, monkeypatch) -> None:
    """emit.cadence asks for full enrichment — asserted on the ctx ``emit`` actually receives.

    Review: code-reviewer (Finding 6) — was a source grep for ``"ctx.full_enrichment = True"``.
    """
    from coordinator_core.ops import emit_cadence

    seen: list[EmitContext] = []
    monkeypatch.setattr(emit_cadence, "main_worktree_root", lambda root: tmp_path)
    monkeypatch.setattr(emit_cadence, "resolve_context", lambda root: _make_ctx(tmp_path))
    monkeypatch.setattr(emit_cadence, "record", lambda ctx: {"ok": True})
    monkeypatch.setattr(
        emit_cadence.envelope, "emit", lambda ctx, out=None: seen.append(ctx) or {"ok": True}
    )

    emit_cadence._emit_cadence({}, repo_root=tmp_path)

    assert [ctx.full_enrichment for ctx in seen] == [True]


def test_artifact_emit_defaults_to_the_full_tier(tmp_path: Path, monkeypatch) -> None:
    """The general-purpose "authoritative" op keeps its pre-gate semantics by default.

    Review: code-reviewer (Finding 3) — the cadence gate silently demoted every direct caller
    of this op to the cheap tier with no way to ask for the full one. Default-cheap on the
    dataclass protects an unknown NEW caller; it must not silently redefine what this op's own
    docstring calls authoritative.
    """
    from coordinator_core.ops import artifact_emit

    seen: list[EmitContext] = []
    monkeypatch.setattr(artifact_emit, "main_worktree_root", lambda root: tmp_path)
    monkeypatch.setattr(artifact_emit._envelope, "resolve_context", lambda root: _make_ctx(tmp_path))
    monkeypatch.setattr(
        artifact_emit._envelope, "emit", lambda ctx, out=None: seen.append(ctx) or {"ok": True}
    )

    artifact_emit._artifact_emit({}, repo_root=tmp_path)
    artifact_emit._artifact_emit({"full_enrichment": False}, repo_root=tmp_path)

    assert [ctx.full_enrichment for ctx in seen] == [True, False], (
        "no param => full tier (the op's documented semantics); explicit False => cheap tier"
    )


def test_artifact_emit_rejects_a_non_boolean_full_enrichment(tmp_path: Path, monkeypatch) -> None:
    """A truthy string would silently select the opposite tier from the one written."""
    import pytest

    from coordinator_core.ops import artifact_emit

    monkeypatch.setattr(artifact_emit, "main_worktree_root", lambda root: tmp_path)
    with pytest.raises(ValueError, match="full_enrichment must be a boolean"):
        artifact_emit._artifact_emit({"full_enrichment": "false"}, repo_root=tmp_path)


# --------------------------------------------------------- 3/4. file_attributions (MUST_COMPUTE)
def test_skipped_file_attribution_reuses_prior_rows(tmp_path: Path) -> None:
    """The cheap tier reuses the previously-emitted rows — never an empty array."""
    prior_row = {"session_id": "s1", "file_path": "a/b.py", "provenance": {"path": "a/b.py"}}
    _write_prior_emission(
        tmp_path,
        {
            "file_attributions": [prior_row],
            "malformed_records": {"file_attributions": [{"path": None, "reason": "r", "excluded": True}]},
        },
    )

    records, malformed = file_attribution.collect(_fresh(tmp_path))

    assert records == [prior_row]
    # The honesty markers travel WITH the rows they describe — reusing rows while dropping
    # their malformed markers would upgrade a degraded prior run to a clean-looking one.
    assert malformed == [{"path": None, "reason": "r", "excluded": True}]


def test_skipped_file_attribution_reuse_does_not_run_the_producer(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the gate: on a reuse, the 552-transcript producer never spawns."""
    _write_prior_emission(tmp_path, {"file_attributions": []})

    def _explode(*_args, **_kwargs):
        raise AssertionError("producer must not be invoked on the reuse path")

    monkeypatch.setattr(file_attribution, "_run_producer", _explode)
    records, malformed = file_attribution.collect(_fresh(tmp_path))
    # A prior emission that genuinely recorded zero rows is a truthful last-known value; the
    # gate reuses it as-is rather than treating "empty" as "no cache".
    assert records == []
    assert malformed == []


def test_skipped_file_attribution_with_no_prior_emission_computes_for_real(
    tmp_path: Path, monkeypatch
) -> None:
    """No prior emission => MUST_COMPUTE => fall through to the producer, never emit [].

    This is the fail-open trap the gate must not reintroduce: ``[]`` is structurally valid for
    the required ``file_attributions`` array and reads to every consumer as "no attributions
    exist".
    """
    calls: list[tuple] = []

    def _fake_producer(producer, *args):
        calls.append((producer, args))
        return [{"session_id": "s9", "file_path": str(tmp_path / "x.py"), "provenance": {}}], None

    monkeypatch.setattr(file_attribution, "_run_producer", _fake_producer)
    records, _malformed = file_attribution.collect(_fresh(tmp_path))

    assert len(calls) == 1, "producer must run when there is no last-known value to reuse"
    assert [r["file_path"] for r in records] == ["x.py"]


def test_unparseable_prior_emission_is_treated_as_no_prior_emission(
    tmp_path: Path, monkeypatch
) -> None:
    """A corrupt reuse source must force a real compute, not a silent empty array."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "cockpit-emission.json").write_text("{not json", encoding="utf-8")

    calls: list[tuple] = []

    def _fake_producer(producer, *args):
        calls.append((producer, args))
        return [], None

    monkeypatch.setattr(file_attribution, "_run_producer", _fake_producer)
    file_attribution.collect(_fresh(tmp_path))
    assert len(calls) == 1


def test_full_tier_always_runs_the_producer_even_with_a_prior_emission(
    tmp_path: Path, monkeypatch
) -> None:
    """full_enrichment=True must be unaffected by the reuse machinery."""
    _write_prior_emission(tmp_path, {"file_attributions": [{"session_id": "stale"}]})

    calls: list[tuple] = []

    def _fake_producer(producer, *args):
        calls.append((producer, args))
        return [], None

    monkeypatch.setattr(file_attribution, "_run_producer", _fake_producer)
    records, _ = file_attribution.collect(_fresh(tmp_path, full_enrichment=True))
    assert len(calls) == 1
    assert records == []


# ------------------------------------------------- VALIDATED REUSE (break-class regression)
def test_prior_emission_with_absolute_paths_is_rejected_and_recomputed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A prior emission predating F2 must NOT be reused — recompute instead.

    Regression for the live defect: DoE-claude's on-disk emission carried 4027 rows of which
    3991 had absolute ``file_path`` values and zero exclusion markers. Reusing it emitted
    absolute paths into a rag/cockpit join key, resurrected the out-of-repo rows F2 excludes,
    and hid the re-widening. Reuse is only safe when the reused value satisfies the same
    contract the computed value must.
    """
    _write_prior_emission(
        tmp_path,
        {
            "file_attributions": [
                {"session_id": "s1", "file_path": "/Users/x/DoE-claude/cross-repo/inbox/a.md"},
                {"session_id": "s2", "file_path": "already/relative.md"},
            ]
        },
    )

    calls: list[tuple] = []

    def _fake_producer(producer, *args):
        calls.append((producer, args))
        return [{"session_id": "s9", "file_path": str(tmp_path / "real.py"), "provenance": {}}], None

    monkeypatch.setattr(file_attribution, "_run_producer", _fake_producer)
    records, _malformed = file_attribution.collect(_fresh(tmp_path))

    assert len(calls) == 1, "a contract-invalid prior value must force a real compute"
    assert [r["file_path"] for r in records] == ["real.py"]
    assert not any(r["file_path"].startswith("/") for r in records)
    # Loud, because the operator-visible symptom is "the cheap tier paid full cost".
    assert "declined to reuse" in capsys.readouterr().err


def test_prior_emission_with_windows_drive_paths_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """Both platforms are first-class: a drive-letter path is as invalid as a POSIX one."""
    _write_prior_emission(
        tmp_path, {"file_attributions": [{"session_id": "s1", "file_path": r"C:\repo\a.md"}]}
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        file_attribution, "_run_producer", lambda p, *a: (calls.append((p, a)), ([], None))[1]
    )
    file_attribution.collect(_fresh(tmp_path))
    assert len(calls) == 1


def test_clean_prior_emission_is_still_reused(tmp_path: Path, monkeypatch) -> None:
    """The validator must not break the happy path — relative paths reuse as before."""
    rows = [
        {"session_id": "s1", "file_path": "coordinator/bin/x.py"},
        {"session_id": "s2", "file_path": "docs/plans/y.md"},
    ]
    _write_prior_emission(tmp_path, {"file_attributions": rows})

    def _explode(*_a, **_k):
        raise AssertionError("a valid prior value must be reused, not recomputed")

    monkeypatch.setattr(file_attribution, "_run_producer", _explode)
    records, _ = file_attribution.collect(_fresh(tmp_path))
    assert records == rows


def test_validator_accepts_and_rejects_the_right_shapes(tmp_path: Path) -> None:
    """Direct unit coverage of the contract check itself."""
    def validate(rows):
        return file_attribution._validate_prior_file_attributions(rows, tmp_path)

    assert validate([]) is None
    assert validate([{"file_path": "a/b.py"}]) is None
    assert "exclude or rewrite" in (validate([{"file_path": "/abs/a.py"}]) or "")
    assert "exclude or rewrite" in (validate([{"file_path": r"C:\a\b.py"}]) or "")
    assert "exclude or rewrite" in (validate([{"file_path": r"\\server\share\a.py"}]) or "")
    assert "expected a list" in (validate({"not": "a list"}) or "")


def test_validator_rejects_every_shape_the_computed_path_excludes(tmp_path: Path) -> None:
    """The validator's predicate must be F2 ITSELF, not a subset of it that agrees on absolutes.

    Review: code-reviewer (Findings 1-3). The hand-rolled first cut ACCEPTED all five shapes
    below — three because they are not "absolute" (leading ``..``, backslash-relative) or not a
    usable string (empty / None / missing key, which the loop skipped rather than rejected).
    Each is a row ``_relativize_or_exclude`` excludes or rewrites, so reusing it verbatim is not
    equivalent to computing it, and each lands in a rag/cockpit join key.

    Parametrised by hand rather than via ``pytest.mark.parametrize`` so the five shapes read as
    one enumerated contract rather than five unrelated cases.
    """
    def validate(rows):
        return file_attribution._validate_prior_file_attributions(rows, tmp_path)

    escapes_repo_root = [{"file_path": "../sibling-repo/file.py"}]
    backslash_relative = [{"file_path": "subdir\\file.py"}]
    empty_path = [{"file_path": ""}]
    null_path = [{"file_path": None}]
    missing_key = [{"session_id": "s1"}]

    for rows in (escapes_repo_root, backslash_relative, empty_path, null_path, missing_key):
        assert validate(rows) is not None, f"must be rejected, was accepted: {rows}"

    # A non-object entry is quarantined by the computed path, so it cannot be reused either.
    assert "not objects" in (validate([{"file_path": "ok/a.py"}, "bare-string"]) or "")

    # Control — the happy path must still reuse. A validator that rejects everything would
    # satisfy every assertion above while silently disabling the whole lever.
    assert validate([{"file_path": "coordinator/bin/x.py"}, {"file_path": "docs/a.md"}]) is None


def test_validator_that_raises_rejects_the_value(tmp_path: Path) -> None:
    """A raising validator must reject, never accept by accident."""
    _write_prior_emission(tmp_path, {"file_attributions": [{"file_path": "a.py"}]})

    def _boom(_value):
        raise RuntimeError("validator exploded")

    satisfied, value = skipped_stage.skipped_stage_value(
        _fresh(tmp_path),
        stage="t",
        extract=_extract_rows,
        absent_policy=skipped_stage.MUST_COMPUTE,
        validate=_boom,
    )
    assert (satisfied, value) == (False, None)


def test_null_sanctioned_stage_writes_null_when_prior_value_fails_validation(tmp_path: Path) -> None:
    """A rejected value routes through absent_policy — null here, not the bad value."""
    _write_prior_emission(tmp_path, {"file_attributions": [{"file_path": "/abs/a.py"}]})
    satisfied, value = skipped_stage.skipped_stage_value(
        _fresh(tmp_path),
        stage="t",
        extract=_extract_rows,
        absent_policy=skipped_stage.NULL_SANCTIONED,
        validate=lambda _v: "nope",
    )
    assert satisfied is True
    assert value is None


# ------------------------------------------------------ 3/4. docs_staleness (NULL_SANCTIONED)
def test_skipped_docs_staleness_reuses_prior_value(tmp_path: Path) -> None:
    prior_entries = [{"path": "README.md", "stale": True, "commits_since": 9}]
    _write_prior_emission(tmp_path, {"exec_summaries": [{"docs_staleness": prior_entries}]})

    from coordinator_core.ops.emit import envelope as envelope_mod

    env = {"exec_summaries": [{"id": "test/repo"}]}
    envelope_mod._stamp_docs_staleness(env, _fresh(tmp_path))
    assert env["exec_summaries"][0]["docs_staleness"] == prior_entries


def test_skipped_docs_staleness_writes_null_when_no_prior_value(tmp_path: Path) -> None:
    """NULL_SANCTIONED: null is the contract-legal "not computed" state for this field.

    Asserted as ``is None`` rather than ``== []`` deliberately — ``[]`` here would claim the
    detector ran and found every declared doc fresh.
    """
    from coordinator_core.ops.emit import envelope as envelope_mod

    env = {"exec_summaries": [{"id": "test/repo"}]}
    envelope_mod._stamp_docs_staleness(env, _fresh(tmp_path))
    assert env["exec_summaries"][0]["docs_staleness"] is None


def test_docs_staleness_validator_accepts_both_entry_variants_and_null() -> None:
    """The reused value must be shape-checked, and both real entry variants must survive it.

    Review: code-reviewer (Finding 1) — this stage reused its prior value with no validator at
    all. The ``no_content_modifying_history`` variant legitimately omits ``commits_since``, so a
    validator that required it would reject valid prior values and quietly disable the lever.
    """
    from coordinator_core.ops.emit import envelope as envelope_mod

    validate = envelope_mod._validate_prior_docs_staleness

    assert validate(None) is None, "null is this field's contract-sanctioned 'not computed'"
    assert validate([]) is None
    assert validate([{"path": "README.md", "status": "no_content_modifying_history", "stale": False}]) is None
    assert validate(
        [{"path": "README.md", "status": "ok", "stale": True, "commits_since": 9, "days_since": 4}]
    ) is None


def test_docs_staleness_validator_rejects_a_changed_entry_shape() -> None:
    """A stale array must not ship an old shape as though it were current."""
    from coordinator_core.ops.emit import envelope as envelope_mod

    validate = envelope_mod._validate_prior_docs_staleness

    assert "expected a list" in (validate({"path": "README.md"}) or "")
    assert "not an object" in (validate(["README.md"]) or "")
    assert "'path'" in (validate([{"stale": False}]) or "")
    assert "'stale'" in (validate([{"path": "a.md", "stale": "yes"}]) or "")
    assert "'commits_since'" in (validate([{"path": "a.md", "stale": True, "commits_since": "9"}]) or "")


def test_invalid_prior_docs_staleness_writes_null_rather_than_the_bad_shape(
    tmp_path: Path, capsys
) -> None:
    """NULL_SANCTIONED + a rejected prior value => null, loudly — never the rejected value."""
    _write_prior_emission(
        tmp_path, {"exec_summaries": [{"docs_staleness": [{"path": "a.md", "stale": "yes"}]}]}
    )

    from coordinator_core.ops.emit import envelope as envelope_mod

    env = {"exec_summaries": [{"id": "test/repo"}]}
    envelope_mod._stamp_docs_staleness(env, _fresh(tmp_path))

    assert env["exec_summaries"][0]["docs_staleness"] is None
    assert "declined to reuse" in capsys.readouterr().err


def test_skipped_docs_staleness_does_not_run_the_detector(tmp_path: Path) -> None:
    """The detector's per-doc git shell-outs must not fire on the cheap tier."""
    (tmp_path / "coordinator.local.md").write_text("# local\n", encoding="utf-8")
    _write_prior_emission(tmp_path, {"exec_summaries": [{"docs_staleness": None}]})

    import coordinator_core.ops.doc_staleness as doc_staleness
    from coordinator_core.ops.emit import envelope as envelope_mod

    called: list[str] = []
    original = doc_staleness.build_doc_staleness_report_from_registry

    def _spy(root):
        called.append(root)
        return original(root)

    doc_staleness.build_doc_staleness_report_from_registry = _spy
    try:
        env = {"exec_summaries": [{"id": "test/repo"}]}
        envelope_mod._stamp_docs_staleness(env, _fresh(tmp_path))
    finally:
        doc_staleness.build_doc_staleness_report_from_registry = original

    assert called == []


# ----------------------------------------------------- 5. the invariant, at its single home
def test_skipped_stage_never_manufactures_an_empty_collection(tmp_path: Path) -> None:
    """MUST_COMPUTE with no prior emission returns "not satisfied" — never a stand-in value."""
    satisfied, value = skipped_stage.skipped_stage_value(
        _fresh(tmp_path),
        stage="t",
        extract=lambda _prior: skipped_stage.absent(),
        absent_policy=skipped_stage.MUST_COMPUTE,
        validate=lambda _v: None,
    )
    assert (satisfied, value) == (False, None)


def test_skipped_stage_null_sanctioned_returns_none_not_empty(tmp_path: Path) -> None:
    satisfied, value = skipped_stage.skipped_stage_value(
        _fresh(tmp_path),
        stage="t",
        extract=lambda _prior: skipped_stage.absent(),
        absent_policy=skipped_stage.NULL_SANCTIONED,
        validate=lambda _v: None,
    )
    assert satisfied is True
    assert value is None


def test_skipped_stage_reads_the_canonical_path_not_an_out_override(tmp_path: Path) -> None:
    """The reuse source is the repo's last PUBLISHED emission, never a scratch ``out`` path."""
    _write_prior_emission(tmp_path, {"marker": "canonical"})
    prior = skipped_stage.load_prior_emission(_fresh(tmp_path))
    assert prior == {"marker": "canonical"}
    assert skipped_stage._prior_emission_path(_make_ctx(tmp_path)) == (
        tmp_path / "state" / "cockpit-emission.json"
    )


def test_reused_value_is_a_deep_copy_of_the_prior_emission(tmp_path: Path) -> None:
    """Reused rows land in the live envelope and are then mutated in place by the stamps.

    ``_stamp_content_hash`` re-hashes every reused ``file_attributions`` row against the
    current working tree — correct, but it must not write through to the shared
    prior-emission memo, or one stage's stamps become another stage's "last known".
    """
    _write_prior_emission(tmp_path, {"file_attributions": [{"session_id": "s1", "nested": {"k": 1}}]})
    ctx = _fresh(tmp_path)

    _satisfied, rows = skipped_stage.skipped_stage_value(
        ctx,
        stage="t",
        extract=_extract_rows,
        absent_policy=skipped_stage.MUST_COMPUTE,
        validate=lambda _v: None,
    )
    rows[0]["nested"]["k"] = 99
    rows[0]["content_hash"] = "stamped"

    again = skipped_stage.load_prior_emission(ctx)
    assert again["file_attributions"][0]["nested"]["k"] == 1
    assert "content_hash" not in again["file_attributions"][0]


def _extract_rows(prior: dict):
    rows = prior.get("file_attributions")
    return rows if isinstance(rows, list) else skipped_stage.absent()


def test_skipped_stage_rejects_an_unknown_absent_policy(tmp_path: Path) -> None:
    """A third policy is not a thing — fail loud rather than guess a fallback."""
    import pytest

    with pytest.raises(ValueError, match="absent_policy"):
        skipped_stage.skipped_stage_value(
            _fresh(tmp_path),
            stage="t",
            extract=lambda _prior: None,
            absent_policy="probably_fine",
            validate=lambda _v: None,
        )


def test_skipped_stage_requires_a_validator(tmp_path: Path) -> None:
    """``validate`` is required, not an optional kwarg with a fail-open default.

    Review: code-reviewer (Finding 2) — optional-with-a-default is the shape that let this
    module's own sibling call site ship unvalidated. A caller sure its prior value cannot be
    contract-invalid passes an explicit no-op, which is visible in the diff.
    """
    import pytest

    with pytest.raises(TypeError, match="validate"):
        skipped_stage.skipped_stage_value(  # type: ignore[call-arg]
            _fresh(tmp_path),
            stage="t",
            extract=lambda _prior: skipped_stage.absent(),
            absent_policy=skipped_stage.MUST_COMPUTE,
        )


def test_skipped_stage_extractor_raising_is_treated_as_absent(tmp_path: Path, capsys) -> None:
    """A malformed prior emission must not be able to break the current one — but must be loud.

    Review: code-reviewer (Finding 4) — the same ``except Exception`` also catches a real bug in
    the extractor itself (a typo'd key, a wrong type assumption). Silently swallowing that while
    a validator rejection logs is backwards: both end in "the cheap tier paid full cost".
    """
    _write_prior_emission(tmp_path, {"exec_summaries": "not-a-list"})

    def _boom(_prior):
        raise TypeError("malformed prior")

    satisfied, value = skipped_stage.skipped_stage_value(
        _fresh(tmp_path),
        stage="t",
        extract=_boom,
        absent_policy=skipped_stage.MUST_COMPUTE,
        validate=lambda _v: None,
    )
    assert (satisfied, value) == (False, None)
    err = capsys.readouterr().err
    assert "declined to reuse" in err
    assert "extractor raised TypeError" in err
