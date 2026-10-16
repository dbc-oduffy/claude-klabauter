"""close_without_script.close_landed_run pays a landed run's close legs with no emitted script.

Fixture: the scripted terminal_commit driver lands a plan-run, then the benchmark module returns it
to the landed-but-unclosed state (plan `executing`, row `coded`, record written before this path).
The emitted script is deleted, as a purged `scratch/warp` leaves it. The review launch is stubbed.
"""

from __future__ import annotations

import copy
import json
import subprocess

import pytest
import yaml

from coordinator_core.benchmarks import close_without_script_full_path as bench
from coordinator_core.benchmarks import terminal_commit_full_path as scripted
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import close_without_script as cws
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

MET = scripted.INLINE_REVIEW["criterion"]
PLAN = scripted.PLAN_REL
RUN = bench.RUN_ID
STEM = scripted.INLINE_REVIEW["integration_stem"]


class Env:
    def __init__(self, repo, launches):
        self.repo, self.launches = repo, launches

    @property
    def review_file(self):
        return self.repo / ".coordinator-local" / "subagent-share" / bench.SESSION_ID / f"{STEM}.md"

    @property
    def record_file(self):
        return self.repo / run_close.close_job_path(RUN)

    def record(self) -> dict:
        return json.loads(self.record_file.read_text(encoding="utf-8"))

    def set_record(self, **changes):
        record = self.record()
        for key, value in changes.items():
            if value is run_close.UNCHANGED:
                record.pop(key, None)
            else:
                record[key] = value
        run_close._write_atomic(self.record_file, record)

    def new_record(self, criterion=MET, falsifier=None):
        """The record a post-plan scripted run writes: verdict stored, mark read."""
        self.set_record(criterion=copy.deepcopy(criterion), falsifier_broken=falsifier)

    def edit_review(self, mutate):
        text = self.review_file.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        data = yaml.safe_load(split.fm_text)
        mutate(data)
        self.review_file.write_text(
            "---\n" + yaml.safe_dump(data, sort_keys=False) + "---" + split.body_with_leading_newline,
            encoding="utf-8",
        )

    def review_criterion(self, **fields):
        def mutate(data):
            data["criterion"].update(fields)

        self.edit_review(mutate)

    def call(self, **extra) -> dict:
        return cws.close_landed_run(
            self.repo, self.repo / ".git",
            {"plan_path": PLAN, "run_id": RUN, "session_id": bench.SESSION_ID, **extra},
        )

    def task_output(self, falsifier=(), criterion=None, name="to") -> str:
        return bench.write_task_output(self.repo, list(falsifier), criterion, name)

    def git(self, *args) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True, **no_console_creationflags()
        ).stdout

    def plan(self) -> dict:
        return yaml.safe_load(split_frontmatter((self.repo / PLAN).read_text(encoding="utf-8")).fm_text)

    def bytes(self) -> tuple:
        return (self.repo / PLAN).read_bytes(), self.record_file.read_bytes()

    def entries(self) -> list:
        root = self.repo / "archive" / "completed"
        return sorted(root.rglob("*.md")) if root.exists() else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    outbox = parent / "lessons-outbox-seed"
    outbox.mkdir()
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", bench.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], []))
    repo, _ = bench.build_fixture(parent)
    launches: list = []
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: launches.append(ctx) or True)
    (repo / bench.SCRIPT).unlink(missing_ok=True)
    return Env(repo, launches)


def _status(env, *paths) -> str:
    return env.git("status", "--porcelain", "--", *paths)


def test_stored_verdict_closes_a_run_with_no_script(env):
    env.new_record()

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"
    assert out["criterion_status"] == "met" and out["verdict_source"] == "close-job"
    assert out["committed"] is True
    plan = env.plan()
    assert plan["status"] == "implemented" and "review_stamp" in plan
    entry = out["close"]["completion_entry"]
    assert _status(env, PLAN, entry) == ""
    record = env.record()
    assert record["review_receipt"] == PLAN and record["legs"]["review"]["status"] == "queued"
    assert record["settled"]["completion-entry"]["status"] == "ran"
    assert len(env.launches) == 1
    close = out["close"]
    assert not {"review", "plan-reconciliation", "completion-entry"} & set(close["outstanding"])


def test_second_identical_call_is_answered_from_disk(env):
    env.new_record()
    env.call()
    before, head = env.bytes(), env.git("rev-parse", "HEAD")

    out = env.call()

    assert out["nothing_to_commit"] is True and out["committed"] is False
    assert env.bytes() == before and env.git("rev-parse", "HEAD") == head
    assert len(env.launches) == 1


