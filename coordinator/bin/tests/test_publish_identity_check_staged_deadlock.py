
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_identity_check_staged_deadlock_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.ops.percolate_identity_check import run_identity_check  # noqa: E402


_SENTINEL_NAME = "PLANTED-FINDING-SENTINEL"
_SYNTHETIC_CHECKER = f'''\
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
sentinel = here.parent.parent / "{_SENTINEL_NAME}"
if sentinel.is_file():
    print("Identity check FAILED:")
    print("  fixture/planted.txt:1: fixture-token 'PLANTED-FINDING-SENTINEL' -- synthetic test fixture")
    sys.exit(1)
print("Identity check passed (0 text files scanned, 0 paths checked).")
sys.exit(0)
'''


def _write_checker(dest: Path) -> Path:
    script_dir = dest / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "check-persona-names.py"
    script_path.write_text(_SYNTHETIC_CHECKER, encoding="utf-8")
    return script_path


class _IdentityCheckClaudeKlabauter:

    def resolve_target(self, store, name):
        return {"hooks": [], "file_surface": {}, "guards": [], "inject": []}

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return run_identity_check(dest)


def _ctx(claude_klabauter_engine) -> "publish.PercolateEngineContext":
    return publish.PercolateEngineContext(engine_claude_klabauter=claude_klabauter_engine, store={"targets": {}})


def _staged_row(tmp_path: Path, name="t"):
    """Builds a real destination repo root plus a SEPARATE staging tree, the
    exact shape `process_target` produces: `sync_target` (the `target` param
    `dispatch_percolate_pre_ci` receives) has `dest_dir` pointed at the
    staging copy, while the real path is threaded through separately via
    `identity_dest_dir`. This row publishes the repo root directly (empty
    `dest_subdir`) -- the case row C6's bug report actually hit."""
    src = tmp_path / "src"
    src.mkdir()

    real_dest = tmp_path / "repo"
    real_dest.mkdir()
    (real_dest / ".git").mkdir()

    staging_dest = tmp_path / "repo.publish-staging"
    staging_dest.mkdir()

    sync_target = publish.ResolvedTarget(
        name=name, mode="flat-mirror", source_dir=src, dest_dir=staging_dest
    )
    return sync_target, real_dest, staging_dest


class TestStagedDeadlockClosed:
    def test_stale_dest_but_clean_staged_source_now_proceeds(self, tmp_path):
        sync_target, real_dest, staging_dest = _staged_row(tmp_path)

        _write_checker(real_dest)
        (real_dest / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        _write_checker(staging_dest)

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine),
            tmp_path / "store.yaml",
            sync_target,
            tmp_path / "src",
            None,
            identity_dest_dir=real_dest,
        )

    def test_leaking_staged_source_still_refuses(self, tmp_path):
        sync_target, real_dest, staging_dest = _staged_row(tmp_path)

        _write_checker(real_dest)

        _write_checker(staging_dest)
        (staging_dest / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        with pytest.raises(publish.EngineUnavailableError) as excinfo:
            publish.dispatch_percolate_pre_ci(
                _ctx(claude_klabauter_engine),
                tmp_path / "store.yaml",
                sync_target,
                tmp_path / "src",
                None,
                identity_dest_dir=real_dest,
            )
        assert "check-persona-names.py exited 1" in str(excinfo.value)
        assert "PLANTED-FINDING-SENTINEL" in str(excinfo.value)

    def test_sibling_subdir_row_defers_to_end_of_run_leg(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        staging_subdir = tmp_path / "coordinator_core.publish-staging"
        staging_subdir.mkdir()

        sync_target = publish.ResolvedTarget(
            name="engine-row", mode="mirror", source_dir=src, dest_dir=staging_subdir
        )
        real_subdir_dest = repo_root / "coordinator_core"
        real_subdir_dest.mkdir()

        _write_checker(repo_root)
        (repo_root / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine),
            tmp_path / "store.yaml",
            sync_target,
            tmp_path / "src",
            None,
            identity_dest_dir=real_subdir_dest,
        )
        captured = capsys.readouterr()
        assert "pre_ci per-row identity scan skipped" in captured.err

        ok = publish.dispatch_end_of_run_identity_check(
            _ctx(claude_klabauter_engine), [repo_root], target_filtered=False
        )
        assert ok is False
