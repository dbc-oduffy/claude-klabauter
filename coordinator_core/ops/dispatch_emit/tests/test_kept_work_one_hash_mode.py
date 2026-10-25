"""One hash mode: raw kept fingerprints cross to committed (checkin-filtered) blobs only via kept_work."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, kept_work
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import crlf_repo, git, hold_kept

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_checkin_form_folds_crlf_under_autocrlf(tmp_path):
    root, rel, _, _ = crlf_repo(tmp_path)
    assert kept_work.checkin_form(root, rel, b"a\r\nb\r\n") == b"a\nb\n"


def test_raw_and_committed_differ_but_match_via_persisted_blob(tmp_path):
    root, rel, committed, raw = crlf_repo(tmp_path)
    assert raw != committed
    assert git(root, "rev-parse", f"HEAD:{rel}") != raw
    # Drift the worktree so only the persisted blob can answer.
    (root / rel).write_bytes(b"other\n")
    assert kept_work.matches_committed(root, rel, raw, committed)


def test_matches_committed_via_worktree_fallback(tmp_path):
    root, rel, committed, _ = crlf_repo(tmp_path)
    raw = kept_work.fingerprint(root, rel)
    assert raw is not None
    assert kept_work.matches_committed(root, rel, raw, committed)


def test_matches_committed_false_for_other_content(tmp_path):
    root, rel, committed, raw = crlf_repo(tmp_path)
    assert not kept_work.matches_committed(root, rel, raw, "1" * 40)
    assert not kept_work.matches_committed(root, rel, None, committed)
    assert not kept_work.matches_committed(root, rel, raw, None)


def test_matches_committed_false_for_absent_blob_and_drifted_worktree(tmp_path):
    root, rel, committed, _ = crlf_repo(tmp_path)
    (root / rel).write_bytes(b"drift\r\n")
    assert not kept_work.matches_committed(root, rel, "2" * 40, committed)


def test_matches_committed_false_for_attribute_pinned_path(tmp_path):
    root, rel, committed, raw = crlf_repo(tmp_path)
    (root / ".gitattributes").write_text(f"{rel} text eol=crlf\n", encoding="utf-8")
    assert not kept_work.matches_committed(root, rel, raw, committed)


def test_held_paths_names_chunk_and_post_checkpoint_not_landed(tmp_path):
    root, rel, _, _ = crlf_repo(tmp_path)
    (root / "post.txt").write_bytes(b"p\n")
    (root / "other.txt").write_bytes(b"o\n")
    hold_kept(root, "run-a", [rel], post=["post.txt"])
    assert kept_work.held_paths(root, [rel, "post.txt", "other.txt"]) == {rel: "run-a", "post.txt": "run-a"}
    halted_withheld_index.remove_run(root, "run-a")
    assert kept_work.held_paths(root, [rel, "post.txt"]) == {}


def test_held_paths_sees_path_shadowed_by_later_run(tmp_path):
    root, rel, _, _ = crlf_repo(tmp_path)
    (root / "x.txt").write_bytes(b"x\n")
    hold_kept(root, "run-a", [rel])
    hold_kept(root, "run-b", ["x.txt"])
    # run-b is lexically last, so withheld_by_path attributes shared paths to it.
    halted_withheld_index.add_run(root, "run-b", [rel, "x.txt"])
    assert halted_withheld_index.withheld_by_path(root)[rel] == "run-b"
    assert kept_work.held_paths(root, [rel]) == {rel: "run-a"}
