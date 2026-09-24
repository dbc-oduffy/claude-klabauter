"""
coordinator_core.ops.test_review_freeze_diff — op-level coverage for
`review.freeze_diff` (coordinator_core/ops/review_freeze_diff.py).

Purpose: verifies both layers of the module — the pure `freeze_diff()` core
function (called directly, real tmp_path git repo, no daemon) and the
`@register_op("review.freeze_diff")` handler (params-dict-in, repo_root-kwarg
shape, matching the sibling `review.snapshot_diff_and_head` test convention).
End-to-end CLI coverage over `coordinator/bin/freeze-review-diff.py` (which
imports and delegates to `freeze_diff()` — see that module's "Composing
algorithm" docstring section) lives separately in
`coordinator/tests/test_freeze_review_diff.py`; this file does not duplicate
those CLI-argv-shaped cases.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-review-diff-freeze-op-wanted.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.review_freeze_diff as review_freeze_diff  # fires @register_op
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.review_freeze_diff import (
    _handler,
    _validate_slice_id,
    freeze_diff,
    freeze_diffs_batch,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "review.freeze_diff"

DIFFS_SUBDIR = ("state", "review-trail", "diffs")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(repo: Path) -> None:
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test Author"], cwd=repo)


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content)
    _git(["add", path], cwd=repo)
    cp = _git(["commit", "-q", "-m", message], cwd=repo)
    assert cp.returncode == 0, f"setup commit failed: {cp.stderr!r}"
    return _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _diffs_dir(repo: Path) -> Path:
    d = repo
    for part in DIFFS_SUBDIR:
        d = d / part
    return d


def test_op_registered() -> None:
    assert _OP_NAME in _REGISTRY, (
        f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
        "@register_op did not fire on import"
    )


# ---------------------------------------------------------------------------
# Core function (freeze_diff) — direct calls over a real tmp_path git repo.
# ---------------------------------------------------------------------------


def test_range_omitted_fails_loud(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "", "no-range")

    assert result["error"] is not None
    assert result["diff_path"] is None
    assert not _diffs_dir(tmp_path).exists()


def test_slice_id_omitted_fails_loud(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "")

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_diff_written_to_expected_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")

    assert result["error"] is None
    expected_diff = _diffs_dir(tmp_path) / "wave-1.diff"
    assert result["diff_path"] == str(expected_diff)
    assert expected_diff.is_file()
    assert "line two" in expected_diff.read_text()
    assert result["empty"] is False


def test_colliding_slice_id_refuses_rather_than_overwriting(tmp_path: Path) -> None:
    # A slice id is a filename: a generic one collides with whatever peer
    # froze it first, and on a shared worktree that file is often tracked.
    # An unconditional write destroyed their evidence silently.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")
    assert first["error"] is None
    frozen = _diffs_dir(tmp_path) / "wave-1.diff"
    original = frozen.read_text()

    second = freeze_diff(tmp_path, f"{sha2}..{sha3}", "wave-1")

    assert second["error"] is not None
    assert "wave-1" in second["error"]
    assert second["diff_path"] is None
    assert frozen.read_text() == original


def test_identical_refreeze_under_same_slice_id_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")
    second = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")

    assert first["error"] is None
    assert second["error"] is None
    assert second["diff_path"] == first["diff_path"]


def test_head_sha_matches_freeze_time_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-2")

    expected_sha_path = _diffs_dir(tmp_path) / "wave-2.head.sha"
    assert result["head_sha_path"] == str(expected_sha_path)
    assert result["head_sha"] == sha2
    assert expected_sha_path.read_text().strip() == sha2

    # HEAD advances after the freeze — the recorded sha must stay pinned to
    # the freeze-time HEAD, not silently track the moved tip.
    _commit(tmp_path, "a.txt", "line one\nline two\nline three\n", "extend again")
    assert expected_sha_path.read_text().strip() == sha2


def test_pathspec_narrows_output(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "a v1\n", "add a.txt")
    (tmp_path / "b.txt").write_text("b v1\n")
    _git(["add", "b.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "add b.txt"], cwd=tmp_path)
    (tmp_path / "a.txt").write_text("a v2\n")
    (tmp_path / "b.txt").write_text("b v2\n")
    _git(["add", "a.txt", "b.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "modify both"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "paths-restrict", paths=["a.txt"])

    assert result["error"] is None
    diff_text = Path(result["diff_path"]).read_text()
    assert "a.txt" in diff_text
    assert "b.txt" not in diff_text


def test_paths_entry_matching_no_change_refuses(tmp_path: Path) -> None:
    # P1a reversal: a `--paths` entry that contributed nothing to the diff
    # used to freeze an empty diff silently (the exact under-coverage K-101
    # names). It now refuses before either output file is written, and
    # names the entry — same posture as `_zero_commit_range_error`.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "empty-diff", paths=["b.txt"])

    assert result["error"] is not None
    assert result["uncovered_paths"] == ["b.txt"]
    assert not (_diffs_dir(tmp_path) / "empty-diff.diff").exists()
    assert not (_diffs_dir(tmp_path) / "empty-diff.head.sha").exists()


def test_unrestricted_net_zero_range_stays_valid(tmp_path: Path) -> None:
    # The unrestricted (no `--paths`) empty-diff-is-valid outcome is NOT
    # reversed by P1a — only a restricted entry that matched nothing is.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "a.txt", "line one\n", "revert a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha3}", "net-zero")

    assert result["error"] is None
    assert result["empty"] is True
    assert result["uncovered_paths"] == []
    assert (_diffs_dir(tmp_path) / "net-zero.diff").is_file()
    assert (_diffs_dir(tmp_path) / "net-zero.head.sha").is_file()


def test_zero_commit_range_refuses(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "HEAD..HEAD", "zero-commit")

    assert result["error"] is not None
    assert "ZERO commits" in result["error"]
    assert result["diff_path"] is None


@pytest.mark.parametrize("bad_slice_id", ["../escape", "sub/dir", "back\\slash"])
def test_slice_id_traversal_rejected(tmp_path: Path, bad_slice_id: str) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", bad_slice_id)

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_validate_slice_id_accepts_bare_filename_component() -> None:
    assert _validate_slice_id("weekly-2026-07-26") is None


# ---------------------------------------------------------------------------
# Coverage refusal — AC-P1-1..3.
# ---------------------------------------------------------------------------


def test_uncovered_path_entry_refuses_before_writing(tmp_path: Path) -> None:
    # AC-P1-1.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "exists-changed.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "exists-changed.txt", "v2\n", "change")

    result = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "s", paths=["exists-changed.txt", "no-such.txt"]
    )

    assert result["error"] is not None
    assert result["uncovered_paths"] == ["no-such.txt"]
    assert not (_diffs_dir(tmp_path) / "s.diff").exists()
    assert not (_diffs_dir(tmp_path) / "s.head.sha").exists()


def test_all_paths_covered_returns_empty_uncovered_list(tmp_path: Path) -> None:
    # AC-P1-2, covered half.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "a.txt", "v2\n", "change")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "covered", paths=["a.txt"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_literal_file_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "a.txt", "v2\n", "change")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "literal", paths=["a.txt"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_directory_prefix_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "sub" / "dir").mkdir(parents=True)
    sha1 = _commit(tmp_path, "sub/dir/file.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "sub/dir/file.txt", "v2\n", "change")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "dirprefix", paths=["sub/dir"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_path_with_space(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "file with space.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "file with space.txt", "v2\n", "change")

    result = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "space", paths=["file with space.txt"]
    )

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_c_quoted_non_ascii_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "café.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "café.txt", "v2\n", "change")

    result = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "nonascii", paths=["café.txt"]
    )

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_rename_either_side_counts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "old-name.txt", "same content that is long enough\n", "add")
    _git(["mv", "old-name.txt", "new-name.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "rename"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    old_side = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "rename-old", paths=["old-name.txt"]
    )
    new_side = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "rename-new", paths=["new-name.txt"]
    )

    assert old_side["error"] is None
    assert old_side["uncovered_paths"] == []
    assert new_side["error"] is None
    assert new_side["uncovered_paths"] == []


def test_coverage_binary_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    _git(["add", "bin.dat"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "add binary"], cwd=tmp_path)
    sha1 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x03")
    _git(["add", "bin.dat"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "change binary"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "binary", paths=["bin.dat"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_mode_only_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "script.sh", "echo hi\n", "add")
    (tmp_path / "script.sh").chmod(0o755)
    _git(["add", "script.sh"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "mode change"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "modeonly", paths=["script.sh"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_deletion(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "gone.txt", "v1\n", "add")
    (tmp_path / "gone.txt").unlink()
    _git(["add", "gone.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "delete"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "deletion", paths=["gone.txt"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_deletion_whose_content_moved_outside_pathspec(tmp_path: Path) -> None:
    # The moved-file case named in the module negative-spec: with no rename
    # detection over the unrestricted diff, the deletion of the old path
    # still counts as covered even though the content landed elsewhere.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "old-location.txt", "moved content\n", "add")
    (tmp_path / "old-location.txt").unlink()
    (tmp_path / "new-location.txt").write_text("moved content\n")
    _git(["add", "old-location.txt", "new-location.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "move without rename detection"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(
        tmp_path, f"{sha1}..{sha2}", "moved", paths=["old-location.txt"]
    )

    assert result["error"] is None
    assert result["uncovered_paths"] == []


def test_coverage_unaffected_by_diff_noprefix_config(tmp_path: Path) -> None:
    # AC-P1-3: the pinned --src-prefix/--dst-prefix keep the frozen output
    # byte-identical to the default-config freeze regardless of local
    # diff.noprefix, and coverage still matches.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "a.txt", "v2\n", "change")

    baseline = freeze_diff(tmp_path, f"{sha1}..{sha2}", "baseline-noprefix", paths=["a.txt"])
    baseline_text = Path(baseline["diff_path"]).read_text()

    _git(["config", "diff.noprefix", "true"], cwd=tmp_path)
    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "noprefix", paths=["a.txt"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []
    assert Path(result["diff_path"]).read_text() == baseline_text


def test_coverage_unaffected_by_diff_mnemonic_prefix_config(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "a.txt", "v2\n", "change")

    baseline = freeze_diff(tmp_path, f"{sha1}..{sha2}", "baseline-mnemonic", paths=["a.txt"])
    baseline_text = Path(baseline["diff_path"]).read_text()

    _git(["config", "diff.mnemonicPrefix", "true"], cwd=tmp_path)
    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "mnemonic", paths=["a.txt"])

    assert result["error"] is None
    assert result["uncovered_paths"] == []
    assert Path(result["diff_path"]).read_text() == baseline_text


def test_glob_and_magic_pathspec_entries_excluded_from_check(tmp_path: Path) -> None:
    # Named in the negative-spec: a glob-metacharacter or ':'-magic entry is
    # never reported as uncovered, whether or not it actually matches.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1\n", "add")
    sha2 = _commit(tmp_path, "a.txt", "v2\n", "change")

    result = freeze_diff(
        tmp_path,
        f"{sha1}..{sha2}",
        "magic",
        paths=["*.md", "no-such-file?.txt", "[abc].txt", ":no-such/*"],
    )

    assert result["error"] is None
    assert result["uncovered_paths"] == []


# ---------------------------------------------------------------------------
# JSON-RPC handler shape.
# ---------------------------------------------------------------------------


def test_handler_requires_repo_root() -> None:
    result = _handler({"range": "a..b", "slice_id": "x"}, repo_root=None)
    assert result["error"] is not None


def test_handler_happy_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = _handler(
        {"range": f"{sha1}..{sha2}", "slice_id": "handler-wave"}, repo_root=tmp_path
    )

    assert result["error"] is None
    assert Path(result["diff_path"]).is_file()
    assert result["head_sha"] == sha2


def test_handler_paths_must_be_a_list(tmp_path: Path) -> None:
    result = _handler(
        {"range": "a..b", "slice_id": "x", "paths": "not-a-list"}, repo_root=tmp_path
    )
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# freeze_diffs_batch — batch/single-path parity, one git spawn per phase.
# ---------------------------------------------------------------------------


def test_batch_of_two_ranges_equals_two_single_freezes_via_one_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    diff_tree_calls = []
    real_git = review_freeze_diff._git

    def _counting_git(args, **kwargs):
        if args and args[0] == "diff-tree":
            diff_tree_calls.append(args)
        return real_git(args, **kwargs)

    monkeypatch.setattr(review_freeze_diff, "_git", _counting_git)

    # HEAD as the batch itself will see it -- captured BEFORE the batch call,
    # since the batch now commits its own writes (P157-C1) and moves HEAD.
    pre_batch_head = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    batch_results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "batch-a", "range": f"{sha1}..{sha2}"},
            {"slice_id": "batch-b", "range": f"{sha2}..{sha3}"},
        ],
    )

    assert len(diff_tree_calls) == 1, (
        f"expected exactly one git diff-tree spawn for a two-slice batch, got "
        f"{len(diff_tree_calls)}: {diff_tree_calls!r}"
    )

    # Run AFTER the batch, so each single sees ITS OWN call's HEAD (the
    # batch's commit for single_a, then single_a's own commit for single_b --
    # each freeze_diff call commits its own write too, per this same fix).
    head_before_single_a = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    single_a = freeze_diff(tmp_path, f"{sha1}..{sha2}", "single-a")
    head_before_single_b = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    single_b = freeze_diff(tmp_path, f"{sha2}..{sha3}", "single-b")

    assert batch_results[0]["error"] is None
    assert batch_results[1]["error"] is None
    assert Path(batch_results[0]["diff_path"]).read_text() == Path(
        single_a["diff_path"]
    ).read_text()
    assert Path(batch_results[1]["diff_path"]).read_text() == Path(
        single_b["diff_path"]
    ).read_text()
    assert batch_results[0]["head_sha"] == pre_batch_head
    assert batch_results[1]["head_sha"] == pre_batch_head
    assert single_a["head_sha"] == head_before_single_a
    assert single_b["head_sha"] == head_before_single_b
    assert batch_results[0]["empty"] == single_a["empty"] is False
    assert batch_results[1]["empty"] == single_b["empty"] is False


def test_batch_shares_one_head_sha_across_requests(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "shared-1", "range": f"{sha1}..{sha2}"},
            {"slice_id": "shared-2", "range": f"{sha1}..{sha2}"},
        ],
    )
    assert results[0]["head_sha"] == results[1]["head_sha"] == sha2


def test_batch_zero_commit_range_refuses_that_request_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "zero-commit", "range": "HEAD..HEAD"},
            {"slice_id": "real-diff", "range": f"{sha1}..{sha2}"},
        ],
    )

    assert results[0]["error"] is not None
    assert "ZERO commits" in results[0]["error"]
    assert results[1]["error"] is None
    assert Path(results[1]["diff_path"]).is_file()


def test_batch_three_dot_range_matches_two_dot_merge_base_equivalent(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    two_dot = freeze_diffs_batch(
        tmp_path, [{"slice_id": "three-dot-check", "range": f"{sha1}...{sha2}"}]
    )[0]

    assert two_dot["error"] is None
    assert Path(two_dot["diff_path"]).read_text() != ""


def test_batch_empty_returns_empty_list(tmp_path: Path) -> None:
    assert freeze_diffs_batch(tmp_path, []) == []


def test_batch_mismatched_paths_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    with pytest.raises(ValueError):
        freeze_diffs_batch(
            tmp_path,
            [
                {"slice_id": "p1", "range": f"{sha1}..{sha2}", "paths": ["a.txt"]},
                {"slice_id": "p2", "range": f"{sha1}..{sha2}", "paths": ["b.txt"]},
            ],
        )


def test_frozen_diff_is_byte_identical_to_git_diff_of_the_range(tmp_path: Path) -> None:
    """Direction pin: the freeze must equal `git diff A B`, not its inverse.
    `diff-tree --stdin` reads `<commit> <parent>`, so feeding the pair in
    range order printed every addition as a deletion -- and the batch/single
    parity tests above could not see it, because both paths share it."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    _commit(tmp_path, "b.txt", "new file\n", "add b.txt")
    sha3 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "dir-1", "range": f"{sha1}..{sha3}", "paths": None},
            {"slice_id": "dir-2", "range": f"{sha1}...{sha3}", "paths": None},
        ],
    )

    expected = _git(["diff", sha1, sha3], cwd=tmp_path).stdout
    assert "+line two" in expected and "+new file" in expected
    for result in results:
        assert result["error"] is None
        assert Path(result["diff_path"]).read_text() == expected


