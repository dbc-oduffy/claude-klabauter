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

from pathlib import Path

import pytest

from coordinator_core.ops.deliverable_equivalence import (
    _reset_equivalence_map_cache,
    canonicalize,
    load_equivalence_map,
)


@pytest.fixture(autouse=True)
def _reset_memo():
    """Clear the module-scope memo before and after each test.

    Required because pytest shares one interpreter across tests, unlike the
    spawn-per-call production model this memo is designed for (see
    _reset_equivalence_map_cache's own docstring).
    """
    _reset_equivalence_map_cache()
    yield
    _reset_equivalence_map_cache()


def _write_artifact(worktree_root: Path, entries: list[dict]) -> Path:
    """Write a minimal state/deliverable-equivalence.yaml fixture and return its path."""
    state_dir = worktree_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    lines = ["entries:"]
    for entry in entries:
        lines.append(f"  - loser: {entry['loser']}")
        lines.append(f"    winner: {entry['winner']}")
        lines.append(f"    evidence: {entry.get('evidence', 'test fixture')!r}")
    artifact_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
