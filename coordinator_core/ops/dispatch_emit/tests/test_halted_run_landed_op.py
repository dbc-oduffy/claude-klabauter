"""dispatch.record_halted_run_landed: sha-verified landed close of a halted run, spawn-free."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PARENT = "ask-parent-20261015T000000-abcdef"
REAL_LAUNCH = op._launch


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _seed(root: Path, statuses: dict):
    runs = [_run("A"), _run("B", ["A"], c.STATUS_AFTER)]
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()), encoding="utf-8")
    for rid, entry in statuses.items():
        st = root / c.status_rel(rid)
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"parent_id": PARENT, **entry}), encoding="utf-8")


def _status(root, rid):
    return json.loads((root / c.status_rel(rid)).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    """Every test stubs the detached close-job launch; the list records each call."""
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


NO_CLOSE = {"close": {"status": "not-applicable", "detail": "no halt record"}, "blocking_rows": []}
JOB_NA_SOLO = {"close": {"status": "not-applicable", "detail": "halt record names no plan"}, "blocking_rows": []}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, "one")
    second = _commit(tmp_path, "two")
    return tmp_path, first, second


def _call(root, **params):
    return op._handler(params, root)


def test_halted_run_lands_and_releases_after_successor(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["written"] is True and out["sha"] == first
    assert out["siblings"]["released"] == [fire_command("state/sizings/B.yaml")]
    assert _status(root, "A")["status"] == "landed" and _status(root, "A")["sha"] == first


def test_second_call_same_sha_is_idempotent(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _call(root, run_id="A", sha=first)
    before = (root / c.status_rel("A")).read_bytes()
    out = _call(root, run_id="A", sha=first)
    assert out == {"ok": True, "run_id": "A", "sha": first, "written": False, **NO_CLOSE}
    assert (root / c.status_rel("A")).read_bytes() == before


@pytest.mark.parametrize("status", ["running", "after", "queued", "ready", "held"])
def test_non_halted_status_refuses(repo, status):
    root, first, _ = repo
    _seed(root, {"A": {"status": status}})
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "not halted" in out["error"]
    assert _status(root, "A")["status"] == status


def test_landed_at_other_sha_refuses(repo):
    root, first, second = repo
    _seed(root, {"A": {"status": "landed", "sha": second}})
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "already landed" in out["error"]


def test_absent_status_refuses(repo):
    root, first, _ = repo
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "never ran" in out["error"]


SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"


def _solo(root: Path, status: str = "halted", **extra) -> Path:
    d = root / "state" / "warp" / SOLO
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    marker.write_text(json.dumps({"status": status, "halted": "x", "sizing_rel": None, **extra}), encoding="utf-8")
    return marker


def test_solo_halted_run_records_landed_marker(repo):
    root, first, _ = repo
    marker = _solo(root)
    out = _call(root, run_id=SOLO, sha=first[:10])
    assert out == {
        "ok": True, "run_id": SOLO, "sha": first, "written": True, "shas": [first], "covers": {},
        "recorded": f"state/warp/{SOLO}/halted.json",
        "close": {"status": "not-applicable", "detail": "halt record names no plan"}, "blocking_rows": [],
    }
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["status"] == "landed" and data["sha"] == first and data["halted"] == "x"
    assert not (root / c.status_rel(SOLO)).exists()


def test_solo_second_call_is_idempotent(repo):
    root, first, _ = repo
    marker = _solo(root)
    _call(root, run_id=SOLO, sha=first)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha=first)
    assert out == {"ok": True, "run_id": SOLO, "sha": first, "written": False, **JOB_NA_SOLO}
    assert marker.read_bytes() == before


def test_solo_unreachable_sha_refuses_and_leaves_marker(repo):
    root, _, _ = repo
    marker = _solo(root)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha="b" * 40)
    assert not out["ok"] and "not a commit" in out["error"]
    assert marker.read_bytes() == before


def test_solo_landed_at_other_sha_refuses(repo):
    root, first, second = repo
    _solo(root, "landed", sha=second)
    out = _call(root, run_id=SOLO, sha=first)
    assert not out["ok"] and "already landed" in out["error"]


def test_solo_running_status_refuses(repo):
    root, first, _ = repo
    marker = _solo(root, "running")
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha=first)
    assert not out["ok"] and "halted or resumed" in out["error"]
    assert marker.read_bytes() == before


SUCCESSOR = "warp-2026-10-21-successor-20261021T000000-abcdef"


def _successor(root: Path, resumed_from: str = SOLO) -> None:
    d = root / "state" / "warp" / SUCCESSOR
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps({"resumed_from": resumed_from}), encoding="utf-8")


def test_solo_resumed_without_live_successor_records_landed(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    root, first, _ = repo
    marker = _solo(root, "resumed")
    _successor(root)
    monkeypatch.setattr(run_token, "is_live", lambda r, rid: False)
    out = _call(root, run_id=SOLO, sha=first[:8])
    assert out["ok"] and out["written"] is True and out["sha"] == first
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "landed"


def test_solo_resumed_with_live_successor_refuses_then_terminal_successor_does_not_hold(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    root, first, _ = repo
    marker = _solo(root, "resumed")
    _successor(root)
    monkeypatch.setattr(run_token, "is_live", lambda r, rid: True)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha=first)
    assert not out["ok"] and SUCCESSOR in out["error"] and "still live" in out["error"]
    assert marker.read_bytes() == before
    (root / "state" / "warp" / SUCCESSOR / "run-stage.json").write_text('{"stage": "halted"}', encoding="utf-8")
    assert _call(root, run_id=SOLO, shas=[first])["ok"]


def test_resumed_solo_with_withheld_still_refuses_unrelated_sha(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    root, first, _ = repo
    marker = _solo(root, "resumed")
    _withhold(marker, "e.txt")
    monkeypatch.setattr(run_token, "is_live", lambda r, rid: False)
    unrelated = _commit(root, "unrelated")
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, shas=[unrelated[:8]])
    assert not out["ok"] and "withheld e.txt" in out["error"]
    assert marker.read_bytes() == before


def test_sha_not_a_commit_refuses(repo):
    root, _, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha="b" * 40)
    assert not out["ok"] and "not a commit" in out["error"]
    assert _status(root, "A")["status"] == "halted"


def test_unmerged_side_branch_sha_refuses(repo):
    root, first, _ = repo
    _git(root, "checkout", "-q", "-b", "side", first)
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-")
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha=side)
    assert not out["ok"] and "ancestor" in out["error"]


@pytest.mark.parametrize("params", [
    {"run_id": "../x", "sha": "a" * 40},
    {"run_id": "A", "sha": "abc"},
    {"run_id": "A", "sha": "xyz1234"},
    {"run_id": "A", "sha": "A" * 40},
    {"run_id": "", "sha": "a" * 40},
])
def test_malformed_params_refuse(repo, params):
    assert not _call(repo[0], **params)["ok"]


def test_abbreviated_sha_records_full_sha(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    out = _call(root, run_id="A", sha=first[:10])
    assert out["ok"] and out["written"] is True and out["sha"] == first
    assert _status(root, "A")["sha"] == first


def test_abbreviated_sha_idempotent_after_landing(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _call(root, run_id="A", sha=first[:10])
    out = _call(root, run_id="A", sha=first[:10])
    assert out == {"ok": True, "run_id": "A", "sha": first, "written": False, **NO_CLOSE}


def test_ambiguous_abbreviation_refuses_by_name(repo, monkeypatch):
    from coordinator_core import git_ancestry

    root = repo[0]
    _seed(root, {"A": {"status": "halted"}})
    monkeypatch.setattr(
        git_ancestry, "resolve_commit_prefixes",
        lambda prefixes, cwd=None: ({}, f"sha prefix {next(iter(prefixes))} is ambiguous"),
    )
    out = _call(root, run_id="A", sha="abcd")
    assert not out["ok"] and "abcd" in out["error"] and "ambiguous" in out["error"]
    assert _status(root, "A")["status"] == "halted"


def test_unknown_abbreviation_refuses_by_name(repo):
    root, first, second = repo
    prefix = "0000" if not (first + second).count("0000") else "ffff"
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha=prefix)
    assert not out["ok"] and prefix in out["error"] and "no commit" in out["error"]
    assert _status(root, "A")["status"] == "halted"


PLAN = "docs/plans/p.md"
FULL_BASE = "c" * 40
UUID = "123e4567-e89b-12d3-a456-426614174000"


def _plan_marker(root: Path, rid: str, **extra) -> Path:
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    body = {
        "status": "halted", "halted": "ledger-check", "plan": PLAN, "blocking": ["R1"],
        "run_base_sha": FULL_BASE, "sizing_rel": None, **extra,
    }
    marker.write_text(json.dumps(body), encoding="utf-8")
    return marker


def _job(root, rid):
    path = root / ".coordinator-local" / "close-jobs" / f"{rid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_sibling_landing_flips_marker_queues_legs_and_launches_once(repo, launches):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    marker = _plan_marker(root, "A")
    out = _call(root, run_id="A", sha=first, session_id=UUID)
    assert out["ok"] and out["written"] is True
    assert out["siblings"]["released"] == [fire_command("state/sizings/B.yaml")]
    assert out["recorded"] == "state/warp/A/halted.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "landed"
    assert out["close"] == {
        "job": ".coordinator-local/close-jobs/A.json", "launched": True,
        "queued": [leg for leg in op.CLOSE_LEGS if leg != "review-stamp"],
    }
    assert out["blocking_rows"] == ["R1"]
    assert launches == ["A"]
    job = _job(root, "A")
    assert job["plan_rel"] == PLAN and job["terminal_sha"] == first and job["run_base_sha"] == FULL_BASE
    assert set(job["legs"]) == set(op.CLOSE_LEGS) - {"review-stamp"}


def test_landed_same_sha_without_job_flips_marker_writes_and_launches(repo, launches):
    root, first, _ = repo
    _seed(root, {"A": {"status": "landed", "sha": first}})
    marker = _plan_marker(root, "A")
    before = (root / c.status_rel("A")).read_bytes()
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["written"] is False and out["close"]["launched"] is True
    assert json.loads(marker.read_text(encoding="utf-8"))["sha"] == first
    assert launches == ["A"]
    assert (root / c.status_rel("A")).read_bytes() == before


def test_landed_same_sha_with_job_launches_nothing(repo, launches):
    root, first, _ = repo
    _seed(root, {"A": {"status": "landed", "sha": first}})
    _plan_marker(root, "A")
    _call(root, run_id="A", sha=first)
    launches.clear()
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["close"]["status"] == "not-applicable" and out["blocking_rows"] == ["R1"]
    assert launches == []


def test_marker_without_plan_is_not_applicable(repo, launches):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    marker = _plan_marker(root, "A")
    data = json.loads(marker.read_text(encoding="utf-8"))
    del data["plan"]
    marker.write_text(json.dumps(data), encoding="utf-8")
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["close"]["status"] == "not-applicable" and out["blocking_rows"] == ["R1"]
    assert launches == [] and _job(root, "A") is None


def test_short_base_sha_and_non_uuid_session_are_dropped(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _plan_marker(root, "A", run_base_sha="abc")
    _call(root, run_id="A", sha=first, session_id="not-a-uuid")
    job = _job(root, "A")
    assert job["run_base_sha"] is None and not job.get("session_id")


def test_failed_launch_reports_unlaunched_and_stays_ok(repo, monkeypatch):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _plan_marker(root, "A")
    monkeypatch.setattr(op, "_launch", lambda root, run_id: False)
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["close"]["launched"] is False and out["close"]["job"]
    assert _status(root, "A")["status"] == "landed"


@pytest.mark.parametrize("params", [{"run_id": "A", "sha": "b" * 40}, {"run_id": "A", "sha": "abc"}])
def test_refusals_write_nothing(repo, launches, params):
    root, _, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    marker = _plan_marker(root, "A")
    before = marker.read_bytes()
    assert not _call(root, **params)["ok"]
    assert marker.read_bytes() == before and launches == [] and _job(root, "A") is None


def test_launch_calls_spawn_detached_once_on_close_background(tmp_path, monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    seen: list = []
    monkeypatch.setattr(detached_spawn, "spawn_detached", lambda *a, **k: seen.append((a, k)) or True)
    assert REAL_LAUNCH(tmp_path, "A") is True
    (args, kwargs), = seen
    assert args[1].endswith("close_background.py") and args[2][0] == "A" and kwargs == {"site_free": True}


def _commit_files(repo: Path, files: dict) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        _git(repo, "add", "--", p.relative_to(repo).as_posix())
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "files")
    return _git(repo, "rev-parse", "HEAD")


def _withhold(marker: Path, *paths: str) -> None:
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["run_artifacts"] = {"withheld": {p: "r" for p in paths}}
    marker.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("solo", [True, False])
def test_sha_not_carrying_withheld_paths_refuses_and_follow_up_is_accepted(repo, solo):
    root, first, _ = repo
    rid = SOLO if solo else "A"
    if solo:
        marker = _solo(root)
    else:
        _seed(root, {"A": {"status": "halted"}})
        marker = _plan_marker(root, "A")
    _withhold(marker, "sub/d.md", "e.txt")
    unrelated = _commit(root, "unrelated")
    before = marker.read_bytes()
    out = _call(root, run_id=rid, sha=unrelated)
    assert not out["ok"] and "withheld e.txt (+1 more)" in out["error"]
    assert marker.read_bytes() == before
    partial = _commit_files(root, {"sub/d.md": "x"})
    out = _call(root, run_id=rid, sha=partial)
    assert not out["ok"] and "withheld e.txt" in out["error"]
    full = _commit_files(root, {"sub/d.md": "later", "e.txt": "e"})
    later = _commit(root, "after")
    out = _call(root, run_id=rid, sha=full)
    assert out["ok"] and out["written"] is True and out["sha"] == full
    assert later != full


@pytest.mark.parametrize("solo", [True, False])
def test_sha_set_covering_withheld_is_accepted_and_insufficient_or_unrelated_refuses(repo, solo):
    root, first, _ = repo
    rid = SOLO if solo else "A"
    if solo:
        marker = _solo(root)
    else:
        _seed(root, {"A": {"status": "halted"}})
        marker = _plan_marker(root, "A")
    _withhold(marker, "sub/d.md", "e.txt")
    one = _commit_files(root, {"sub/d.md": "x"})
    unrelated = _commit(root, "unrelated")
    two = _commit_files(root, {"e.txt": "e"})
    before = marker.read_bytes()
    out = _call(root, run_id=rid, shas=[one])
    assert not out["ok"] and "withheld e.txt" in out["error"]
    out = _call(root, run_id=rid, shas=[one, unrelated])
    assert not out["ok"] and unrelated in out["error"]
    out = _call(root, run_id=rid, shas=[one, two, unrelated])
    assert not out["ok"] and unrelated in out["error"]
    out = _call(root, run_id=rid, shas=[one, "b" * 40])
    assert not out["ok"] and "not a commit" in out["error"]
    assert marker.read_bytes() == before
    out = _call(root, run_id=rid, sha=one, shas=[two[:10]])
    assert out["ok"] and out["written"] is True and out["sha"] == two
    again = _call(root, run_id=rid, shas=[one, two])
    assert again["ok"] and again["written"] is False and again["sha"] == two


@pytest.mark.parametrize("solo", [True, False])
def test_multi_sha_reply_echoes_every_sha_with_the_withheld_paths_each_covered(repo, solo):
    root, first, _ = repo
    rid = SOLO if solo else "A"
    if solo:
        marker = _solo(root)
    else:
        _seed(root, {"A": {"status": "halted"}})
        marker = _plan_marker(root, "A")
    _withhold(marker, "sub/d.md", "e.txt")
    one = _commit_files(root, {"sub/d.md": "x"})
    two = _commit_files(root, {"e.txt": "e"})
    out = _call(root, run_id=rid, sha=one, shas=[two[:10]])
    assert out["ok"] and out["sha"] == two
    assert out["shas"] == [one, two]
    assert out["covers"] == {one: ["sub/d.md"], two: ["e.txt"]}


def test_single_sha_reply_without_withheld_echoes_the_sha_and_no_covers(repo):
    root, first, _ = repo
    _solo(root)
    out = _call(root, run_id=SOLO, sha=first[:8])
    assert out["ok"] and out["shas"] == [first] and out["covers"] == {}
    assert "withheld" not in out and out["blocking_rows"] == []


def test_shas_param_must_be_a_list_of_hex(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    assert not _call(root, run_id="A", shas="abcd1234")["ok"]
    assert not _call(root, run_id="A", shas=[first, "xyz1234"])["ok"]
    assert not _call(root, run_id="A", shas=[])["ok"]


def test_withheld_check_ignores_run_without_withheld_paths(repo):
    root, first, _ = repo
    _solo(root)
    assert _call(root, run_id=SOLO, sha=first)["ok"]


def test_process_time_of_the_op_is_under_the_bar(repo):
    import time

    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    _plan_marker(root, "A")
    start = time.process_time()
    out = _call(root, run_id="A", sha=first)
    elapsed_ms = (time.process_time() - start) * 1000
    assert out["close"]["launched"] is True
    print(f"record_halted_run_landed process_time_ms={elapsed_ms:.1f}")
    assert elapsed_ms <= 200
