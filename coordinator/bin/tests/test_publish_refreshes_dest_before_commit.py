"""`publish.main` refreshes each destination from origin before any row runs, and a failed refresh refuses the round."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-refresh-order-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Refresh Order Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "chore: init")
    _git(root, "remote", "add", "origin", str(root))
    _git(root, "fetch", "--no-tags", "origin")
    _git(root, "branch", "--set-upstream-to=origin/main", "main")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_refreshes_dest_before_commit_under_test", _BIN_DIR / "publish.py"
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

        def run_parse_sweep(self, repo_root, **_):
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
    for gate in (
        "dispatch_end_of_run_identity_check",
        "dispatch_end_of_run_install_doc_payload_check",
        "dispatch_end_of_run_unscanned_published_check",
        "dispatch_end_of_run_function_gate",
        "dispatch_end_of_run_entrypoint_gate",
    ):
        monkeypatch.setattr(publish, gate, lambda *a, **k: True)


def _stage_payload(dest_dir: Path, files: "dict[str, str]"):
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    for rel, content in files.items():
        path = staging_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return publish.StagedRowResult(
        staging_dir=staging_dir,
        row_visited=set(),
        row_changed_files=None,
        row_removed_files=set(),
        row_published_files={Path(rel) for rel in files},
        report_text="",
        synced=len(files),
        deleted=0,
    )


def _arm(monkeypatch, tmp_path, refresh_ok: bool):
    """Wire one row against a throwaway repo; return the ordered event log."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    src = tmp_path / "src-a"
    src.mkdir(parents=True, exist_ok=True)
    rows = [f"row-a|mirror|{src}|{dest_root / 'sub-a'}"]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    events: "list[str]" = []

    def fake_process_target(target, setup_dir, totals, **kwargs):
        events.append("row")
        totals.processed += 1
        return _stage_payload(target.dest_dir, {"payload.txt": "published\n"})

    def fake_commit(*a, **k):
        events.append("commit")
        raise AssertionError("_commit_published_dests must not run on this path")

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    publish._bootstrap_engine()
    from percolate import dest_refresh

    def fake_refresh(root, **kwargs):
        events.append("refresh")
        return SimpleNamespace(ok=refresh_ok, reason="simulated stale candidate")

    monkeypatch.setattr(dest_refresh, "refresh_dest_from_origin", fake_refresh)
    return dest_root, events, fake_commit


def test_failed_refresh_returns_1_with_no_row_and_no_commit(monkeypatch, tmp_path, capsys):
    dest_root, events, fake_commit = _arm(monkeypatch, tmp_path, refresh_ok=False)
    monkeypatch.setattr(publish, "_commit_published_dests", fake_commit)

    rc = publish.main(["row-a"])
    out, err = capsys.readouterr()

    assert rc == 1
    assert "FATAL: simulated stale candidate" in err
    assert events == ["refresh"]
    assert not (dest_root / "sub-a").exists()
    assert _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_refresh_runs_before_the_first_row(monkeypatch, tmp_path):
    dest_root, events, _ = _arm(monkeypatch, tmp_path, refresh_ok=True)
    rc = publish.main(["row-a"])

    assert rc == 0
    assert events == ["refresh", "row"]
    assert _git(dest_root, "rev-list", "--count", "HEAD").stdout.strip() == "2"