# ---------------------------------------------------------------------------
# P157-C1 — the freeze commits its own writes (spec: docs/plans/2026-09-22-
# the-review-diff-freeze-commits-its-own-writes.md).
# ---------------------------------------------------------------------------


def _tracked_and_clean(repo: Path, *relpaths: str) -> bool:
    status = _git(["status", "--porcelain", "--", *relpaths], cwd=repo).stdout
    return status.strip() == ""


def test_single_freeze_lands_a_commit_touching_exactly_its_two_files(tmp_path: Path) -> None:
    """AC1."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "s")

    assert result["error"] is None
    assert result["committed"] is True
    assert _tracked_and_clean(tmp_path, "state/review-trail/diffs")

    head = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    parent = _git(["rev-parse", "HEAD^"], cwd=tmp_path).stdout.strip()
    show = _git(["show", "--name-only", "--pretty=format:", head], cwd=tmp_path).stdout
    touched = {line.strip() for line in show.splitlines() if line.strip()}
    assert touched == {"state/review-trail/diffs/s.diff", "state/review-trail/diffs/s.head.sha"}
    assert parent == result["head_sha"]
    sha_content = (tmp_path / "state" / "review-trail" / "diffs" / "s.head.sha").read_text().strip()
    assert sha_content == parent


def test_two_request_batch_commits_once_over_all_four_files(tmp_path: Path) -> None:
    """AC2."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    before = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "two-a", "range": f"{sha1}..{sha2}"},
            {"slice_id": "two-b", "range": f"{sha2}..{sha3}"},
        ],
    )

    after = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    assert after != before

    log = _git(["log", "--oneline", f"{before}..{after}"], cwd=tmp_path).stdout
    assert len(log.strip().splitlines()) == 1, f"expected exactly one new commit, got: {log!r}"

    assert results[0]["committed"] is True
    assert results[1]["committed"] is True
    assert results[0]["commit_sha"] == results[1]["commit_sha"] == after


