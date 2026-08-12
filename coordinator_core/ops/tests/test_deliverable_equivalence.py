"""
coordinator_core.ops.tests.test_deliverable_equivalence

Unit tests for the shared deliverable-id equivalence read-model module
(coordinator_core/ops/deliverable_equivalence.py) — the C4a module half of AC6, plus
AC12's idempotence-by-construction.

Coverage:
  (i)    none_passthrough        — canonicalize(None, ...) returns None (None-safe).
  (ii)   unknown_id_passthrough  — an id absent from the equivalence map is returned
                                   unchanged (absence is never a silent merge).
  (iii)  known_loser_maps        — a declared loser id maps to its declared winner.
  (iv)   missing_artifact_empty  — no state/deliverable-equivalence.yaml on disk ->
                                   load_equivalence_map returns {} -> every id
                                   canonicalizes to itself.
  (v)    memoization              — the artifact is read at most once per process;
                                   editing the file after the first load does not
                                   change what a second call returns.
  (vi)   idempotence              — canonicalize(canonicalize(x, m), m) == canonicalize(x, m)
                                   for both a known-loser id and a passthrough id.

Spec backlink: docs/plans/2026-08-01-deliverable-id-fork-remediation.md § C4 (AC6, AC12)
"""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.deliverable_equivalence import (
    DeliverableDualReadMismatchError,
    DeliverableLedgerValidationError,
    _reset_deliverable_ledger_cache,
    _reset_equivalence_map_cache,
    canonicalize,
    dual_read_deliverable_id,
    dual_read_deliverable_ids_for_corpus,
    load_deliverable_ledger,
    load_equivalence_map,
    seed_deliverable_ledger_rows,
    validate_deliverable_ledger_rows,
)


@pytest.fixture(autouse=True)
def _reset_memo():
    """Clear the module-scope memo before and after each test.

    Required because pytest shares one interpreter across tests, unlike the
    spawn-per-call production model this memo is designed for (see
    _reset_equivalence_map_cache's own docstring).
    """
    _reset_equivalence_map_cache()
    _reset_deliverable_ledger_cache()
    yield
    _reset_equivalence_map_cache()
    _reset_deliverable_ledger_cache()


def _write_artifact(worktree_root: Path, entries: list[dict]) -> Path:
    """Write a minimal state/deliverable-equivalence.yaml fixture and return its path."""
    state_dir = worktree_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    dumped_entries = [
        {
            "loser": entry["loser"],
            "winner": entry["winner"],
            "evidence": entry.get("evidence", "test fixture"),
        }
        for entry in entries
    ]
    artifact_path.write_text(
        yaml.safe_dump({"entries": dumped_entries}, sort_keys=False), encoding="utf-8"
    )
    return artifact_path


def test_none_passthrough():
    """canonicalize(None, ...) is None-safe: returns None regardless of map contents."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    assert canonicalize(None, equivalence_map) is None


def test_unknown_id_passthrough():
    """An id with no declared equivalence entry canonicalizes to itself (no silent merge)."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    assert canonicalize("dlv-unrelated", equivalence_map) == "dlv-unrelated"


