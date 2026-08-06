"""Unit tests for the content_hash stamping stage in envelope.py.

Purpose: verify that _stamp_content_hash correctly attaches / omits the R5 change-signal
sub-shape, that the version gate keeps 2.4.0 a complete no-op, and that nested record
arrays (completion_rollups.day, backlogs.bug) are reached by the SECMAP walk.

No golden-fixture dependency — all envelopes are synthetic in-memory objects.

Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md
Producer contract: coordinator_core/contract/example-retrieval-repo-producer-contract.md § 3.3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import cache
from coordinator_core.ops.emit.envelope import (
    SECMAP,
    _get_dotpath,
    _resolve_provenance_path,
    _stamp_content_hash,
)
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.testing.home_sandbox import sandbox_home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(repo_root: Path) -> EmitContext:
    """Minimal EmitContext with repo_root set; other fields are non-load-bearing for stamping."""
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root,
        git_branch="test-branch",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


def _empty_envelope() -> dict:
    """Return a minimal envelope skeleton sufficient for the stamping stage."""
    return {
        "handoffs": [],
        "backlogs": {"bug": [], "debt": [], "improvement": []},
        "review_trail": [],
        "routine_signals": [],
        "completion_rollups": {"day": [], "week": []},
        "goals_current": [],
        "coordinator_roots": [],
        "branches": [],
        "lessons": [],
        "plans": [],
        "cross_repo_memos": [],
        "roadmaps": [],
        "trackers": [],
        "health": [],
        "decision_guides": [],
        "session_hierarchies": [],
        "file_attributions": [],
        "initiatives": [],
        "exec_summaries": [],
        "narrative_views": None,
        "malformed_records": {},
    }


def _make_record(path: str) -> dict:
    """Minimal work-state record with the given provenance.path."""
    return {
        "id": "test-record",
        "provenance": {
            "source_kind": "local",
            "repo": "test/repo",
            "ref": None,
            "path": path,
            "observed_at": "2026-07-04T00:00:00Z",
            "derivation": "parsed",
        },
    }


# ---------------------------------------------------------------------------
# Core stamping behaviour
# ---------------------------------------------------------------------------

def test_stamps_resolvable_record(tmp_path: Path) -> None:
    """A record whose provenance.path resolves to a real file gets content_hash."""
    source_file = tmp_path / "some_record.md"
    source_file.write_bytes(b"hello content hash world")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    record = envelope["handoffs"][0]
    assert "content_hash" in record, "content_hash should be present for a resolvable file"
    ch = record["content_hash"]
    assert ch["algo"] == "sha256"
    assert ch["hex"] == cache.compute_stamp(source_file)
    assert len(ch["hex"]) == 64
    assert all(c in "0123456789abcdef" for c in ch["hex"])
    # source_path must be the verbatim provenance.path string, NOT the resolved abs path
    assert ch["source_path"] == str(source_file)


def test_omits_when_path_is_empty(tmp_path: Path) -> None:
    """A record with provenance.path == '' must NOT receive content_hash."""
    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(""))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert "content_hash" not in envelope["handoffs"][0]


def test_omits_when_path_does_not_resolve(tmp_path: Path) -> None:
    """A record whose path does not resolve to any file must NOT receive content_hash."""
    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record("nonexistent/path/to/nowhere.md"))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert "content_hash" not in envelope["handoffs"][0]
    # Must not raise — graceful omit only


# ---------------------------------------------------------------------------
# Version gate
# ---------------------------------------------------------------------------

def test_version_gate_2_4_0_is_noop(tmp_path: Path) -> None:
    """At schema_version 2.4.0 the stage must be a complete no-op."""
    source_file = tmp_path / "record.md"
    source_file.write_bytes(b"content")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.4.0")

    # No record anywhere should have content_hash
    assert "content_hash" not in envelope["handoffs"][0]


def test_version_gate_2_5_0_stamps(tmp_path: Path) -> None:
    """At schema_version 2.5.0 the stage activates and stamps resolvable records."""
    source_file = tmp_path / "record.md"
    source_file.write_bytes(b"content")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert "content_hash" in envelope["handoffs"][0]


@pytest.mark.parametrize("bad_version", ["", "notasemver", "2.5", "2"])
def test_version_gate_malformed_versions_are_noop(bad_version: str, tmp_path: Path) -> None:
    """Malformed or short version strings must be treated as below the gate (no-op)."""
    source_file = tmp_path / "record.md"
    source_file.write_bytes(b"content")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, bad_version)

    assert "content_hash" not in envelope["handoffs"][0]


# ---------------------------------------------------------------------------
# Nested record walk coverage
# ---------------------------------------------------------------------------

def test_nested_completion_rollups_day_is_reached(tmp_path: Path) -> None:
    """Records under completion_rollups.day (nested dot-path) are stamped."""
    source_file = tmp_path / "rollup.md"
    source_file.write_bytes(b"rollup body")

    envelope = _empty_envelope()
    envelope["completion_rollups"]["day"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert "content_hash" in envelope["completion_rollups"]["day"][0]


def test_nested_backlogs_bug_is_reached(tmp_path: Path) -> None:
    """Records under backlogs.bug (nested dot-path) are stamped."""
    source_file = tmp_path / "bug.yaml"
    source_file.write_bytes(b"bug body")

    envelope = _empty_envelope()
    envelope["backlogs"]["bug"].append(_make_record(str(source_file)))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert "content_hash" in envelope["backlogs"]["bug"][0]


# ---------------------------------------------------------------------------
# Path resolution helper (unit)
# ---------------------------------------------------------------------------

def test_resolve_provenance_path_absolute(tmp_path: Path) -> None:
    """An absolute path that is a file resolves directly."""
    f = tmp_path / "file.md"
    f.write_bytes(b"data")
    result = _resolve_provenance_path(str(f), tmp_path / "other", Path("/somewhere"))
    assert result == f


def test_resolve_provenance_path_repo_relative(tmp_path: Path) -> None:
    """A relative path that resolves under repo_root is returned."""
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "file.md"
    f.write_bytes(b"data")
    result = _resolve_provenance_path("sub/file.md", tmp_path, Path("/nowhere"))
    assert result == f


def test_resolve_provenance_path_missing_returns_none(tmp_path: Path) -> None:
    """A path that does not resolve anywhere returns None without raising."""
    result = _resolve_provenance_path("ghost.md", tmp_path, tmp_path)
    assert result is None


def test_resolve_provenance_path_tilde_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A '~'-rooted path expands against HOME and resolves to the real file.

    Regression: '~/...' is not is_absolute(), so without expanduser() it fell through
    all three candidates and returned None — silently omitting content_hash.
    """
    home = sandbox_home(monkeypatch, tmp_path / "home")
    f = home / "cockpit-emission.json"
    f.write_bytes(b"data")
    result = _resolve_provenance_path("~/cockpit-emission.json", tmp_path / "other", Path("/nowhere"))
    assert result == f


