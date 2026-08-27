"""
coordinator_core.ops.tests.test_cutover_gate_handler — scoped tests for the
"cutover.gate" op handler (C4b): the two-way AGREEMENT test, the narrowing
check, the foreign-repo fail-closed rule, and signal-2 re-verification
dispatch. Does NOT cover derive()'s own derivation-mode correctness (C4a's
test_cutover_gate_derivation.py) or schema resolution (C4c's
test_cutover_gate_schema_resolution.py) — those are separate, already-landed
test files this module deliberately does not duplicate or collide with.

The synthetic red-case regression (a record whose derivation yields a
consumer absent from confirmed_consumers) and the live DR-084 red/green
provenance demonstration are C6's (test_cutover_gate.py, not authored by
this chunk) — this file exercises the handler's OTHER agreement legs.

Async invocation follows the house convention (test_coverage_gate.py):
plain sync test functions wrapping the async handler in `asyncio.run(...)`
— no `pytest.mark.asyncio` / pytest-asyncio dependency.

Spec backlink: DoE-claude:pln-cutover-state-machine-a-phase--96db57 § C4b
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from coordinator_core.ops.cutover_gate import _cutover_gate


def _write_record(
    tmp_path: Path,
    *,
    phase: str = "dual-write",
    confirmed_consumers: list | None = None,
    gate_source: dict | None = None,
    derivation_history: list | None = None,
) -> Path:
    fm: dict = {
        "surface": "test surface",
        "phase": phase,
        "confirmed_consumers": confirmed_consumers or [],
        "gate_source": gate_source
        or {
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [{"repo": "doe-claude", "foreign": False}],
        },
    }
    if derivation_history is not None:
        fm["derivation_history"] = derivation_history
    fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
    record_path = tmp_path / "record.md"
    record_path.write_text(f"---\n{fm_text}---\n\n# Test record\n", encoding="utf-8")
    return record_path


def _write_consumer_writer(tmp_path: Path, filename: str = "producer.py") -> None:
    sub = tmp_path / "sub"
    sub.mkdir(exist_ok=True)
    (sub / filename).write_text('TOKEN = "TARGET_TOKEN"\n', encoding="utf-8")


def _gate(params: dict, repo_root) -> dict:
    return asyncio.run(_cutover_gate(params, repo_root=repo_root))


def test_missing_record_param_is_setup_error(tmp_path: Path) -> None:
    result = _gate({}, tmp_path / ".git")
    assert result["exit_code"] == 1
    assert "record" in result["notes"][0]


def test_repo_root_none_is_setup_error() -> None:
    result = _gate({"record": "record.md"}, None)
    assert result["exit_code"] == 1


def test_empty_derivation_is_indeterminate_never_pass(tmp_path: Path) -> None:
    # No sub/ files at all — derive() finds nothing, derived_count == 0.
    record_path = _write_record(tmp_path)
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert result["derivation_history_entry"]["derived_count"] == 0


def test_confirmed_consumer_derivation_cannot_refind_is_refuse(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/nonexistent.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert "derivation cannot" in result["notes"][0]


def test_agreement_holds_and_signal2_probe_op_key_reverifies_pass(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 0
    assert "VERDICT=PASS" in result["verdict_line"]
    assert result["derivation_history_entry"]["derived_count"] == 1


def test_signal2_unregistered_probe_op_key_is_indeterminate(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "no.such.op"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert any("not registered" in note for note in result["notes"])


def test_unrecognized_verified_by_kind_is_indeterminate(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "free-prose", "ref": "trust me"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]


def _write_commitment(tmp_path: Path, filename: str, *, status: str = "fulfilled") -> None:
    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    commitments_dir.mkdir(parents=True, exist_ok=True)
    commitment = {
        "created": "2026-07-25",
        "title": "sibling confirms consumer migrated",
        "body": "sibling attests the consumer now handles the new form",
        "status": status,
        "committed_by": "sibling-repo",
        "memo": "cross-repo/inbox/2026-07-25-sibling-em-confirms.md",
        "commitment": "consumer now handles the new form",
        "observed": "2026-07-25",
    }
    (commitments_dir / filename).write_text(
        yaml.safe_dump(commitment, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_sibling_commitment_ref_fulfilled_reverifies_pass(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    filename = "2026-07-25-sibling-confirms-x-a1b2c3d4e5f6.yaml"
    _write_commitment(tmp_path, filename, status="fulfilled")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "sibling-commitment-ref", "ref": filename},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 0
    assert "VERDICT=PASS" in result["verdict_line"]


def test_sibling_commitment_ref_missing_record_is_indeterminate(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {
                    "kind": "sibling-commitment-ref",
                    "ref": "2026-07-25-no-such-commitment-a1b2c3d4e5f6.yaml",
                },
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert any("not found" in note for note in result["notes"])


def test_sibling_commitment_ref_not_fulfilled_is_indeterminate(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    filename = "2026-07-25-sibling-open-commitment-a1b2c3d4e5f6.yaml"
    _write_commitment(tmp_path, filename, status="open")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "sibling-commitment-ref", "ref": filename},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert any("not attest" in note for note in result["notes"])


def test_shrinking_derived_count_without_reason_is_refuse(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
        derivation_history=[
            {
                "phase": "dual-write",
                "derived_count": 5,
                "derived_ids": ["doe-claude:sub/a.py"] * 5,
                "at": "2026-07-24T00:00:00Z",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert "SHRANK" in result["notes"][0]


def test_shrinking_derived_count_with_reason_does_not_refuse_on_narrowing(
    tmp_path: Path,
) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
        derivation_history=[
            {
                "phase": "dual-write",
                "derived_count": 5,
                "derived_ids": ["doe-claude:sub/a.py"] * 5,
                "at": "2026-07-24T00:00:00Z",
                "derivation_narrowed_reason": "4 consumers were deleted outright, see PR #1",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert "VERDICT=REFUSE" not in result["verdict_line"]


def test_foreign_repo_with_no_sibling_confirmation_is_indeterminate(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
        gate_source={
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [
                {"repo": "doe-claude", "foreign": False},
                {"repo": "cockpit", "foreign": True},
            ],
        },
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert any("foreign repo" in note for note in result["notes"])


def test_foreign_repo_confirmed_id_not_rederivable_refuses_at_agreement_leg(
    tmp_path: Path,
) -> None:
    """A confirmed_consumers entry claiming a foreign-repo id (e.g. "cockpit:...")
    is never re-findable by derive() (only doe-claude/claude-klabauter are ever
    scanned — see _build_repo_roots), so the subset-agreement leg (2) refuses
    before the foreign-repo fail-closed leg (4) is ever reached. This proves
    leg (2) fires first — a foreign entry cannot be used to short-circuit
    agreement by simply being *claimed* confirmed.
    """
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            },
            {
                "id": "cockpit:some/sibling-confirmed-file.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            },
        ],
        gate_source={
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [
                {"repo": "doe-claude", "foreign": False},
                {"repo": "cockpit", "foreign": True},
            ],
        },
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]


def test_bare_relpath_confirmed_id_matches_unambiguous_derived_id(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 0
    assert "VERDICT=PASS" in result["verdict_line"]


def test_repo_qualified_confirmed_id_mismatch_still_refuses(tmp_path: Path) -> None:
    """A repo-qualified id must match BOTH halves — never falls back to a bare
    relpath match against a different repo's derived id."""
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "claude-klabauter:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert "derivation cannot" in result["notes"][0]


