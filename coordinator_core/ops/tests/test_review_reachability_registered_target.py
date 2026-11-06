"""review.reachability `registered-target` entries: a registered module or hook command that does not exist."""

from __future__ import annotations

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.review_reachability import _ALL_ENTRY_KINDS, _handler
from coordinator_core.ops.tests._reachability_fixture import _git, _write

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

MAP = "coordinator_core/ops/_registry_map.py"


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _write(root, "README.md", "# repo\n")
    _write(root, "coordinator_core/ops/present.py", "X = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()


def _targets(root, base, paths):
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": paths})
    assert res["error"] is None
    return {e["ref"]: e["verdict"] for e in res["entries"] if e["kind"] == "registered-target"}, res


def test_kind_is_a_default_entry_kind():
    assert "registered-target" in _ALL_ENTRY_KINDS


def test_registry_map_entry_for_missing_module_is_missing_target(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, MAP, '_MAP = {\n    "a.b": "coordinator_core.ops.ghost",\n}\n')
    got, res = _targets(root, base, [MAP])
    assert got == {f"{MAP}:coordinator_core.ops.ghost": "missing-target"}
    assert "1 registered target missing-target" in res["summary"]


def test_registry_map_entry_for_existing_module_is_reachable(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, MAP, '_MAP = {\n    "a.b": "coordinator_core.ops.present",\n}\n')
    got, res = _targets(root, base, [MAP])
    assert got == {f"{MAP}:coordinator_core.ops.present": "reachable"}
    assert "missing-target" not in res["summary"]


def test_package_target_resolves_via_init(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, "coordinator_core/pkg/__init__.py", "")
    _write(root, MAP, '_MAP = {"a": "coordinator_core.pkg"}\n')
    got, _ = _targets(root, base, [MAP])
    assert got == {f"{MAP}:coordinator_core.pkg": "reachable"}


def test_hooks_json_command_module_missing(tmp_path):
    root, base = _repo(tmp_path)
    _write(
        root,
        "plugin/hooks.json",
        '{\n  "hooks": [\n    {\n      "command": "python -m coordinator_core.hooks.ghost",\n'
        '      "comment": "coordinator_core.ops.other"\n    }\n  ]\n}\n',
    )
    got, _ = _targets(root, base, ["plugin/hooks.json"])
    assert got == {"plugin/hooks.json:coordinator_core.hooks.ghost": "missing-target"}


def test_test_file_is_not_a_surface(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, "tests/test_x_registry_map.py", '_MAP = {"a": "coordinator_core.ops.ghost"}\n')
    got, _ = _targets(root, base, ["tests/test_x_registry_map.py"])
    assert got == {}


def test_default_call_over_committed_diff_flags_missing_target(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, MAP, '_MAP = {"a": "coordinator_core.ops.ghost"}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "wire ghost")
    res = _handler({"repo_root": str(root), "base_sha": base})
    got = {e["ref"]: e["verdict"] for e in res["entries"] if e["kind"] == "registered-target"}
    assert got == {f"{MAP}:coordinator_core.ops.ghost": "missing-target"}


def test_kind_filter_excludes_entries(tmp_path):
    root, base = _repo(tmp_path)
    _write(root, MAP, '_MAP = {"a": "coordinator_core.ops.ghost"}\n')
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": [MAP], "entry_kinds": ["op"]})
    assert not [e for e in res["entries"] if e["kind"] == "registered-target"]
