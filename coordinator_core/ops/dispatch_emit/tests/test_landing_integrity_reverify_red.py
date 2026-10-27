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


def test_all_pre_existing_failing_on_importing_test_gives_none(tmp_path):
    _tree(tmp_path)
    ids = [f"{TEST}::test_a", f"{TEST}::test_b"]
    p = _params(status_before="fail", status_after="fail", failing=ids, pre_existing=ids)
    assert li.reverify_red_findings(tmp_path, p, [LANDED]) == []


def test_mixed_pre_existing_and_caused_names_only_the_caused_id(tmp_path):
    _tree(tmp_path)
    p = _params(status_after="fail", failing=[f"{TEST}::old", f"{TEST}::new"], pre_existing=[f"{TEST}::old"])
    found = li.reverify_red_findings(tmp_path, p, [LANDED])
    assert len(found) == 1
    assert f"{TEST}::new" in found[0].detail and f"{TEST}::old" not in found[0].detail


def test_foreign_only_red_over_a_landing_path_gives_none(tmp_path):
    _tree(tmp_path)
    ids = [f"{TEST}::a", f"{TEST}::b"]
    p = _params(status_after="fail", failing=ids, **{li.FOREIGN_FIELD: ids})
    assert li.reverify_red_findings(tmp_path, p, [LANDED]) == []


def test_mixed_foreign_and_owned_names_only_the_owned_id(tmp_path):
    _tree(tmp_path)
    p = _params(status_after="fail", failing=[f"{TEST}::theirs", f"{TEST}::mine"],
                **{li.FOREIGN_FIELD: [f"{TEST}::theirs"]})
    found = li.reverify_red_findings(tmp_path, p, [LANDED])
    assert len(found) == 1
    assert f"{TEST}::mine" in found[0].detail and f"{TEST}::theirs" not in found[0].detail


def test_record_without_foreign_key_still_refuses(tmp_path):
    _tree(tmp_path)
    p = _params(status_after="fail", failing=[f"{TEST}::a"])
    assert len(li.reverify_red_findings(tmp_path, p, [LANDED])) == 1


def test_fail_with_empty_failing_still_trips_the_naming_trap(tmp_path):
    _tree(tmp_path)
    found = li.reverify_red_findings(tmp_path, _params(status_after="fail", failing=[], pre_existing=[]), [LANDED])
    assert len(found) == 1 and "naming no failing test" in found[0].detail


def test_pre_existing_failing_does_not_hide_reverted_tests(tmp_path):
    _tree(tmp_path)
    p = _params(status_after="fail", failing=[f"{TEST}::old"], pre_existing=[f"{TEST}::old"],
                reverted=[{"tests": [f"{TEST}::r"]}])
    assert len(li.reverify_red_findings(tmp_path, p, [LANDED])) == 1


def _subdir_suite(root: Path, body: str) -> None:
    (root / "lab" / "tests").mkdir(parents=True)
    (root / "lab" / "ledger_tr.py").write_text("X = 1\n", encoding="utf-8")
    (root / "lab" / "other.py").write_text("X = 1\n", encoding="utf-8")
    (root / "lab" / "tests" / "test_ledger.py").write_text(body, encoding="utf-8")


def test_suite_run_from_a_subdirectory_with_a_sys_path_import_is_implicated(tmp_path):
    body = "def test_t():\n    import ledger_tr\n"
    _subdir_suite(tmp_path, body)
    p = _params(status_before="fail", status_after="fail", failing=["tests/test_ledger.py::test_t"])
    found = li.reverify_red_findings(tmp_path, p, ["lab/ledger_tr.py"])
    assert [f.reason for f in found] == [li.REVIEW_REVERIFY_FAILED]


def test_subdirectory_test_file_that_is_itself_landed_is_implicated(tmp_path):
    _subdir_suite(tmp_path, "import other\n")
    p = _params(status_after="fail", failing=["tests/test_ledger.py::test_t"])
    assert len(li.reverify_red_findings(tmp_path, p, ["lab/tests/test_ledger.py"])) == 1


def test_subdirectory_test_file_missing_from_disk_but_landed_is_implicated(tmp_path):
    p = _params(status_after="fail", failing=["tests/test_gone.py::test_t"])
    assert len(li.reverify_red_findings(tmp_path, p, ["tests/test_gone.py"])) == 1


def test_suite_run_from_a_subdirectory_importing_nothing_landed_gives_none(tmp_path):
    _subdir_suite(tmp_path, "import other\n")
    p = _params(status_after="fail", failing=["tests/test_ledger.py::test_t"])
    assert li.reverify_red_findings(tmp_path, p, ["lab/ledger_tr.py"]) == []


def test_reverted_tests_and_landed_test_file_itself_are_implicated(tmp_path):
    _tree(tmp_path, "import os\n")
    p = _params(status_after="fail", reverted=[{"tests": [f"{TEST}::t"]}])
    assert len(li.reverify_red_findings(tmp_path, p, [TEST])) == 1
