"""Tests for bug_blitz.evidence: target resolution and the closed failing-test grammars."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bug_blitz import evidence
from coordinator_core.bug_blitz.contract import FailingTest, TargetSpec

FIX = Path(__file__).parent / "fixtures" / "evidence"


def test_resolve_backlog(tmp_path):
    assert evidence.resolve_target("backlog", tmp_path) == TargetSpec(kind="backlog")


def test_resolve_relative_dir_and_file(tmp_path):
    (tmp_path / "rs").mkdir()
    (tmp_path / "f.txt").write_text("x")
    assert evidence.resolve_target("rs", tmp_path).kind == "dir"
    assert evidence.resolve_target("f.txt", tmp_path).kind == "file"


def test_resolve_refuses_relative_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "out.txt").write_text("x")
    with pytest.raises(evidence.TargetRefused):
        evidence.resolve_target("../out.txt", repo)


def test_resolve_accepts_existing_absolute_sibling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sibling = tmp_path / "sib"
    sibling.mkdir()
    assert evidence.resolve_target(str(sibling), repo).path == str(sibling.resolve())


def test_resolve_refuses_missing_and_empty(tmp_path):
    with pytest.raises(evidence.TargetRefused):
        evidence.resolve_target(str(tmp_path / "nope"), tmp_path)
    with pytest.raises(evidence.TargetRefused):
        evidence.resolve_target("  ", tmp_path)


def test_backlog_parses_empty_without_raising():
    assert evidence.parse_targets(TargetSpec(kind="backlog")).tests == ()


def test_redset_dir_groups_by_file_and_accounts_every_line():
    spec = evidence.resolve_target(str(FIX / "redset"), FIX)
    res = evidence.parse_targets(spec)
    assert len(res.tests) == 5
    assert res.unparsed_count == 0
    grouped = evidence.group_by_file(res.tests)
    assert len(grouped) == 4
    assert len(grouped[("ddct", "test/on-box.test.ts")]) == 2
    assert FailingTest("scripts/lib/parity/player-parity.test.ts", "parity > matches the committed seed", "root") in res.tests


def test_pytest_log():
    res = evidence.parse_targets(TargetSpec(kind="file", path=str(FIX / "pytest.log")))
    assert [(t.file, t.name) for t in res.tests] == [
        ("pkg/tests/test_a.py", "test_one"),
        ("pkg/tests/test_a.py", "TestK::test_two"),
        ("pkg/tests/test_b.py", "test_three"),
    ]
    assert res.tests[0].message == "AssertionError: assert 1 == 2"
    assert res.unparsed_count == 4
    assert len(res.unparsed_sample) == 4


def test_vitest_log_with_and_without_package():
    res = evidence.parse_targets(TargetSpec(kind="file", path=str(FIX / "vitest.log")))
    assert [(t.package, t.file) for t in res.tests] == [
        ("ddct", "test/on-box.test.ts"),
        ("", "src/solo.spec.ts"),
    ]
    assert res.unparsed_count == 5


def test_ansi_is_stripped(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("\x1b[31mFAILED\x1b[0m a/test_x.py::test_y\nFAILED a/test_x.py::test_y\n")
    res = evidence.parse_targets(TargetSpec(kind="file", path=str(p)))
    assert res.tests == (FailingTest("a/test_x.py", "test_y"),)


def test_bare_node_id_list(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text("a/test_x.py::test_y\n\nnot a test\n")
    res = evidence.parse_targets(TargetSpec(kind="file", path=str(p)))
    assert res.tests == (FailingTest("a/test_x.py", "test_y"),)
    assert res.unparsed_count == 1
    assert res.unparsed_sample == ("not a test",)


def test_json_failing_list():
    res = evidence.parse_targets(TargetSpec(kind="file", path=str(FIX / "failing.json")))
    assert res.tests == (
        FailingTest("pkg/tests/test_a.py", "test_one"),
        FailingTest("src/x.test.ts", "x > y", "web", "boom"),
    )
    assert res.unparsed_count == 1
    assert res.unparsed_sample == ("42",)


def test_json_without_failing_raises(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"exit": 0}')
    with pytest.raises(evidence.NoFailingTests):
        evidence.parse_targets(TargetSpec(kind="file", path=str(p)))


def test_zero_parsed_raises_never_empty_run(tmp_path):
    p = tmp_path / "junk.txt"
    p.write_text("nothing here\n")
    with pytest.raises(evidence.NoFailingTests):
        evidence.parse_targets(TargetSpec(kind="file", path=str(p)))


def test_dir_falls_back_to_output_then_log_then_raises(tmp_path):
    with pytest.raises(evidence.NoFailingTests):
        evidence.parse_targets(TargetSpec(kind="dir", path=str(tmp_path)))
    (tmp_path / "z.log").write_text("FAILED a/test_l.py::t\n")
    assert evidence.parse_targets(TargetSpec(kind="dir", path=str(tmp_path))).tests[0].file == "a/test_l.py"
    (tmp_path / "x-output.txt").write_text("FAILED a/test_o.py::t\n")
    assert evidence.parse_targets(TargetSpec(kind="dir", path=str(tmp_path))).tests[0].file == "a/test_o.py"
