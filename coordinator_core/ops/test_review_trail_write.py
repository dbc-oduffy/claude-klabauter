"""Characterization tests for coordinator_core.ops.review_trail_write.

Port of: coordinator-write-review-trail.sh (example-doctrine-repo 30f4c5fc, 2026-07-19) — the
Example-doctrine-repo-side artifact was already a thin strangler-facade veneer over this
module's registered ``review_trail.write`` op (dispatched via cc_invoke);
this module carries the full write-path logic (session-id/workstream
resolution, validation, atomic write).

Spec backlink: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md § C3
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2

This test file fills a pre-existing coverage gap: the module was fully landed and
registered (T2-g1 finish-strangler) but had no co-located pytest — only an indirect
bash-level facade-routing test (coordinator-write-review-trail-facade.test.sh, example-doctrine-repo
a2fe06f8, 2026-07-22) on the example-doctrine-repo side, which fakes coordinator_core.invoke and never
exercises this module's actual write-path logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.review_trail_write import (
    _build_json_record,
    _compute_timestamp,
    _reject_empty_sha_range,
    _review_trail_write_handler,
    _scan_workstream,
    _validate,
    write_review_trail_entry,
)


def _init_git_repo_with_one_commit(worktree: Path) -> str:
    """Create a minimal real git repo at ``worktree`` with one commit;
    return that commit's full SHA. Needed by the empty-sha_range regression
    tests below — `_reject_empty_sha_range` only engages against a real
    ``git rev-parse --is-inside-work-tree`` repo (test-isolation no-op
    contract mirrored from `_resolve_symbolic_range`)."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    kwargs = dict(cwd=str(worktree), capture_output=True, text=True, **no_console_creationflags())
    subprocess.run(["git", "init", "-q"], **kwargs, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], **kwargs, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], **kwargs, check=True)
    (worktree / "f.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], **kwargs, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], **kwargs, check=True)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], **kwargs, check=True)
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# _scan_workstream
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_scan_workstream_unreadable_dir_logs_and_returns_none(tmp_path: Path, caplog) -> None:
    """A permission-denied state/handoffs/ dir logs a WARNING and returns None —
    distinguishable from the genuinely-no-active-handoff case, which also returns
    None but without a log line. glob("*.md")'s selector would otherwise swallow
    the PermissionError silently, stamping workstream: null with no signal that an
    active handoff might exist behind the block."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "2026-07-01-active.md").write_text(
        "---\nstatus: open\nclaimed_by: test-session-id\nworkstream: my-workstream\n---\nBody.\n",
        encoding="utf-8",
    )

    original_mode = handoffs_dir.stat().st_mode
    os.chmod(handoffs_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.review_trail_write"):
            result = _scan_workstream(handoffs_dir, "test-session-id")
    finally:
        os.chmod(handoffs_dir, original_mode)

    assert result is None

    dir_warnings = [
        r
        for r in caplog.records
        if str(handoffs_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable handoffs dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_scan_workstream_no_active_handoff_returns_none_without_warning(
    tmp_path: Path, caplog
) -> None:
    """A genuinely-empty state/handoffs/ dir also returns None, but with no
    WARNING logged — the log line is the only signal distinguishing this case
    from the unreadable-dir case above."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.review_trail_write"):
        result = _scan_workstream(handoffs_dir, "test-session-id")

    assert result is None
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


def test_validate_accepts_well_formed_diff_scope_kind() -> None:
    _validate("abc1234..def5678", "code-reviewer", "chain", "ok", 10, "diff")


def test_validate_rejects_missing_sha_range() -> None:
    with pytest.raises(ValueError, match="--sha-range is required"):
        _validate("", "code-reviewer", "chain", "ok", 10, "diff")


def test_validate_rejects_missing_reviewer() -> None:
    with pytest.raises(ValueError, match="--reviewer is required"):
        _validate("a..b", "", "chain", "ok", 10, "diff")


def test_validate_rejects_missing_scope() -> None:
    with pytest.raises(ValueError, match="--scope is required"):
        _validate("a..b", "code-reviewer", "", "ok", 10, "diff")


def test_validate_rejects_missing_verdict() -> None:
    with pytest.raises(ValueError, match="--verdict is required"):
        _validate("a..b", "code-reviewer", "chain", "", 10, "diff")


