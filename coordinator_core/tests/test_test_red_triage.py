"""Tests for coordinator_core.ops.test_red_triage: grouping, first-line capture, rotation."""
from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops import test_red_triage as tr

MACHINE = "box-a"

OUTPUT = """\
=================================== FAILURES ===================================
____________________________ test_alpha ____________________________

    def test_alpha():
>       assert open("pkg/mod.py")
E       AssertionError: pkg/mod.py is missing the hook
E       assert False

tests/test_a.py:3: AssertionError
____________________________ TestK.test_beta ____________________________
E       AssertionError: pkg\\win.py has no entry
E       second line
____________________________ test_gamma ____________________________
E       RuntimeError: nothing about a repo file here
=========================== short test summary info ============================
FAILED tests/test_a.py::test_alpha - AssertionError: pkg/mod.py is missing the hook
FAILED tests/test_a.py::TestK::test_beta - AssertionError: pkg\\win.py has no entry
FAILED tests/test_a.py::test_gamma - RuntimeError: nothing about a repo file here
FAILED tests/test_a.py::test_nosection - ValueError: see pkg/mod.py
"""

FAILING = [
    "tests/test_a.py::test_alpha",
    "tests/test_a.py::TestK::test_beta",
    "tests/test_a.py::test_gamma",
    "tests/test_a.py::test_nosection",
]


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "win.py").write_text("y = 1\n", encoding="utf-8")
    return tmp_path


def _build(repo: Path) -> dict:
    return tr.build_triage(OUTPUT, FAILING, repo, ran_at="2026-10-17T10:00:00Z", tier="fast")


def _by_file(doc: dict) -> dict[str, dict[str, str]]:
    return {g["file"]: {t["id"]: t["assertion"] for t in g["tests"]} for g in doc["groups"]}


def test_groups_by_named_repo_file_with_either_separator(tmp_path):
    groups = _by_file(_build(_repo(tmp_path)))
    assert set(groups["pkg/mod.py"]) == {
        "tests/test_a.py::test_alpha",
        "tests/test_a.py::test_nosection",
    }
    assert set(groups["pkg/win.py"]) == {"tests/test_a.py::TestK::test_beta"}


def test_unnamed_failure_goes_to_ungrouped_bucket_last(tmp_path):
    doc = _build(_repo(tmp_path))
    assert doc["groups"][-1]["file"] == tr.UNGROUPED
    assert [t["id"] for t in doc["groups"][-1]["tests"]] == ["tests/test_a.py::test_gamma"]
    assert doc["failing_count"] == len(FAILING)


def test_first_assertion_line_is_verbatim(tmp_path):
    groups = _by_file(_build(_repo(tmp_path)))
    assert groups["pkg/mod.py"]["tests/test_a.py::test_alpha"] == (
        "AssertionError: pkg/mod.py is missing the hook"
    )
    assert groups["pkg/win.py"]["tests/test_a.py::TestK::test_beta"] == (
        "AssertionError: pkg\\win.py has no entry"
    )


def test_summary_message_used_when_no_section(tmp_path):
    groups = _by_file(_build(_repo(tmp_path)))
    assert groups["pkg/mod.py"]["tests/test_a.py::test_nosection"] == "ValueError: see pkg/mod.py"


def test_nonexistent_path_is_not_a_group(tmp_path):
    out = "____ test_z ____\nE       AssertionError: ghost/none.py broke\n"
    doc = tr.build_triage(out, ["t.py::test_z"], _repo(tmp_path), ran_at="2026-10-17T10:00:00Z", tier="fast")
    assert [g["file"] for g in doc["groups"]] == [tr.UNGROUPED]


def test_identical_input_gives_identical_output(tmp_path):
    repo = _repo(tmp_path)
    a = tr.render_triage(_build(repo))
    b = tr.render_triage(_build(repo))
    assert a == b
    assert tr.render_triage(
        tr.build_triage(OUTPUT, list(reversed(FAILING)), repo, ran_at="2026-10-17T10:00:00Z", tier="fast")
    ) == a


def test_write_places_file_under_state_test_red_with_no_absolute_path(tmp_path):
    repo = _repo(tmp_path)
    p = tr.write_triage(
        repo_root=repo, output=OUTPUT, failing=FAILING, tier="fast",
        machine=MACHINE, ran_at="2026-10-17T10:00:00Z",
    )
    assert p.parent == repo / "state" / "test-red"
    assert p.name.startswith(f"{MACHINE}.triage.")
    text = p.read_text(encoding="utf-8")
    assert str(repo).replace("\\", "/") not in text.replace("\\\\", "/")
    assert yaml.safe_load(text)["failing_count"] == len(FAILING)


def test_three_writes_leave_latest_and_prior_only(tmp_path):
    repo = _repo(tmp_path)
    stamps = ["2026-10-17T10:00:00Z", "2026-10-17T11:00:00Z", "2026-10-17T12:00:00Z"]
    paths = []
    for i, s in enumerate(stamps):
        paths.append(tr.write_triage(
            repo_root=repo, output=OUTPUT, failing=FAILING[: i + 2], tier="fast",
            machine=MACHINE, ran_at=s,
        ))
    assert tr.list_triage_files(repo, MACHINE) == paths[1:]
    assert not paths[0].exists()
    assert yaml.safe_load(paths[1].read_text(encoding="utf-8"))["failing_count"] == 3
    assert yaml.safe_load(paths[2].read_text(encoding="utf-8"))["failing_count"] == 4


def test_rotation_ignores_other_machines_and_the_record(tmp_path):
    repo = _repo(tmp_path)
    d = repo / "state" / "test-red"
    d.mkdir(parents=True)
    record = d / f"{MACHINE}.yaml"
    other = d / "box-b.triage.20260101T000000Z.yaml"
    record.write_text("failing: []\n", encoding="utf-8")
    other.write_text("x: 1\n", encoding="utf-8")
    for s in ["2026-10-17T10:00:00Z", "2026-10-17T11:00:00Z", "2026-10-17T12:00:00Z"]:
        tr.write_triage(repo_root=repo, output=OUTPUT, failing=FAILING, tier="fast",
                        machine=MACHINE, ran_at=s)
    assert record.read_text(encoding="utf-8") == "failing: []\n"
    assert other.exists()
    assert len(tr.list_triage_files(repo, MACHINE)) == tr.KEEP == 2


def test_record_write_also_writes_triage_and_green_run_does_not(tmp_path):
    from coordinator_core.ops.test_red_record import write_test_red_record

    repo = _repo(tmp_path)
    # The record path resolves its home by walking up for .git; no git process is needed.
    (repo / ".git").mkdir()
    kw = dict(repo_root=repo, tier="fast", sha="abc", exit_code=1,
              outcome="test-failures", runner="pytest", machine=MACHINE)
    write_test_red_record(failing=FAILING, output=OUTPUT, ran_at="2026-10-17T10:00:00Z", **kw)
    assert len(tr.list_triage_files(repo, MACHINE)) == 1
    write_test_red_record(failing=[], output="", ran_at="2026-10-17T11:00:00Z", **kw)
    assert len(tr.list_triage_files(repo, MACHINE)) == 1
