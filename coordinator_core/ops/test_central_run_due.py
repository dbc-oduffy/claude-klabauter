"""Characterization + parity tests for coordinator_core.ops.central_run_due.

Port of: central-run-due.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from coordinator_core.ops.central_run_due import (
    _claude_home,
    _resolve_threshold,
    main,
)


def _make_doe_content_root(tmp_path: Path) -> Path:
    """Build a minimal coordinator-claude-shaped coordinator/ tree with the one remaining
    subprocess-boundary sibling this module shells out to (extract-lessons.py,
    already-Python — see module docstring), so main() can run end-to-end without
    the real coordinator-claude repo. `coordinator-state-root.sh` and `learn-lessons-roots.sh`
    are retired bash bridges as of C11 (2026-07-21) — their native peers are
    exercised via monkeypatch in `_run_main`, not via on-disk fake scripts.
    """
    root = tmp_path / "coordinator"
    (root / "bin").mkdir(parents=True)

    extract_py = root / "bin" / "extract-lessons.py"
    extract_py.write_text(
        "import sys\n"
        "n = 0\n"
        "for arg in sys.argv:\n"
        "    if arg.endswith('.md'):\n"
        "        try:\n"
        "            with open(arg) as fh:\n"
        "                n = len([l for l in fh if l.strip()])\n"
        "        except OSError:\n"
        "            pass\n"
        "print(f'# record_count: {n}')\n"
    )
    return root


def _env_for(tmp_path: Path, doe_root: Path, central_state_root: Path, roots_file: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_HOME"] = str(tmp_path)  # CLAUDE_HOME env var overrides $HOME, per oracle
    env["COORDINATOR_ROOT"] = str(doe_root)
    # Consumed by the monkeypatched native-resolver stand-ins in _run_main below,
    # not by any on-disk fake script (those two bash bridges are retired, C11).
    env["CENTRAL_STATE_ROOT"] = str(central_state_root)
    env["LL_ROOTS_FILE"] = str(roots_file)
    return env


def _run_main(argv, env, monkeypatch=None):
    """Run `main(argv)` with `env` installed, monkeypatching the two native
    resolver call-sites (`_coordinator_state_root_central`, `_learn_lessons_roots`)
    to stand-ins driven by CENTRAL_STATE_ROOT / LL_ROOTS_FILE respectively —
    these replace the pre-C11 fake bash scripts the same fixtures used to drive.
    """
    import coordinator_core.ops.central_run_due as _mod

    central_state_root = env.get("CENTRAL_STATE_ROOT", "")
    roots_file = env.get("LL_ROOTS_FILE", "")

    def _fake_state_root():
        return central_state_root

    def _fake_roots():
        if not roots_file or not os.path.isfile(roots_file):
            return []
        with open(roots_file, encoding="utf-8") as fh:
            return [line for line in fh.read().splitlines() if line.strip()]

    old_environ = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    old_state_root_fn = _mod._coordinator_state_root_central
    old_roots_fn = _mod._learn_lessons_roots
    _mod._coordinator_state_root_central = _fake_state_root
    _mod._learn_lessons_roots = _fake_roots
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        _mod._coordinator_state_root_central = old_state_root_fn
        _mod._learn_lessons_roots = old_roots_fn
    return rc, out.getvalue(), err.getvalue()


def _claude_home_dir(tmp_path: Path) -> Path:
    return tmp_path / ".claude"


def _write_config(central_state_root: Path, threshold: str | None) -> None:
    central_state_root.mkdir(parents=True, exist_ok=True)
    cfg = central_state_root / "learn-lessons-config.md"
    if threshold is not None:
        cfg.write_text(f"central_volume_threshold: {threshold}\n")
    else:
        cfg.write_text("# no threshold key\n")


def _write_sentinel(claude_home: Path, date: str) -> None:
    d = claude_home / "tasks" / f"learn-lessons-{date}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "COMPLETE").write_text("")


class TestPositive:
    def test_below_threshold_reports_on_stderr(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold="150")
        _write_sentinel(claude_home, "2026-07-01")

        source_repo = tmp_path / "repo-a"
        (source_repo / "state").mkdir(parents=True)
        (source_repo / "state" / "lessons.md").write_text("")
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text(f"{claude_home}\n{source_repo}\n")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out == ""
        assert "below threshold" in err
        assert "2026-07-01" in err

    def test_over_threshold_emits_central_run_due(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold=None)
        _write_sentinel(claude_home, "2026-07-01")

        source_repo = tmp_path / "repo-a"
        (source_repo / "state").mkdir(parents=True)
        # 2 non-blank lines -> the fake extractor reports record_count=2
        (source_repo / "state" / "lessons.md").write_text("line1\nline2\n")
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text(f"{claude_home}\n{source_repo}\n")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main(["1"], env)  # arg threshold overrides config

        assert rc == 0
        assert "CENTRAL_RUN_DUE volume: 2" in out
        assert "repo-a:2" in out

    def test_self_excludes_claude_home_root(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold="1")
        _write_sentinel(claude_home, "2026-07-01")

        (claude_home / "state").mkdir(parents=True)
        (claude_home / "state" / "lessons.md").write_text("would-count\n")
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text(f"{claude_home}\n")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out == ""  # self-excluded -> total stays 0, below threshold 1


class TestNegative:
    def test_invalid_threshold_skips(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold="150")
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text("")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main(["notanumber"], env)

        assert rc == 0
        assert out == ""
        assert "invalid threshold 'notanumber'" in err

    def test_missing_config_skips(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state-absent"
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text("")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out == ""
        assert "no config at" in err

    def test_missing_sentinel_skips(self, tmp_path: Path):
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold="150")
        # no COMPLETE sentinel written under claude_home/tasks
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text("")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out == ""
        assert "no COMPLETE central-run sentinel found" in err

    def test_empty_roots_list_stays_below_threshold(self, tmp_path: Path):
        """C11 (2026-07-21): the bash-executable-presence skip-gate this test used
        to exercise ("roots helper not executable — skipping") no longer exists —
        `_learn_lessons_roots` is now a native in-process call to
        `coordinator_core.ops.learn_lessons_roots.resolve_roots()`, which cannot be
        "missing" the way an on-disk bash script could. An empty roots list simply
        contributes zero universals and reports below-threshold, same as any other
        zero-total run."""
        doe_root = _make_doe_content_root(tmp_path)
        claude_home = _claude_home_dir(tmp_path)
        central_state_root = tmp_path / "central-state"
        _write_config(central_state_root, threshold="150")
        _write_sentinel(claude_home, "2026-07-01")
        roots_file = tmp_path / "roots.txt"
        roots_file.write_text("")

        env = _env_for(tmp_path, doe_root, central_state_root, roots_file)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out == ""
        assert "0/150" in err


class TestUnitHelpers:
    def test_claude_home_uses_env_override_as_home(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/tmp/fake-home")
        assert _claude_home() == os.path.join("/tmp/fake-home", ".claude")

    def test_resolve_threshold_arg_wins_over_config(self, tmp_path: Path):
        cfg = tmp_path / "learn-lessons-config.md"
        cfg.write_text("central_volume_threshold: 999\n")
        assert _resolve_threshold(["42"], str(cfg)) == 42

    def test_resolve_threshold_config_wins_over_default(self, tmp_path: Path):
        cfg = tmp_path / "learn-lessons-config.md"
        cfg.write_text("central_volume_threshold: 77\n")
        assert _resolve_threshold([], str(cfg)) == 77

    def test_resolve_threshold_default_when_nothing_set(self, tmp_path: Path):
        cfg = tmp_path / "does-not-exist.md"
        assert _resolve_threshold([], str(cfg)) == 150

    def test_resolve_threshold_invalid_arg_returns_none(self, tmp_path: Path):
        cfg = tmp_path / "does-not-exist.md"
        assert _resolve_threshold(["abc"], str(cfg)) is None