def test_already_implemented_run_with_a_failed_review_leg_relaunches_it(env):
    env.new_record()
    env.call()
    env.set_record(legs={**env.record()["legs"], "review": {"status": "failed", "detail": "x"}})
    plan_before, head = (env.repo / PLAN).read_bytes(), env.git("rev-parse", "HEAD")

    out = env.call()

    assert out["committed"] is False and out["nothing_to_commit"] is True
    assert (env.repo / PLAN).read_bytes() == plan_before and env.git("rev-parse", "HEAD") == head
    assert len(env.launches) == 2
    assert env.record()["legs"]["review"]["status"] == "queued"


def test_read_verdict_takes_the_first_source_with_a_judged_status(tmp_path):
    review = {"plan_id": "p", "criterion": {"status": "met"}}
    record = {"criterion": {"status": "not_met"}, "falsifier_broken": ["t"]}

    first = cws.read_verdict(tmp_path, record, review, None, "run", "plan.md", "p")
    assert first.source == "close-job" and first.criterion["status"] == "not_met"
    assert first.falsifier_known and first.falsifier == ["t"]
    second = cws.read_verdict(tmp_path, {"criterion": {"status": "bogus"}}, review, None, "run", "plan.md", "p")
    assert second.source == "review-record" and not second.falsifier_known
    with pytest.raises(cws.CloseRefusal) as refused:
        cws.read_verdict(tmp_path, {}, {**review, "plan_id": "q"}, None, "run", "plan.md", "p")
    assert refused.value.reason == "no-judge-verdict"


def test_foreign_review_record_is_not_a_verdict_source(env):
    env.set_record(criterion=None, falsifier_broken=None)
    env.edit_review(lambda data: data.update(plan_id="pln-other-000000"))

    out = env.call()

    assert out["refused"] == "no-judge-verdict"


def test_review_record_is_the_source_when_the_record_holds_none(env):
    env.set_record(falsifier_broken=None)

    out = env.call()

    assert out["verdict_source"] == "review-record"
    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"


def test_task_output_is_the_last_source_and_a_stored_source_wins_over_it(env):
    env.set_record(falsifier_broken=None)
    unmet = {**MET, "status": "not_met", "observation": "no", "reason": "r"}

    out = env.call(task_output_path=env.task_output(criterion=unmet))

    assert out["verdict_source"] == "review-record" and out["criterion_status"] == "met"


def test_record_criterion_wins_whatever_the_others_hold(env):
    env.new_record(criterion={**MET, "observation": "stored"})
    env.review_criterion(status="not_met")

    out = env.call()

    assert out["verdict_source"] == "close-job" and out["criterion_status"] == "met"


@pytest.mark.parametrize("review_has_criterion", [False, True])
def test_no_judge_verdict_refuses_and_writes_nothing(env, review_has_criterion):
    env.set_record(criterion=None)
    if review_has_criterion:
        env.edit_review(lambda data: data.pop("criterion"))
    else:
        env.review_file.unlink()
    before = env.bytes()

    out = env.call()

    assert out["refused"] == "no-judge-verdict" and out["committed"] is False
    assert env.bytes() == before and env.launches == []


def test_task_output_verdict_lands_in_the_record_before_the_legs(env):
    env.set_record(criterion=None)
    env.review_file.unlink()

    out = env.call(task_output_path=env.task_output())

    assert out["verdict_source"] == "task-output" and out["review_stamp"] == "refused"
    assert env.record()["criterion"]["status"] == "met"
    assert env.record()["falsifier_broken"] is None
    assert env.plan()["status"] == "executing"
    again = env.call()
    assert again["verdict_source"] == "close-job"


def _close_job_output(env, run_id=None) -> str:
    """A ``close_job``-kind task output: ``{run_id, close}`` params and no ``script_path``."""
    path = env.task_output()
    data = json.loads(open(path, encoding="utf-8").read())
    params = data["next_action"]["params"]
    del params["script_path"]
    if run_id is not None:
        params["run_id"] = run_id
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def test_close_job_task_output_binds_by_run_id(env):
    env.set_record(criterion=copy.deepcopy(MET), falsifier_broken=run_close.UNCHANGED)

    out = env.call(task_output_path=_close_job_output(env, RUN))

    assert out["plan_status"] == "implemented" and "refused" not in out
    assert env.record()["falsifier_broken"] is None


