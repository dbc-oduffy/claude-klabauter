"""dispatch.land_run on a ledger-bearing ask run with recovery-withheld chunks: the backed chunks and their
import-closure dependents stay out, every other finished chunk lands, and the open row rides the commit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as base
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_VERDICT = {"items": [{"id": "R1", "status": "not_met", "withheld_by": "C3", "remaining": "C3 test still red"}]}


def _chunks(repo: Path, rows: dict) -> None:
    request = CommitRequest(
        chunks=tuple(ChunkCommit(id=cid, title=cid, paths=tuple(paths)) for cid, paths in rows.items()),
        deliverable_id=base.DLV, plan_path=base.PLAN,
    )
    base._write(repo, f"{RUN_DIR_ROOT}/{base.RUN}/commit-request.txt", render_marker(request) + "\n")


def _record(repo: Path, chunks: dict) -> None:
    base._write(repo, f"{RUN_DIR_ROOT}/{base.RUN}/{lr.RECOVERY_RECORD_FILE}", json.dumps({"cap": 2, "chunks": chunks}))


def _exhausted(paths=()) -> dict:
    return {"rows": ["R1"], "attempts": lr.RECOVERY_ATTEMPT_CAP, "status": "exhausted", "paths": list(paths),
            "last_reason": "test_x still red"}


def _package(repo: Path) -> None:
    base._write(repo, "pkg/__init__.py", "")
    base._git(repo, "add", "pkg/__init__.py")
    base._git(repo, "commit", "-q", "-m", "package")


def _call(repo: Path, **extra) -> dict:
    return base._call(repo, ledger_verdict=_VERDICT, **extra)


def _message(repo: Path) -> str:
    return base._git(repo, "log", "-1", "--format=%B")


def test_a_backed_exhausted_chunk_stays_out_and_the_others_land(repo):
    base._write(repo, "b.py", "b\n")
    base._write(repo, "c.py", "c\n")
    _chunks(repo, {"C1": ["a.py"], "C2": ["b.py"], "C3": ["c.py"]})
    _record(repo, {"C3": _exhausted()})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["committed"] is True, out
    names = base._names(repo, out["sha"])
    assert {"a.py", "b.py"} <= names and "c.py" not in names
    msg = _message(repo)
    assert "Lands-Chunks: C1, C2" in msg
    assert "Requirement-Open: R1 recovery-cap-exhausted (2 attempts): test_x still red" in msg
    assert out["recovery"]["withheld"] == {"C3": "recovery-cap-exhausted (2 attempts): test_x still red"}
    assert out["recovery"]["open_rows"] == ["R1"]
    assert "C3" in out["withheld"]


def test_an_unbacked_param_refuses_and_nothing_lands(repo):
    base._write(repo, "c.py", "c\n")
    _chunks(repo, {"C1": ["a.py"], "C3": ["c.py"]})
    _record(repo, {"C3": {**_exhausted(), "status": "fixed"}})
    head = base._git(repo, "rev-parse", "HEAD")

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["refused"] == "recovery-withheld-unbacked", out
    assert out["unbacked"] == ["C3"]
    assert base._git(repo, "rev-parse", "HEAD") == head


def test_an_exhausted_record_below_the_cap_does_not_back_the_param(repo):
    base._write(repo, "c.py", "c\n")
    _chunks(repo, {"C1": ["a.py"], "C3": ["c.py"]})
    _record(repo, {"C3": {**_exhausted(), "attempts": 1}})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["refused"] == "recovery-withheld-unbacked", out


def test_a_finished_chunk_importing_the_exhausted_chunks_new_module_is_withheld_not_refused(repo):
    _package(repo)
    base._write(repo, "pkg/new_mod.py", "X = 1\n")
    base._write(repo, "pkg/user.py", "from pkg.new_mod import X\n")
    _chunks(repo, {"C1": ["a.py"], "C2": ["pkg/user.py"], "C3": ["pkg/new_mod.py"]})
    _record(repo, {"C3": _exhausted()})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["committed"] is True, out
    names = base._names(repo, out["sha"])
    assert "a.py" in names and "pkg/user.py" not in names and "pkg/new_mod.py" not in names
    assert "Lands-Chunks: C1" in _message(repo) and "C2" not in _message(repo).split("Lands-Chunks:")[1].split("\n")[0]
    assert out["recovery"]["withheld"]["C2"] == lr.IMPORT_CLOSURE_OPEN


def test_a_dependent_of_a_dependent_is_withheld_too(repo):
    _package(repo)
    base._write(repo, "pkg/new_mod.py", "X = 1\n")
    base._write(repo, "pkg/mid.py", "from pkg.new_mod import X\nY = X\n")
    base._write(repo, "pkg/top.py", "from pkg.mid import Y\n")
    _chunks(repo, {"C1": ["a.py"], "C2": ["pkg/mid.py"], "C4": ["pkg/top.py"], "C3": ["pkg/new_mod.py"]})
    _record(repo, {"C3": _exhausted()})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["committed"] is True, out
    assert out["recovery"]["withheld"]["C2"] == lr.IMPORT_CLOSURE_OPEN
    assert out["recovery"]["withheld"]["C4"] == lr.IMPORT_CLOSURE_OPEN
    assert base._names(repo, out["sha"]) - {base.PLAN, base.SIZING} >= {"a.py"}
    assert "pkg/top.py" not in base._names(repo, out["sha"])


def test_an_out_of_footprint_fix_path_the_record_names_withholds_its_importer(repo):
    _package(repo)
    base._write(repo, "pkg/_substrate.py", "S = 1\n")
    base._write(repo, "pkg/user.py", "from pkg._substrate import S\n")
    _chunks(repo, {"C1": ["a.py"], "C2": ["pkg/user.py"], "C3": ["c.py"]})
    base._write(repo, "c.py", "c\n")
    _record(repo, {"C3": _exhausted(paths=["pkg/_substrate.py"])})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out["committed"] is True, out
    assert out["withheld"]["pkg/_substrate.py"].startswith(lr.CAP_EXHAUSTED)
    assert out["recovery"]["withheld"]["C2"] == lr.IMPORT_CLOSURE_OPEN
    assert "pkg/user.py" not in base._names(repo, out["sha"])


def test_an_import_of_a_path_no_withheld_chunk_owns_still_refuses(repo):
    _package(repo)
    base._write(repo, "pkg/orphan.py", "O = 1\n")
    base._write(repo, "pkg/user.py", "from pkg.orphan import O\n")
    base._write(repo, "c.py", "c\n")
    _chunks(repo, {"C1": ["a.py"], "C2": ["pkg/user.py"], "C3": ["c.py"]})
    _record(repo, {"C3": _exhausted()})
    head = base._git(repo, "rev-parse", "HEAD")

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert out.get("committed") is not True, out
    assert "import-closure-untracked" in str(out), out
    assert base._git(repo, "rev-parse", "HEAD") == head


def test_a_held_only_run_lands_no_product_and_the_reply_still_names_the_recovery(repo):
    base._write(repo, "c.py", "c\n")
    _chunks(repo, {"C3": ["c.py"]})
    _record(repo, {"C3": _exhausted()})

    out = _call(repo, **{lr.RECOVERY_WITHHELD_PARAM: ["C3"]})

    assert "c.py" not in (base._names(repo, out["sha"]) if out.get("sha") else set()), out
    assert out["recovery"]["withheld"] == {"C3": "recovery-cap-exhausted (2 attempts): test_x still red"}


def test_the_param_is_validated_as_a_string_list(repo):
    _chunks(repo, {"C1": ["a.py"]})

    out = land_run._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": base._REVIEWED,
         lr.RECOVERY_WITHHELD_PARAM: "C3"},
        repo_root=repo / ".git",
    )

    assert lr.RECOVERY_WITHHELD_PARAM in out["error"], out
