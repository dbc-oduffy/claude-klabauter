"""test_bug_sweep_probes — pytest tests for coordinator/bin/bug-sweep-probes.py.

Spec backlink: DoE-claude coordinator/skills/bug-sweep/SKILL.md § Phase 0 step 1
  and § Phase 4 step 0.
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
  (chunk C-BUGSWEEP)

Coverage:
  detect-stack:
    test_detect_stack_finds_languages_test_dirs_and_config
    test_detect_stack_head_limit_caps_at_20
    test_detect_stack_empty_dir_returns_empty_lists
  verify-diff:
    test_verify_diff_no_missing_when_all_expected_changed
    test_verify_diff_alert_branch_reports_missing_files
    test_verify_diff_missing_fix_now_file_exits_2
    test_verify_diff_malformed_json_exits_2
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# Declared, not excused: `test_detect_stack_cli_exits_2_on_non_directory`
# spawns a real `python3` subprocess to exercise the probe CLI's real
# exit-code contract at the process boundary, and the `verify-diff` tests
# spawn real `git` (via `_init_repo`) because the property under test is
# real git-diff comparison against actual tracked/committed files -- no
# mock stands in for either. `_init_repo` is invoked per-test, not hoisted
# to module scope, since each verify-diff scenario needs its own distinct
# commit/diff state. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "bug_sweep_probes",
        _BIN_DIR / "bug-sweep-probes.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# detect-stack
# ---------------------------------------------------------------------------


def test_detect_stack_finds_languages_test_dirs_and_config(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n")
    (tmp_path / "src" / "widget.tsx").write_text("export default 1;\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "tsconfig.json").write_text("{}\n")
    (tmp_path / "jest.config.js").write_text("module.exports = {};\n")

    result = _mod._detect_stack(tmp_path)

    assert "src/main.py" in result["language_files"]
    assert "src/widget.tsx" in result["language_files"]
    assert result["test_dirs"] == ["tests/"]
    assert "pyproject.toml" in result["config_files"]
    assert "tsconfig.json" in result["config_files"]
    assert "jest.config.js" in result["config_files"]


def test_detect_stack_head_limit_caps_at_20(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"file_{i:02d}.py").write_text("pass\n")

    result = _mod._detect_stack(tmp_path)

    assert len(result["language_files"]) == 20


def test_detect_stack_empty_dir_returns_empty_lists(tmp_path: Path) -> None:
    result = _mod._detect_stack(tmp_path)

    assert result["language_files"] == []
    assert result["test_dirs"] == []
    assert result["config_files"] == []


def test_detect_stack_cli_exits_2_on_non_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "nope.txt"
    not_a_dir.write_text("x\n")

    proc = subprocess.run(
        ["python3", str(_BIN_DIR / "bug-sweep-probes.py"), "detect-stack", str(not_a_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# verify-diff
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "a.py").write_text("print(1)\n")
    (repo / "b.py").write_text("print(2)\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.py", "b.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


def test_verify_diff_no_missing_when_all_expected_changed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("print(1)\nmodified\n")

    fix_now = tmp_path / "phase2-fix-now.json"
    fix_now.write_text('[{"id": "F-A-01", "file": "a.py", "line": 1}]')

    proc = subprocess.run(
        [
            "python3",
            str(_BIN_DIR / "bug-sweep-probes.py"),
            "verify-diff",
            "--fix-now",
            str(fix_now),
            "--repo-root",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "missing" in proc.stdout
    assert '"missing": []' in proc.stdout


def test_verify_diff_alert_branch_reports_missing_files(tmp_path: Path) -> None:
    """Mirrors the SKILL.md ALERT branch: a fix-now file with no actual diff."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("print(1)\nmodified\n")
    # b.py claimed fix-now but left byte-identical (no diff) — the
    # false-positive / no-op cohort the ALERT branch exists to surface.

    fix_now = tmp_path / "phase2-fix-now.json"
    fix_now.write_text(
        '[{"id": "F-A-01", "file": "a.py"}, {"id": "F-A-02", "file": "b.py"}]'
    )

    proc = subprocess.run(
        [
            "python3",
            str(_BIN_DIR / "bug-sweep-probes.py"),
            "verify-diff",
            "--fix-now",
            str(fix_now),
            "--repo-root",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "ALERT: fix-now files with no diff" in proc.stderr
    assert "b.py" in proc.stderr
    assert '"missing": [\n    "b.py"\n  ]' in proc.stdout or '"b.py"' in proc.stdout


def test_verify_diff_missing_fix_now_file_exits_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    proc = subprocess.run(
        [
            "python3",
            str(_BIN_DIR / "bug-sweep-probes.py"),
            "verify-diff",
            "--fix-now",
            str(tmp_path / "does-not-exist.json"),
            "--repo-root",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2


def test_verify_diff_malformed_json_exits_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fix_now = tmp_path / "phase2-fix-now.json"
    fix_now.write_text("not json")

    proc = subprocess.run(
        [
            "python3",
            str(_BIN_DIR / "bug-sweep-probes.py"),
            "verify-diff",
            "--fix-now",
            str(fix_now),
            "--repo-root",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
