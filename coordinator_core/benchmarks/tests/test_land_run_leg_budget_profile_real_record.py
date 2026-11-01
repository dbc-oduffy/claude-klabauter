"""`--real-record` replays a land_run up to the commit leg and commits nothing."""

from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof
from coordinator_core.benchmarks.land_run_slow_disk_profile import _bind_session, build_land_fixture


pytestmark = pytest.mark.spawns_process


@pytest.fixture(autouse=True)
def _env_restored(monkeypatch):
    for name in ("COORDINATOR_INVOCATION_ORIGIN", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "x")


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


def _landing(tmp_path: Path, stop: bool) -> tuple:
    prof.declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        root, base = build_land_fixture(tmp_path, mode="clean", tracked=20, chain=3, stack=stack)
        before = _head(root)
        rows = prof.profile_land(root, base, attribute=False, stop_before_commit=stop)
        return before, _head(root), rows[0]


def test_stop_before_commit_takes_no_commit(tmp_path):
    before, after, whole = _landing(tmp_path, stop=True)
    assert after == before
    assert whole.attribution["commit_leg_reached"] is True
    assert whole.attribution["outcome"] == "stopped_before_commit"
    assert any(leg["to"] == prof.LEG_COMMIT for leg in whole.attribution["legs"])


def test_without_the_stop_the_same_landing_commits(tmp_path):
    before, after, whole = _landing(tmp_path, stop=False)
    assert whole.attribution["commit_leg_reached"] is True
    assert after != before


def test_stop_is_not_an_exception_subclass():
    assert issubclass(prof.StopBeforeCommit, BaseException)
    assert not issubclass(prof.StopBeforeCommit, Exception)