def test_validate_rejects_negative_diff_loc() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        _validate("a..b", "code-reviewer", "chain", "ok", -1, "diff")


def test_validate_rejects_unknown_reviewer() -> None:
    with pytest.raises(ValueError, match="reviewer .* is invalid"):
        _validate("a..b", "not-a-reviewer", "chain", "ok", 10, "diff")


def test_validate_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="scope .* is invalid"):
        _validate("a..b", "code-reviewer", "not-a-scope", "ok", 10, "diff")


def test_validate_rejects_unknown_verdict() -> None:
    with pytest.raises(ValueError, match="verdict .* is invalid"):
        _validate("a..b", "code-reviewer", "chain", "not-a-verdict", 10, "diff")


def test_validate_rejects_unknown_scope_kind() -> None:
    with pytest.raises(ValueError, match="scope_kind .* is invalid"):
        _validate("a..b", "code-reviewer", "chain", "ok", 10, "not-a-kind")


def test_validate_rejects_diff_scope_kind_without_dotdot() -> None:
    with pytest.raises(ValueError, match="requires sha_range to contain"):
        _validate("abc1234", "code-reviewer", "chain", "ok", 10, "diff")


def test_validate_accepts_non_diff_scope_kind_without_dotdot() -> None:
    _validate("abc1234", "code-reviewer", "session", "ok", 10, "plan")


def test_validate_rejects_json_unsafe_sha_range() -> None:
    with pytest.raises(ValueError, match="unsafe JSON character"):
        _validate('a"..b', "code-reviewer", "chain", "ok", 10, "diff")


# ---------------------------------------------------------------------------
# _build_json_record — byte-parity with oracle's hand-interpolated printf output
# ---------------------------------------------------------------------------


def test_build_json_record_key_order_and_null_workstream() -> None:
    record = _build_json_record(
        sha_range="abc1234..def5678",
        reviewer="code-reviewer",
        scope="chain",
        scope_kind="diff",
        verdict="ok",
        diff_loc=100,
        session_id="abcdef12",
        workstream=None,
    )
    # scope_kind="diff" with no reviewed_paths supplied: the 9th key is still emitted,
    # as null (present-as-null discipline, mirroring workstream).
    assert record == (
        '{"sha_range":"abc1234..def5678",'
        '"reviewer":"code-reviewer",'
        '"scope":"chain",'
        '"scope_kind":"diff",'
        '"verdict":"ok",'
        '"diff_loc":100,'
        '"session_id":"abcdef12",'
        '"workstream":null,'
        '"reviewed_paths":null}'
    )
    # No trailing newline (oracle uses printf '%s', not echo).
    assert not record.endswith("\n")


def test_build_json_record_with_workstream() -> None:
    record = _build_json_record(
        sha_range="a..b",
        reviewer="waived",
        scope="session",
        scope_kind="plan",
        verdict="waived",
        diff_loc=0,
        session_id="12345678",
        workstream="my-workstream",
    )
    assert '"workstream":"my-workstream"' in record
    # scope_kind="plan" OMITS reviewed_paths entirely, even though not passed here.
    assert "reviewed_paths" not in record
    # Round-trips as valid JSON despite hand-serialization.
    parsed = json.loads(record)
    assert parsed["workstream"] == "my-workstream"
    assert parsed["diff_loc"] == 0
    assert "reviewed_paths" not in parsed


def test_build_json_record_diff_with_reviewed_paths() -> None:
    record = _build_json_record(
        sha_range="a..b",
        reviewer="code-reviewer",
        scope="chain",
        scope_kind="diff",
        verdict="ok",
        diff_loc=5,
        session_id="12345678",
        workstream=None,
        reviewed_paths=["a.py", "b/c.py"],
    )
    assert record.endswith('"reviewed_paths":["a.py","b/c.py"]}')
    parsed = json.loads(record)
    assert parsed["reviewed_paths"] == ["a.py", "b/c.py"]


