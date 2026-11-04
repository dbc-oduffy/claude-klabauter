"""An ask emit's run token and its state/warp/<id> dir exist together or not at all."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import op, run_token
from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed import repo  # noqa: F401


def _run_ids(root):
    base = root / RECORD_DIR_ROOT
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


def test_a_minting_ask_emit_leaves_the_run_dir_beside_its_token(repo, capsys):  # noqa: F811
    assert cli.main(["--ask", "add a thing", "--repo-root", str(repo)]) == 0
    (run_id,) = _run_ids(repo)
    assert (repo / RECORD_DIR_ROOT / run_id).is_dir()
    assert run_token.is_live(repo, run_id)


@pytest.mark.parametrize("failing", ["_start_requirements", "compose_ask_script"])
def test_an_emit_failing_after_the_mint_leaves_no_token(repo, monkeypatch, failing):  # noqa: F811
    minted: list[str] = []
    real_mint = run_token.mint

    def spy(root, run_id, *a, **kw):
        minted.append(run_id)
        return real_mint(root, run_id, *a, **kw)

    def boom(*a, **kw):
        raise RuntimeError("outran the hook budget")

    monkeypatch.setattr(run_token, "mint", spy)
    if failing == "_start_requirements":
        monkeypatch.setattr(op, "_start_requirements", boom)
    else:
        from coordinator_core.ops.dispatch_emit import ask_compose

        monkeypatch.setattr(ask_compose, "compose_ask_script", boom)
    with pytest.raises(RuntimeError):
        cli.main(["--ask", "add a thing", "--repo-root", str(repo)])
    assert len(minted) == 1
    assert not run_token.is_live(repo, minted[0])


def test_a_failed_emit_drops_the_hooks_dirless_marker_token_of_a_premined_id(repo, monkeypatch):  # noqa: F811
    run_id = "ask-20261103T204518-b06bac"
    run_token.mint(repo, run_id, "x.js", marker=True)
    assert run_token.is_live(repo, run_id) and not (repo / RECORD_DIR_ROOT / run_id).exists()

    def boom(*a, **kw):
        raise RuntimeError("late failure")

    monkeypatch.setattr(op, "_start_requirements", boom)
    with pytest.raises(RuntimeError):
        op._dispatch_emit({"ask": "add a thing", "preminted_run_id": run_id}, repo_root=repo)
    assert not run_token.is_live(repo, run_id)


def test_a_failed_emit_removes_the_partial_run_dir_it_created(repo, monkeypatch):  # noqa: F811
    def boom(*a, **kw):
        raise RuntimeError("late failure")

    monkeypatch.setattr(op, "_start_requirements", boom)
    with pytest.raises(RuntimeError):
        cli.main(["--ask", "add a thing", "--repo-root", str(repo)])
    assert _run_ids(repo) == []


def test_a_failed_emit_keeps_the_dir_of_a_run_that_already_has_one(repo, monkeypatch):  # noqa: F811
    run_id = "ask-20261103T204518-b06bac"
    kept = repo / RECORD_DIR_ROOT / run_id
    kept.mkdir(parents=True)
    (kept / "marker.txt").write_text("x", encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("late failure")

    monkeypatch.setattr(op, "_start_requirements", boom)
    with pytest.raises(RuntimeError):
        op._dispatch_emit({"ask": "add a thing", "preminted_run_id": run_id}, repo_root=repo)
    assert (kept / "marker.txt").is_file()


def test_a_failed_emit_keeps_the_token_of_a_run_that_already_has_its_dir(repo, monkeypatch):  # noqa: F811
    run_id = "ask-20261103T204518-b06bac"
    run_token.mint(repo, run_id, "x.js", marker=True)
    (repo / RECORD_DIR_ROOT / run_id).mkdir(parents=True)

    def boom(*a, **kw):
        raise RuntimeError("late failure")

    monkeypatch.setattr(op, "_start_requirements", boom)
    with pytest.raises(RuntimeError):
        op._dispatch_emit({"ask": "add a thing", "preminted_run_id": run_id}, repo_root=repo)
    assert run_token.is_live(repo, run_id)
