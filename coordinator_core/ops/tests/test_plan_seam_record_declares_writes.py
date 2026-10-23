"""plan.seam_record declares every sidecar it writes, matching the reply's ``sidecars``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops import plan_seam_check as op
from coordinator_core.session import declared_writes

HEAD = "a" * 40
RANGE = "b" * 40 + ".." + "c" * 40


def _fake_git(args, **kw):
    if "check-ignore" in args:
        return SimpleNamespace(ok=True, stdout="")
    if "cat-file" in args:
        out = []
        for line in kw["input"].decode().split("\n"):
            if line == "HEAD":
                out.append(f"{HEAD} commit 10")
            elif line:
                out.append(f"{line} missing")
        return SimpleNamespace(ok=True, stdout="\n".join(out) + "\n")
    return SimpleNamespace(ok=True, stdout="\0")


@pytest.fixture(autouse=True)
def _git(monkeypatch):
    monkeypatch.setattr(op, "run_git", _fake_git)


def _plan(root, name, rid, writes):
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: [{writes}]\n"
    p.write_text(
        f"---\ntitle: t\nstatus: approved\ncapabilities: []\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return rel


def _record(root, params):
    (root / ".git").mkdir(exist_ok=True)
    with declared_writes.collecting() as got:
        reply = op._record_handler(params, repo_root=root)
    return reply, got


def test_prep_record_declares_exactly_the_reply_sidecars(tmp_path):
    a = _plan(tmp_path, "a", "R1", "x.py")
    b = _plan(tmp_path, "b", "R2", "y.py")
    reply, got = _record(tmp_path, {"plans": [a, b], "phase": "prep", "named_set": True})
    assert reply["sidecars"] == ["docs/plans/a.seam.yaml", "docs/plans/b.seam.yaml"]
    assert got == reply["sidecars"]


def test_wave_boundary_record_declares_exactly_the_reply_sidecars(tmp_path):
    a = _plan(tmp_path, "a", "A1", "lib.py")
    reply, got = _record(
        tmp_path,
        {"plans": [a], "phase": "wave-boundary", "named_set": True, "landed_range": RANGE,
         "landed_rows": [], "wave": 2},
    )
    assert reply["sidecars"] == ["docs/plans/a.wave-2.seam.yaml"]
    assert got == reply["sidecars"]


def test_fire_record_declares_exactly_the_reply_sidecars(tmp_path):
    a = _plan(tmp_path, "a", "R1", "x.py")
    reply, got = _record(tmp_path, {"plans": [a], "phase": "fire", "named_set": True})
    assert reply["sidecars"] == ["docs/plans/a.fire.seam.yaml"]
    assert got == reply["sidecars"]


def test_rewrite_of_existing_sidecar_is_declared_again(tmp_path):
    a = _plan(tmp_path, "a", "R1", "x.py")
    params = {"plans": [a], "phase": "prep", "named_set": True}
    _record(tmp_path, params)
    reply, got = _record(tmp_path, params)
    assert got == reply["sidecars"] == ["docs/plans/a.seam.yaml"]


def test_no_collection_open_is_a_noop(tmp_path):
    a = _plan(tmp_path, "a", "R1", "x.py")
    (tmp_path / ".git").mkdir(exist_ok=True)
    reply = op._record_handler({"plans": [a], "phase": "prep", "named_set": True}, repo_root=tmp_path)
    assert reply["sidecars"] == ["docs/plans/a.seam.yaml"]


def test_check_declares_nothing(tmp_path):
    a = _plan(tmp_path, "a", "R1", "x.py")
    (tmp_path / ".git").mkdir(exist_ok=True)
    with declared_writes.collecting() as got:
        op._check_handler({"plans": [a], "phase": "prep", "named_set": True}, repo_root=tmp_path)
    assert got == []
