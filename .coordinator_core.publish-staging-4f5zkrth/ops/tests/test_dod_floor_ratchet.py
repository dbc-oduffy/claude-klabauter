"""
coordinator_core.ops.tests.test_dod_floor_ratchet

Tests for the per-dimension DoD floor ratchet (C9 leg (c),
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C9).

Coverage:
  - raise accepted, writes a fresh floor record.
  - lower refused: RatchetRefused, file left byte-unchanged.
  - equal is a no-op: no write, no exception.
  - missing stored floor: first write always succeeds (genuinely absent is
    a fresh start).
  - malformed stored floor (bad JSON / non-dict / missing/non-numeric
    fail_under): refused with `StoredFloorUnreadable`, never overwritten
    -- including by a lower value -- and the file is left byte-unchanged;
    a present-but-corrupt floor is an error state, not "no floor yet".
  - the existing `.github/docstring-coverage-floor.json` consumer
    (`gate_dimension_docstrings._load_fail_under`) still reads a floor this
    module writes, unchanged file shape.
  - `raise_floor` measurement seam: a registered fake `Measurer` drives the
    write path; an unregistered dimension raises a legible `ValueError`
    rather than silently doing nothing or accepting a bare number.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C9
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dod_floor_ratchet import (
    FloorMeasurement,
    MEASURERS,
    RatchetRefused,
    StoredFloorUnreadable,
    floor_path,
    main,
    read_floor,
    raise_floor,
    register_measurer,
    write_floor,
)


def _measurement(value: float, sha: str = "deadbeef", via: str = "test-stub") -> FloorMeasurement:
    return FloorMeasurement(value=value, head_sha=sha, measured_via=via)


def test_first_write_always_succeeds(tmp_path):
    result = write_floor("widgets", _measurement(50.0), repo_root=tmp_path)
    assert result == floor_path("widgets", tmp_path)
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["fail_under"] == 50.0
    assert record["measured_at_head_sha"] == "deadbeef"
    assert record["measured_via"] == "test-stub"
    assert "ratchet_rule" in record and "_purpose" in record


def test_raise_accepted(tmp_path):
    write_floor("widgets", _measurement(50.0), repo_root=tmp_path)
    result = write_floor("widgets", _measurement(60.0), repo_root=tmp_path)
    assert result == floor_path("widgets", tmp_path)
    assert read_floor("widgets", tmp_path)["fail_under"] == 60.0


def test_lower_refused_and_file_unchanged(tmp_path):
    write_floor("widgets", _measurement(50.0), repo_root=tmp_path)
    before = floor_path("widgets", tmp_path).read_text(encoding="utf-8")

    with pytest.raises(RatchetRefused) as excinfo:
        write_floor("widgets", _measurement(40.0), repo_root=tmp_path)

    assert excinfo.value.stored == 50.0
    assert excinfo.value.attempted == 40.0
    after = floor_path("widgets", tmp_path).read_text(encoding="utf-8")
    assert before == after


def test_equal_is_a_noop(tmp_path):
    write_floor("widgets", _measurement(50.0), repo_root=tmp_path)
    before = floor_path("widgets", tmp_path).read_text(encoding="utf-8")

    result = write_floor("widgets", _measurement(50.0), repo_root=tmp_path)

    assert result is None
    after = floor_path("widgets", tmp_path).read_text(encoding="utf-8")
    assert before == after


def test_missing_floor_file_reads_as_none(tmp_path):
    assert read_floor("nonexistent-dim", tmp_path) is None


@pytest.mark.parametrize(
    "payload",
    [
        "{not valid json",
        "[]",
        "{}",
        json.dumps({"fail_under": "not-a-number"}),
        json.dumps({"fail_under": None}),
    ],
)
def test_malformed_stored_floor_refuses_write(tmp_path, payload):
    """This was `test_malformed_stored_floor_treated_as_absent`, asserting
    that a malformed stored floor is silently treated as "no floor yet" and
    freely overwritten. That was the bug: a corrupt/truncated floor file is
    indistinguishable from "never written" under that behavior, so a lower
    re-measurement could silently reset the ratchet. Corrected: malformed
    is a hard refusal, byte-unchanged file, regardless of whether the new
    value would have been higher or lower."""
    path = floor_path("widgets", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    assert read_floor("widgets", tmp_path) is None

    before = path.read_bytes()
    with pytest.raises(StoredFloorUnreadable) as excinfo:
        write_floor("widgets", _measurement(10.0), repo_root=tmp_path)
    assert excinfo.value.dimension == "widgets"
    assert excinfo.value.path == path
    assert path.read_bytes() == before


def test_malformed_stored_floor_cannot_be_overwritten_by_a_lower_value(tmp_path):
    """The regression this chunk closes: a malformed stored floor must not
    be silently overwritable by a *lower* value either -- it is refused
    outright, not ratchet-compared."""
    path = floor_path("widgets", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(StoredFloorUnreadable):
        write_floor("widgets", _measurement(0.0), repo_root=tmp_path)
    assert path.read_bytes() == before


def test_docstrings_uses_the_landed_filename_unchanged(tmp_path):
    path = floor_path("docstrings", tmp_path)
    assert path == tmp_path / ".github" / "docstring-coverage-floor.json"


def test_existing_docstrings_consumer_still_reads_a_written_floor(tmp_path):
    """The committed consumer, `gate_dimension_docstrings._load_fail_under`,
    reads `.github/docstring-coverage-floor.json` via `json.loads(...)
    ["fail_under"]` verbatim -- reproduced here (not imported) so this test
    does not depend on that module's internals shifting, only on the on-disk
    contract it already ships with."""
    from coordinator_core.ops import gate_dimension_docstrings

    write_floor("docstrings", _measurement(91.0, sha="cafef00d"), repo_root=tmp_path)

    fail_under = gate_dimension_docstrings._load_fail_under(tmp_path)
    assert fail_under == 91.0


def test_raise_floor_uses_registered_measurer(tmp_path, monkeypatch):
    calls = []

    def fake_measurer(repo_root):
        calls.append(repo_root)
        return _measurement(77.0, sha="feedface", via="fake-measurer")

    register_measurer("fake-dimension", fake_measurer)
    try:
        result = raise_floor("fake-dimension", repo_root=tmp_path)
    finally:
        del MEASURERS["fake-dimension"]

    assert result == floor_path("fake-dimension", tmp_path)
    assert calls == [tmp_path]
    record = read_floor("fake-dimension", tmp_path)
    assert record["fail_under"] == 77.0
    assert record["measured_via"] == "fake-measurer"


def test_raise_floor_refuses_unregistered_dimension(tmp_path):
    assert "totally-unregistered-dimension" not in MEASURERS
    with pytest.raises(ValueError, match="no measurer registered"):
        raise_floor("totally-unregistered-dimension", repo_root=tmp_path)


def test_docstrings_measurer_is_registered_at_import_time():
    assert "docstrings" in MEASURERS


# Review: coordinator:code-reviewer-3c4f24d7 -- CLI/argparse wiring (`main()`)
# had no direct coverage; only the library functions it delegates to were
# exercised. `main()` reads `repo_root=None` internally (cwd-relative), so
# these tests chdir into `tmp_path` via monkeypatch rather than passing
# `repo_root` through argv (there is no such flag, by design).
def test_main_raises_and_prints_wrote(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_measurer(repo_root):
        return _measurement(88.0, sha="cliface", via="cli-fake-measurer")

    register_measurer("cli-fake-dimension", fake_measurer)
    try:
        rc = main(["cli-fake-dimension"])
    finally:
        del MEASURERS["cli-fake-dimension"]

    assert rc == 0
    out = capsys.readouterr().out
    assert "floor raised, wrote" in out
    assert read_floor("cli-fake-dimension", tmp_path)["fail_under"] == 88.0


def test_main_noop_prints_no_change(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_floor("cli-noop-dimension", _measurement(50.0), repo_root=tmp_path)

    def fake_measurer(repo_root):
        return _measurement(50.0, sha="cliface", via="cli-fake-measurer")

    register_measurer("cli-noop-dimension", fake_measurer)
    try:
        rc = main(["cli-noop-dimension"])
    finally:
        del MEASURERS["cli-noop-dimension"]

    assert rc == 0
    out = capsys.readouterr().out
    assert "no change" in out


def test_main_unregistered_dimension_prints_error_and_returns_1(capsys):
    assert "totally-unregistered-dimension" not in MEASURERS
    rc = main(["totally-unregistered-dimension"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no measurer registered" in err


def test_main_lower_measurement_refused_prints_error_and_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_floor("cli-refuse-dimension", _measurement(50.0), repo_root=tmp_path)

    def fake_measurer(repo_root):
        return _measurement(10.0, sha="cliface", via="cli-fake-measurer")

    register_measurer("cli-refuse-dimension", fake_measurer)
    try:
        rc = main(["cli-refuse-dimension"])
    finally:
        del MEASURERS["cli-refuse-dimension"]

    assert rc == 1
    err = capsys.readouterr().err
    assert "refused to lower" in err