def test_legacy_record_with_a_recorded_met_rejudge_reads_as_never_marked(env):
    env.set_record(criterion=copy.deepcopy(MET), falsifier_broken=run_close.UNCHANGED)
    _rejudge_record(env, {"status": "met", "observation": "now true"})

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"
    assert env.plan()["status"] == "implemented"
    assert env.record()["falsifier_broken"] is None


@pytest.mark.parametrize("shape", ["other-run", "unreadable", "no-script", "close-job-other-run"])
def test_task_output_not_bound_to_the_run_refuses(env, shape):
    env.new_record()
    if shape == "unreadable":
        path = str(env.repo.parent / "absent.json")
    elif shape == "close-job-other-run":
        path = _close_job_output(env, "another-run")
    else:
        path = env.task_output()
        data = json.loads(open(path, encoding="utf-8").read())
        if shape == "other-run":
            data["next_action"]["params"]["script_path"] = "another-run.workflow.mjs"
        else:
            del data["next_action"]["params"]["script_path"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    before = env.bytes()

    out = env.call(task_output_path=path)

    assert out["refused"] == "task-output-run-mismatch"
    assert env.bytes() == before and env.launches == []


def test_plan_mismatch_absent_record_and_unlanded_sha_refuse(env):
    env.new_record()
    other = cws.close_landed_run(
        env.repo, env.repo / ".git", {"plan_path": "docs/other.md", "run_id": RUN}
    )
    assert other["refused"] == "close-job-plan-mismatch"
    absent = cws.close_landed_run(env.repo, env.repo / ".git", {"plan_path": PLAN, "run_id": "no-such-run"})
    assert absent["refused"] == "no-close-job" and run_close.close_job_path("no-such-run") in absent["error"]
    env.set_record(terminal_sha="f" * 40)
    unlanded = env.call()
    assert unlanded["refused"] == "terminal-commit-not-landed"


@pytest.mark.parametrize("run_id", ["../x", "a/b", "a\\b", ".."])
def test_run_id_that_is_not_a_file_stem_reads_no_file(env, monkeypatch, run_id):
    def boom(*a, **k):
        raise AssertionError("read a file")

    monkeypatch.setattr(run_close, "_load", boom)

    out = cws.close_landed_run(env.repo, env.repo / ".git", {"plan_path": PLAN, "run_id": run_id})

    assert out["refused"] == "no-close-job"


def test_not_met_verdict_stamps_not_met_and_pays_nothing(env):
    env.new_record(criterion={**MET, "status": "not_met", "reason": "r"})
    env.review_criterion(status="not_met")

    out = env.call()

    assert out["review_stamp"] == "minted" and out["criterion_status"] == "not_met"
    assert env.plan()["status"] == "executing"
    assert "--rejudge" in out["plan_status_reason"]
    assert env.entries() == []
    assert out["close"]["legs"]["completion-entry"]["status"] == "failed"


def test_review_record_that_disagrees_with_the_verdict_refuses_the_stamp(env, monkeypatch):
    env.new_record(criterion={**MET, "status": "not_met", "reason": "r"})
    written: list = []
    real = terminal_commit._register_writeback

    def spy(root, plan, review, sha):
        written.append(review["criterion"]["status"])
        return real(root, plan, review, sha)

    monkeypatch.setattr(terminal_commit, "_register_writeback", spy)

    out = env.call()

    assert out["review_stamp"] == "refused"
    assert "not_met" in out["review_stamp_refusal"] and "met" in out["review_stamp_refusal"]
    assert out["criterion_status"] == "not_met" and env.plan()["status"] == "executing"
    assert env.entries() == [] and written == []


def _rejudge_record(env, criterion):
    path = env.repo / "state" / "delivery-verdicts" / "x" / "rejudge.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n" + yaml.safe_dump(
            {"kind": "criterion-rejudge", "plan_id": scripted.PLAN_ID, "recorded_at": "2099-01-01T00:00:00Z",
             "criterion": criterion}, sort_keys=False,
        ) + "---\n",
        encoding="utf-8",
    )
    return "state/delivery-verdicts/x/rejudge.md"