def test_known_loser_maps_to_winner():
    """A declared loser id canonicalizes to its declared winner."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    assert canonicalize("dlv-loser", equivalence_map) == "dlv-winner"
    # The winner itself is untouched — it is not a loser in this map.
    assert canonicalize("dlv-winner", equivalence_map) == "dlv-winner"


def test_missing_artifact_returns_empty_map(tmp_path):
    """No state/deliverable-equivalence.yaml on disk -> load_equivalence_map returns {}.

    A missing artifact is NOT an error (C3b has not landed yet in this wave) — every
    id then canonicalizes to itself, matching today's raw-comparison behaviour.
    """
    worktree_root = tmp_path
    equivalence_map = load_equivalence_map(worktree_root)
    assert equivalence_map == {}
    assert canonicalize("dlv-anything", equivalence_map) == "dlv-anything"


def test_known_loser_end_to_end(tmp_path):
    """A fixture-built artifact resolves through load_equivalence_map + canonicalize."""
    _write_artifact(
        tmp_path,
        [{"loser": "dlv-sat-01-old", "winner": "dlv-sat-01", "evidence": "creation order"}],
    )
    equivalence_map = load_equivalence_map(tmp_path)
    assert equivalence_map == {"dlv-sat-01-old": "dlv-sat-01"}
    assert canonicalize("dlv-sat-01-old", equivalence_map) == "dlv-sat-01"
    assert canonicalize("dlv-sat-01", equivalence_map) == "dlv-sat-01"
    assert canonicalize("dlv-unrelated", equivalence_map) == "dlv-unrelated"


def test_memoization_reads_artifact_at_most_once(tmp_path):
    """load_equivalence_map memoizes per process — a post-first-call edit is not observed.

    Mirrors deliverable_rollup.py's _central_initiatives_dir memoization contract:
    resolve once, reuse for the process lifetime.
    """
    _write_artifact(
        tmp_path,
        [{"loser": "dlv-a-old", "winner": "dlv-a", "evidence": "first read"}],
    )
    first = load_equivalence_map(tmp_path)
    assert first == {"dlv-a-old": "dlv-a"}

    # Rewrite the artifact with different contents; the memo must NOT observe this.
    _write_artifact(
        tmp_path,
        [{"loser": "dlv-b-old", "winner": "dlv-b", "evidence": "second read, unread"}],
    )
    second = load_equivalence_map(tmp_path)
    assert second == first
    assert second == {"dlv-a-old": "dlv-a"}


def test_idempotence_known_loser(tmp_path):
    """canonicalize(canonicalize(x, m), m) == canonicalize(x, m) for a known loser id."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    once = canonicalize("dlv-loser", equivalence_map)
    twice = canonicalize(once, equivalence_map)
    assert once == "dlv-winner"
    assert twice == once