def test_real_record_params_drop_halt_envelope_and_token(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    (tmp_path / "scratch" / "warp" / run_id).mkdir(parents=True)
    (tmp_path / "state" / "warp" / run_id).mkdir(parents=True)
    (tmp_path / "state" / "warp" / run_id / "manifest.json").write_text(json.dumps({"head_sha": "b" * 40}))
    (tmp_path / "scratch" / "warp" / run_id / "terminal-commit-params.json").write_text(json.dumps({
        "script_path": f"scratch/warp/{run_id}.workflow.mjs", "run_id": run_id, "run_token": "tok",
        "halted_by": {"halted": "commit"}, "doc_paths": ["docs/d.md"],
        "run_outcome": {"inline_review": {"prep": {"run_base_sha": None, "x": 1}}, "incomplete_chunks": [],
                        "landed_chunks": [], "ledger_verdict": {"items": []}},
    }))
    params = prof.real_record_params(tmp_path, run_id)
    assert "run_token" not in params and "halted_by" not in params
    assert params["run_base_sha"] == "b" * 40
    assert params["inline_review"]["prep"] == {"run_base_sha": "b" * 40, "x": 1}
    assert params["doc_paths"] == ["docs/d.md"]


def _record_without_params(root: Path, run_id: str) -> None:
    state, scratch = root / "state" / "warp" / run_id, root / "scratch" / "warp" / run_id
    state.mkdir(parents=True)
    scratch.mkdir(parents=True)
    (state / "manifest.json").write_text(json.dumps({"head_sha": "9" * 40, "doc_writes": ["docs/delivery/d.md"]}))
    (state / "halted.json").write_text(json.dumps({
        "run_base_sha": "3" * 40, "run_artifacts": {"withheld": {"src/w.py": "halt-record-only"}},
    }))
    (state / "run-outcome.json").write_text(json.dumps({
        "inline_review": {"prep": {"run_base_sha": None, "x": 1}}, "incomplete_chunks": [],
        "landed_chunks": ["C9"], "ledger_verdict": {"items": []},
    }))
    (scratch / "run-base").write_text("5" * 40 + "\n")


def test_real_record_params_rebuild_a_run_with_no_terminal_commit_params(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    _record_without_params(tmp_path, run_id)
    params = prof.real_record_params(tmp_path, run_id)
    assert params["run_base_sha"] == "5" * 40
    assert params["inline_review"]["prep"] == {"run_base_sha": "5" * 40, "x": 1}
    assert params["script_path"] == f"scratch/warp/{run_id}.workflow.mjs"
    assert params["doc_paths"] == ["docs/delivery/d.md"]
    assert params["landed_chunks"] == ["C9"]
    assert "run_token" not in params


def test_run_base_falls_back_to_halted_then_manifest(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    _record_without_params(tmp_path, run_id)
    (tmp_path / "scratch" / "warp" / run_id / "run-base").unlink()
    assert prof.record_run_base(tmp_path, run_id) == "3" * 40
    (tmp_path / "state" / "warp" / run_id / "halted.json").unlink()
    assert prof.record_run_base(tmp_path, run_id) == "9" * 40


def test_a_run_with_neither_params_nor_outcome_is_refused(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    (tmp_path / "state" / "warp" / run_id).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        prof.real_record_params(tmp_path, run_id)


def test_seed_copies_withheld_paths_the_live_tree_holds(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    src, dst = tmp_path / "src", tmp_path / "dst"
    _record_without_params(src, run_id)
    (src / "src").mkdir()
    (src / "src" / "w.py").write_text("dirty\n")
    dst.mkdir()
    prof.seed_real_record(src, dst, run_id)
    assert (dst / "src" / "w.py").read_text() == "dirty\n"


def test_halted_shape_fixture_lands_seven_py_and_a_doc_past_peer_dirt(tmp_path):
    prof.declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        shape = prof.build_halted_shape_fixture(tmp_path, stack)
        root = shape["root"]
        assert len([p for p in shape["paths"] if p.endswith(".py")]) == 7
        assert all(p.startswith("coordinator_core/ops/dispatch_emit/") for p in shape["paths"] if p.endswith(".py"))
        assert [p for p in shape["paths"] if p.endswith(".md")] == [prof.HALTED_SHAPE_DOC]
        before = _head(root)
        rows = prof.profile_land(root, shape["base"], attribute=False, legs=True)
        whole = rows[0]
        assert whole.attribution["commit_leg_reached"] is True
        assert any(r.axis.startswith("leg:land_run._integrity") for r in rows)
        assert _head(root) != before
        listing = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(root), capture_output=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode().split()
        assert set(shape["paths"]) <= set(listing)
        assert not set(shape["peer_dirty"]) & set(listing)


def test_seed_real_record_copies_the_record_and_script(tmp_path):
    run_id = "ask-20260101T000000-abcdef"
    src, dst = tmp_path / "src", tmp_path / "dst"
    for rel, text in (
        (f"state/warp/{run_id}/manifest.json", "{}"),
        (f"scratch/warp/{run_id}/terminal-commit-params.json", "{}"),
        (f"scratch/warp/{run_id}.workflow.mjs", "// s"),
    ):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    dst.mkdir()
    prof.seed_real_record(src, dst, run_id)
    assert (dst / f"state/warp/{run_id}/manifest.json").is_file()
    assert (dst / f"scratch/warp/{run_id}/terminal-commit-params.json").is_file()
    assert (dst / f"scratch/warp/{run_id}.workflow.mjs").read_text() == "// s"


def test_seed_real_record_copies_halt_withheld_index_and_sentinel(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as hwi

    src, dst = tmp_path / "src", tmp_path / "dst"
    index = src / hwi.INDEX_DIR
    index.mkdir(parents=True)
    (index / "other-run.json").write_text("{}")
    (index / hwi.SENTINEL).write_text("")
    dst.mkdir()
    prof.seed_real_record(src, dst, "ask-20260101T000000-abcdef")
    assert (dst / hwi.INDEX_DIR / "other-run.json").is_file()
    assert (dst / hwi.INDEX_DIR / hwi.SENTINEL).is_file()


def test_seed_real_record_without_an_index_copies_none(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as hwi

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    prof.seed_real_record(src, dst, "ask-20260101T000000-abcdef")
    assert not (dst / hwi.INDEX_DIR).exists()
