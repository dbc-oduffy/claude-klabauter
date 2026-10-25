"""dispatch.land_run answers a repeat call for a landed run `already_landed`, refreshes the close-job
record from the call's criterion and re-queues the record's unfinished legs; it never re-lands product
and names what was actually missing when it refuses."""

from __future__ import annotations

import ast
import copy
import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import land_run, run_close
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

KEY = "run"
_FRESH = {"status": "met", "observation": "a.py exists at HEAD, re-judged", "sidecar": "j2.md"}


class Env:
    def __init__(self, repo: Path, script: str, sha: str, launches: list):
        self.repo, self.script, self.sha, self.launches = repo, script, sha, launches

    @property
    def record_file(self) -> Path:
        return self.repo / run_close.close_job_path(KEY)

    def record(self) -> dict:
        return json.loads(self.record_file.read_text(encoding="utf-8"))

    def set_record(self, **changes) -> None:
        record = self.record()
        record.update(changes)
        run_close._write_atomic(self.record_file, record)

    def head(self) -> str:
        return land_run_head(self.repo)

    def digest(self, *, kind="close_job", criterion=None, run_id=KEY) -> str:
        result = {"next_action": {"kind": kind, "op": None, "params": {"run_id": run_id, "close": {"legs": {}}}}}
        if criterion is not None:
            result["inline_review"] = {"criterion": criterion}
        path = self.repo.parent / f"{self.repo.name}-digest.json"
        path.write_text(json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": result}), encoding="utf-8")
        return str(path)

    def reland(self, **params) -> dict:
        return land_run._handler(
            {"script_path": self.script, "session_id": driver.SESSION_ID, **params}, repo_root=self.repo / ".git"
        )


def land_run_head(repo: Path) -> str:
    from coordinator_core.git.git_state import head_sha

    return head_sha(repo, fresh=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A run whose product landed once: HEAD carries it and the close-job record holds its queued legs."""
    repo, script = driver.build_fixture(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    launches: list = []
    monkeypatch.setattr(land_run, "_launch_close_job", lambda root, key, batons, memo: launches.append(key) or True)
    inline_review = copy.deepcopy(driver.INLINE_REVIEW)
    sidecar = tmp_path / "review-sidecar.md"
    sidecar.write_text("verdict: PASS\n", encoding="utf-8")
    inline_review["wave_reviews"] = [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": str(sidecar)}]
    first = land_run._handler(
        {
            "script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
            "inline_review": inline_review,
        },
        repo_root=repo / ".git",
    )
    assert first["committed"] is True, first
    launches.clear()
    return Env(repo, script, first["sha"], launches)


def test_a_close_job_digest_answers_already_landed_and_requeues_the_unfinished_legs(env):
    head = env.head()

    out = env.reland(task_output_path=env.digest())

    assert out.get("refused") is None, out
    assert out["committed"] is True and out[ALREADY_LANDED_KEY] is True and out["sha"] == env.sha
    assert env.head() == head
    assert env.launches == [KEY]
    legs = env.record()["legs"]
    assert legs and all(entry["status"] == "queued" for entry in legs.values())


def test_a_script_call_with_no_run_outcome_reads_the_same_record(env):
    out = env.reland()

    assert out[ALREADY_LANDED_KEY] is True and out["sha"] == env.sha
    assert env.launches == [KEY]


def test_the_digest_criterion_refreshes_the_record_and_is_read_back(env):
    before = env.record()["criterion"]
    assert before != _FRESH

    out = env.reland(task_output_path=env.digest(criterion=_FRESH))

    assert env.record()["criterion"] == _FRESH
    assert out["criterion_status"] == "met"
    assert out["review_stamp"] == "deferred" and out["plan_status"] == "deferred"


def test_the_criterion_status_is_the_records_never_computed_in_the_op(env, monkeypatch):
    from coordinator_core.ops.dispatch_emit import register_gate

    monkeypatch.setattr(
        register_gate, "register_gate", lambda *a, **k: pytest.fail("the op judged the criterion itself")
    )
    demoted = {**_FRESH, "status": "not_met", "reason": "register row R1: no observed entry point"}

    out = env.reland(task_output_path=env.digest(criterion=demoted))

    assert out["criterion_status"] == "not_met"
    assert env.record()["criterion"]["status"] == "not_met"


def test_a_call_with_nothing_owed_and_no_new_criterion_launches_nothing(env):
    for leg in env.record()["legs"]:
        run_close.record_leg_outcome(env.repo, KEY, leg, "ran", "done")

    out = env.reland(task_output_path=env.digest(criterion=env.record()["criterion"]))

    assert out[ALREADY_LANDED_KEY] is True
    assert env.launches == []


def test_a_digest_run_id_without_the_workflow_suffix_matches_the_record_key(env):
    shutil.move(env.record_file, env.repo / run_close.close_job_path(f"{KEY}.workflow"))

    out = env.reland(task_output_path=env.digest(run_id=KEY))

    assert out.get("refused") is None and out[ALREADY_LANDED_KEY] is True, out
    assert env.launches == [f"{KEY}.workflow"]


def test_a_record_whose_terminal_sha_is_not_on_head_names_that(env):
    env.set_record(terminal_sha="b" * 40)

    out = env.reland(task_output_path=env.digest())

    assert out["refused"] == "terminal-commit-not-landed" and out["committed"] is False
    assert env.launches == []


def test_a_plan_the_record_does_not_belong_to_is_refused(env):
    out = env.reland(plan_path="docs/other.md")

    assert out["refused"] == "close-job-plan-mismatch"
    assert env.launches == []


def test_a_run_with_no_close_job_record_names_what_is_missing(env):
    out = env.reland(plan_path=driver.PLAN_REL, script_path="nobody.mjs")

    assert out["refused"] == "no-close-job"
    assert env.launches == []


def test_a_digest_with_no_outcome_and_no_close_job_names_what_was_missing(env):
    path = env.digest(kind="wait")

    out = env.reland(task_output_path=path)

    assert out["refused"] == "missing-run-outcome"
    assert "no params.task_output_path was given" not in out["error"]
    assert Path(path).name in out["error"]


@pytest.mark.parametrize("module", ["land_run.py", "cli.py", "retest_targeted.py"])
def test_the_module_imports_neither_close_without_script_nor_terminal_commit(module):
    tree = ast.parse((Path(land_run.__file__).parent / module).read_text(encoding="utf-8"))
    named: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            named.update((node.module or "").split("."))
            named.update(alias.name for alias in node.names)
    assert not named & {"close_without_script", "terminal_commit"}, sorted(named & {"close_without_script", "terminal_commit"})
