
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


def _init_git_repo(root: Path) -> None:
    # IDEMPOTENT ON PURPOSE — see the sibling fixture
    if (root / ".git").is_dir():
        return

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "publish-dry-run-honesty-test@claude-klabauter.test")
    _git("config", "user.name", "Publish Dry-Run Honesty Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    _git("remote", "add", "origin", str(root))
    _git("fetch", "--no-tags", "origin")
    _git("branch", "--set-upstream-to=origin/main", "main")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dry_run_honesty_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_NAMES = ["row-a", "row-b"]


def _wire_common_fakes(monkeypatch, tmp_path, *, rows_reached: list):
    def fake_row(name: str) -> str:
        src = tmp_path / f"src-{name}"
        dst = tmp_path / f"dst-{name}"
        src.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)
        _init_git_repo(dst)
        return f"{name}|mirror|{src}|{dst}"

    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter=None, **_: [
            fake_row(n) for n in _ROW_NAMES
        ]
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

    def fake_process_target(target, setup_dir, totals, **kwargs):
        rows_reached.append(target.name)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)


def test_dry_run_all_rows_succeeded_names_skipped_guard_phases(monkeypatch, tmp_path, capsys):
    rows_reached: list = []
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    rc = publish.main([",".join(_ROW_NAMES), "--dry-run"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rows_reached == _ROW_NAMES
    assert rc == 0
    assert f"Rows succeeded: {len(_ROW_NAMES)}/{len(_ROW_NAMES)}" in combined
    assert "post_rsync" in combined and "not evaluated" in combined


def test_real_run_all_rows_succeeded_does_not_carry_the_dry_run_qualifier(monkeypatch, tmp_path, capsys):
    rows_reached: list = []
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    rc = publish.main([",".join(_ROW_NAMES)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rows_reached == _ROW_NAMES
    assert f"Rows succeeded: {len(_ROW_NAMES)}/{len(_ROW_NAMES)}" in combined
    assert "engine-phase guards were" not in combined
