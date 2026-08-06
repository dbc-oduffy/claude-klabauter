"""Regression tests — sections/file_attribution.py F1/F2/F3 defect fixes.

F1 (break-class): ``_run_producer`` used to collapse EVERY failure mode (subprocess spawn
error, timeout, non-zero exit, unparseable JSON, non-list payload) to a bare ``[]`` —
indistinguishable from "the producer ran and genuinely attributed zero files this run".
These tests assert the fix: ``_run_producer`` returns ``(records, producer_error)`` and
``collect()`` surfaces a producer failure as a ``producer_failed: True`` marker row in
``malformed_records.file_attributions`` (plus a loud ``warnings.warn``), while a genuine
empty result still yields ``([], [])`` non-fatally (graceful-absent preserved).

F2 (break-class): the FileAttribution contract declares ``file_path`` as repo-relative,
forward-slash, directly comparable to ``git ls-files`` output. The producer emits
absolute paths (in-repo and out-of-repo alike). These tests assert the porter-owned fix:
in-repo absolute paths are rewritten to forward-slash repo-relative form; out-of-repo/
ephemeral paths are excluded from ``file_attributions`` AND counted/visible as an
``excluded: True`` marker row in ``malformed_records.file_attributions`` — never silently
dropped. Windows-shaped inputs (drive letters, backslashes, mixed separators, sibling-repo
prefix collisions) are covered directly at the ``_relativize_or_exclude`` unit level so the
containment logic is exercised the same way regardless of the host OS running the suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.sections.file_attribution import (
    _PRODUCER_TIMEOUT_SECONDS,
    _relativize_or_exclude,
    _run_producer,
    collect,
)


def _make_ctx(repo_root: str, repo_name: str = "test-org/test-repo") -> MagicMock:
    """Minimal EmitContext stub sufficient for section collect() calls under test."""
    ctx = MagicMock()
    ctx.repo_name = repo_name
    ctx.repo_root = Path(repo_root)
    ctx.coordinator_root = Path(repo_root) / "coordinator"
    ctx.git_branch = "main"
    ctx.git_sha = "deadbeef"
    ctx.observed_at = "2026-07-28T00:00:00Z"
    return ctx


_RUN_TARGET = "coordinator_core.ops.emit.sections.file_attribution.subprocess.run"


# ---------------------------------------------------------------------------
# _run_producer — distinguishing success-empty from failure
# ---------------------------------------------------------------------------

def test_run_producer_success_empty_list_has_no_error():
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is None


def test_run_producer_success_with_records_has_no_error():
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='[{"file_path": "x.py"}]', stderr=""
        )
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == [{"file_path": "x.py"}]
    assert error is None


def test_run_producer_oserror_is_reported_as_error():
    with patch(_RUN_TARGET, side_effect=OSError("no such file")):
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is not None
    assert "OSError" in error


def test_run_producer_timeout_is_reported_as_error():
    with patch(
        _RUN_TARGET,
        side_effect=subprocess.TimeoutExpired(
            cmd=["x"], timeout=_PRODUCER_TIMEOUT_SECONDS
        ),
    ):
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is not None
    assert "timed out" in error
    assert str(_PRODUCER_TIMEOUT_SECONDS) in error


def test_run_producer_passes_the_named_timeout_ceiling_to_subprocess():
    """The call site uses the named constant rather than its own literal.

    Every other timeout test mocks ``subprocess.run`` to *raise*, so none of them observes
    the ``timeout=`` kwarg at all — a hardcoded literal at the call site would drift from the
    constant with nothing failing.

    Deliberately paired with ``test_producer_timeout_ceiling_clears_the_measured_cold_cost``
    rather than merged into it: this equality is self-referential and cannot detect a revert
    that lowers the constant AND the call site together, which is exactly the shape a
    "tidy up the timeout" edit takes. Distinct assertions so each failure mode names itself.
    Review: coordinator:review-integrator — flagged the merged form's blind spot.
    """
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        _run_producer(Path("/does/not/matter"))

    assert mock_run.call_args.kwargs["timeout"] == _PRODUCER_TIMEOUT_SECONDS


def test_producer_timeout_ceiling_clears_the_measured_cold_cost():
    """The ceiling's VALUE must retain headroom over a cold full-corpus scan.

    Independent of where the call site reads it from. Cold is routine rather than
    exceptional — any ``_DERIVATION_VERSION`` bump invalidates the whole cache and forces a
    full rescan — and tripping the ceiling reaches the F1 path and emits an empty required
    array, i.e. silent data loss, so the floor here is a correctness bound and not a style
    preference.

    The floor is deliberately expressed as an order-of-magnitude bound, not as a multiple of
    a specific measurement, because the absolute cold cost is NOT reproducible: three runs of
    the same nominal quantity (non-recursive path, empty stat cache) gave 14.12s, 2.56s and
    4.87s, varying with OS page-cache state and concurrent load on the box. What replicates is
    the ratio — the recursive read costs ~2.5x the non-recursive one, measured back-to-back in
    one run (12.16s vs 4.87s). Do not tighten this bound on the strength of a single fast
    measurement; a fast reading means the page cache was warm, not that the work got cheaper.
    """
    assert _PRODUCER_TIMEOUT_SECONDS >= 60


def test_run_producer_nonzero_exit_is_reported_as_error():
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Traceback: boom"
        )
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is not None
    assert "exited with code 1" in error
    assert "boom" in error


def test_run_producer_malformed_json_is_reported_as_error():
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{not json", stderr=""
        )
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is not None
    assert "not valid JSON" in error


def test_run_producer_non_list_payload_is_reported_as_error():
    with patch(_RUN_TARGET) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"oops": true}', stderr=""
        )
        records, error = _run_producer(Path("/does/not/matter"))

    assert records == []
    assert error is not None
    assert "non-list" in error


# ---------------------------------------------------------------------------
# collect() — producer failure surfaces as an observable malformed marker row;
# genuine empty stays non-fatal (graceful-absent preserved).
# ---------------------------------------------------------------------------

_RUN_PRODUCER_TARGET = "coordinator_core.ops.emit.sections.file_attribution._run_producer"


def test_collect_genuine_empty_result_is_non_fatal(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    with patch(_RUN_PRODUCER_TARGET, return_value=([], None)):
        records, malformed = collect(ctx)

    assert records == []
    assert malformed == []


def test_collect_producer_failure_yields_distinguishable_malformed_marker(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    with patch(
        _RUN_PRODUCER_TARGET, return_value=([], "producer exited with code 1: boom")
    ):
        with pytest.warns(UserWarning, match="producer failed"):
            records, malformed = collect(ctx)

    assert records == []
    assert len(malformed) == 1
    marker = malformed[0]
    assert marker["producer_failed"] is True
    assert marker["path"] is None
    assert "boom" in marker["reason"]


def test_collect_producer_failure_does_not_raise(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    with patch(_RUN_PRODUCER_TARGET, return_value=([], "some failure")):
        with pytest.warns(UserWarning):
            records, malformed = collect(ctx)  # must not raise

    assert isinstance(records, list)
    assert isinstance(malformed, list)


# ---------------------------------------------------------------------------
# collect() — F2 path normalisation / exclusion
# ---------------------------------------------------------------------------

def test_collect_in_repo_absolute_path_becomes_repo_relative(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    raw = [
        {
            "session_id": "s1",
            "file_path": str(tmp_path / "sub" / "file.py"),
            "provenance": {"derivation": "derived", "ref": {"branch": "x", "sha": "y"}},
        }
    ]
    with patch(_RUN_PRODUCER_TARGET, return_value=(raw, None)):
        records, malformed = collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["file_path"] == "sub/file.py"
    assert records[0]["provenance"]["derivation"] == "parsed"
    assert records[0]["provenance"]["ref"] is None


def test_collect_out_of_repo_path_is_excluded_and_counted(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    sibling = tmp_path.parent / "some-sibling-repo" / "file.py"
    raw = [
        {
            "session_id": "s1",
            "file_path": str(sibling),
            "provenance": {"derivation": "derived", "ref": None},
        }
    ]
    with patch(_RUN_PRODUCER_TARGET, return_value=(raw, None)):
        records, malformed = collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["excluded"] is True
    assert malformed[0]["path"] == str(sibling)
    assert malformed[0]["session_id"] == "s1"


def test_collect_non_dict_array_element_is_excluded_and_counted(tmp_path):
    """Review: code-reviewer (Finding 1) — a non-dict element in the producer's JSON
    array (e.g. the producer emits `[123, {...}]`) must be counted in `malformed`, not
    silently dropped via a bare `continue`."""
    ctx = _make_ctx(str(tmp_path))
    good = {
        "session_id": "s1",
        "file_path": str(tmp_path / "a.py"),
        "provenance": {"derivation": "derived", "ref": None},
    }
    raw = [123, None, good]
    with patch(_RUN_PRODUCER_TARGET, return_value=(raw, None)):
        records, malformed = collect(ctx)

    assert len(records) == 1
    assert records[0]["file_path"] == "a.py"
    assert len(malformed) == 2
    assert all(m["malformed_type"] is True for m in malformed)
    assert all(m["path"] is None for m in malformed)


def test_collect_mixed_rows_partition_correctly(tmp_path):
    ctx = _make_ctx(str(tmp_path))
    in_repo = str(tmp_path / "a.py")
    out_of_repo = str(tmp_path.parent / "scratch" / "b.py")
    raw = [
        {"session_id": "s1", "file_path": in_repo, "provenance": {"derivation": "derived", "ref": None}},
        {"session_id": "s2", "file_path": out_of_repo, "provenance": {"derivation": "derived", "ref": None}},
    ]
    with patch(_RUN_PRODUCER_TARGET, return_value=(raw, None)):
        records, malformed = collect(ctx)

    assert len(records) == 1
    assert records[0]["file_path"] == "a.py"
    assert len(malformed) == 1
    assert malformed[0]["excluded"] is True
    assert malformed[0]["path"] == out_of_repo


# ---------------------------------------------------------------------------
# _relativize_or_exclude — unit-level containment/normalisation coverage,
# including Windows-shaped inputs regardless of host OS.
# ---------------------------------------------------------------------------

def test_relativize_posix_in_repo():
    result = _relativize_or_exclude("/Users/x/claude-klabauter/sub/file.py", Path("/Users/x/claude-klabauter"))
    assert result == "sub/file.py"


def test_relativize_posix_out_of_repo():
    result = _relativize_or_exclude("/Users/x/example-doctrine-repo/sub/file.py", Path("/Users/x/claude-klabauter"))
    assert result is None


def test_relativize_posix_sibling_prefix_collision_is_excluded():
    """A sibling dir sharing a path PREFIX (claude-klabauter vs claude_klabauter2) must not
    false-positive as in-repo — this is exactly the bug a string ``startswith`` check
    would introduce."""
    result = _relativize_or_exclude(
        "/Users/x/claude_klabauter2/file.py", Path("/Users/x/claude-klabauter")
    )
    assert result is None


def test_relativize_already_relative_path_passes_through():
    result = _relativize_or_exclude("sub/file.py", Path("/Users/x/claude-klabauter"))
    assert result == "sub/file.py"


def test_relativize_windows_shaped_in_repo():
    result = _relativize_or_exclude(
        r"C:\Users\x\claude-klabauter\sub\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result == "sub/file.py"


def test_relativize_windows_shaped_mixed_separators():
    result = _relativize_or_exclude(
        r"C:\Users\x\claude-klabauter/sub\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result == "sub/file.py"


def test_relativize_windows_shaped_case_insensitive_drive_and_components():
    result = _relativize_or_exclude(
        r"c:\USERS\x\PROJECT-CLAUDE-KLABAUTER\sub\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result == "sub/file.py"


def test_relativize_windows_shaped_out_of_repo():
    result = _relativize_or_exclude(
        r"C:\Users\x\example-doctrine-repo\sub\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result is None


def test_relativize_windows_shaped_sibling_prefix_collision_is_excluded():
    result = _relativize_or_exclude(
        r"C:\Users\x\claude_klabauter2\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result is None


def test_relativize_non_string_input_is_excluded():
    assert _relativize_or_exclude(None, Path("/Users/x/claude-klabauter")) is None
    assert _relativize_or_exclude(123, Path("/Users/x/claude-klabauter")) is None
    assert _relativize_or_exclude("", Path("/Users/x/claude-klabauter")) is None


def test_relativize_relative_path_with_leading_dotdot_is_excluded():
    """Review: code-reviewer (Finding 2) — a relative path with a leading ".." (e.g.
    "../sibling-repo/file.py") normalises to a value that can never appear in
    `git ls-files` output, so it must be excluded, not waved through as "already
    repo-relative"."""
    result = _relativize_or_exclude(
        "../sibling-repo/file.py", Path("/Users/x/claude-klabauter")
    )
    assert result is None