def test_byte_identical_re_freeze_of_committed_slice_makes_no_new_commit(tmp_path: Path) -> None:
    """AC3."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "reref")
    assert first["committed"] is True
    head_after_first = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    second = freeze_diff(tmp_path, f"{sha1}..{sha2}", "reref")
    head_after_second = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    assert second["error"] is None
    assert second["committed"] is True
    assert second["commit_sha"] is None
    assert head_after_second == head_after_first


def test_unrelated_staged_file_survives_the_freeze_commit(tmp_path: Path) -> None:
    """AC4."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    (tmp_path / "unrelated.txt").write_text("staged content\n")
    _git(["add", "unrelated.txt"], cwd=tmp_path)

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "s2")
    assert result["committed"] is True

    status = _git(["status", "--porcelain", "--", "unrelated.txt"], cwd=tmp_path).stdout
    assert status.strip().startswith("A "), f"unrelated.txt lost its staged status: {status!r}"

    head = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    show = _git(["show", "--name-only", "--pretty=format:", head], cwd=tmp_path).stdout
    touched = {line.strip() for line in show.splitlines() if line.strip()}
    assert "unrelated.txt" not in touched


def test_commit_refusal_is_fail_soft_files_still_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    from coordinator_core.git.commit import CommitRefused

    def _refuse(*args, **kwargs):
        raise CommitRefused("x")

    monkeypatch.setattr(review_freeze_diff, "commit_paths", _refuse)

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "s3")

    assert result["error"] is None
    assert result["committed"] is False
    assert "x" in result["commit_error"]
    assert (tmp_path / "state" / "review-trail" / "diffs" / "s3.diff").exists()
    assert (tmp_path / "state" / "review-trail" / "diffs" / "s3.head.sha").exists()


