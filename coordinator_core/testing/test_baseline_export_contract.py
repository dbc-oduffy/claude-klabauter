"""Contract pins for baseline_export: argv grammar, root naming, cwd counterpart, result line."""
import json
from pathlib import Path

import pytest

from coordinator_core.testing import baseline_export as be

SHA40 = "0123456789abcdef0123456789abcdef01234567"


def test_exit_codes():
    assert (be.EXIT_OK, be.EXIT_USAGE, be.EXIT_EXPORT_FAILED, be.EXIT_REMOVAL_FAILED) == (0, 2, 3, 4)


def test_parse_ok():
    inv = be.parse_argv(["--sha", "abc1", "--link", "node_modules", "--link", "a/b", "--", "pytest", "-q"])
    assert inv.sha == "abc1"
    assert inv.links == ("node_modules", "a/b")
    assert inv.runner == ("pytest", "-q")


def test_runner_may_contain_dashes():
    inv = be.parse_argv(["--sha", "abc1", "--", "x", "--", "--sha"])
    assert inv.runner == ("x", "--", "--sha")


@pytest.mark.parametrize(
    "argv",
    [
        ["--", "pytest"],
        ["--sha", "abc1", "pytest"],
        ["--sha", "abc1", "--"],
        ["--sha", "abc1", "--link", "/abs", "--", "pytest"],
        ["--sha", "abc1", "--link", "../x", "--", "pytest"],
        ["--sha", "abc1", "--link", "a/../x", "--", "pytest"],
        ["--sha", "abc1", "--link", "", "--", "pytest"],
        ["--sha", "a", "--sha", "b", "--", "pytest"],
        ["--sha", "abc1", "--bogus", "--", "pytest"],
    ],
)
def test_usage_errors(argv):
    with pytest.raises(be.UsageError):
        be.parse_argv(argv)
    assert be.main(argv) == be.EXIT_USAGE


def test_export_root(tmp_path):
    assert be.export_root(tmp_path, SHA40, 77) == tmp_path / "scratch" / "test-base-01234567-77"


def test_counterpart_cwd(tmp_path):
    root = tmp_path / "scratch" / "test-base-01234567-1"
    assert be.counterpart_cwd(tmp_path, tmp_path, root) == root
    assert be.counterpart_cwd(tmp_path, tmp_path / "pkg" / "a", root) == root / "pkg" / "a"
    assert be.counterpart_cwd(tmp_path, Path("/elsewhere"), root) == root


def test_result_line_keys_and_order():
    line = be.render_result_line(SHA40, "ok", None, 0, True)
    obj = json.loads(line)
    assert list(obj) == ["baseline_export"]
    assert list(obj["baseline_export"]) == ["sha", "export", "why", "runner_exit", "removed"]
    assert obj["baseline_export"] == {"sha": SHA40, "export": "ok", "why": None, "runner_exit": 0, "removed": True}
    assert "\n" not in line


def test_result_line_unresolved_sha_is_as_supplied():
    obj = json.loads(be.render_result_line("zzz", "failed", "bad sha", None, True))
    assert obj["baseline_export"]["sha"] == "zzz"
    assert obj["baseline_export"]["runner_exit"] is None
