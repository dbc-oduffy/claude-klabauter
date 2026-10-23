"""reverify_red_findings: a red review re-verify on the landing set is a refusal; anything else is not."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_integrity as li

LANDED = "pkg/mod.py"
TEST = "pkg/tests/test_mod.py"


def _tree(root: Path, test_body: str = "from pkg import mod\n") -> None:
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / LANDED).write_text("X = 1\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / TEST).write_text(test_body, encoding="utf-8")


def _params(**reverify) -> dict:
    return {"inline_review": {li.REVERIFY_KEY: reverify}}


def test_fail_before_and_after_on_importing_test_gives_one_finding(tmp_path):
    _tree(tmp_path)
    p = _params(status_before="fail", status_after="fail", failing=[f"{TEST}::test_a", f"{TEST}::test_b"])
    found = li.reverify_red_findings(tmp_path, p, [LANDED])
    assert len(found) == 1
    assert found[0].path == TEST and found[0].reason == li.REVIEW_REVERIFY_FAILED
    assert f"{TEST}::test_a" in found[0].detail and li.RETEST_LAUNCHER in found[0].detail


def test_pass_after_revert_gives_none(tmp_path):
    _tree(tmp_path)
    p = _params(status_before="fail", status_after="pass", failing=[f"{TEST}::t"],
                reverted=[{"tests": [f"{TEST}::t"]}])
    assert li.reverify_red_findings(tmp_path, p, [LANDED]) == []


def test_null_after_with_pass_before_gives_none(tmp_path):
    _tree(tmp_path)
    p = _params(status_before="pass", status_after=None, failing=[f"{TEST}::t"])
    assert li.reverify_red_findings(tmp_path, p, [LANDED]) == []


def test_null_after_with_fail_before_still_refuses(tmp_path):
    _tree(tmp_path)
    p = _params(status_before="fail", status_after=None, failing=[f"{TEST}::t"])
    assert len(li.reverify_red_findings(tmp_path, p, [LANDED])) == 1


def test_absent_or_null_reverify_gives_none(tmp_path):
    _tree(tmp_path)
    assert li.reverify_red_findings(tmp_path, {}, [LANDED]) == []
    assert li.reverify_red_findings(tmp_path, {"inline_review": {li.REVERIFY_KEY: None}}, [LANDED]) == []


def test_fail_on_test_importing_nothing_landed_gives_none(tmp_path):
    _tree(tmp_path, "import os\n")
    p = _params(status_after="fail", failing=[f"{TEST}::t"])
    assert li.reverify_red_findings(tmp_path, p, [LANDED]) == []


def test_windows_separator_node_ids_and_landing_paths_match(tmp_path):
    _tree(tmp_path)
    p = _params(status_after="fail", failing=["pkg\\tests\\test_mod.py::t"])
    assert len(li.reverify_red_findings(tmp_path, p, ["pkg\\mod.py"])) == 1


def test_reverted_tests_and_landed_test_file_itself_are_implicated(tmp_path):
    _tree(tmp_path, "import os\n")
    p = _params(status_after="fail", reverted=[{"tests": [f"{TEST}::t"]}])
    assert len(li.reverify_red_findings(tmp_path, p, [TEST])) == 1