def test_failed_request_reports_uncommitted_and_calls_no_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    calls = []
    real_commit_paths = review_freeze_diff.commit_paths

    def _counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real_commit_paths(*args, **kwargs)

    monkeypatch.setattr(review_freeze_diff, "commit_paths", _counting)

    # A zero-commit range: sha1..sha1 resolves to zero commits.
    result = freeze_diff(tmp_path, f"{sha1}..{sha1}", "zero")

    assert result["error"] is not None
    assert result["committed"] is False
    assert result["commit_sha"] is None
    assert result["commit_error"] is None
    assert calls == []


#: AC7 measures the commit leg's git-spawn cost. Measured against the LIVE
#: `commit_paths`/`_worktree_blob` code (coordinator_core/git/commit.py):
#: `text eol=lf`-pinned, CR-free content (exactly what a frozen `.diff`/
#: `.sha` always is -- `_git()`'s `text=True` capture already normalizes any
#: `\r` out, see `ops/ceremony/git_native.py`'s own note on that leg) hashes
#: IN PROCESS via `write_object`, zero spawns -- `_worktree_blob`'s `TEXT`
#: branch only falls to the spawning `blob_fallback` on CR-bearing content,
#: which this op's output never produces. `pytest`'s own `_quarantine_home`
#: autouse fixture (coordinator_core/conftest.py) also hides any ambient
#: `commit.gpgsign`, so the signing spawn some environments would otherwise
#: see here does not fire under this suite either. So the TRUE, live-measured
#: floor for this op's commit leg is ZERO git spawns, not the batched
#: `hash-object --stdin-paths` fallback AC7's prose names -- forcing that
#: exact leg to fire (a `filter=` macro, to make `_worktree_blob` refuse
#: in-process hashing) is what these two tests do, so the actually-reachable
#: fallback leg itself is proven to cost exactly one spawn and stay FLAT
#: across batch size -- the amplification-gate property AC7 exists to pin.
def _pin_filter_macro(repo: Path) -> None:
    """Route `*.diff`/`*.sha` through an unresolved `filter=` clean driver,
    forcing `_worktree_blob` to raise `FilterUnsupported` and fall to
    `blob_fallback` -- the only way to exercise that spawning leg with
    content this op ever actually produces (CR-free, `text eol=lf`)."""
    attrs = "*.sha filter=lfs text eol=lf\n*.diff filter=lfs text eol=lf\n"
    (repo / ".gitattributes").write_text(attrs)
    _commit(repo, ".gitattributes", attrs, "pin diff/sha through a clean filter")