def test_idempotence_passthrough():
    """canonicalize(canonicalize(x, m), m) == canonicalize(x, m) for a passthrough id."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    once = canonicalize("dlv-unrelated", equivalence_map)
    twice = canonicalize(once, equivalence_map)
    assert once == "dlv-unrelated"
    assert twice == once


def test_idempotence_none():
    """canonicalize(canonicalize(None, m), m) == None."""
    equivalence_map = {"dlv-loser": "dlv-winner"}
    once = canonicalize(None, equivalence_map)
    twice = canonicalize(once, equivalence_map)
    assert once is None
    assert twice is None


# Review: coordinatorcode-reviewer-67ffaa7e Finding 3 — the defensive branches added to
# guard against a hand-authored/malformed artifact had zero coverage. The cases below
# target the branches most likely to fire on a real authoring mistake.


def test_unparsable_yaml_falls_back_to_empty_map(tmp_path, caplog):
    """A present-but-unparsable artifact degrades to {} with a logged WARNING, not a raise."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_text("entries: [this is: not, valid: yaml: at all", encoding="utf-8")

    with caplog.at_level("WARNING"):
        equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {}
    assert canonicalize("dlv-anything", equivalence_map) == "dlv-anything"
    assert any(
        "could not read/parse" in record.message for record in caplog.records
    )


def test_duplicate_loser_keeps_first_seen_and_warns(tmp_path, caplog):
    """A duplicate `loser` keeps the FIRST mapping and logs a WARNING, per C3b's
    uniqueness obligation."""
    _write_artifact(
        tmp_path,
        [
            {"loser": "dlv-dup", "winner": "dlv-first-winner", "evidence": "first"},
            {"loser": "dlv-dup", "winner": "dlv-second-winner", "evidence": "second"},
        ],
    )

    with caplog.at_level("WARNING"):
        equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {"dlv-dup": "dlv-first-winner"}
    assert any("duplicate loser id" in record.message for record in caplog.records)


def test_transitive_chain_warns_but_resolves_only_one_level(tmp_path, caplog):
    """A winner that also appears as a loser (a transitive chain) is not the loader's
    to walk — it must warn, not raise, and canonicalize() resolves only one level."""
    _write_artifact(
        tmp_path,
        [
            {"loser": "dlv-a-old", "winner": "dlv-a-mid", "evidence": "first hop"},
            {"loser": "dlv-a-mid", "winner": "dlv-a-final", "evidence": "second hop"},
        ],
    )

    with caplog.at_level("WARNING"):
        equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {"dlv-a-old": "dlv-a-mid", "dlv-a-mid": "dlv-a-final"}
    # One level only — the loader does not walk the chain to dlv-a-final.
    assert canonicalize("dlv-a-old", equivalence_map) == "dlv-a-mid"
    assert any("transitive chain" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "artifact_text",
    [
        pytest.param("just: a\nstring: not-a-dict-of-entries\n", id="parsed_not_dict"),
        pytest.param("- not\n- a\n- dict\n", id="parsed_is_list_not_dict"),
        pytest.param("entries: not-a-list\n", id="entries_not_a_list"),
    ],
)
def test_malformed_top_level_shapes_degrade_to_empty_map(tmp_path, artifact_text):
    """Non-dict `parsed`, or `entries` present but not a list, both degrade to {}."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(artifact_text, encoding="utf-8")

    equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {}


def test_non_dict_entry_is_skipped_with_warning(tmp_path, caplog):
    """A non-dict entry in `entries` is skipped with a WARNING, not a raise."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "entries:\n  - just-a-string\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {}
    assert any("non-mapping entry" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"winner": "dlv-winner"}, id="missing_loser"),
        pytest.param({"loser": "  ", "winner": "dlv-winner"}, id="blank_loser"),
        pytest.param({"loser": "dlv-loser"}, id="missing_winner"),
        pytest.param({"loser": "dlv-loser", "winner": "  "}, id="blank_winner"),
    ],
)
def test_missing_or_blank_loser_or_winner_is_skipped_with_warning(tmp_path, caplog, entry):
    """An entry with a missing/blank `loser` or `winner` is skipped with a WARNING."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = ["entries:", "  - evidence: 'test fixture'"]
    for key, value in entry.items():
        lines.append(f"    {key}: {value!r}")
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {}
    assert any(
        "missing/invalid" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Close-out ledger (sedge-06) — load_deliverable_ledger, its own memo, and
# validate_deliverable_ledger_rows.
# ---------------------------------------------------------------------------


def _write_artifact_with_ledger(
    worktree_root: Path, entries: list[dict], ledger_rows: list[dict]
) -> Path:
    """Write an artifact carrying BOTH `entries:` and `ledger:` — the additive-
    compatibility fixture shape."""
    state_dir = worktree_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_text(
        yaml.safe_dump({"entries": entries, "ledger": ledger_rows}, sort_keys=False),
        encoding="utf-8",
    )
    return artifact_path


def _well_formed_row(**overrides) -> dict:
    row = {
        "deliverable_id": "dlv-example",
        "status": "shipped",
        "closed_at": "2026-08-01",
        "governing_plan": "docs/plans/2026-08-01-example.md",
        "closure_evidence": {
            "realizing_commits": ["abc123"],
            "join_provenance": "joined",
        },
        "superseded_by": None,
        "adjudicator": "sedge-06 test fixture",
        "evidence_source": "test fixture",
    }
    row.update(overrides)
    return row


def test_load_deliverable_ledger_returns_full_rows(tmp_path):
    """The second loader returns full rows, not a projection."""
    row = _well_formed_row()
    _write_artifact_with_ledger(tmp_path, [], [row])

    rows = load_deliverable_ledger(tmp_path)

    assert rows == [row]


def test_load_deliverable_ledger_missing_artifact_returns_empty_list(tmp_path):
    assert load_deliverable_ledger(tmp_path) == []


def test_load_deliverable_ledger_missing_ledger_key_returns_empty_list(tmp_path):
    _write_artifact(tmp_path, [{"loser": "dlv-a", "winner": "dlv-b"}])

    assert load_deliverable_ledger(tmp_path) == []


def test_load_deliverable_ledger_unparsable_yaml_returns_empty_list(tmp_path, caplog):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "entries: [this is: not, valid: yaml: at all", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        rows = load_deliverable_ledger(tmp_path)

    assert rows == []
    # Review: coordinatorreview-integrator-574d6d25 — the caplog.at_level context
    # implied "and it warns"; assert the warning was actually emitted, not just
    # the return value.
    assert any("could not read/parse" in record.message for record in caplog.records)


def test_load_equivalence_map_unaffected_by_ledger_block(tmp_path):
    """Additive-compatibility pin: an artifact carrying BOTH `entries:` and `ledger:`
    still yields the same {loser: winner} map from load_equivalence_map as one with
    only `entries:` — the ledger block must not change the fork-equivalence loader's
    behaviour at all."""
    _write_artifact_with_ledger(
        tmp_path,
        [{"loser": "dlv-old", "winner": "dlv-new", "evidence": "test"}],
        [_well_formed_row()],
    )

    equivalence_map = load_equivalence_map(tmp_path)

    assert equivalence_map == {"dlv-old": "dlv-new"}


def test_deliverable_ledger_memoization_reads_at_most_once(tmp_path):
    _write_artifact_with_ledger(tmp_path, [], [_well_formed_row(deliverable_id="dlv-first")])
    first = load_deliverable_ledger(tmp_path)
    assert first == [_well_formed_row(deliverable_id="dlv-first")]

    _write_artifact_with_ledger(tmp_path, [], [_well_formed_row(deliverable_id="dlv-second")])
    second = load_deliverable_ledger(tmp_path)

    assert second == first


def test_reset_deliverable_ledger_cache_forces_reread(tmp_path):
    _write_artifact_with_ledger(tmp_path, [], [_well_formed_row(deliverable_id="dlv-first")])
    first = load_deliverable_ledger(tmp_path)
    assert first == [_well_formed_row(deliverable_id="dlv-first")]

    _write_artifact_with_ledger(tmp_path, [], [_well_formed_row(deliverable_id="dlv-second")])
    _reset_deliverable_ledger_cache()
    second = load_deliverable_ledger(tmp_path)

    assert second == [_well_formed_row(deliverable_id="dlv-second")]


def test_validate_deliverable_ledger_rows_accepts_well_formed_rows():
    validate_deliverable_ledger_rows([_well_formed_row(), _well_formed_row(deliverable_id="dlv-2")])


def test_validate_deliverable_ledger_rows_accepts_open_row():
    validate_deliverable_ledger_rows(
        [_well_formed_row(status="open", closed_at=None, superseded_by=None)]
    )


def test_validate_deliverable_ledger_rows_raises_on_bad_status_enum():
    # Review: coordinatorreview-integrator-574d6d25 — pin the NATURE of the
    # violation (an invalid enum value), not just the bare field name "status".
    with pytest.raises(DeliverableLedgerValidationError, match="status.*invalid|invalid.*status"):
        validate_deliverable_ledger_rows([_well_formed_row(status="deprecated")])


def test_validate_deliverable_ledger_rows_raises_on_duplicate_deliverable_id():
    with pytest.raises(
        DeliverableLedgerValidationError, match="duplicat.*deliverable_id|deliverable_id.*duplicat"
    ):
        validate_deliverable_ledger_rows(
            [_well_formed_row(deliverable_id="dlv-dup"), _well_formed_row(deliverable_id="dlv-dup")]
        )


def test_validate_deliverable_ledger_rows_raises_on_missing_closed_at_for_non_open():
    with pytest.raises(
        DeliverableLedgerValidationError, match="closed_at.*required|required.*closed_at"
    ):
        validate_deliverable_ledger_rows([_well_formed_row(status="shipped", closed_at=None)])


def test_validate_deliverable_ledger_rows_raises_on_bad_join_provenance():
    with pytest.raises(
        DeliverableLedgerValidationError, match="join_provenance.*invalid|invalid.*join_provenance"
    ):
        validate_deliverable_ledger_rows(
            [
                _well_formed_row(
                    closure_evidence={
                        "realizing_commits": ["abc123"],
                        "join_provenance": "not-a-real-value",
                    }
                )
            ]
        )


def test_validate_deliverable_ledger_rows_does_not_merely_warn(caplog):
    """The malformed path must raise, never WARN-and-skip like the fork loader does."""
    with caplog.at_level("WARNING"):
        with pytest.raises(DeliverableLedgerValidationError):
            validate_deliverable_ledger_rows([_well_formed_row(status="bogus")])

    # No row should have been silently accepted/skipped via a WARNING instead of a
    # raise — pin "raise instead of ANY warn", not just "raise instead of this one
    # particular wording". Review: coordinatorreview-integrator-574d6d25.
    assert not caplog.records


def test_validate_deliverable_ledger_rows_raises_on_superseded_without_target():
    with pytest.raises(
        DeliverableLedgerValidationError, match="superseded_by.*required|required.*superseded_by"
    ):
        validate_deliverable_ledger_rows(
            [_well_formed_row(status="superseded", superseded_by=None)]
        )


# ---------------------------------------------------------------------------
# sedge-07 — seed_deliverable_ledger_rows (Step 1) and dual_read_deliverable_id
# / dual_read_deliverable_ids_for_corpus (Step 2).
# ---------------------------------------------------------------------------


def _write_handoff(path: Path, deliverable_id: str | None = None, kind: str = "handoff") -> None:
    lines = ["---"]
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines.append(f"kind: {kind}")
    lines.append("---")
    lines.append("body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_ledger_only(worktree_root: Path, ledger_rows: list[dict]) -> Path:
    """Write an artifact carrying an empty `entries:` and the given `ledger:` rows."""
    state_dir = worktree_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_text(
        yaml.safe_dump({"entries": [], "ledger": ledger_rows}, sort_keys=False),
        encoding="utf-8",
    )
    return artifact_path


def test_loaded_equivalence_map_carries_all_19_entries():
    """AC1, read per the completion-notes reading: AC1's literal wording asks to
    compare the SEED's row count to 19, but the seed's row count is a corpus-
    derived deduped count with no reason to equal 19 (winners absorb multiple
    losers — research corpus §0). AC1's real intent — that the map DRIVING the
    seed carries all 19 declared entries, not the stale "14" — is discharged
    directly here against the real repo artifact.

    NOTE: this test intentionally reads the real on-disk
    state/deliverable-equivalence.yaml rather than a tmp_path fixture, so its
    pass/fail is coupled to future edits of that production ledger file — any
    addition/removal of an entries: row will break this test with no code
    change. If that happens, update the literal 19 below to match the new
    entry count."""
    repo_root = Path(__file__).resolve().parents[3]
    equivalence_map = load_equivalence_map(repo_root)
    assert len(equivalence_map) == 19


def test_seed_known_fork_loser_seeds_as_its_winner(tmp_path):
    """AC3: a known fork-loser id seeds under its declared WINNER, never its own
    raw id."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "2026-01-01_000000_example.md", "dlv-loser-example")

    _write_artifact(
        tmp_path,
        [{"loser": "dlv-loser-example", "winner": "dlv-winner-example", "evidence": "test"}],
    )

    rows = seed_deliverable_ledger_rows(tmp_path)

    seeded_ids = {row["deliverable_id"] for row in rows}
    assert "dlv-winner-example" in seeded_ids
    assert "dlv-loser-example" not in seeded_ids


