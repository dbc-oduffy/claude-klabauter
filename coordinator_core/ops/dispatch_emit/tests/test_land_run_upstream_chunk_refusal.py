"""dispatch.land_run withholds a dependent run's product while its plan holds an upstream chunk."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.land_run import UPSTREAM_WITHHOLD_REASON, upstream_withholds
from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as rig
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as ask_run_repo  # noqa: F401

UP = "docs/plans/up.md"
DEP = "docs/plans/dep.md"
_TASKS = "## Tasks\n\n```yaml plan-tasks\n{rows}\n```\n"
_ROW = "- id: {id}\n  title: t\n  surface: a.py\n  writes: [a.py]\n  change_kind: code-edit{extra}"
_EDGE = f"\n  depends_on_plan:\n    - {{plan: {UP}, chunk: C7, gate_kind: output-consumption-runtime}}"


def _plan(root: Path, rel: str, rows: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntitle: t\nstatus: executing\n---\n\n" + _TASKS.format(rows=rows), encoding="utf-8")


def _pair(root: Path, up_extra: str = "") -> None:
    _plan(root, UP, _ROW.format(id="C7", extra=up_extra))
    _plan(root, DEP, _ROW.format(id="D1", extra=_EDGE))


def _request(plan: str | None) -> CommitRequest:
    return CommitRequest(chunks=(ChunkCommit(id="D1", title="D1", paths=("d1.py", "d1b.py")),), plan_path=plan)


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_uncoded_upstream_chunk_withholds_every_product_path_naming_it(tmp_path):
    root = _root(tmp_path)
    _pair(root)
    out = upstream_withholds(root, _request(DEP))
    assert set(out) == {"d1.py", "d1b.py"}
    assert f"upstream chunk {UP} C7 is unexecuted" in out["d1.py"]
    assert "land or re-fire the upstream chunk" in out["d1.py"]
    assert UPSTREAM_WITHHOLD_REASON == "upstream-chunk-unexecuted"


def test_halted_upstream_run_is_named(tmp_path):
    root = _root(tmp_path)
    _pair(root)
    rec = root / RECORD_DIR_ROOT / "run-a"
    rec.mkdir(parents=True)
    (rec / "run-stage.json").write_text(json.dumps({"stage": "halted", "plan_path": UP}), encoding="utf-8")
    (rec / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    out = upstream_withholds(root, _request(DEP))
    assert f"upstream chunk {UP} C7 is halted (run run-a)" in out["d1.py"]


def test_coded_upstream_chunk_withholds_nothing(tmp_path):
    root = _root(tmp_path)
    _pair(root, "\n  disposition: coded")
    assert upstream_withholds(root, _request(DEP)) == {}


def test_no_plan_path_or_unreadable_plan_withholds_nothing(tmp_path):
    root = _root(tmp_path)
    _pair(root)
    assert upstream_withholds(root, _request(None)) == {}
    assert upstream_withholds(root, _request("docs/plans/absent.md")) == {}


def test_plan_with_no_edge_withholds_nothing(tmp_path):
    root = _root(tmp_path)
    _plan(root, DEP, _ROW.format(id="D1", extra=""))
    assert upstream_withholds(root, _request(DEP)) == {}


def test_a_row_edge_withholds_only_its_own_chunk_and_a_frontmatter_edge_every_chunk(tmp_path):
    root = _root(tmp_path)
    _plan(root, UP, _ROW.format(id="C7", extra=""))
    _plan(root, DEP, _ROW.format(id="D1", extra=_EDGE) + "\n" + _ROW.format(id="D2", extra=""))
    request = CommitRequest(
        chunks=(ChunkCommit(id="D1", title="D1", paths=("d1.py",)), ChunkCommit(id="D2", title="D2", paths=("d2.py",))),
        plan_path=DEP,
    )
    assert set(upstream_withholds(root, request)) == {"d1.py"}

    p = root / DEP
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "status: executing\n", f"status: executing\ndepends_on_plan:\n  - {{plan: {UP}, status: implemented, gate_kind: epistemic-premise}}\n", 1
        ),
        encoding="utf-8",
    )
    assert set(upstream_withholds(root, request)) == {"d1.py", "d2.py"}


# -- through the production land entry --------------------------------------------------------


_UNMET_ONLY_COMMIT = {
    "items": [{"id": "R1", "status": "not_met", "essential": True, "wired": True, "remaining": ["commit"]}]
}


def _land_dependent(root: Path, up_extra: str = "", monkeypatch=None) -> dict:
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None, **kw: ([], [])
    )
    plan = (root / rig.PLAN).read_text(encoding="utf-8")
    (root / rig.PLAN).write_text(
        plan.replace("  disposition: open\n", f"  disposition: open{_EDGE}\n"), encoding="utf-8"
    )
    _plan(root, UP, _ROW.format(id="C7", extra=up_extra))
    return rig._call(root, ledger_verdict=_UNMET_ONLY_COMMIT)


def test_land_run_withholds_the_dependent_product_while_the_upstream_chunk_is_uncoded(ask_run_repo, monkeypatch) -> None:
    out = _land_dependent(ask_run_repo, monkeypatch=monkeypatch)
    assert out["withheld"]["a.py"].startswith(UPSTREAM_WITHHOLD_REASON), out
    assert f"upstream chunk {UP} C7 is unexecuted" in out["withheld"]["a.py"], out
    assert out.get("committed") is not True or "a.py" not in rig._names(ask_run_repo, out["sha"]), out


def test_land_run_lands_the_dependent_product_once_the_upstream_chunk_is_coded(ask_run_repo, monkeypatch) -> None:
    out = _land_dependent(ask_run_repo, "\n  disposition: coded", monkeypatch=monkeypatch)
    assert out["committed"] is True, out
    assert "a.py" in rig._names(ask_run_repo, out["sha"])