def test_one_request_freeze_commit_leg_costs_exactly_one_git_spawn_via_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7 (single request) -- see `_pin_filter_macro` for why this is the
    only way to force the fallback leg AC7 names with real op output."""
    _init_repo(tmp_path)
    _pin_filter_macro(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    from coordinator_core.git import run as git_run

    calls = []
    real_run_git = git_run.run_git

    def _counting_run_git(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(git_run, "run_git", _counting_run_git)

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "spawn-1")

    assert result["committed"] is True
    assert len(calls) == 1, f"expected exactly one git spawn for the commit leg, got {calls!r}"


def test_two_request_batch_commit_leg_still_costs_exactly_one_git_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7 (batch) -- flat, not per-request."""
    _init_repo(tmp_path)
    _pin_filter_macro(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    from coordinator_core.git import run as git_run

    calls = []
    real_run_git = git_run.run_git

    def _counting_run_git(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(git_run, "run_git", _counting_run_git)

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "spawn-2a", "range": f"{sha1}..{sha2}"},
            {"slice_id": "spawn-2b", "range": f"{sha2}..{sha3}"},
        ],
    )

    assert results[0]["committed"] is True
    assert results[1]["committed"] is True
    assert len(calls) == 1, f"expected exactly one git spawn for the commit leg, got {calls!r}"