def test_bare_relpath_confirmed_id_ambiguous_across_repos_refuses_and_asks_to_qualify(
    tmp_path: Path,
) -> None:
    """doe-claude and doe_claude both alias to the same on-disk root
    (_DOE_ROOT_ALIASES), so declaring both as gate_source repos derives the
    same file twice under two different repo-qualified ids — a genuine
    cross-repo ambiguity for a bare relpath confirmation."""
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
        gate_source={
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [
                {"repo": "doe-claude", "foreign": False},
                {"repo": "doe_claude", "foreign": False},
            ],
        },
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert any("ambiguous" in note and "qualify" in note.lower() for note in result["notes"])


def test_bare_relpath_confirmed_id_genuinely_unresolvable_still_refuses(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "sub/nonexistent.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert "derivation cannot" in result["notes"][0]


def test_underconfirmed_record_refuses_and_names_unconfirmed_derived_ids(
    tmp_path: Path,
) -> None:
    """The missing clause (2b): a derived id with NO matching confirmed_consumers
    entry is a REFUSE, not a PASS — even though the one confirmed entry IS
    re-findable by derive() (clause 2 alone would be satisfied)."""
    _write_consumer_writer(tmp_path, filename="producer.py")
    _write_consumer_writer(tmp_path, filename="producer_two.py")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    refusal_notes = [n for n in result["notes"] if "no matching" in n.lower()]
    assert refusal_notes, result["notes"]
    assert "doe-claude:sub/producer_two.py" in refusal_notes[0]
    assert "confirmed_consumers entry" in refusal_notes[0]
    assert "verified_by" in refusal_notes[0]


def test_fully_confirmed_record_with_multiple_consumers_passes(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path, filename="producer.py")
    _write_consumer_writer(tmp_path, filename="producer_two.py")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            },
            {
                "id": "doe-claude:sub/producer_two.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            },
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 0
    assert "VERDICT=PASS" in result["verdict_line"]
    assert result["derivation_history_entry"]["derived_count"] == 2


def test_unknown_candidate_blocks_advance_even_when_confirmed_agrees(tmp_path: Path) -> None:
    """Regression — census fold-in constraint 2: a candidate the derivation
    cannot classify (AST-unparseable `.py` file whose raw text still
    matches `pattern`) must block the gate (INDETERMINATE) EVEN when the
    classifiable half of the derivation already agrees perfectly with
    confirmed_consumers — unknown is checked FIRST, before the subset
    agreement legs, so it can never be silently absorbed by a passing
    two-way AGREEMENT test on the classifiable ids alone."""
    _write_consumer_writer(tmp_path)
    sub = tmp_path / "sub"
    (sub / "broken.py").write_text("def broken(:\n    TOKEN = 'TARGET_TOKEN'\n", encoding="utf-8")

    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert any("broken.py" in note for note in result["notes"])


def test_gate_never_writes_the_record_file(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    before = record_path.read_text(encoding="utf-8")
    before_mtime = record_path.stat().st_mtime_ns
    result = _gate({"record": str(record_path)}, tmp_path / ".git")
    after = record_path.read_text(encoding="utf-8")
    assert after == before
    assert record_path.stat().st_mtime_ns == before_mtime
    assert "derivation_history_entry" in result


# ---------------------------------------------------------------------------
# DR-349 § 4 — the collection-error localization pass shares ONE deadline
#
# `_reverify_test_node_ids_batch` falls back to a per-ref pytest invocation
# when a batch aborts during collection. That fallback is spawn-count-exempt
# from the amplification gate (it fires only on failure and carries a filtered
# subset of the batched primary's collection), but nothing bounded its TIME in
# aggregate: N refs x `_pytest_batch_timeout(1)` with no shared ceiling is the
# self-raising shape `_pytest_batch_timeout`'s own cap exists to prevent,
# reintroduced one call frame out.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Stand-in for `cutover_gate`'s own `time` binding, so a deadline test
    advances a dict-held clock instead of sleeping — and without touching the
    real `time.monotonic` the surrounding thread pool depends on."""

    def __init__(self, cell: dict) -> None:
        self._cell = cell

    def monotonic(self) -> float:
        return self._cell["t"]


def test_localization_pass_stops_at_its_shared_deadline(tmp_path: Path, monkeypatch) -> None:
    """Past the shared deadline no further isolation run is spawned, and the
    unlocalized refs fall through to REFUSE — fail-safe, never fail-open."""
    from coordinator_core.ops import cutover_gate as cg

    (tmp_path / "test_consumers.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    refs = [f"test_consumers.py::test_{i}" for i in range(5)]

    spawned: list = []
    clock = {"t": 0.0}

    def _fake_batch(root, batch_refs, *, timeout_secs=None):
        spawned.append(list(batch_refs))
        # Every isolation run burns the whole deadline it was granted.
        clock["t"] += timeout_secs if timeout_secs is not None else 0.0
        return {}

    monkeypatch.setattr(cg, "_run_pytest_batch", _fake_batch)
    # Patch the MODULE's own `time` binding, never `time.monotonic` globally --
    # the thread pool this path runs under uses the real clock.
    monkeypatch.setattr(cg, "time", _FakeClock(clock))

    results = cg._reverify_test_node_ids_batch(refs, {"here": tmp_path})

    # The batched primary, then ONE isolation run that consumed the deadline.
    assert len(spawned) == 2
    assert spawned[0] == refs
    assert len(spawned[1]) == 1
    assert all(results[r][0] is False for r in refs)
    assert all("cannot re-verify" in results[r][1] for r in refs)


def test_localization_runs_derive_their_bound_from_the_remainder(
    tmp_path: Path, monkeypatch
) -> None:
    """Each isolation run is handed the deadline REMAINDER, so the pass's
    total authorised occupancy never exceeds one batch bound for the same
    refs — stacking spawns cannot buy time."""
    from coordinator_core.ops import cutover_gate as cg

    (tmp_path / "test_consumers.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    refs = [f"test_consumers.py::test_{i}" for i in range(3)]

    handed: list = []
    clock = {"t": 0.0}

    def _fake_batch(root, batch_refs, *, timeout_secs=None):
        if timeout_secs is not None:
            handed.append(timeout_secs)
            clock["t"] += 1.0
        return {}

    monkeypatch.setattr(cg, "_run_pytest_batch", _fake_batch)
    # Patch the MODULE's own `time` binding, never `time.monotonic` globally --
    # the thread pool this path runs under uses the real clock.
    monkeypatch.setattr(cg, "time", _FakeClock(clock))

    cg._reverify_test_node_ids_batch(refs, {"here": tmp_path})

    budget = cg._pytest_batch_timeout(len(refs))
    assert len(handed) == 3
    assert handed == [budget, budget - 1.0, budget - 2.0]
    assert max(handed) <= budget


def test_run_pytest_batch_timeout_override_is_narrow_only(tmp_path: Path, monkeypatch) -> None:
    """`timeout_secs` may only ever LOWER the batch bound (DR-349 § 3)."""
    from coordinator_core.ops import cutover_gate as cg

    seen: list = []

    def _fake_run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise cg.subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(cg.subprocess, "run", _fake_run)

    cg._run_pytest_batch(tmp_path, ["a.py::t"], timeout_secs=5.0)
    cg._run_pytest_batch(tmp_path, ["a.py::t"], timeout_secs=999999.0)
    cg._run_pytest_batch(tmp_path, ["a.py::t"])

    flat = cg._pytest_batch_timeout(1)
    assert seen == [5.0, flat, flat]