@pytest.mark.parametrize("with_summary", [True, False])
def test_recorded_rejudge_replaces_an_unmet_verdict(env, with_summary):
    env.new_record(criterion={**MET, "status": "not_met", "reason": "r"})
    env.review_criterion(status="not_met")
    again = {"status": "met", "observation": "now true"}
    if with_summary:
        again["completion_summary"] = "Rejudged met."
    rec = _rejudge_record(env, again)

    out = env.call()

    assert out["criterion_status"] == "met" and out["rejudged_by"] == rec
    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"
    leg = out["close"]["legs"]["completion-entry"]["status"]
    assert leg == "ran"
    if not with_summary:
        assert any("now true" in p.read_text(encoding="utf-8") for p in env.entries())


def test_unmet_verdict_with_no_review_record_stands(env):
    env.new_record(criterion={**MET, "status": "not_met", "reason": "r"})
    env.review_file.unlink()
    _rejudge_record(env, {"status": "met", "observation": "now true"})

    out = env.call()

    assert out["criterion_status"] == "not_met" and "rejudged_by" not in out


def test_broken_falsifier_mints_but_withholds_the_flip(env):
    env.new_record(falsifier=[])

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "not-stamped"
    assert "BROKEN" in out["plan_status_reason"] and env.plan()["status"] == "executing"


def test_register_failure_withholds_the_flip_and_the_demotion_sticks(env, monkeypatch):
    env.new_record()
    seen: list = []
    real_mint, real_gate = terminal_commit._mint_review_stamp, terminal_commit._register_gate

    def gate(root, plan, inline):
        inline["criterion"]["status"] = "not_met"
        return "register row R1: not wired"

    def spy(root, plan, sha, path, review, *a, **k):
        seen.append(copy.deepcopy(review["criterion"]))
        return real_mint(root, plan, sha, path, review, *a, **k)

    monkeypatch.setattr(terminal_commit, "_register_gate", gate)
    monkeypatch.setattr(terminal_commit, "_mint_review_stamp", spy)

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "not-stamped"
    assert out["plan_status_reason"] == "register row R1: not wired"
    assert seen[0]["status"] == "met"
    assert env.record()["criterion"]["status"] == "not_met"
    assert env.plan()["status"] == "executing"
    monkeypatch.setattr(terminal_commit, "_register_gate", real_gate)
    monkeypatch.setattr(terminal_commit, "_mint_review_stamp", real_mint)
    second = env.call()
    assert second["review_stamp"] == "refused" and env.plan()["status"] == "executing"


def test_unknown_falsifier_mark_withholds_the_flip_until_a_task_output_supplies_it(env):
    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "not-stamped"
    assert "task_output_path" in out["plan_status_reason"] and env.plan()["status"] == "executing"
    assert "falsifier_broken" not in env.record()
    second = env.call(task_output_path=env.task_output())
    assert second["plan_status"] == "implemented"
    assert env.record()["falsifier_broken"] is None


def test_completion_entry_rides_the_one_commit(env):
    env.new_record()
    assert env.record()["settled"]["completion-entry"]["status"] == "failed"

    out = env.call()

    entry = out["close"]["completion_entry"]
    shown = env.git("show", "--name-only", "--format=", out["sha"]).split()
    process_half = env.git("show", "--name-only", "--format=", "HEAD").split()
    assert PLAN in shown and entry in shown + process_half
    assert env.record()["settled"]["completion-entry"]["status"] == "ran"


def test_commit_that_does_not_land_removes_the_entry_and_leaves_the_leg(env, monkeypatch):
    env.new_record()
    monkeypatch.setattr(
        terminal_commit, "reentrant_dispatch", lambda op, params, repo_root=None: {"committed": False, "error": "boom"}
    )

    out = env.call()

    assert "coded_stamp_error" in out and out["committed"] is False
    assert env.entries() == []
    assert env.record()["settled"]["completion-entry"]["status"] == "failed"
    assert out["close"]["legs"]["completion-entry"]["status"] == "failed"


def test_foreign_plan_review_record_refuses_the_stamp(env):
    env.new_record()
    env.edit_review(lambda data: data.update(plan_id="pln-other-000000"))
    before = (env.repo / PLAN).read_bytes()

    out = env.call()

    assert out["review_stamp"] == "refused" and (env.repo / PLAN).read_bytes() == before


def test_delivery_supersession_refuses_the_stamp(env):
    env.new_record()
    run_rel = env.review_file.relative_to(env.repo).as_posix()
    path = env.repo / "state" / "delivery-verdicts" / "x" / "reverify.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n" + yaml.safe_dump(
            {"kind": "delivery-verdict", "supersedes": run_rel, "recorded_at": "2099-01-01T00:00:00Z",
             "head_sha": "a" * 40, "delivery": {"verdict": "PASS", "product_files": 1}}, sort_keys=False,
        ) + "---\n",
        encoding="utf-8",
    )
    before = (env.repo / PLAN).read_bytes()

    out = env.call()

    assert out["review_stamp"] == "refused" and "mint" in out["review_stamp_refusal"]
    assert (env.repo / PLAN).read_bytes() == before