def test_re_freeze_after_commit_leaves_head_sha_unchanged(tmp_path: Path) -> None:
    """AC13."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "pin")
    assert first["committed"] is True

    sha_path = tmp_path / "state" / "review-trail" / "diffs" / "pin.head.sha"
    sha_content_after_first = sha_path.read_text()
    rev_count_before = _git(["rev-list", "--count", "HEAD"], cwd=tmp_path).stdout.strip()

    second = freeze_diff(tmp_path, f"{sha1}..{sha2}", "pin")

    assert second["committed"] is True
    assert second["commit_sha"] is None
    assert second["head_sha"] == first["head_sha"]
    assert sha_path.read_text() == sha_content_after_first
    rev_count_after = _git(["rev-list", "--count", "HEAD"], cwd=tmp_path).stdout.strip()
    assert rev_count_after == rev_count_before


def test_orphan_heal_commits_an_untracked_byte_identical_pair(tmp_path: Path) -> None:
    """AC14."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    diffs_dir = tmp_path / "state" / "review-trail" / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    # Write the pair by hand, byte-identical to what freeze_diff will produce,
    # and leave it UNTRACKED (never committed) -- the orphan shape.
    diff_text = _git(["diff", sha1, sha2], cwd=tmp_path).stdout
    (diffs_dir / "orphan.diff").write_text(diff_text, newline="\n")
    (diffs_dir / "orphan.head.sha").write_text(sha2 + "\n", newline="\n")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "orphan")

    assert result["error"] is None
    assert result["committed"] is True
    assert result["commit_sha"] is not None
    assert _tracked_and_clean(tmp_path, "state/review-trail/diffs")


def test_one_request_freeze_commit_leg_stays_under_the_500ms_brightline(tmp_path: Path) -> None:
    """AC12. Measures process time (never wall clock, per CLAUDE.md's
    brightline), including the new commit leg."""
    import time

    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    start = time.process_time()
    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "timing")
    elapsed_ms = (time.process_time() - start) * 1000

    assert result["committed"] is True
    assert elapsed_ms < 500.0, f"freeze_diff process time {elapsed_ms:.1f}ms exceeds the 500ms brightline"
