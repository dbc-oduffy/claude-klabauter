
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
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


def _stage_payload(publish, dest_dir: Path, files: "dict[str, str]"):
    """DR-445: `process_target` is stage-only, so a test double standing in
    for it must return a `StagedRowResult` naming an on-disk `staging_dir`
    — never write `target.dest_dir` itself (that write now happens once,
    later, inside `_run_round_dr445` / `_swap_all_rows_into_dest`).
    `staging_dir` is minted beside `dest_dir` (same filesystem), matching
    `_create_publish_staging_dir`'s own convention closely enough for
    `_swap_publish_staging_into_dest`'s rename-based swap to work."""
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    for rel, content in files.items():
        target_path = staging_dir / rel
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    return publish.StagedRowResult(
        staging_dir=staging_dir,
        row_visited=set(),
        # `None` (UNDETERMINED), matching the pre-DR-445 fakes this replaces:
        # they never populated a changed-files sink either, which kept this
        # round's repo root out of `end_of_run_changed_by_repo_root` and out
        # of the round-manifest write (§ `_run_round_dr445`'s manifest loop,
        # `if _manifest_root in end_of_run_changed_undetermined_roots:
        # continue`) — a real (non-`None`) set here makes the round write
        # `.percolate/round-manifest.json`, an untracked file outside this
        # round's commit pathspec, which is incidental bookkeeping these
        # tests were never meant to exercise and would otherwise show up as
        # unrelated residue in every `porcelain_after == ""` assertion below.
        row_changed_files=None,
        row_removed_files=set(),
        row_published_files={Path(rel) for rel in files},
        report_text="",
        synced=len(files),
        deleted=0,
    )


def test_partial_round_commits_succeeded_rows_excludes_failed(monkeypatch, tmp_path, capsys):
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
        totals.processed += 1
        return _stage_payload(
            publish, target.dest_dir, {"payload.txt": f"published by {target.name}\n"}
        )

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

    assert "publish.py: uncommitted in" not in combined


