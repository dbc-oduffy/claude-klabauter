"""minted_parent: digest, retry reuse of a minted-not-started parent, orphan reap, explicit --reap."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as fan
from coordinator_core.ops.dispatch_emit import halted_run, live_run_index, run_token
from coordinator_core.ops.dispatch_emit import minted_parent as mp
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.ops.dispatch_emit.fan_launcher import launcher_rel
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED, MINTED_STALE_SECONDS
from coordinator_core.session_baton import store

SID = "sid-minted-parent"
NOW = 1_800_000_000.0
PARAMS = {"asks": [{"ask": f"job {i}", "writes": [f"d{i}"]} for i in range(3)], "session_id": SID}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("split these jobs please, they are unrelated", session_id=SID)["ok"]
    return tmp_path


def _mint(repo, params=PARAMS, at=NOW):
    """A real fan-out plus the on-disk residue the hook leaves behind: launcher, per-run dirs and scripts."""
    out = fan.fanout(dict(params), repo)
    assert out["exit_code"] == 0, out
    entry = repo / mp.digest_rel(mp.asks_digest(params))
    entry.write_text(json.dumps({"parent_id": out["parent_id"], "minted_at": at}), encoding="utf-8")
    launcher = repo / launcher_rel(out["parent_id"])
    launcher.write_text("// launcher", encoding="utf-8")
    launcher.with_name(launcher.name + ".emitted.json").write_text("{}", encoding="utf-8")
    for run in out["runs"]:
        rid = run["run_id"]
        rdir = record_dir(repo, rid)
        rdir.mkdir(parents=True, exist_ok=True)
        write_run_stage_at(rdir, "minted")
        (repo / "scratch" / "warp" / rid).mkdir(parents=True, exist_ok=True)
        (repo / "scratch" / "warp" / rid / "ask.md").write_text("x", encoding="utf-8")
        script = repo / "scratch" / "warp" / f"{rid}.workflow.mjs"
        script.write_text("// run", encoding="utf-8")
        script.with_name(script.name + ".emitted.json").write_text("{}", encoding="utf-8")
        live_run_index.add(repo, rid, at)
    return out, entry


def _run_ids(out):
    return [r["run_id"] for r in out["runs"]]


def _residue(repo, out):
    launcher = repo / launcher_rel(out["parent_id"])
    paths = [launcher, launcher.with_name(launcher.name + ".emitted.json"), repo / "scratch" / "warp" / out["parent_id"]]
    for rid in _run_ids(out):
        script = repo / "scratch" / "warp" / f"{rid}.workflow.mjs"
        paths += [repo / "scratch" / "warp" / rid, script, script.with_name(script.name + ".emitted.json")]
    return paths


def _assert_closed(repo, out):
    """Every run record survives carrying the ABANDONED_AT_MINTED halt marker."""
    for rid in _run_ids(out):
        marker = halted_run._read(record_dir(repo, rid))
        assert marker is not None and marker["halted"] == ABANDONED_AT_MINTED and marker["status"] == "halted"


def test_digest_is_stable_for_the_same_submission_and_differs_by_content_session_sender_and_split():
    base = mp.asks_digest(PARAMS)
    assert mp.asks_digest({**PARAMS, "session_root": "/elsewhere"}) == base
    assert mp.asks_digest({**PARAMS, "session_id": "other"}) != base
    assert mp.asks_digest({**PARAMS, "sender": "em"}) != base
    assert mp.asks_digest({**PARAMS, "confirm_split": True}) != base
    assert mp.asks_digest({**PARAMS, "asks": PARAMS["asks"][:2]}) != base
    assert mp.digest_rel(base) == f"scratch/warp/ask-digest/{base}.json"


def test_fanout_indexes_the_parent_under_the_digest(repo):
    out = fan.fanout(dict(PARAMS), repo)
    entry = json.loads((repo / mp.digest_rel(mp.asks_digest(PARAMS))).read_text(encoding="utf-8"))
    assert entry["parent_id"] == out["parent_id"] and isinstance(entry["minted_at"], float)


def test_find_reusable_returns_the_minted_parent_with_its_launcher_and_ready_runs(repo):
    out, _ = _mint(repo)
    hit = mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60)
    assert hit is not None and hit.parent_id == out["parent_id"]
    assert hit.launcher == repo / launcher_rel(out["parent_id"])
    assert [r for r, _ in hit.ready] == _run_ids(out)


def test_find_reusable_refuses_an_unknown_digest_and_an_aged_entry(repo):
    _mint(repo)
    digest = mp.asks_digest(PARAMS)
    assert mp.find_reusable(repo, "0" * 64, NOW) is None
    assert mp.find_reusable(repo, digest, NOW + mp.REUSE_TTL_S) is None


def test_find_reusable_returns_a_launcherless_minted_parent_with_no_launcher(repo):
    out, _ = _mint(repo)
    (repo / launcher_rel(out["parent_id"])).unlink()
    hit = mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60)
    assert hit is not None and hit.parent_id == out["parent_id"] and hit.launcher is None
    assert [r for r, _ in hit.ready] == _run_ids(out)


def test_find_reusable_refuses_a_launcherless_parent_whose_run_started(repo):
    out, _ = _mint(repo)
    (repo / launcher_rel(out["parent_id"])).unlink()
    write_run_stage_at(record_dir(repo, _run_ids(out)[0]), "staged")
    assert mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60) is None


@pytest.mark.parametrize("how", ["staged", "halted", "live-token"])
def test_find_reusable_refuses_a_parent_any_of_whose_runs_started(repo, how):
    out, _ = _mint(repo)
    rid = _run_ids(out)[1]
    if how == "staged":
        write_run_stage_at(record_dir(repo, rid), "staged")
    elif how == "halted":
        halted_run.write_halt_marker(record_dir(repo, rid), halted="x", sizing_rel=None)
    else:
        run_token.mint(repo, rid, "ask.mjs")
    assert mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60) is None


def test_a_run_dir_without_a_stage_file_still_counts_as_minted(repo):
    out, _ = _mint(repo)
    (record_dir(repo, _run_ids(out)[0]) / "run-stage.json").unlink()
    assert mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60) is not None


def test_reap_orphans_deletes_an_aged_all_minted_parent_and_its_entry(repo):
    out, entry = _mint(repo)
    sizings = [repo / r["slice_sizing"] for r in json.loads(
        (repo / "scratch/warp" / out["parent_id"] / "parent.json").read_text(encoding="utf-8"))["runs"]]
    assert all(s.is_file() for s in sizings)
    assert mp.reap_orphans(repo, NOW + mp.REUSE_TTL_S + 1) == [out["parent_id"]]
    assert not entry.exists()
    assert all(s.is_file() for s in sizings)
    assert not any(p.exists() for p in _residue(repo, out))
    _assert_closed(repo, out)
    assert live_run_index.read(repo, 1000.0) == {}


def test_reuse_ttl_is_the_stale_minted_window():
    assert mp.REUSE_TTL_S == MINTED_STALE_SECONDS


def test_a_run_the_stale_minted_reaper_already_halted_counts_as_never_started_for_the_reap_only(repo):
    out, entry = _mint(repo)
    first = record_dir(repo, _run_ids(out)[0])
    halted_run.write_halt_marker(first, halted=ABANDONED_AT_MINTED, sizing_rel=None)
    assert mp.find_reusable(repo, mp.asks_digest(PARAMS), NOW + 60) is None
    assert mp.reap_orphans(repo, NOW + mp.REUSE_TTL_S + 1) == [out["parent_id"]]
    _assert_closed(repo, out)
    assert not any(p.exists() for p in _residue(repo, out))


def test_a_run_halted_for_another_reason_keeps_the_parent_out_of_the_reap(repo):
    out, _ = _mint(repo)
    halted_run.write_halt_marker(record_dir(repo, _run_ids(out)[0]), halted="x", sizing_rel=None)
    assert mp.reap_parent(repo, out["parent_id"]) is False
    assert (repo / launcher_rel(out["parent_id"])).is_file()


def test_reap_orphans_keeps_the_files_of_an_aged_parent_that_started_and_drops_its_entry(repo):
    out, entry = _mint(repo)
    write_run_stage_at(record_dir(repo, _run_ids(out)[0]), "staged")
    assert mp.reap_orphans(repo, NOW + mp.REUSE_TTL_S + 1) == []
    assert not entry.exists()
    assert (repo / "scratch/warp" / out["parent_id"] / "parent.json").is_file()
    assert all(record_dir(repo, r).is_dir() for r in _run_ids(out))
    assert all(halted_run._read(record_dir(repo, r)) is None for r in _run_ids(out))
    assert (repo / launcher_rel(out["parent_id"])).is_file()


def test_reap_orphans_leaves_a_fresh_entry_alone(repo):
    out, entry = _mint(repo)
    assert mp.reap_orphans(repo, NOW + 60) == []
    assert entry.is_file() and (repo / launcher_rel(out["parent_id"])).is_file()


def test_reap_orphans_handles_at_most_limit_entries_per_call(repo):
    for n in range(3):
        _mint(repo, {**PARAMS, "asks": [{"ask": f"batch {n}", "writes": [f"w{n}"]}]})
    later = NOW + mp.REUSE_TTL_S + 1
    assert len(mp.reap_orphans(repo, later, limit=2)) == 2
    assert len(os.listdir(repo / mp.DIGEST_DIR_REL)) == 1
    assert len(mp.reap_orphans(repo, later)) == 1


def test_reap_orphans_without_an_index_is_empty(repo):
    assert mp.reap_orphans(repo, NOW) == []


def test_reap_orphans_drops_an_unreadable_aged_entry(repo):
    index = repo / mp.DIGEST_DIR_REL
    index.mkdir(parents=True)
    bad = index / ("a" * 64 + ".json")
    bad.write_text("not json", encoding="utf-8")
    assert mp.reap_orphans(repo, os.stat(bad).st_mtime + mp.REUSE_TTL_S + 1) == []
    assert not bad.exists()


def test_reap_parent_deletes_only_paths_named_from_the_parent_record(repo):
    out, _ = _mint(repo)
    bystander = repo / "scratch" / "warp" / "ask-parent-20200101T000000-bbbbbb"
    bystander.mkdir(parents=True)
    (bystander / "parent.json").write_text("{}", encoding="utf-8")
    foreign = repo / "state" / "warp" / "ask-20200101T000000-cccccc"
    foreign.mkdir(parents=True)
    assert mp.reap_parent(repo, out["parent_id"]) is True
    assert bystander.is_dir() and foreign.is_dir()
    assert mp.reap_parent(repo, out["parent_id"]) is False


def test_reap_parent_refuses_a_started_parent_and_deletes_nothing(repo):
    out, _ = _mint(repo)
    write_run_stage_at(record_dir(repo, _run_ids(out)[2]), "staged")
    assert mp.reap_parent(repo, out["parent_id"]) is False
    assert (repo / launcher_rel(out["parent_id"])).is_file()
    assert all(record_dir(repo, r).is_dir() for r in _run_ids(out))


def test_reap_parent_refuses_an_unsafe_parent_id(repo):
    assert mp.reap_parent(repo, "../escape") is False


def test_cli_reap_of_an_all_minted_parent_deletes_it_and_exits_zero(repo, capsys):
    out, entry = _mint(repo)
    assert mp.main(["--reap", out["parent_id"], "--root", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == {"parent_id": out["parent_id"], "reaped": True}
    assert not any(p.exists() for p in _residue(repo, out)) and not entry.exists()
    _assert_closed(repo, out)


def test_cli_reap_of_a_started_parent_names_the_run_and_keeps_the_files(repo, capsys):
    out, entry = _mint(repo)
    started = _run_ids(out)[1]
    write_run_stage_at(record_dir(repo, started), "staged")
    assert mp.main(["--reap", out["parent_id"], "--root", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "parent_id": out["parent_id"], "reaped": False, "kept_because_started": started,
    }
    assert entry.is_file() and (repo / launcher_rel(out["parent_id"])).is_file()


def test_cli_reap_of_a_parent_minted_before_the_index_existed(repo, capsys):
    out, entry = _mint(repo)
    entry.unlink()
    assert mp.main(["--reap", out["parent_id"], "--root", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["reaped"] is True
    assert not (repo / "scratch/warp" / out["parent_id"]).exists()
