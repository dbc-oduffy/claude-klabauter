"""dispatch.terminal_commit writes the run's completion receipts into its one commit (plan D5)."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.completion_receipts import model
from coordinator_core.completion_receipts.store import ReceiptWriteError
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

import yaml

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DLV = "dlv-alpha-aaaaaa"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check,
        **no_console_creationflags(),
    ).stdout


def _call(repo, params):
    return terminal_commit._handler({"inline_review": _REVIEWED, **params}, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    return root


@pytest.fixture
def claims(monkeypatch):
    claimed: list = []
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked",
        lambda sid, cwd=None: ([("handoff", n) for n in claimed], []),
    )
    return claimed


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _script(repo, request):
    _write(repo, "run.mjs", "// emitted\n" + render_marker(request) + "\n")
    return "run.mjs"


_PLAN = (
    f"---\ntitle: p\ndeliverable_id: {DLV}\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n"
)
_SID = "11111111-2222-3333-4444-555555555555"


def _plan_run(repo):
    _write(repo, "docs/plan.md", _PLAN)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),),
        deliverable_id=DLV,
        plan_path="docs/plan.md",
    )
    return _script(repo, request)


def _receipt_files(repo):
    return sorted((repo / "state").rglob("*.md")) if (repo / "state").exists() else []


def test_plan_run_commit_tree_carries_one_valid_receipt(repo, claims):
    script = _plan_run(repo)
    out = _call(repo, {"script_path": script, "incomplete_chunks": [], "session_id": _SID})
    assert out["committed"] is True
    assert len(out["receipts"]) == 1 and out["receipt_coverage"] == "written"
    assert "receipt_divergence" not in out
    tree = _git(["show", "--name-only", "--format=", out["sha"]], repo).split()
    assert out["receipts"][0] in tree
    text = _git(["show", f"{out['sha']}:{out['receipts'][0]}"], repo)
    fm = yaml.safe_load(split_frontmatter(text.replace("\r\n", "\n")).fm_text)
    assert model.validate(fm) == []
    assert fm["deliverable_id"] == DLV and fm["baton_id"] is None


def test_unreviewed_call_writes_no_receipt(repo, claims):
    script = _plan_run(repo)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": []}, repo_root=repo / ".git"
    )
    assert out["refused"] == "unreviewed"
    assert _receipt_files(repo) == []


def test_receipt_write_failure_refuses_without_a_commit(repo, claims, monkeypatch):
    script = _plan_run(repo)
    head = _git(["rev-parse", "HEAD"], repo)

    def boom(root, fm, prose):
        raise ReceiptWriteError("disk said no")

    monkeypatch.setattr(cr, "write_receipt", boom)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["refused"] == "receipt-write-failed"
    assert "completion receipt write failed: disk said no" in out["error"]
    assert out["committed"] is False
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert _receipt_files(repo) == []


def test_second_receipt_failure_removes_the_first(repo, claims, monkeypatch):
    script = _plan_run(repo)
    _write(repo, "state/handoffs/b1.md", f"---\nhandoff_id: hnd-one-111111\ndeliverable_id: {DLV}\n---\n\nx\n")
    _write(repo, "state/handoffs/b2.md", f"---\nhandoff_id: hnd-two-222222\ndeliverable_id: {DLV}\n---\n\nx\n")
    claims.extend(["b1.md", "b2.md"])
    real = cr.write_receipt
    calls = []

    def flaky(root, fm, prose):
        calls.append(1)
        if len(calls) == 2:
            raise ReceiptWriteError("second failed")
        return real(root, fm, prose)

    monkeypatch.setattr(cr, "write_receipt", flaky)
    out = _call(repo, {"script_path": script, "incomplete_chunks": [], "session_id": _SID})
    assert out["refused"] == "receipt-write-failed"
    assert len(calls) == 2
    assert [p for p in _receipt_files(repo) if "handoffs" not in p.parts] == []


def test_inventory_run_writes_one_receipt_per_claimed_baton(repo, claims):
    _write(repo, "docs/plan.md", _PLAN)
    _write(
        repo,
        "state/mise-inventory/run1.md",
        "---\nrun_id: run1\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plan.md` | s | `a.py` | — | v | S | pending |\n",
    )
    _write(
        repo,
        "state/mise-inventory/run1.spine.md",
        "---\nrun_id: run1\nderived_from: mise inventory record\n---\n\n## Tasks\n",
    )
    _write(repo, "state/handoffs/b1.md", f"---\nhandoff_id: hnd-one-111111\ndeliverable_id: {DLV}\n---\n\nx\n")
    _write(repo, "state/handoffs/b2.md", "---\nhandoff_id: hnd-two-222222\n---\n\nx\n")
    claims.extend(["b1.md", "b2.md"])
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="P1.C1", title="t", paths=("a.py",)),),
        plan_path="state/mise-inventory/run1.spine.md",
    )
    script = _script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": [], "session_id": _SID})
    assert out["committed"] is True
    assert len(out["receipts"]) == 2
    assert out["chunks_committed"] == ["P1.C1"]
    ids = set()
    for rel in out["receipts"]:
        text = _git(["show", f"{out['sha']}:{rel}"], repo)
        fm = yaml.safe_load(split_frontmatter(text.replace("\r\n", "\n")).fm_text)
        assert fm["run"]["kind"] == "mise-en-place"
        ids.add(fm["baton_id"])
    assert ids == {"hnd-one-111111", "hnd-two-222222"}


def test_nothing_to_commit_writes_no_receipt(repo, claims):
    _write(repo, "docs/plan.md", _PLAN)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("gone.py",)),),
        deliverable_id=DLV,
        plan_path="docs/plan.md",
    )
    script = _script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out == {"committed": False, "nothing_to_commit": True}
    assert _receipt_files(repo) == []
