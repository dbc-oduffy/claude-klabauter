"""Pins which runner argv heads the Tier-T classifier admits as scoped when given test files."""
from __future__ import annotations

import json
import sys

import pytest

from coordinator_core.bash_guards.check_test_suite_invocation import (
    RUNNER_FOOTPRINT_SCOPED,
    classify_command,
    classify_command_precision,
    classify_runner_footprint,
)

_PY = sys.executable

# Runner table the targeted-run selector imports: a head absent here is not admitted.
ADMITTED_HEADS = {
    "pytest": [_PY, "-m", "pytest"],
    "vitest-pnpm": ["pnpm", "exec", "vitest", "run"],
    "vitest-npx": ["npx", "vitest", "run"],
    "vitest-yarn-exec": ["yarn", "exec", "vitest", "run"],
    "jest-pnpm": ["pnpm", "exec", "jest"],
}

# Heads the classifier does not call scoped (footprint `unproven`); left out of ADMITTED_HEADS.
NOT_ADMITTED_HEADS = {
    "vitest-yarn": ["yarn", "vitest", "run"],
    "node-test": ["node", "--test"],
}

_EXT = {"pytest": "py", "node-test": "mjs"}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    (tmp_path / "tests").mkdir()
    (tmp_path / "coordinator.local.md").write_text("fast_test_cmd: pytest -q\n")
    return tmp_path


def _files(repo, name, n):
    ext = _EXT.get(name, "ts")
    out = []
    for i in range(n):
        p = repo / "tests" / f"test_{i}.{ext}" if ext == "py" else repo / "tests" / f"a{i}.test.{ext}"
        p.write_text("")
        out.append(f"tests/{p.name}")
    return out


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("name", sorted(ADMITTED_HEADS))
def test_head_with_files_is_scoped(repo, name, n):
    cmd = " ".join(ADMITTED_HEADS[name] + _files(repo, name, n))
    assert classify_runner_footprint(cmd, cwd=str(repo)) == RUNNER_FOOTPRINT_SCOPED
    assert classify_command_precision(cmd, cwd=str(repo)) == []
    assert classify_command(cmd, cwd=str(repo)) == []


@pytest.mark.parametrize("cmd", ["pnpm test", "npm test", "pytest", "pytest -q"])
def test_suite_shapes_are_not_scoped(repo, cmd):
    assert classify_runner_footprint(cmd, cwd=str(repo)) != RUNNER_FOOTPRINT_SCOPED


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("name", sorted(NOT_ADMITTED_HEADS))
def test_unadmitted_head_is_not_scoped(repo, name, n):
    cmd = " ".join(NOT_ADMITTED_HEADS[name] + _files(repo, name, n))
    assert classify_runner_footprint(cmd, cwd=str(repo)) != RUNNER_FOOTPRINT_SCOPED


def test_declared_fast_test_cmd_bare_is_denied_but_files_appended_is_scoped(repo):
    (repo / "tests" / "test_0.py").write_text("")
    assert classify_runner_footprint("pytest -q", cwd=str(repo)) != RUNNER_FOOTPRINT_SCOPED
    assert classify_command("pytest -q", cwd=str(repo))
    cmd = "pytest -q tests/test_0.py"
    assert classify_runner_footprint(cmd, cwd=str(repo)) == RUNNER_FOOTPRINT_SCOPED
    assert classify_command(cmd, cwd=str(repo)) == []