def test_failed_row_subtree_excluded_even_as_succeeded_ancestor(monkeypatch, tmp_path, capsys):
    """AC2. The toplevel-row shape: a SUCCEEDED row's dest dir is the repo
    root itself (an ancestor of every other row's dest dir), and a FAILED
    row's dest dir is a subdir. That subdir's tracked content must not be
    touched by the succeeded toplevel row's commit despite being inside the
    (wider) scope the commit walks.

    `existing.txt` is committed (tracked, clean) here rather than left
    uncommitted at round start as the pre-DR-445 fixture did: a separate,
    unrelated round-start guard landed on this branch since
    (`coordinator/bin/publish.py` "publish: refuse to start a round on a
    dirty source or destination repo", `_dirty_round_roots` dispatched from
    `main()` before any row is processed) and now refuses the WHOLE round
    before `process_target` ever runs against an uncommitted destination —
    this test's own dirty-subtree fixture would trip THAT guard instead of
    reaching the partial-round/AC2 behavior it exists to cover. Isolation is
    now asserted via content-unchanged rather than stays-uncommitted."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)

    failed_subdir = dest_root / "sub-failed"
    failed_subdir.mkdir(parents=True, exist_ok=True)
    pre_existing = failed_subdir / "existing.txt"
    pre_existing.write_text("pre-existing content\n", encoding="utf-8")
    _git(dest_root, "add", "sub-failed/existing.txt")
    _git(dest_root, "commit", "-m", "seed sub-failed/existing.txt")

    rows = [
        _row_string("row-top", tmp_path / "src-top", dest_root),
        _row_string("row-sub", tmp_path / "src-sub", failed_subdir),
    ]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        if target.name == "row-sub":
            raise SystemExit(3)
        totals.processed += 1
        return _stage_payload(
            publish, target.dest_dir, {"toplevel_new.txt": "published by row-top\n"}
        )

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-top,row-sub"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 1

    porcelain_after = _porcelain(dest_root)
    assert porcelain_after == "", (
        f"row-sub never staged/swapped anything (SystemExit before staging) — "
        f"nothing under sub-failed/ should be dirty, got: {porcelain_after!r}"
    )
    assert pre_existing.read_text(encoding="utf-8") == "pre-existing content\n", (
        "the failed row's tracked content must be untouched by the succeeded "
        "ancestor row's commit"
    )

    head_ls = _git(dest_root, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "toplevel_new.txt" in head_ls
    assert "sub-failed/existing.txt" in head_ls


def test_mutated_root_skipped_nothing_committed(monkeypatch, tmp_path, capsys):
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
        # DR-445 (2026-09-29 revision: git moves the delta): `process_target`
        # never writes `target.dest_dir` and never raises `PublishSwapPartial`
        # itself — that exception now comes out of `_commit_throwaway_and_
        # merge_into_dest`, called once per REPO ROOT (not per row) by
        # `_swap_all_rows_into_dest`. row-a and row-b share `dest_root`, so
        # their swap is now ONE atomic commit+merge for the whole root —
        # the fault is injected below by wrapping that function instead of a
        # per-row primitive, which no longer exists.
        totals.processed += 1
        return _stage_payload(
            publish, target.dest_dir, {"payload.txt": f"published by {target.name}\n"}
        )

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    def fake_commit_merge(repo_root, throwaway_root, **kwargs):
        # Simulate content landing on disk (e.g. an external interference
        # mid-merge) before the operation still fails to complete — the
        # scenario `PublishSwapPartial(content_swapped=True)` exists to
        # name: dest IS mutated, but this round's commit did not happen.
        dest_b.mkdir(parents=True, exist_ok=True)
        (dest_b / "swapped.txt").write_text("landed\n", encoding="utf-8")
        raise publish.PublishSwapPartial(
            "simulated stranded .git re-home failure",
            prior_backup=repo_root / ".prior",
            content_swapped=True,
        )

    monkeypatch.setattr(publish, "_commit_throwaway_and_merge_into_dest", fake_commit_merge)

    rc = publish.main(["row-a,row-b"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 1
    log_count = _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip()
    assert log_count == "1", "the mutated root must gain no new commit this round"
    # 2026-09-29 PM ruling (structural restore-or-commit invariant): a
    # residue root's published bytes are now restored to HEAD before
    # `main()` returns -- the mutated root is skipped from THIS round's
    # commit exactly as before, but it is no longer left dirty for a human
    # to reconcile, it is byte-identical with HEAD again.
    porcelain_after = _porcelain(dest_root)
    assert porcelain_after == "", (
        f"residue roots must be restored to HEAD, not left dirty: {porcelain_after!r}"
    )
    assert "publish.py: uncommitted in" in combined
    assert str(dest_root) in combined


def test_gate_failure_alone_still_commits_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: False)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        totals.processed += 1
        return _stage_payload(publish, target.dest_dir, {"payload.txt": "published\n"})

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 2
    log_count = _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip()
    assert log_count == "1", "a gate failure must commit nothing, unchanged from before this fix"
    # DR-445 (docs/decisions/DR-445-publish-assembles-in-a-throwaway-and-
    # moves-once.md): every end-of-run gate — including the identity check
    # stubbed False here — now runs against a throwaway BEFORE the one real
    # swap, not against `dest_dir` after a per-row swap that already
    # happened. A gate failure this early means `dest_dir` was never
    # touched at all this round, so there is no swapped-but-uncommitted
    # residue to restore or report — retiring the 2026-09-29 PM ruling's
    # own "restored to HEAD"/"uncommitted in" assertions FOR THIS PATH
    # specifically (the mutated-root sibling test below still covers a
    # genuine post-swap residue, which DR-445 leaves reachable via a
    # swap-time failure).
    porcelain_after = _porcelain(dest_root)
    assert porcelain_after == "", (
        f"a gate-failure round must leave dest untouched, not dirty: {porcelain_after!r}"
    )
    assert "publish.py: uncommitted in" not in combined, (
        "no residue message: dest was never written this round", combined
    )


def test_clean_round_no_residue_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        totals.processed += 1
        return _stage_payload(publish, target.dest_dir, {"payload.txt": "published\n"})

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    rc = publish.main(["row-a"])
    out, err = capsys.readouterr()
    combined = out + err

    assert rc == 0
    assert "publish.py: uncommitted in" not in combined
    porcelain_after = _porcelain(dest_root)
    assert porcelain_after == ""