def test_resolve_provenance_path_tilde_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A '~'-rooted path that does not resolve to a real file still returns None gracefully."""
    sandbox_home(monkeypatch, tmp_path / "home")
    result = _resolve_provenance_path("~/nonexistent-file.json", tmp_path, tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# KPI/regression: per-call dedup — many records sharing one abs_path must
# read/hash that file exactly once, not once per record.
#
# Regression target: a real corpus profile showed 5864 stamped records
# resolving to only 2633 unique on-disk paths (many records — different
# sections, different dotpaths — sharing the same provenance.path). Before
# this dedup, cache.compute_stamp() ran once per RECORD; this test pins that
# it now runs once per unique PATH within a single _stamp_content_hash() call.
# ---------------------------------------------------------------------------

def test_shared_path_hashed_once_across_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple records resolving to the SAME abs_path call compute_stamp exactly once."""
    source_file = tmp_path / "shared.md"
    source_file.write_bytes(b"shared body")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(source_file)))
    envelope["backlogs"]["bug"].append(_make_record(str(source_file)))
    envelope["completion_rollups"]["day"].append(_make_record(str(source_file)))

    calls: list = []
    real_compute_stamp = cache.compute_stamp

    def counting_compute_stamp(path):
        calls.append(path)
        return real_compute_stamp(path)

    monkeypatch.setattr(cache, "compute_stamp", counting_compute_stamp)

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert len(calls) == 1, f"expected exactly one compute_stamp call for one shared path, got {len(calls)}"

    expected_hex = real_compute_stamp(source_file)
    assert envelope["handoffs"][0]["content_hash"]["hex"] == expected_hex
    assert envelope["backlogs"]["bug"][0]["content_hash"]["hex"] == expected_hex
    assert envelope["completion_rollups"]["day"][0]["content_hash"]["hex"] == expected_hex


def test_distinct_paths_each_hashed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two distinct abs_paths each get their own compute_stamp call (no over-collapsing)."""
    f1 = tmp_path / "one.md"
    f1.write_bytes(b"one")
    f2 = tmp_path / "two.md"
    f2.write_bytes(b"two")

    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(str(f1)))
    envelope["backlogs"]["bug"].append(_make_record(str(f2)))

    calls: list = []
    real_compute_stamp = cache.compute_stamp

    def counting_compute_stamp(path):
        calls.append(path)
        return real_compute_stamp(path)

    monkeypatch.setattr(cache, "compute_stamp", counting_compute_stamp)

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    assert len(calls) == 2
    assert envelope["handoffs"][0]["content_hash"]["hex"] != envelope["backlogs"]["bug"][0]["content_hash"]["hex"]


# ---------------------------------------------------------------------------
# source_path contract: verbatim provenance.path, not the resolved abs path
# ---------------------------------------------------------------------------

def test_source_path_is_verbatim_provenance_path(tmp_path: Path) -> None:
    """content_hash.source_path must equal provenance.path verbatim (contract § 3.3)."""
    source_file = tmp_path / "artifact.md"
    source_file.write_bytes(b"body")

    # Use a repo-relative provenance.path (not the absolute path)
    rel_path = "artifact.md"
    envelope = _empty_envelope()
    envelope["handoffs"].append(_make_record(rel_path))

    ctx = _make_ctx(tmp_path)
    _stamp_content_hash(envelope, ctx, "2.5.0")

    ch = envelope["handoffs"][0]["content_hash"]
    # Must be the original relative string, NOT the resolved absolute path
    assert ch["source_path"] == rel_path
    assert ch["source_path"] != str(source_file)