def test_untracked_ignored_plan_is_flipped_on_disk_and_not_committed(env):
    env.new_record()
    env.set_record(settled={**env.record()["settled"], "completion-entry": {"status": "ran", "detail": "entry x"}})
    env.git("rm", "--cached", "-q", PLAN)
    (env.repo / ".gitignore").write_text(PLAN + "\n", encoding="utf-8")
    env.git("add", ".gitignore")
    env.git("commit", "-q", "-m", "ignore the plan")

    out = env.call()

    assert out["committed"] is False and out["nothing_to_commit"] is True
    assert env.plan()["status"] == "implemented" and "review_stamp" in env.plan()


def test_empty_batons_and_memo_settle_not_applicable(env):
    env.new_record()
    env.set_record(claimed_batons=[], source_memo=None)
    legs = env.call()["close"]

    assert legs["legs"]["baton-drain"]["status"] == "not-applicable"
    assert legs["legs"]["memo-lifecycle"]["status"] == "not-applicable"
    assert "baton-drain" not in legs["outstanding"] and "memo-lifecycle" not in legs["outstanding"]
    assert env.launches[0].claimed_batons == ()


def test_record_batons_reach_the_review_launcher(env):
    env.new_record()

    env.call()

    assert env.launches[0].claimed_batons == (scripted.BATON_REL,)


def test_launch_follows_the_last_amend(env, monkeypatch):
    env.new_record()
    order: list = []
    real = run_close.amend_close_job
    monkeypatch.setattr(run_close, "amend_close_job", lambda *a, **k: order.append("amend") or real(*a, **k))
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: order.append("launch") or True)

    env.call()

    assert order == ["amend", "launch"]


def test_launch_that_does_not_start_records_the_leg_failed(env, monkeypatch):
    env.new_record()
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: False)

    out = env.call()

    assert env.record()["legs"]["review"]["status"] == "failed"
    assert out["close"]["legs"]["review"]["status"] == "failed"


def test_waived_not_met_and_attested_indeterminate_are_not_mismatches(env, monkeypatch):
    from coordinator_core.ops import operator_attestation, plan_narrow_criterion

    env.new_record(criterion={**MET, "status": "not_met", "reason": "r"})
    env.review_criterion(status="not_met")
    env.edit_review(lambda data: data["tests"].update(status="pass", run=1, failed=0))
    real_waiver = plan_narrow_criterion.criterion_waiver
    monkeypatch.setattr(plan_narrow_criterion, "criterion_waiver", lambda *a, **k: "narrowed")

    waived = env.call()

    assert waived["review_stamp"] == "minted" and waived["criterion_status"] == "not_met"
    assert waived["stamp_criterion_status"] == "not_run" and env.entries() == []

    env.git("checkout", "--", PLAN)
    env.new_record(criterion={**MET, "status": "indeterminate", "reason": "r"})
    env.review_criterion(status="indeterminate")
    monkeypatch.setattr(plan_narrow_criterion, "criterion_waiver", real_waiver)
    monkeypatch.setattr(operator_attestation, "attested", lambda *a, **k: ("attested", None))

    attested = env.call()

    assert attested["review_stamp"] == "minted" and attested["criterion_status"] == "indeterminate"
    assert attested["stamp_criterion_status"] == "met"


def _stamped_not_implemented(env) -> dict:
    """Close once, then leave the plan `executing` with its minted stamp uncommitted, as a re-stamped plan sits."""
    env.new_record()
    base = env.git("rev-parse", "HEAD").strip()
    env.call()
    text = (env.repo / PLAN).read_text(encoding="utf-8")
    assert "status: implemented" in text
    env.git("reset", "--hard", base)
    (env.repo / PLAN).write_text(text.replace("status: implemented", "status: executing", 1), encoding="utf-8")
    env.set_record(legs={**env.record()["legs"], "review": {"status": "failed", "detail": "x"}})
    return env.plan()["review_stamp"]