def test_relativize_relative_path_with_leading_dotdot_windows_shaped_is_excluded():
    result = _relativize_or_exclude(
        r"..\sibling-repo\file.py", Path(r"C:\Users\x\claude-klabauter")
    )
    assert result is None


def test_relativize_path_equal_to_repo_root_is_excluded():
    """Review: code-reviewer (Finding 3) — pin the `<=` boundary at :283 (now shifted by
    the Finding 1/2 edits) that excludes a file_path exactly equal to repo_root (zero
    remaining components), distinct from the shorter/outside-containment branch the other
    exclusion tests exercise."""
    result = _relativize_or_exclude(
        "/Users/x/claude-klabauter", Path("/Users/x/claude-klabauter")
    )
    assert result is None


def test_relativize_windows_shaped_unc_in_repo():
    """Review: code-reviewer (Finding 4) — UNC paths (`\\\\server\\share\\...`) route
    through `_is_absolute`'s `\\\\` special-case and `ntpath.splitdrive`; pin that the
    drive-component folding works for an in-repo UNC path."""
    result = _relativize_or_exclude(
        r"\\server\share\claude-klabauter\sub\file.py",
        Path(r"\\server\share\claude-klabauter"),
    )
    assert result == "sub/file.py"


def test_relativize_windows_shaped_unc_out_of_repo():
    result = _relativize_or_exclude(
        r"\\server\share\other-repo\file.py",
        Path(r"\\server\share\claude-klabauter"),
    )
    assert result is None
