"""
coordinator_core.testing.test_golden — hermetic unit tests for the golden-fixture
helper (`coordinator_core.testing.golden`).

Purpose: proves the three load-bearing behaviors the rest of the de-node sweep
depends on: (1) a missing golden is a hard `GoldenMissingError`, never a skip; (2)
`CAPTURE_GOLDENS=1` round-trips a value through disk cleanly; (3) JSON comparison is
normalized (parsed-object equality), not a literal-text diff.

Port source: none — net-new (2026-07-21 de-node Gate A).
Spec backlink: docs/plans/2026-07-21-parity-suites-freeze-to-goldens.md § C0

Negative-spec: every fixture this suite exercises is written under pytest's
`tmp_path`, never committed to disk — `_resolve_goldens_dir` is monkeypatched to
point at a tmp directory for every test that touches the filesystem, so this suite
never writes into its own package tree's `_goldens/` (consistent with this package's
existing "no committed fixture dummies" convention — see `coordinator_core/testing/
__init__.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.testing import golden


@pytest.fixture()
def goldens_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect golden.py's caller-resolved `_goldens/` dir to a throwaway tmp dir."""
    target = tmp_path / "_goldens"
    monkeypatch.setattr(golden, "_resolve_goldens_dir", lambda: target)
    return target


def test_resolve_goldens_dir_points_beside_caller() -> None:
    """Un-monkeypatched: resolves relative to THIS file's own directory (pure path
    computation only — asserts the value, never writes anything to disk)."""
    resolved = golden._resolve_goldens_dir()
    assert resolved == Path(__file__).resolve().parent / "_goldens"


def test_load_golden_missing_raises_hard_error(goldens_dir: Path) -> None:
    with pytest.raises(golden.GoldenMissingError):
        golden.load_golden("some-namespace", "absent-case", kind="text")


def test_load_golden_missing_never_skips(goldens_dir: Path) -> None:
    # Explicit negative-spec proof: the exception raised is exactly GoldenMissingError
    # — a plain hard failure, not pytest's Skipped outcome or any other escape hatch.
    with pytest.raises(golden.GoldenMissingError) as excinfo:
        golden.load_golden("ns", "case", kind="json")
    assert type(excinfo.value) is golden.GoldenMissingError
    assert excinfo.type.__name__ != "Skipped"


def test_capture_then_load_round_trips_text(
    goldens_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTURE_GOLDENS", "1")
    golden.assert_matches_golden(b"hello world\n", "ns", "text-case", kind="text")

    monkeypatch.delenv("CAPTURE_GOLDENS", raising=False)
    loaded = golden.load_golden("ns", "text-case", kind="text")
    assert loaded == b"hello world\n"
    # Round-trip also satisfies the assertion path (no exception raised).
    golden.assert_matches_golden(b"hello world\n", "ns", "text-case", kind="text")


def test_capture_then_load_round_trips_json(
    goldens_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"orderedPaths": ["a", "b"], "terminatedEarly": ""}
    monkeypatch.setenv("CAPTURE_GOLDENS", "1")
    golden.assert_matches_golden(json.dumps(payload), "ns", "json-case", kind="json")

    monkeypatch.delenv("CAPTURE_GOLDENS", raising=False)
    loaded = golden.load_golden("ns", "json-case", kind="json")
    assert loaded == payload


def test_json_comparison_is_normalized_not_literal_text(
    goldens_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTURE_GOLDENS", "1")
    golden.assert_matches_golden(
        json.dumps({"a": 1, "b": 2}), "ns", "normalize-case", kind="json"
    )
    monkeypatch.delenv("CAPTURE_GOLDENS", raising=False)

    # Different key order and whitespace than what was captured — still matches,
    # because comparison is on the parsed object, not the literal fixture text.
    reordered = '{\n  "b": 2,\n  "a": 1\n}'
    golden.assert_matches_golden(reordered, "ns", "normalize-case", kind="json")

    # Genuine value divergence still fails loud.
    with pytest.raises(AssertionError):
        golden.assert_matches_golden(
            json.dumps({"a": 1, "b": 999}), "ns", "normalize-case", kind="json"
        )


def test_text_mismatch_raises_assertion_error(
    goldens_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTURE_GOLDENS", "1")
    golden.assert_matches_golden(b"expected-value", "ns", "mismatch-case", kind="text")
    monkeypatch.delenv("CAPTURE_GOLDENS", raising=False)

    with pytest.raises(AssertionError):
        golden.assert_matches_golden(b"different-value", "ns", "mismatch-case", kind="text")


def test_unknown_kind_raises_value_error(goldens_dir: Path) -> None:
    with pytest.raises(ValueError):
        golden.load_golden("ns", "case", kind="xml")


def test_is_capturing_reflects_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTURE_GOLDENS", raising=False)
    assert golden.is_capturing() is False
    monkeypatch.setenv("CAPTURE_GOLDENS", "1")
    assert golden.is_capturing() is True
    monkeypatch.setenv("CAPTURE_GOLDENS", "0")
    assert golden.is_capturing() is False
