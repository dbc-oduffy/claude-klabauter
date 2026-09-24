"""coordinator/bin/tests/test_publish_partial_round_commits_succeeded_rows.py
— regression test for docs/plans/2026-09-23-partial-round-strand.md (P179-C1):
`publish.py::main` used to `return 1` on a partial round (some rows failed,
gates green) BEFORE `_commit_published_dests` ever ran, leaving every
succeeded row's synced bytes uncommitted on disk. This module pins the fixed
behaviour: `main()` now commits the succeeded rows' bytes despite the failed
row(s), excludes a failed row's own dest-dir subtree from that commit, skips
a repo root a failed row actually mutated (`PublishSwapPartial(content_
swapped=True)`) whole, and names the residue on every non-zero return.

Reuses `test_publish_row_failure_aggregates_gates.py`'s fixture shape (same
fake-row / real-git-dest wiring), but the fake `process_target` here writes
REAL files into its dest dir and populates the sinks `main()`'s own
`process_target` call populates on a real publish, so the commit this test
exercises is `main()`'s real end-of-run `_commit_published_dests` call
running against real git state — not a hand-rolled stand-in for it.

Run: python -m pytest coordinator/bin/tests/test_publish_partial_round_commits_succeeded_rows.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_git_repo(root: Path) -> None:
    if (root / ".git").is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-partial-round-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Partial Round Test")
    _git(root, "config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "chore: init")
    # A self-origin the dest is level with — `main()` refuses a dest whose
    # branch tracks nothing (§ `percolate.dest_refresh.refresh_dest_from_origin`),
    # same shape as the aggregate-gates fixture this module reuses.
    _git(root, "remote", "add", "origin", str(root))
    _git(root, "fetch", "--no-tags", "origin")
    _git(root, "branch", "--set-upstream-to=origin/main", "main")


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_partial_round_commits_succeeded_rows_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _wire_common_fakes(monkeypatch, tmp_path, rows):
    """Same shape as `test_publish_row_failure_aggregates_gates.py::
    _wire_common_fakes`, minus the identity/entrypoint knobs this module
    does not need — every end-of-run gate is wired green so a row failure
    is the only source of a non-zero exit unless a test says otherwise."""

    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter=None, **_: list(rows)
    )

    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            return ()

    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda path: publish.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)

    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_function_gate", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_entrypoint_gate", lambda *a, **k: True)


def _row_string(name: str, src: Path, dst: Path) -> str:
    src.mkdir(parents=True, exist_ok=True)
    return f"{name}|mirror|{src}|{dst}"


def test_partial_round_commits_succeeded_rows_excludes_failed(monkeypatch, tmp_path, capsys):
    """AC1 + AC5. Three rows against one git dest, per-row subdirs: the
    middle row raises in `process_target`, the other two write real files
    under their dest dirs. `main()` exits 1, but dest HEAD contains both
    succeeded rows' files, `git status --porcelain` is clean under their
    dirs, the commit subject names exactly the two succeeded rows, and the
    residue line names the failed row's dest root."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)

    dest_a = dest_root / "sub-a"
    dest_b = dest_root / "sub-b"
    dest_c = dest_root / "sub-c"
    rows = [
        _row_string("row-a", tmp_path / "src-a", dest_a),
        _row_string("row-b", tmp_path / "src-b", dest_b),
        _row_string("row-c", tmp_path / "src-c", dest_c),
    ]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        if target.name == "row-b":
            raise SystemExit(3)
        target.dest_dir.mkdir(parents=True, exist_ok=True)
        payload = target.dest_dir / "payload.txt"
        payload.write_text(f"published by {target.name}\n", encoding="utf-8")
        kwargs["published_dest_dirs_sink"].add(target.dest_dir)
        kwargs["published_files_sink"].add(payload)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a,row-b,row-c"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 1
    assert "Rows FAILED" in combined and "row-b" in combined

    head_ls = _git(dest_root, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "sub-a/payload.txt" in head_ls
    assert "sub-c/payload.txt" in head_ls
    assert "sub-b/payload.txt" not in head_ls

    porcelain = _porcelain(dest_root)
    assert "sub-a" not in porcelain
    assert "sub-c" not in porcelain

    log_subject = _git(dest_root, "log", "-1", "--format=%s").stdout
    assert "row-a" in log_subject and "row-c" in log_subject
    assert "row-b" not in log_subject

    # `row-b` wrote nothing before its raise (pre-check), and the commit for
    # this shared root succeeded for everything else — no residue remains,
    # so AC5's "commit succeeded for every [affected] root" carve-out
    # applies and no residue line is printed.
    assert "publish.py: uncommitted in" not in combined


def test_failed_row_subtree_excluded_even_as_succeeded_ancestor(monkeypatch, tmp_path, capsys):
    """AC2. The toplevel-row shape: a SUCCEEDED row's dest dir is the repo
    root itself (an ancestor of every other row's dest dir), and a FAILED
    row's dest dir is a subdir with a pre-existing dirty file. That dirty
    file must not be swept into the succeeded toplevel row's commit despite
    being inside the (wider) scope the commit walks."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)

    failed_subdir = dest_root / "sub-failed"
    failed_subdir.mkdir(parents=True, exist_ok=True)
    pre_existing = failed_subdir / "existing.txt"
    pre_existing.write_text("pre-existing dirty content\n", encoding="utf-8")
    # Left untracked deliberately — the failed row never wrote anything
    # (pre-check: nothing after the swap can fail a row except
    # `PublishSwapPartial(content_swapped=True)`), so this file is dirty
    # for a reason unrelated to this round and must stay untouched by it.

    rows = [
        _row_string("row-top", tmp_path / "src-top", dest_root),
        _row_string("row-sub", tmp_path / "src-sub", failed_subdir),
    ]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        if target.name == "row-sub":
            raise SystemExit(3)
        payload = target.dest_dir / "toplevel_new.txt"
        payload.write_text("published by row-top\n", encoding="utf-8")
        kwargs["published_dest_dirs_sink"].add(target.dest_dir)
        kwargs["published_files_sink"].add(payload)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-top,row-sub"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 1

    porcelain_after = _porcelain(dest_root)
    assert "sub-failed/" in porcelain_after, (
        "the failed row's pre-existing dirty file must stay uncommitted, "
        f"got: {porcelain_after!r}"
    )

    head_ls = _git(dest_root, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "toplevel_new.txt" in head_ls
    assert "existing.txt" not in head_ls


def test_mutated_root_skipped_nothing_committed(monkeypatch, tmp_path, capsys):
    """AC3. A row raising `PublishSwapPartial(content_swapped=True)` marks
    its whole repo root skipped this round — nothing is committed there —
    and the residue line names it."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)

    dest_a = dest_root / "sub-a"
    dest_b = dest_root / "sub-b"
    rows = [
        _row_string("row-a", tmp_path / "src-a", dest_a),
        _row_string("row-b", tmp_path / "src-b", dest_b),
    ]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        if target.name == "row-b":
            # Content DID land at dest before the raise (content_swapped=True).
            target.dest_dir.mkdir(parents=True, exist_ok=True)
            (target.dest_dir / "swapped.txt").write_text("landed\n", encoding="utf-8")
            raise publish.PublishSwapPartial(
                "simulated stranded .git re-home failure",
                prior_backup=target.dest_dir.parent / ".prior",
                content_swapped=True,
            )
        target.dest_dir.mkdir(parents=True, exist_ok=True)
        payload = target.dest_dir / "payload.txt"
        payload.write_text(f"published by {target.name}\n", encoding="utf-8")
        kwargs["published_dest_dirs_sink"].add(target.dest_dir)
        kwargs["published_files_sink"].add(payload)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a,row-b"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 1
    # `row-a` is the only row whose subtree could be committed this round —
    # `row-b`'s mutated ROOT (the whole dest_root, since both rows share it)
    # is skipped whole, so nothing at all is committed.
    log_count = _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip()
    assert log_count == "1", "the mutated root must gain no new commit this round"
    porcelain_after = _porcelain(dest_root)
    assert "sub-a/" in porcelain_after
    assert "sub-b/" in porcelain_after
    assert "publish.py: uncommitted in" in combined
    assert str(dest_root) in combined


def test_gate_failure_alone_still_commits_nothing(monkeypatch, tmp_path, capsys):
    """AC4. Every row succeeds but an end-of-run gate fails: no commit is
    made (AC15 fail-closed, unchanged), exit code is 2, and the residue
    line names the dest root left with synced-but-uncommitted bytes."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: False)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        target.dest_dir.mkdir(parents=True, exist_ok=True)
        payload = target.dest_dir / "payload.txt"
        payload.write_text("published\n", encoding="utf-8")
        kwargs["published_dest_dirs_sink"].add(target.dest_dir)
        kwargs["published_files_sink"].add(payload)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 2
    log_count = _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip()
    assert log_count == "1", "a gate failure must commit nothing, unchanged from before this fix"
    porcelain_after = _porcelain(dest_root)
    assert "sub-a/" in porcelain_after
    assert "publish.py: uncommitted in" in combined
    assert str(dest_root) in combined


def test_clean_round_no_residue_line(monkeypatch, tmp_path, capsys):
    """No residue line, and a clean exit 0, on a round with no failed row
    and every gate green — the happy path is unaffected by this fix."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        target.dest_dir.mkdir(parents=True, exist_ok=True)
        payload = target.dest_dir / "payload.txt"
        payload.write_text("published\n", encoding="utf-8")
        kwargs["published_dest_dirs_sink"].add(target.dest_dir)
        kwargs["published_files_sink"].add(payload)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 0
    assert "publish.py: uncommitted in" not in combined
    porcelain_after = _porcelain(dest_root)
    assert porcelain_after == ""
