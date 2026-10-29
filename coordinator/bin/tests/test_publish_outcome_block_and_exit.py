"""A publish run with a refused row exits nonzero and ends on a per-row outcome block, not on [timing] rows.

Drives `publish.main` at default arguments with stubbed targets and engine.

Run: python -m pytest coordinator/bin/tests/test_publish_outcome_block_and_exit.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_outcome_block_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _init_git_repo(root: Path) -> None:
    if (root / ".git").is_dir():
        return

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True, creationflags=_NO_WINDOW)

    _git("init", "-b", "main")
    _git("config", "user.email", "outcome-block-test@claude-klabauter.test")
    _git("config", "user.name", "Outcome Block Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    _git("remote", "add", "origin", str(root))
    _git("fetch", "--no-tags", "origin")
    _git("branch", "--set-upstream-to=origin/main", "main")


def _wire(monkeypatch, tmp_path, row_names, declined):
    def fake_row(name: str) -> str:
        src = tmp_path / f"src-{name}"
        dst = tmp_path / f"dst-{name}"
        src.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)
        _init_git_repo(dst)
        return f"{name}|mirror|{src}|{dst}"

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung"))
    monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [fake_row(n) for n in row_names])

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
        publish, "parse_percolate_identity", lambda path: publish.PercolateIdentity(review=["dummy-pattern"])
    )
    monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)

    def fake_process_target(target, setup_dir, totals, *, timing_sink=None, **kwargs):
        if target.name in declined:
            print(f"  Error: field 7 is stale: + index_distribution. Remedy: run publish-allowlist-generate.py", file=sys.stderr)
            print(f"  Skipping {target.name}.", file=sys.stderr)
            if timing_sink is not None:
                timing_sink.append((target.name, "REFUSED: pre-sync gate declined", 0.0, 0.0))
            return None
        if timing_sink is not None:
            timing_sink.append((target.name, "run_pre_sync_gates", 0.01, 0.01))
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)


def _run(monkeypatch, tmp_path, row_names, declined):
    _wire(monkeypatch, tmp_path, row_names, declined)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)
    rc = publish.main([",".join(row_names)])
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return rc, lines


def test_pre_sync_gate_declined_row_exits_nonzero_and_ends_on_outcome_block(monkeypatch, tmp_path):
    rc, lines = _run(monkeypatch, tmp_path, ["row-a", "row-b"], declined={"row-b"})

    assert rc != 0
    assert any("[timing]" in ln for ln in lines)
    assert lines.index("=== publish outcome ===") > max(i for i, ln in enumerate(lines) if "[timing]" in ln)
    tail = "\n".join(lines[lines.index("=== publish outcome ==="):])
    assert "row-a: PUBLISHED" in tail
    assert "row-b: REFUSED/FAILED" in tail
    assert "field 7 is stale" in tail
    assert "run publish-allowlist-generate.py" in tail
    assert "[timing]" not in tail
    assert lines[-1].strip().startswith("result: NOT OK")


def test_refusal_without_error_line_falls_back_to_timing_label(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, ["row-a"], declined={"row-a"})

    def quiet(target, setup_dir, totals, *, timing_sink=None, **kwargs):
        timing_sink.append((target.name, "REFUSED: dest not ready", 0.0, 0.0))

    monkeypatch.setattr(publish, "process_target", quiet)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)

    assert publish.main(["row-a"]) != 0
    assert "reason:    dest not ready" in buf.getvalue()


def test_all_rows_published_exits_zero_and_still_ends_on_outcome_block(monkeypatch, tmp_path):
    rc, lines = _run(monkeypatch, tmp_path, ["row-a", "row-b"], declined=set())

    assert rc == 0
    assert "=== publish outcome ===" in lines
    assert lines[-1].strip() == "result: OK (exit 0)"
    assert "[timing]" not in "\n".join(lines[lines.index("=== publish outcome ==="):])