def test_seed_rows_pass_validate_deliverable_ledger_rows(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "2026-01-01_000000_example.md", "dlv-plain-example")

    rows = seed_deliverable_ledger_rows(tmp_path)

    assert rows  # sanity: the fixture actually produced a row
    validate_deliverable_ledger_rows(rows)  # must not raise


def _make_guarded_open(real_open):
    """Build the archive write-guard for `open`, shared by the AC8 guard test and its
    negative control.

    Review: coordinatorcode-reviewer-s4-integration — the two tests previously each
    carried their own copy of this closure. Editing one and not the other would have
    silently decoupled the negative control from the guard it is the control FOR, so
    the shared definition is the point, not a tidiness preference.

    Catches "w"/"a"/"x": "w" alone misses append and exclusive-create.
    """

    def _guarded_open(file, mode="r", *args, **kwargs):
        norm = str(file).replace("\\", "/")
        if any(flag in mode for flag in ("w", "a", "x")) and "/archive/" in norm:
            raise AssertionError(f"write-mode open() reached an archive/ path: {file}")
        return real_open(file, mode, *args, **kwargs)

    return _guarded_open


def _make_guarded_replace(real_replace):
    """Build the archive write-guard for `os.replace`, shared by the AC8 guard test and
    its negative control — the `open` guard's symmetric twin, extracted for the same
    reason."""

    def _guarded_replace(src, dst, *args, **kwargs):
        norm = str(dst).replace("\\", "/")
        if "/archive/" in norm:
            raise AssertionError(f"os.replace() reached an archive/ path: {dst}")
        return real_replace(src, dst, *args, **kwargs)

    return _guarded_replace


