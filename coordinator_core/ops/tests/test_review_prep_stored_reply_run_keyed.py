"""A manifest-driven run's stored review.prep reply is keyed on run_id plus run_base_sha: it never serves another run's slices."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
from coordinator_core.ops.tests.test_review_prep import _base, _git, _write, repo  # noqa: F401
from coordinator_core.session.machinery_paths import share_dir

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-run-keyed"
RUN_A = "ask-20261118T184138-454d5f"
RUN_B = "ask-20261119T045538-d1b26f"
PLAN = "manifest.json"


def _stage(repo: Path, run_id: str, declared: list) -> None:
    d = record_dir(repo, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _prep(repo: Path, run_id: str) -> dict:
    return rp._handler(
        {"base": _base(repo), "plan_path": PLAN, "worktree": True, "session_id": SESSION, "run_id": run_id}, repo
    )


def _two_runs(repo: Path, same_base: bool = False):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, RUN_A, ["a/one.py"])
    first = _prep(repo, RUN_A)
    assert first["error"] is None and first["slices"]
    if not same_base:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "second")
    _write(repo, ["b/two.py"], 3)
    _stage(repo, RUN_B, ["b/two.py"])
    return first


def _files(reply: dict) -> list:
    return [f for s in reply["slices"] for f in s["files"]]


def test_second_manifest_run_at_another_base_gets_only_its_own_slices(repo):
    first = _two_runs(repo)
    second = _prep(repo, RUN_B)
    assert second["error"] is None
    assert _files(second) == ["b/two.py"]
    assert second["sidecar_path"] != first["sidecar_path"]
    assert second["run_base_sha"] != first["run_base_sha"]
    assert RUN_B in second["sidecar_path"] and RUN_A not in second["sidecar_path"]


def test_lookups_by_run_key_never_return_the_other_runs_reply(repo):
    first = _two_runs(repo)
    base_b = _base(repo)
    base_a = first["run_base_sha"]
    assert rp.stored_reply_for_run(repo, SESSION, base_b, run_key=RUN_B) is None
    assert rp.stored_reply_for_run(repo, SESSION, None, run_key=RUN_B) is None
    mine = rp.stored_reply_for_run(repo, SESSION, base_a, run_key=RUN_A)
    assert mine is not None and mine["sidecar_path"] == first["sidecar_path"]
    second = _prep(repo, RUN_B)
    got = rp.stored_reply_for_run(repo, SESSION, base_b, run_key=RUN_B)
    assert got is not None and got["sidecar_path"] == second["sidecar_path"]


def test_a_copy_of_the_other_runs_sidecar_under_this_runs_canonical_name_is_not_served(repo):
    first = _two_runs(repo, same_base=True)
    base = _base(repo)
    share = Path(share_dir(str(repo), SESSION))
    foreign = share / f"{run_prep_slice_prefix(RUN_A, base)}.review-prep.md"
    canonical = share / f"{run_prep_slice_prefix(RUN_B, base)}.review-prep.md"
    shutil.copyfile(foreign, canonical)
    _stage(repo, RUN_B, ["a/one.py"])
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_B) is None
    served = _prep(repo, RUN_B)
    assert served["error"] is None
    assert served["sidecar_path"] != first["sidecar_path"]
    assert RUN_B in served["sidecar_path"]
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_B)["sidecar_path"] == served["sidecar_path"]


def test_a_pointer_naming_the_other_runs_sidecar_is_not_served(repo):
    first = _two_runs(repo, same_base=True)
    base = _base(repo)
    share = Path(share_dir(str(repo), SESSION))
    (share / rp._pointer_name(RUN_B, base)).write_text(
        json.dumps({"sidecar": Path(first["sidecar_path"]).name, "ambiguous": False}), encoding="utf-8"
    )
    (share / rp._pointer_name(RUN_B, None)).write_text(
        json.dumps({"sidecar": Path(first["sidecar_path"]).name, "ambiguous": False}), encoding="utf-8"
    )
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_B) is None
    assert rp.stored_reply_for_run(repo, SESSION, None, run_key=RUN_B) is None


def test_a_typed_slice_prefix_of_the_other_run_is_not_served_to_a_run_id_call(repo):
    first = _two_runs(repo, same_base=True)
    base = _base(repo)
    share = Path(share_dir(str(repo), SESSION))
    foreign_name = Path(first["sidecar_path"]).name
    stored = rp._stored_reply(repo, share / foreign_name, base, ["a/one.py"], None, None, run_id=RUN_B)
    assert stored is None
    own = rp._stored_reply(repo, share / foreign_name, base, ["a/one.py"], None, None, run_id=RUN_A)
    assert own is not None and own["sidecar_path"] == first["sidecar_path"]
