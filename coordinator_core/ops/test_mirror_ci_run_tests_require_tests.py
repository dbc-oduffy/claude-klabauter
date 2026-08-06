"""Tests for the mirror-native CI test runner's `--require-tests` contract.

Covers AC6 of `state/handoffs/2026-08-04-oss-payload-drop-working-content.md`
and DR-262 consequence 1
(`docs/decisions/DR-262-the-oss-payload-ships-tests-and-the-scrub-is-fixed-at-root.md`):
the mirror's CI harness must not report success over an empty or absent test
tree once `--require-tests` is wired on in
`dist/mirror-native/claude-klabauter/.github/workflows/ci.yml`. This is the
same green-over-nothing failure class as the zero-count guard fixed at
`3ae2e949d` and the identity checker's ~4%-coverage zero -- asserted here,
per the AC's own instruction, rather than reasoned about.

The runner (`dist/mirror-native/claude-klabauter/.github/scripts/run-tests.py`)
derives its own repo root from `__file__` (three parents up from
`.github/scripts/`), so it cannot be imported and driven in-process against a
synthetic tree -- it is exercised here as the real subprocess CI invokes,
copied byte-for-byte into a throwaway repo-shaped `tmp_path` layout so the
assertion is against the exact bytes that ship, not a paraphrase of them.

No existing home tests the mirror-native `.github/scripts/` harness --
`coordinator_core/ops/test_percolate_ci_smoke_check.py` and
`test_check_posix_exec_assumptions.py` are the nearest precedent (mirror/
publish-adjacent script coverage homed under `coordinator_core/ops/`), so
this module joins them there rather than living un-collected under `dist/`
(outside `testpaths`) or shipping inside the published `.github/scripts/`
tree itself.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

RUNNER_SRC = (
    Path(__file__).resolve().parents[2]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "run-tests.py"
)

MINIMAL_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["coordinator_core"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
markers = []
"""

PASSING_TEST_FILE = """\
def test_trivially_passes():
    assert True
"""


def _make_fixture_repo(tmp_path: Path) -> Path:
    """A repo-shaped tmp_path: <root>/.github/scripts/run-tests.py + pyproject.toml."""
    scripts_dir = tmp_path / ".github" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copyfile(RUNNER_SRC, scripts_dir / "run-tests.py")
    (tmp_path / "pyproject.toml").write_text(MINIMAL_PYPROJECT, encoding="utf-8")
    return tmp_path


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(repo_root: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo_root / ".github" / "scripts" / "run-tests.py"), *extra_args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=NO_WINDOW,
    )


class TestRequireTestsOverEmptyOrAbsentTree:
    """AC6: `--require-tests` must turn an empty/absent suite into a failure."""

    def test_absent_test_tree_is_a_failure(self, tmp_path):
        repo_root = _make_fixture_repo(tmp_path)
        # testpaths = ["coordinator_core"] and that directory does not exist at all.
        result = _run(repo_root, "--require-tests")

        assert result.returncode != 0, (
            "run-tests.py --require-tests exited 0 over an ABSENT test tree "
            f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
        )

    def test_empty_test_tree_is_a_failure(self, tmp_path):
        repo_root = _make_fixture_repo(tmp_path)
        (repo_root / "coordinator_core").mkdir()
        # Directory exists but contains no test_*.py files.

        result = _run(repo_root, "--require-tests")

        assert result.returncode != 0, (
            "run-tests.py --require-tests exited 0 over an EMPTY test tree "
            f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
        )

    def test_without_the_flag_empty_tree_still_reports_success(self, tmp_path):
        """Documents the pre-flag SKIP behaviour this test must not regress into
        thinking is the failure -- the flag is what changes the contract, not
        the runner's default (mid-bootstrap) posture."""
        repo_root = _make_fixture_repo(tmp_path)

        result = _run(repo_root)

        assert result.returncode == 0
        assert "SKIPPED" in result.stdout


class TestPopulatedTreeStillPasses:
    """The `--require-tests` wiring must not break the normal, populated path."""

    def test_populated_tree_passes_with_require_tests(self, tmp_path):
        repo_root = _make_fixture_repo(tmp_path)
        suite_dir = repo_root / "coordinator_core"
        suite_dir.mkdir()
        (suite_dir / "test_smoke.py").write_text(PASSING_TEST_FILE, encoding="utf-8")

        result = _run(repo_root, "--require-tests")

        assert result.returncode == 0, (
            f"populated tree failed under --require-tests "
            f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
        )
        assert "test files discovered: 1" in result.stdout