def test_seed_makes_zero_writes_against_an_archived_fixture_path(tmp_path, monkeypatch):
    """AC8: the seed reads an archived artifact's deliverable_id but never opens
    it (or anything else) in write mode, and never `os.replace`s into
    `archive/`."""
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    archived = archive_dir / "2026-01-01_000000_roadmap-example.md"
    _write_handoff(archived, "dlv-archived-example", kind="roadmap-baton")

    monkeypatch.setattr(builtins, "open", _make_guarded_open(builtins.open))
    monkeypatch.setattr(os, "replace", _make_guarded_replace(os.replace))

    rows = seed_deliverable_ledger_rows(tmp_path)

    assert any(row["deliverable_id"] == "dlv-archived-example" for row in rows)


def test_seed_zero_write_guard_negative_control_open(tmp_path, monkeypatch):
    """Negative control for the `open` half of the AC8 zero-write guard: the guard must
    actually FIRE against a real violation, in every write mode it claims to catch.

    Without this, the guard test above only ever exercises its no-violation path and
    would pass identically against a broken guard.
    Review: coordinatorreview-integrator-574d6d25."""
    monkeypatch.setattr(builtins, "open", _make_guarded_open(builtins.open))

    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    violating_path = archive_dir / "violation.md"

    for mode in ("w", "a", "x"):
        with pytest.raises(AssertionError, match="write-mode open"):
            open(violating_path, mode, encoding="utf-8")


