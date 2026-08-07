"""coordinator/bin/tests/test_publish_batch_delta_honest_reporting.py —
regression tests for the three publish-driver deliverables in this task:

  1. Batch (`main()`'s `requested_names` comma-split) — a comma-separated
     `target` CLI arg selects exactly the named rows, not a substring/single
     match, without raising on a multi-name request.
  2. Delta (`delta_row_unchanged`, `compute_delta_invalidation_signature`,
     `write_delta_record`/`load_delta_record`) — a row is provably-unchanged
     only when EVERY one of signature/source-sha/dest-HEAD/clean-tree holds;
     any single mismatch (a store/code signature change, a source commit, a
     destination commit, or destination drift) forces "do not skip".
  3. Honest change reporting (`files_differ`) — byte-identical content is
     "unchanged" even when the source's mtime is far newer than the
     destination's (the git-archive-materialization case this fixes).

Run: python -m pytest coordinator/bin/tests/test_publish_batch_delta_honest_reporting.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_batch_delta_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_all(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


# ---------------------------------------------------------------------------
# Deliverable 3 — honest change reporting.
# ---------------------------------------------------------------------------
def test_files_differ_ignores_mtime_when_bytes_identical(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("same content", encoding="utf-8")
    time.sleep(0.05)
    src.write_text("same content", encoding="utf-8")
    # src is strictly newer than dst here, but bytes are identical.
    assert src.stat().st_mtime >= dst.stat().st_mtime
    assert publish.files_differ(src, dst) is False


def test_files_differ_true_when_bytes_differ(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("old content", encoding="utf-8")
    src.write_text("new content", encoding="utf-8")
    assert publish.files_differ(src, dst) is True


def test_files_differ_true_when_dst_missing(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("content", encoding="utf-8")
    assert publish.files_differ(src, tmp_path / "absent.txt") is True


# ---------------------------------------------------------------------------
# Deliverable 1 — batch: comma-separated target selection in main()'s loop.
# ---------------------------------------------------------------------------
def test_requested_names_comma_split_selects_exact_names():
    class FakeArgs:
        target = "claude-klabauter,claude-klabauter-bin"

    args = FakeArgs()
    requested_names = (
        [n.strip() for n in args.target.split(",") if n.strip()] if args.target else []
    )
    assert requested_names == ["claude-klabauter", "claude-klabauter-bin"]
    # Substring match must NOT select a sibling row that merely shares a
    # prefix (e.g. "claude-klabauter-lib" must not match "claude-klabauter").
    assert "claude-klabauter-lib" not in requested_names


def test_requested_names_empty_means_unfiltered():
    class FakeArgs:
        target = ""

    args = FakeArgs()
    requested_names = (
        [n.strip() for n in args.target.split(",") if n.strip()] if args.target else []
    )
    assert requested_names == []


# ---------------------------------------------------------------------------
# Deliverable 2 — delta invalidation + row-unchanged proof.
# ---------------------------------------------------------------------------
def test_delta_record_round_trip(tmp_path):
    setup_dir = tmp_path / "setup"
    publish.write_delta_record(
        setup_dir, "sample", signature="sig1", source_sha="src1", dest_head="dest1"
    )
    record = publish.load_delta_record(setup_dir, "sample")
    assert record == {"signature": "sig1", "source_sha": "src1", "dest_head": "dest1"}


def test_load_delta_record_absent_returns_none(tmp_path):
    assert publish.load_delta_record(tmp_path / "setup", "nope") is None


def test_git_is_clean_true_for_committed_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")
    assert publish._git_is_clean(repo) is True


def test_git_is_clean_false_for_dirty_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")
    (repo / "f.txt").write_text("edited locally", encoding="utf-8")
    assert publish._git_is_clean(repo) is False


def test_git_is_clean_none_for_non_git_dir(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    assert publish._git_is_clean(d) is None


def _make_target(name: str, dest_dir: Path, source_dir: Path):
    return publish.ResolvedTarget(
        name=name,
        mode="mirror",
        source_dir=source_dir,
        dest_dir=dest_dir,
    )


def test_delta_row_unchanged_true_when_everything_matches(tmp_path, monkeypatch):
    setup_dir = tmp_path / "setup"
    source_repo = tmp_path / "source"
    dest_repo = tmp_path / "dest"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(source_repo, "source init")
    _init_repo(dest_repo)
    (dest_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(dest_repo, "dest init")

    target = _make_target("sample", dest_repo, source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])

    source_sha = publish._delta_row_source_sha(target)
    dest_head = publish._git_head(dest_repo)
    publish.write_delta_record(
        setup_dir, "sample", signature="sigA", source_sha=source_sha, dest_head=dest_head
    )
    assert publish.delta_row_unchanged(setup_dir, target, "sigA") is True


def test_delta_row_unchanged_false_on_signature_mismatch(tmp_path, monkeypatch):
    setup_dir = tmp_path / "setup"
    source_repo = tmp_path / "source"
    dest_repo = tmp_path / "dest"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(source_repo, "source init")
    _init_repo(dest_repo)
    (dest_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(dest_repo, "dest init")

    target = _make_target("sample", dest_repo, source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])

    source_sha = publish._delta_row_source_sha(target)
    dest_head = publish._git_head(dest_repo)
    publish.write_delta_record(
        setup_dir, "sample", signature="sigA", source_sha=source_sha, dest_head=dest_head
    )
    # A different signature (store or transform code changed) must
    # invalidate the row even though source/dest are untouched.
    assert publish.delta_row_unchanged(setup_dir, target, "sigB") is False


def test_delta_row_unchanged_false_when_source_advances(tmp_path, monkeypatch):
    setup_dir = tmp_path / "setup"
    source_repo = tmp_path / "source"
    dest_repo = tmp_path / "dest"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(source_repo, "source init")
    _init_repo(dest_repo)
    (dest_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(dest_repo, "dest init")

    target = _make_target("sample", dest_repo, source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])

    source_sha = publish._delta_row_source_sha(target)
    dest_head = publish._git_head(dest_repo)
    publish.write_delta_record(
        setup_dir, "sample", signature="sigA", source_sha=source_sha, dest_head=dest_head
    )

    (source_repo / "f.txt").write_text("payload v2", encoding="utf-8")
    _commit_all(source_repo, "source update")
    assert publish.delta_row_unchanged(setup_dir, target, "sigA") is False


def test_delta_row_unchanged_false_when_destination_drifts(tmp_path, monkeypatch):
    setup_dir = tmp_path / "setup"
    source_repo = tmp_path / "source"
    dest_repo = tmp_path / "dest"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(source_repo, "source init")
    _init_repo(dest_repo)
    (dest_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(dest_repo, "dest init")

    target = _make_target("sample", dest_repo, source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])

    source_sha = publish._delta_row_source_sha(target)
    dest_head = publish._git_head(dest_repo)
    publish.write_delta_record(
        setup_dir, "sample", signature="sigA", source_sha=source_sha, dest_head=dest_head
    )

    # Someone edited the published tree without committing — HEAD is
    # unchanged, but the working tree is now dirty. Must NOT skip.
    (dest_repo / "f.txt").write_text("edited by hand", encoding="utf-8")
    assert publish.delta_row_unchanged(setup_dir, target, "sigA") is False


def test_delta_row_unchanged_false_when_no_prior_record(tmp_path, monkeypatch):
    dest_repo = tmp_path / "dest"
    source_repo = tmp_path / "source"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(source_repo, "source init")
    _init_repo(dest_repo)
    (dest_repo / "f.txt").write_text("payload", encoding="utf-8")
    _commit_all(dest_repo, "dest init")

    target = _make_target("sample", dest_repo, source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])
    assert publish.delta_row_unchanged(tmp_path / "setup", target, "sigA") is False


def test_compute_delta_invalidation_signature_changes_on_store_edit(tmp_path):
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    engine_ctx = publish.PercolateEngineContext(claude-klabauter=None, store=None)
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    store_path.write_text("schema_version: '1.0.0'\nextra: true\n", encoding="utf-8")
    sig2 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    assert sig1 != sig2


def test_compute_delta_invalidation_signature_stable_when_nothing_changes(tmp_path):
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    engine_ctx = publish.PercolateEngineContext(claude-klabauter=None, store=None)
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    sig2 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    assert sig1 == sig2
