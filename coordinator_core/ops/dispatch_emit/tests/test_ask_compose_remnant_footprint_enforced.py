"""A remnant-complete leg's write outside its recorded footprint is withheld at landing by the engine
(`outside-row-footprint`), whether the leg reported the path or its self-report is clean; in-footprint
writes still land."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    RUN,
    _call,
    _git,
    _names,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

STRAY = "stray.py"


def _record_remnant(root, *, footprint, paths) -> None:
    remnant = {
        "field": "remnant", "loop_owner": "engine", "cap": 1, "items": [], "footprint": footprint,
        "status": "blocked" if paths else "open", "paths": paths, "last_reason": "",
    }
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/{lr.RECOVERY_RECORD_FILE}", json.dumps({"remnant": remnant}))


def test_a_reported_out_of_footprint_remnant_write_is_withheld_while_the_in_footprint_write_lands(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[STRAY])

    out = _call(repo)

    assert out["committed"] is True, out
    assert out["footprint_breach"] == [STRAY]
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert "a.py" in landed and STRAY not in landed
    assert (repo / STRAY).read_text(encoding="utf-8") == "x = 1\n"


def test_an_unreported_out_of_footprint_remnant_write_with_a_clean_self_report_is_never_landed(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[])

    out = _call(repo)

    assert out["committed"] is True, out
    assert "footprint_breach" not in out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert "a.py" in landed and STRAY not in landed
    assert STRAY in _git(repo, "status", "--porcelain", "--", STRAY)


def test_a_record_whose_status_hides_the_breach_still_withholds_it(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[STRAY])
    path = repo / RUN_DIR_ROOT / RUN / lr.RECOVERY_RECORD_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["remnant"]["status"] = "open"
    path.write_text(json.dumps(data), encoding="utf-8")

    out = _call(repo)

    assert out["footprint_breach"] == [STRAY]
    assert STRAY not in set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())


def test_an_in_footprint_only_remnant_record_withholds_nothing(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _record_remnant(repo, footprint=["a.py"], paths=[])

    out = _call(repo)

    assert out["committed"] is True and "footprint_breach" not in out, out
    assert "a.py" in _names(repo, out["sha"])
