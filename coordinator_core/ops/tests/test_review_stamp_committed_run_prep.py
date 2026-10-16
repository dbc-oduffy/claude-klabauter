"""review_stamp mint on a committed run whose relayed prep lost its file counts: the engine's stored
`review.prep` record is found by run id and its files are measured run_base_sha..terminal_sha."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import review_stamp as m

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261016T145242-9ed64d"
ZERO_MSG = "review-stamp: refusing to mint: zero files in the reviewed diff and the exit criterion is not met"
FILES = ["src/a.py", "src/b.py", "src/c.py"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, files: list[str], text: str) -> str:
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", text)
    return _git(repo, "rev-parse", "HEAD")


def _engine_record(share: Path, base: str, files: list[str], *, run: str = RUN) -> None:
    fm = {
        "agent_type": "engine:prep-freeze", "plan_id": "manifest", "share_dir": str(share),
        "run_base_sha": base, "verdict": "ok", "basis": "worktree", "whole_diff_path": "d.diff",
        "product_files": len(files), "foreign_claims": [], "whole_diff_sidecars": [],
        "slices": [{"files": files}], "sidecar_path": "x",
    }
    (share / f"manifest-{run}-{base[:12]}-prep.review-prep.md").write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8"
    )


def _fixture(tmp_path: Path, *, relay_base: bool = False, engine_files=None, change=FILES, engine=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, ["README.md"], "base")
    head = _commit(repo, change, "work") if change else base
    share = repo / ".coordinator-local" / "subagent-share" / "sess"
    share.mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text("---\ntitle: P\nplan_id: pln-x-abc123\n---\n\nbody\n", encoding="utf-8")
    if engine:
        _engine_record(share, base, FILES if engine_files is None else engine_files)
    record_path = share / f"{RUN}.review-wave-bookkeeping.md"
    record_path.write_text("---\nrun_id: r\n---\n", encoding="utf-8")
    record = {
        "plan_id": "pln-x-abc123", "unresolved": [], "confinement_violations": 0, "slices": 1,
        "brief_conformance": {"items": 1, "met": 1, "unmet": 0},
        "prep": {
            "run_base_sha": base if relay_base else None, "product_files": None,
            "foreign_claims": [], "slice_files": [],
        },
        "delivery": {"verdict": "PASS", "product_files": 0, "claims_unbacked": 0},
        "tests": {"status": "pass", "run": 3, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "not_met", "observation": "partial", "sidecar": None},
    }
    return repo, plan, record_path, record, base, head


def _mint(repo, plan, record_path, record, head):
    return m.mint(
        plan, repo, build_test_path=None, resolved=(head, record_path, record),
        criterion_open_ok=True, terminal_tree_sha="b" * 40,
    )


@pytest.mark.parametrize("relay_base", [False, True])
def test_a_zero_file_relayed_prep_mints_from_the_engine_record(tmp_path, relay_base):
    repo, plan, rp, record, base, head = _fixture(tmp_path, relay_base=relay_base)
    stamp = _mint(repo, plan, rp, record, head)
    assert stamp["delivery"]["product_files"] == 3
    assert stamp["run_base_sha"] == base


def test_files_the_commit_did_not_change_still_refuse(tmp_path):
    repo, plan, rp, record, _, head = _fixture(tmp_path, engine_files=["src/other.py"])
    with pytest.raises(m.MintRefusal) as exc:
        _mint(repo, plan, rp, record, head)
    assert str(exc.value).startswith(ZERO_MSG)


def test_no_engine_record_still_refuses(tmp_path):
    repo, plan, rp, record, _, head = _fixture(tmp_path, engine=False)
    with pytest.raises(m.MintRefusal) as exc:
        _mint(repo, plan, rp, record, head)
    assert str(exc.value).startswith(ZERO_MSG)


def test_a_non_zero_prep_is_read_unchanged_and_spawns_no_git(tmp_path, monkeypatch):
    repo, _, rp, record, base, head = _fixture(tmp_path)
    prep = {"run_base_sha": base, "product_files": 2, "foreign_claims": [], "slice_files": ["x"]}
    calls: list = []
    monkeypatch.setattr(m, "_run_git", lambda *a, **k: calls.append(a) or "")
    assert m._committed_prep(repo, rp, prep, head) is prep
    assert calls == []


def test_peer_files_in_the_range_are_not_counted(tmp_path):
    repo, plan, rp, record, _, head = _fixture(tmp_path, change=FILES + ["peer/z.py"])
    assert _mint(repo, plan, rp, record, head)["delivery"]["product_files"] == 3


def test_mint_receipt_on_an_inventory_record_mints(tmp_path):
    repo, _, rp, record, base, head = _fixture(tmp_path)
    inv = repo / "inv.md"
    inv.write_text("---\nrun_id: run-1\n---\n\nbody\n", encoding="utf-8")
    stamp = m.mint_receipt(inv, repo, resolved=(head, rp, record), terminal_tree_sha="b" * 40)
    assert stamp["delivery"]["product_files"] == 3
    assert stamp["run_base_sha"] == base
