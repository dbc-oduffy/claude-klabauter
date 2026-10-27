"""dispatch.objective_write: write-once objective.json, tracked+executable script, candidate sha, no pin."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import objective_write as ow
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

AXES = [{"id": "a", "claim": "c", "baseline": 5, "threshold": 2}]
GOOD = (
    "#!{py}\nimport json\n"
    "print(json.dumps({{'axis': 'a', 'kind': 'measure', 'command': 'c', 'base': 5, 'now': 5, "
    "'threshold': 2, 'comparator': '<=', 'pass': False, 'evidence': 'e'}}))\n"
    "print(json.dumps({{'summary': True, 'pass': False, 'failing': ['a']}}))\n"
    "raise SystemExit(1)\n"
)
GARBAGE = "#!{py}\nprint('not json')\n"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def _script(repo, body=GOOD, name="fals.py", track=True, executable=True):
    path = repo / name
    path.write_text(body.format(py=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | (stat.S_IXUSR if executable else 0))
    if not executable:
        path.chmod(0o644)
    if track:
        _git(repo, "add", name)
    return path


def _call(repo, **over):
    params = {"run_id": "run-1", "verbatim": "the PM promise", "falsifier_script": "fals.py", "axes": AXES}
    params.update(over)
    return ow._handler(params, repo)


def test_first_write_records_candidate_sha_and_leaves_pin_null(repo):
    path = _script(repo)
    res = _call(repo)
    assert res["written"] is True
    obj = json.loads((repo / "state/warp/run-1/objective.json").read_text(encoding="utf-8"))
    assert obj["verbatim"] == "the PM promise"
    assert obj["falsifier_candidate_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert obj["falsifier_sha256"] is None and obj["pinned_at"] is None
    assert obj["axes"][0]["id"] == "a"


def test_base_json_written_when_script_runs_at_base(repo):
    _script(repo)
    res = _call(repo)
    assert res["base_written"] is True
    base = json.loads((repo / "state/warp/run-1/base.json").read_text(encoding="utf-8"))
    assert base["axes"][0]["axis"] == "a" and base["axes"][0]["pass"] is False


def test_no_base_json_when_script_output_is_unparseable_and_pin_still_null(repo):
    _script(repo, GARBAGE)
    res = _call(repo)
    assert res["written"] is True and res["base_written"] is False
    assert not (repo / "state/warp/run-1/base.json").exists()
    obj = json.loads((repo / "state/warp/run-1/objective.json").read_text(encoding="utf-8"))
    assert obj["falsifier_sha256"] is None and obj["pinned_at"] is None


def test_second_write_is_refused_and_file_unchanged(repo):
    _script(repo)
    _call(repo)
    before = (repo / "state/warp/run-1/objective.json").read_bytes()
    res = _call(repo, verbatim="a drifted promise")
    assert res["written"] is False and "already written" in res["error"]
    assert (repo / "state/warp/run-1/objective.json").read_bytes() == before


def test_untracked_script_refused(repo):
    _script(repo, track=False)
    res = _call(repo)
    assert res["written"] is False and "not git-tracked" in res["error"]
    assert not (repo / "state/warp/run-1/objective.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="no exec bit on Windows")
def test_non_executable_script_refused(repo):
    _script(repo, name="fals", executable=False)
    res = _call(repo, falsifier_script="fals")
    assert res["written"] is False and "not executable" in res["error"]


def test_python_script_needs_no_exec_bit(repo):
    _script(repo, executable=False)
    assert _call(repo)["written"] is True


def test_script_escaping_repo_refused(repo):
    res = _call(repo, falsifier_script="../outside.py")
    assert res["written"] is False


def test_missing_verbatim_and_no_sizing_refused(repo):
    _script(repo)
    res = _call(repo, verbatim="")
    assert res["written"] is False and "no verbatim" in res["error"]


def test_verbatim_falls_back_to_sizing_pm_verbatims_newest(repo, monkeypatch):
    _script(repo)
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    sizing = repo / "sz.yaml"
    sizing.write_text("pm_verbatims:\n  - text: newest words\n  - text: older words\n", encoding="utf-8")
    monkeypatch.setattr(rl, "read_run", lambda d: {"sizing_rel": "sz.yaml"})
    monkeypatch.setattr(rl, "_sizing_path", lambda r, rel: sizing)
    res = _call(repo, verbatim=None)
    assert res["written"] is True
    obj = json.loads((repo / "state/warp/run-1/objective.json").read_text(encoding="utf-8"))
    assert obj["verbatim"] == "newest words"


def test_verbatim_falls_back_to_register_first_row_source_text(repo, monkeypatch):
    _script(repo)
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    sizing = repo / "sz.yaml"
    sizing.write_text(
        "requirement_register:\n  rows:\n    - id: R1\n      source_text: first row words\n", encoding="utf-8"
    )
    monkeypatch.setattr(rl, "read_run", lambda d: {"sizing_rel": "sz.yaml"})
    monkeypatch.setattr(rl, "_sizing_path", lambda r, rel: sizing)
    res = _call(repo, verbatim=None)
    assert res["written"] is True
    obj = json.loads((repo / "state/warp/run-1/objective.json").read_text(encoding="utf-8"))
    assert obj["verbatim"] == "first row words"


def _sizing(tshirt="M", **ec):
    return {"estimate": {"tshirt": tshirt}, "exit_criterion": {"statement": "ship it", **ec}}


def _obj(repo):
    return json.loads((repo / "state/warp/run-1/objective.json").read_text(encoding="utf-8"))


def test_at_sizing_writes_judge_only_objective_without_falsifier(repo):
    res = ow.write_objective_at_sizing(repo, "run-1", _sizing())
    assert res["written"] is True
    obj = _obj(repo)
    assert obj["verbatim"] == "ship it" and obj["falsifier_script"] == "" and obj["axes"] == []
    assert obj["falsifier_sha256"] is None and obj["pinned_at"] is None
    assert obj["criteria"] == [
        {"id": "K1", "statement": "ship it", "kind": "judged", "command": "", "expect": ""}
    ]
    from coordinator_core.ops.dispatch_emit import objective_contract as oc

    assert oc.Objective.from_json(obj).verbatim == "ship it"


def test_at_sizing_tags_command_with_expect_deterministic_else_judged(repo):
    sizing = _sizing(criteria=[
        {"statement": "greps one", "command": "grep -c x f", "expect": "1"},
        {"statement": "command but no expected result", "command": "ls"},
        "plain words",
    ], falsifier="scripts/f.py")
    ow.write_objective_at_sizing(repo, "run-1", sizing)
    obj = _obj(repo)
    assert obj["falsifier_script"] == "scripts/f.py"
    assert [c["kind"] for c in obj["criteria"]] == ["deterministic", "judged", "judged"]
    assert obj["criteria"][0]["command"] == "grep -c x f" and obj["criteria"][0]["expect"] == "1"


@pytest.mark.parametrize("tshirt", ["XS", "S", "", "bogus"])
def test_at_sizing_noop_below_m(repo, tshirt):
    res = ow.write_objective_at_sizing(repo, "run-1", _sizing(tshirt))
    assert res["written"] is False
    assert not (repo / "state/warp/run-1/objective.json").exists()


@pytest.mark.parametrize("tshirt", ["M", "L", "XL", "XXL"])
def test_at_sizing_writes_at_m_and_above(repo, tshirt):
    assert ow.write_objective_at_sizing(repo, "run-1", _sizing(tshirt))["written"] is True


def test_at_sizing_noop_when_file_exists(repo):
    ow.write_objective_at_sizing(repo, "run-1", _sizing())
    before = (repo / "state/warp/run-1/objective.json").read_bytes()
    res = ow.write_objective_at_sizing(repo, "run-1", _sizing(statement="drifted"))
    assert res["written"] is False
    assert (repo / "state/warp/run-1/objective.json").read_bytes() == before


def test_axes_must_be_unique_and_nonempty(repo):
    _script(repo)
    assert _call(repo, axes=[])["written"] is False
    assert _call(repo, axes=[{"id": "a"}, {"id": "a"}])["written"] is False