def test_build_json_record_non_diff_ignores_reviewed_paths() -> None:
    # A reviewed_paths value passed for a non-diff scope_kind is silently omitted,
    # not surfaced — the key belongs to scope_kind="diff" records only.
    record = _build_json_record(
        sha_range="a..b",
        reviewer="code-reviewer",
        scope="chain",
        scope_kind="integration",
        verdict="ok",
        diff_loc=5,
        session_id="12345678",
        workstream=None,
        reviewed_paths=["a.py"],
    )
    assert "reviewed_paths" not in record


# ---------------------------------------------------------------------------
# _compute_timestamp
# ---------------------------------------------------------------------------


def test_compute_timestamp_shape() -> None:
    ts = _compute_timestamp()
    # Either 17-char second-precision (macOS/Windows) or 23-char ns-truncated (Linux).
    assert len(ts) in (17, 23)
    assert ts[4] == "-" and ts[7] == "-" and ts[10] == "-"


# ---------------------------------------------------------------------------
# write_review_trail_entry — end-to-end atomic-write path
# ---------------------------------------------------------------------------


def test_write_review_trail_entry_creates_file(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    result = write_review_trail_entry(
        sha_range="abc1234..def5678",
        reviewer="code-reviewer",
        scope="chain",
        verdict="ok",
        diff_loc=42,
        session_id="sess1234abcd",
        caller_worktree=worktree,
        workstream="",
        _timestamp="2026-07-16-120000",
    )
    out_path = Path(result["out_path"])
    assert out_path.is_file()
    assert out_path.parent == worktree / "state" / "review-trail"
    assert out_path.name == "2026-07-16-120000-sess1234.json"

    written = out_path.read_text(encoding="utf-8")
    assert not written.endswith("\n")
    parsed = json.loads(written)
    assert parsed["sha_range"] == "abc1234..def5678"
    assert parsed["diff_loc"] == 42
    # The result/JSON record carries the FULL resolved session_id (only the
    # filename component is truncated to the first 8 chars).
    assert parsed["session_id"] == "sess1234abcd"
    assert parsed["workstream"] is None
    assert result["workstream"] is None


def test_write_review_trail_entry_same_timestamp_does_not_overwrite(tmp_path: Path) -> None:
    """Same timestamp+session_id_short across two calls must NOT clobber the first
    record (DR-216 D2(i)'s last-write-wins design SUPERSEDED 2026-07-27 — see
    review_trail_write.py's module docstring for the incident that falsified its
    "impossible in practice" premise). Both records must survive as distinct files."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    common_kwargs = dict(
        reviewer="code-reviewer",
        scope="chain",
        session_id="sess1234abcd",
        caller_worktree=worktree,
        workstream="",
        _timestamp="2026-07-16-120000",
    )
    # Hex-shaped (not bare "a"/"b"/"c") so write-time ref concretization treats
    # them as already-concrete SHAs and skips a git call — worktree here is a
    # plain tmp dir, not a git repo (this test exercises no-clobber semantics,
    # not ref resolution).
    result1 = write_review_trail_entry(
        sha_range="aaa1111..bbb2222", verdict="ok", diff_loc=1, **common_kwargs
    )
    result2 = write_review_trail_entry(
        sha_range="aaa1111..ccc3333", verdict="blocked", diff_loc=2, **common_kwargs
    )

    assert result1["out_path"] != result2["out_path"], (
        "second write must land in its own uniquely-suffixed file, not clobber the first"
    )

    parsed1 = json.loads(Path(result1["out_path"]).read_text(encoding="utf-8"))
    parsed2 = json.loads(Path(result2["out_path"]).read_text(encoding="utf-8"))
    assert parsed1["sha_range"] == "aaa1111..bbb2222"
    assert parsed1["verdict"] == "ok"
    assert parsed2["sha_range"] == "aaa1111..ccc3333"
    assert parsed2["verdict"] == "blocked"


def test_write_review_trail_entry_requires_resolvable_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    with pytest.raises(ValueError, match="could not resolve session_id"):
        write_review_trail_entry(
            sha_range="a..b",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            caller_worktree=worktree,
        )


# ---------------------------------------------------------------------------
# _reject_empty_sha_range (2026-08-08 cmd-exe caret-eating shim defect —
# state/bug-backlog/2026-08-08-cmd-exe-shim-eats-the-caret-in-a-git-rev-
# 6679bf76eb8a.yaml)
# ---------------------------------------------------------------------------


def test_reject_empty_sha_range_raises_on_zero_commit_range(tmp_path: Path) -> None:
    """The exact shape the caret-eating cmd.exe shim produced: a per-commit
    `<sha>^..<sha>` request arriving with the caret stripped becomes
    `<sha>..<sha>` — git resolves this to zero commits. Must raise loudly,
    not silently persist a discharging-nothing record."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    sha = _init_git_repo_with_one_commit(worktree)
    with pytest.raises(ValueError, match="resolves to ZERO commits"):
        _reject_empty_sha_range(f"{sha}..{sha}", worktree)


def test_reject_empty_sha_range_allows_nonempty_range(tmp_path: Path) -> None:
    """A genuine parent..child range (>=1 commit) must not be rejected."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    worktree = tmp_path / "repo"
    worktree.mkdir()
    first_sha = _init_git_repo_with_one_commit(worktree)
    kwargs = dict(cwd=str(worktree), capture_output=True, text=True, **no_console_creationflags())
    (worktree / "f.txt").write_text("hello again", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], **kwargs, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], **kwargs, check=True)
    second_proc = subprocess.run(["git", "rev-parse", "HEAD"], **kwargs, check=True)
    second_sha = second_proc.stdout.strip()
    _reject_empty_sha_range(f"{first_sha}..{second_sha}", worktree)  # must not raise


def test_reject_empty_sha_range_noop_outside_git_repo(tmp_path: Path) -> None:
    """Test-isolation contract: a non-git-repo caller_worktree (synthetic
    SHAs, as every other characterization test in this file uses) is a
    no-op, matching `_resolve_symbolic_range`'s own carve-out — this must
    NOT regress `test_write_review_trail_entry_creates_file` et al."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _reject_empty_sha_range("aaa1111..bbb2222", worktree)  # must not raise


def test_write_review_trail_entry_rejects_empty_sha_range(tmp_path: Path) -> None:
    """End-to-end: write_review_trail_entry itself refuses an empty range
    rather than persisting a record and exiting cleanly."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    sha = _init_git_repo_with_one_commit(worktree)
    with pytest.raises(ValueError, match="resolves to ZERO commits"):
        write_review_trail_entry(
            sha_range=f"{sha}..{sha}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id="sess1234abcd",
            caller_worktree=worktree,
            workstream="",
        )
    trail_dir = worktree / "state" / "review-trail"
    assert not trail_dir.is_dir() or not list(trail_dir.glob("*.json")), (
        "an empty-range write must not persist any record"
    )


# ---------------------------------------------------------------------------
# _review_trail_write_handler — JSON-RPC op entrypoint
# ---------------------------------------------------------------------------


def test_handler_writes_entry_under_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "handlersessid")
    worktree = tmp_path / "repo"
    worktree.mkdir()
    git_common_dir = worktree / ".git"
    git_common_dir.mkdir()

    params = {
        "sha_range": "abc1234..def5678",
        "reviewer": "code-reviewer",
        "scope": "chain",
        "verdict": "ok",
        "diff_loc": "7",  # str, per CLI-serialization param cast contract
        "scope_kind": "diff",
        "workstream": "",
    }
    # repo_root is socket-authoritative git_common_dir (main_worktree_root(repo_root)
    # derives the worktree as repo_root.parent) — see fleet/_common.py:236.
    result = asyncio.run(_review_trail_write_handler(params, repo_root=git_common_dir))

    assert result["diff_loc"] == 7
    assert result["session_id"] == "handlersessid"
    out_path = Path(result["out_path"])
    assert out_path.is_file()
    assert out_path.parent == worktree / "state" / "review-trail"


def test_handler_rejects_non_integer_diff_loc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "handlersessid")
    worktree = tmp_path / "repo"
    worktree.mkdir()
    git_common_dir = worktree / ".git"
    git_common_dir.mkdir()

    params = {
        "sha_range": "a..b",
        "reviewer": "code-reviewer",
        "scope": "chain",
        "verdict": "ok",
        "diff_loc": "not-an-int",
    }
    with pytest.raises(ValueError, match="diff_loc must be a non-negative integer"):
        asyncio.run(_review_trail_write_handler(params, repo_root=git_common_dir))