def _forbid_mint(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a kept stamp is never re-minted")

    monkeypatch.setattr(terminal_commit, "_mint_review_stamp", refuse)


def test_valid_stamp_is_kept_and_committed_whatever_the_review_record_says_of_build_test(env, monkeypatch):
    stamp = _stamped_not_implemented(env)
    env.edit_review(lambda data: data.update(build_test={"verdict": "fail"}))
    _forbid_mint(monkeypatch)
    before = int(env.git("rev-list", "--count", "HEAD"))

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"
    assert int(env.git("rev-list", "--count", "HEAD")) == before + 1
    assert PLAN in env.git("show", "--name-only", "--format=", out["sha"]).split()
    committed = yaml.safe_load(split_frontmatter(env.git("show", f"HEAD:{PLAN}")).fm_text)
    assert committed["status"] == "implemented" and committed["review_stamp"] == stamp
    assert _status(env, PLAN) == ""
    assert not {"completion-entry", "review", "plan-reconciliation"} & set(out["close"]["outstanding"])


def test_kept_stamp_runs_the_close_legs_as_a_fresh_mint_does(env, monkeypatch):
    env.new_record()
    fresh = env.call()
    env.set_record(legs={**env.record()["legs"], "review": {"status": "failed", "detail": "x"}})
    text = (env.repo / PLAN).read_text(encoding="utf-8")
    (env.repo / PLAN).write_text(text.replace("status: implemented", "status: executing", 1), encoding="utf-8")
    _forbid_mint(monkeypatch)

    kept = env.call()

    assert kept["close"]["outstanding"] == fresh["close"]["outstanding"]
    assert kept["close"]["legs"]["completion-entry"]["status"] == fresh["close"]["legs"]["completion-entry"]["status"]
    assert len(env.launches) == 2


@pytest.mark.parametrize("field", ["terminal_commit_sha", "terminal_tree_sha"])
def test_stamp_for_another_commit_or_tree_is_not_kept_and_the_refused_mint_lands_nothing(env, field):
    _stamped_not_implemented(env)
    text = (env.repo / PLAN).read_text(encoding="utf-8")
    current = env.plan()["review_stamp"][field]
    (env.repo / PLAN).write_text(text.replace(current, "b" * 40, 1), encoding="utf-8")
    env.edit_review(lambda data: data.update(plan_id="pln-other-000000"))
    head, count, status = env.git("rev-parse", "HEAD"), env.git("rev-list", "--count", "HEAD"), env.git("status", "--porcelain")

    out = env.call()

    assert out["review_stamp"] == "refused" and out["committed"] is False
    assert env.git("rev-parse", "HEAD") == head and env.git("rev-list", "--count", "HEAD") == count
    assert env.git("status", "--porcelain") == status


def test_refused_mint_lands_no_commit_and_leaves_no_entry_or_register_row(env):
    env.new_record()
    env.edit_review(lambda data: data.update(plan_id="pln-other-000000"))
    head, status = env.git("rev-parse", "HEAD"), env.git("status", "--porcelain")

    out = env.call()

    assert out["review_stamp"] == "refused" and out["committed"] is False
    assert env.git("rev-parse", "HEAD") == head and env.git("status", "--porcelain") == status
    assert env.entries() == [] and "register_sizing" not in out


def test_kept_stamp_with_a_broken_falsifier_withholds_the_flip(env, monkeypatch):
    env.new_record(falsifier=[])
    env.call()
    _forbid_mint(monkeypatch)

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "not-stamped"
    assert "BROKEN" in out["plan_status_reason"] and env.plan()["status"] == "executing"


def test_kept_stamp_with_an_unknown_falsifier_mark_withholds_the_flip(env, monkeypatch):
    env.call()
    _forbid_mint(monkeypatch)

    out = env.call()

    assert out["review_stamp"] == "minted" and out["plan_status"] == "not-stamped"
    assert "task_output_path" in out["plan_status_reason"] and env.plan()["status"] == "executing"


def test_kept_stamp_with_a_register_failure_withholds_the_flip(env, monkeypatch):
    env.new_record()
    env.call()
    text = (env.repo / PLAN).read_text(encoding="utf-8")
    (env.repo / PLAN).write_text(text.replace("status: implemented", "status: executing", 1), encoding="utf-8")
    _forbid_mint(monkeypatch)
    monkeypatch.setattr(terminal_commit, "_register_gate", lambda root, plan, inline: "register row R1: not wired")

    out = env.call()

    assert out["plan_status"] == "not-stamped" and out["plan_status_reason"] == "register row R1: not wired"
    assert env.plan()["status"] == "executing"
