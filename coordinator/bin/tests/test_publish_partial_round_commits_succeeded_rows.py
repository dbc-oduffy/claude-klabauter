
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
    log_count = _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip()
    assert log_count == "1", "the mutated root must gain no new commit this round"
    porcelain_after = _porcelain(dest_root)
    assert "sub-a/" in porcelain_after
    assert "sub-b/" in porcelain_after
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
