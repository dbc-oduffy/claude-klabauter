"""review.prep: one in-process freeze + partition + foreign-claims call returning review-prep-result."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _write(repo: Path, rels: list[str], lines: int) -> None:
    for rel in rels:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v = 1\n" * lines, encoding="utf-8")


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _call(repo: Path, paths: list[str], **kw) -> dict:
    return rp.review_prep(
        repo, base=_base(repo), paths=paths, slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, **kw,
    )


def test_one_product_file_is_one_whole_diff_slice_and_writes_the_sidecar(repo):
    _write(repo, ["a/b/one.py"], 3)
    started = time.process_time()
    result = _call(repo, ["a/b/one.py"])
    cpu_s = time.process_time() - started
    assert result["error"] is None
    assert result["verdict"] == "single-reviewer-ok"
    assert result["plan_id"] == "pln-test-1"
    assert result["basis"] == "worktree"
    assert result["product_files"] == 1
    assert result["foreign_claims"] == []
    assert Path(result["whole_diff_path"]).read_text(encoding="utf-8").startswith("diff --git")
    (slice_,) = result["slices"]
    assert slice_["files"] == ["a/b/one.py"]
    assert slice_["diff_path"] == result["whole_diff_path"]
    assert slice_["sidecar_path"] == result["whole_diff_sidecars"]["personas"][0]
    sidecar = (repo / result["sidecar_path"]).read_text(encoding="utf-8")
    assert sidecar.startswith("---\n") and "product_files: 1" in sidecar and "plan_id: pln-test-1" in sidecar
    assert cpu_s < 0.5


def test_partition_mandatory_returns_one_slice_per_frozen_group(repo):
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    result = _call(repo, rels)
    assert result["error"] is None
    assert result["verdict"] == "PARTITION-MANDATORY"
    assert 1 < len(result["slices"]) <= 6
    assert sorted(f for s in result["slices"] for f in s["files"]) == sorted(rels)
    for s in result["slices"]:
        assert s["id"].startswith("prep-t-")
        assert Path(s["diff_path"]).read_text(encoding="utf-8").startswith("diff --git")
        assert s["diff_path"] != result["whole_diff_path"]


def test_zero_product_files_returns_no_slices(repo):
    _write(repo, ["state/note.md"], 3)
    result = _call(repo, ["state/note.md"])
    assert result["error"] is None
    assert result["product_files"] == 0
    assert result["slices"] == []


def test_a_freeze_refusal_is_returned_as_error_with_no_slices(repo):
    _write(repo, ["a.py"], 3)
    result = rp.review_prep(
        repo, base="deadbeef", paths=["a.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )
    assert result["error"]
    assert result["slices"] == []


def test_range_mode_refusal_passes_through(repo):
    _write(repo, ["a.py"], 3)
    result = _call(repo, ["a.py"], head="no-such-ref-xyz")
    assert result["error"]
    assert result["slices"] == []


def test_handler_requires_exactly_one_of_head_and_worktree(repo):
    common = {"base": _base(repo), "paths": ["seed.py"], "slice_prefix": "p", "plan_path": "docs/plans/p.md"}
    assert "exactly one" in rp._handler({**common, "head": "x", "worktree": True}, repo)["error"]
    assert "exactly one" in rp._handler(dict(common), repo)["error"]
    assert rp._handler(dict(common, worktree=True), None)["error"]


def _landed_commit(repo: Path, rel: str) -> str:
    base = _base(repo)
    _write(repo, [rel], 3)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "peer")
    return base


def test_foreign_claims_keep_only_paths_a_live_other_session_holds(repo, monkeypatch):
    base = _landed_commit(repo, "pkg/held.py")
    _write(repo, ["pkg/own.py"], 3)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "own")
    claims = {"pkg/held.py": ["sess-peer"], "pkg/own.py": [SESSION]}
    monkeypatch.setattr(rp.claim_index, "lookup", lambda paths, **kw: {p: claims.get(p, []) for p in paths})
    found = rp.foreign_claims(repo, f"{base}..HEAD", ["pkg"], SESSION)
    assert found == ["pkg/held.py"]


def test_unanswerable_claim_is_not_foreign_and_empty_paths_spawn_nothing(repo, monkeypatch):
    base = _landed_commit(repo, "pkg/x.py")
    monkeypatch.setattr(
        rp.claim_index, "lookup", lambda paths, **kw: {p: [rp.claim_index.UNANSWERABLE] for p in paths}
    )
    assert rp.foreign_claims(repo, f"{base}..HEAD", ["pkg"], SESSION) == []

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rp, "run_git", boom)
    assert rp.foreign_claims(repo, f"{base}..HEAD", [], SESSION) == []


def test_registered_in_every_op_set():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass  # noqa: PLC0415
    from coordinator_core.op_scopes import _OP_KEY_SCOPE  # noqa: PLC0415
    from coordinator_core.ops._registry_map import OP_MODULE_MAP  # noqa: PLC0415

    assert OP_CLASSIFICATION["review.prep"] is OpClass.MUTATING
    assert _OP_KEY_SCOPE["review.prep"] == "show_top"
    assert OP_MODULE_MAP["review.prep"] == "coordinator_core.ops.review_prep"