def test_seed_zero_write_guard_negative_control_replace(tmp_path, monkeypatch):
    """Negative control for the `os.replace` half of the AC8 zero-write guard.

    Review: coordinatorcode-reviewer-s4-integration — the first negative control named
    both guards in its docstring and exercised only `open`, so a broken `_guarded_replace`
    would have gone uncaught. That is the same inert-probe failure the negative control
    was added to close, reproduced for the second guard; this closes it for real."""
    monkeypatch.setattr(os, "replace", _make_guarded_replace(os.replace))

    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    src = tmp_path / "source.md"
    src.write_text("x", encoding="utf-8")
    violating_dst = archive_dir / "violation.md"

    with pytest.raises(AssertionError, match=r"os\.replace\(\) reached an archive/ path"):
        os.replace(src, violating_dst)


def test_dual_read_raises_on_divergent_ledger_and_frontmatter(tmp_path, caplog):
    """AC5: a deliberately divergent ledger/frontmatter pair RAISES, and does not
    merely warn."""
    artifact = tmp_path / "handoff.md"
    _write_handoff(artifact, "dlv-frontmatter-value")

    _write_ledger_only(
        tmp_path,
        [
            _well_formed_row(
                deliverable_id="dlv-ledger-value",
                status="open",
                closed_at=None,
                superseded_by=None,
                evidence_source="handoff.md",
            )
        ],
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(DeliverableDualReadMismatchError, match="mismatch"):
            dual_read_deliverable_id(tmp_path, str(artifact), {})

    # This must be a raise, never a mere warning (departure from
    # ownership_index.build_ownership_index's WARN precedent, per AC6) — pin
    # "raise instead of ANY warn", not just "raise instead of this one particular
    # wording". Review: coordinatorreview-integrator-574d6d25.
    assert not caplog.records


def test_batch_wrapper_collects_mismatch_without_aborting_the_walk(tmp_path):
    """The corpus-level wrapper catches the per-record raise into its `errors`
    arm and still returns every OTHER good row (Q1's op-fail-vs-read-fail
    split-by-altitude answer)."""
    good_path = tmp_path / "good.md"
    _write_handoff(good_path, "dlv-good")
    bad_path = tmp_path / "bad.md"
    _write_handoff(bad_path, "dlv-frontmatter-bad")

    _write_ledger_only(
        tmp_path,
        [
            _well_formed_row(
                deliverable_id="dlv-good",
                status="open",
                closed_at=None,
                superseded_by=None,
                evidence_source="good.md",
            ),
            _well_formed_row(
                deliverable_id="dlv-ledger-bad",
                status="open",
                closed_at=None,
                superseded_by=None,
                evidence_source="bad.md",
            ),
        ],
    )

    results, errors = dual_read_deliverable_ids_for_corpus(
        tmp_path, [str(good_path), str(bad_path)], {}
    )

    assert results[str(good_path)] == ("dlv-good", "hit")
    assert str(bad_path) not in results
    assert len(errors) == 1
    assert "dlv-ledger-bad" in errors[0]


def test_dual_read_three_way_outcome_is_distinguishable(tmp_path):
    """hit / genuine_miss / ledger_unreadable must be three distinguishable
    outcomes, never collapsed (research corpus §6)."""
    fm_only = tmp_path / "fm_only.md"
    _write_handoff(fm_only, "dlv-fm-only")

    # genuine_miss: ledger present and READABLE, but no row references this path.
    _write_ledger_only(
        tmp_path,
        [
            _well_formed_row(
                deliverable_id="dlv-other",
                status="open",
                closed_at=None,
                superseded_by=None,
                evidence_source="other.md",
            )
        ],
    )
    result, outcome = dual_read_deliverable_id(tmp_path, str(fm_only), {})
    assert outcome == "genuine_miss"
    assert result == "dlv-fm-only"

    # ledger_unreadable: the artifact is PRESENT but malformed YAML.
    _reset_deliverable_ledger_cache()
    (tmp_path / "state" / "deliverable-equivalence.yaml").write_text(
        "not: [valid: yaml: at all", encoding="utf-8"
    )
    result2, outcome2 = dual_read_deliverable_id(tmp_path, str(fm_only), {})
    assert outcome2 == "ledger_unreadable"
    assert result2 == "dlv-fm-only"

    # hit: the ledger names an id for this path and frontmatter is silent.
    no_fm = tmp_path / "no_fm.md"
    _write_handoff(no_fm, deliverable_id=None)
    _write_ledger_only(
        tmp_path,
        [
            _well_formed_row(
                deliverable_id="dlv-hit-value",
                status="open",
                closed_at=None,
                superseded_by=None,
                evidence_source="no_fm.md",
            )
        ],
    )
    _reset_deliverable_ledger_cache()
    result3, outcome3 = dual_read_deliverable_id(tmp_path, str(no_fm), {})
    assert outcome3 == "hit"
    assert result3 == "dlv-hit-value"
