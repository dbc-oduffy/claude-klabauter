"""fresh_targeted_verdict keeps a retest record fresh across HEAD moves that miss its footprint."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_retest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-fp"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _commit(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "VALUE = 1\n",
            "pkg/test_a.py": "from pkg import helper\n\n\ndef test_a():\n    assert helper.VALUE\n",
        },
        "init",
    )
    # test_a.py is dirty at retest time, so it is covered.
    (root / "pkg/test_a.py").write_text(
        "from pkg import helper\n\n\ndef test_a():\n    assert helper.VALUE == 1\n", encoding="utf-8"
    )
    return root


def _write_record(root: Path) -> str:
    head = _git(root, "rev-parse", "HEAD")
    path = landing_retest.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN_ID, "status": "pass", "head": head,
                "covered": landing_retest.landing_fingerprint(root, ["pkg/test_a.py"]),
            }
        ),
        encoding="utf-8",
    )
    return head


def test_halt_replay_peer_commits_outside_footprint_stay_fresh(repo):
    _write_record(repo)
    _commit(repo, {"state/warp/halt.md": "halted\n"}, "warp: record halt")
    _commit(repo, {"docs/delivery/x.md": "delivered\n"}, "docs: delivery")
    assert _git(repo, "rev-parse", "HEAD") != json.loads(
        landing_retest.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8")
    )["head"]
    record = landing_retest.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"


def test_commit_changing_a_footprint_file_voids_it(repo):
    _write_record(repo)
    _commit(repo, {"pkg/test_a.py": "def test_a():\n    pass\n"}, "peer edits covered test")
    (repo / "pkg/test_a.py").write_text(
        "from pkg import helper\n\n\ndef test_a():\n    assert helper.VALUE == 1\n", encoding="utf-8"
    )
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_commit_changing_an_imported_module_voids_it(repo):
    _write_record(repo)
    _commit(repo, {"pkg/helper.py": "VALUE = 2\n"}, "peer edits imported module")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_commit_changing_a_transitive_import_voids_it(repo):
    _commit(repo, {"pkg/helper.py": "from pkg import deep\nVALUE = deep.X\n", "pkg/deep.py": "X = 1\n"}, "chain")
    _write_record(repo)
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is not None
    _commit(repo, {"pkg/deep.py": "X = 2\n"}, "peer edits transitive import")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_commit_changing_a_parent_package_init_of_a_dotted_import_voids_it(repo):
    _commit(repo, {"pkg/sub/__init__.py": "", "pkg/sub/leaf.py": "Y = 1\n"}, "subpackage")
    (repo / "pkg/test_a.py").write_text("import pkg.sub.leaf\n\n\ndef test_a():\n    pass\n", encoding="utf-8")
    _write_record(repo)
    _commit(repo, {"pkg/sub/__init__.py": "Z = 1\n"}, "peer edits parent package init")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_unreadable_record_head_is_stale_not_fresh(repo):
    _write_record(repo)
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    path = landing_retest.fresh_verdict_path(repo, RUN_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["head"] = "0" * 40
    path.write_text(json.dumps(record), encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_dirty_covered_bytes_still_void_it(repo):
    _write_record(repo)
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    (repo / "pkg/test_a.py").write_text("changed\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_moved_head_check_spawns_no_process(repo, monkeypatch):
    _write_record(repo)
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real_run(*a, **k))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a) or real_popen(*a, **k))
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is not None
    # The one allowed spawn is head_sha; the footprint read itself is in-process.
    assert len(calls) <= 1


def test_empty_judged_set_across_a_head_move_is_not_fresh(repo):
    head = _git(repo, "rev-parse", "HEAD")
    path = landing_retest.fresh_verdict_path(repo, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": RUN_ID, "status": "pass", "head": head, "covered": {}}), encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is not None
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None
    assert landing_retest._footprint_unchanged_between(repo, head, _git(repo, "rev-parse", "HEAD"), []) is False


def test_workflow_suffixed_run_keeps_its_own_rewritten_run_stage_fresh(repo):
    run_id = "run-fp.workflow"
    stage = "state/warp/run-fp/run-stage.json"
    _commit(repo, {stage: '{"n": 1}\n'}, "stage")
    head = _git(repo, "rev-parse", "HEAD")
    path = landing_retest.fresh_verdict_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id, "status": "pass", "head": head,
                "covered": landing_retest.landing_fingerprint(repo, ["pkg/test_a.py", stage]),
            }
        ),
        encoding="utf-8",
    )
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is not None
    (repo / stage).write_text('{"n": 2}\n', encoding="utf-8")
    record = landing_retest.fresh_targeted_verdict(repo, run_id)
    assert record is not None and record["run_id"] == run_id
    (repo / "pkg/test_a.py").write_text("changed\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is None


def test_bare_run_keeps_its_own_rewritten_run_stage_fresh_across_a_head_move(repo):
    stage = f"state/warp/{RUN_ID}/run-stage.json"
    _commit(repo, {stage: '{"n": 1}\n'}, "stage")
    path = landing_retest.fresh_verdict_path(repo, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN_ID, "status": "pass", "head": _git(repo, "rev-parse", "HEAD"),
                "covered": landing_retest.landing_fingerprint(repo, ["pkg/test_a.py", stage]),
            }
        ),
        encoding="utf-8",
    )
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    (repo / stage).write_text('{"n": 2}\n', encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is not None


def _write_covering_record(repo, run_id, files):
    path = landing_retest.fresh_verdict_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id, "status": "pass", "head": _git(repo, "rev-parse", "HEAD"),
                "covered": landing_retest.landing_fingerprint(repo, ["pkg/test_a.py", *files]),
            }
        ),
        encoding="utf-8",
    )


def test_workflow_run_keeps_its_whole_own_record_dir_fresh(repo):
    run_id = "run-fp.workflow"
    files = [f"state/warp/run-fp/{n}" for n in ("halted.json", "notes.jsonl", "requirements.md")]
    _commit(repo, {f: "v1\n" for f in files}, "records")
    _write_covering_record(repo, run_id, files)
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is not None
    (repo / files[0]).write_text("v2\n", encoding="utf-8")
    (repo / files[1]).write_text("v2\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is not None


def test_sibling_run_record_dir_with_shared_prefix_still_voids_it(repo):
    run_id = "run-fp.workflow"
    sibling = "state/warp/run-fp-2/halted.json"
    _commit(repo, {sibling: "v1\n"}, "sibling record")
    _write_covering_record(repo, run_id, [sibling])
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is not None
    (repo / sibling).write_text("v2\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, run_id) is None


def test_truncated_import_closure_is_stale_not_fresh(repo, monkeypatch):
    _write_record(repo)
    _commit(repo, {"state/warp/n.md": "n\n"}, "unrelated")
    monkeypatch.setattr(landing_retest, "_CLOSURE_FILE_CAP", 1)
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_workflow_written_record_is_credited_when_read_with_the_bare_id(repo):
    _write_covering_record(repo, "run-fp.workflow", [])
    assert landing_retest.fresh_verdict_path(repo, "run-fp") == landing_retest.fresh_verdict_path(repo, "run-fp.workflow")
    record = landing_retest.fresh_targeted_verdict(repo, "run-fp")
    assert record is not None and record["status"] == "pass"


def test_bare_written_record_is_credited_when_read_with_the_workflow_id(repo):
    _write_covering_record(repo, "run-fp", [])
    record = landing_retest.fresh_targeted_verdict(repo, "run-fp.workflow")
    assert record is not None and record["status"] == "pass"


def test_a_different_runs_record_is_never_credited(repo):
    _write_covering_record(repo, "run-other.workflow", [])
    assert landing_retest.fresh_targeted_verdict(repo, "run-fp") is None
    assert landing_retest.fresh_targeted_verdict(repo, "run-fp.workflow") is None
    path = landing_retest.fresh_verdict_path(repo, "run-fp")
    path.write_text(path.parent.joinpath("run-other.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, "run-fp") is None
