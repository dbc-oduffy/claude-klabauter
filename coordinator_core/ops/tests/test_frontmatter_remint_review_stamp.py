"""frontmatter.remint review_stamp family: delegates to review_stamp mint / write_register."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import review_stamp
from coordinator_core.ops.frontmatter_remint import review_stamp_fields as m
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved

SIZING_REL = "state/sizings/s.yaml"
STAMP = (
    "review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
    "  criterion:\n    status: met\n    observation: o\n    sidecar: null\n"
    "  stamped_at: '2026-10-01T00:00:00Z'\n"
)


def _row(rid, status: str = "met", wired: bool = True):
    return {
        "id": rid, "source_text": f"text {rid}", "anchor": "§1", "surface": "internal",
        "status": status, "claimed_by": [], "wired": wired, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }


def _plan(tmp_path: Path, rows=None, *, stamp: bool = True, claims: bool = True) -> Path:
    if rows is not None:
        (tmp_path / "state/sizings").mkdir(parents=True)
        (tmp_path / SIZING_REL).write_text(
            "schema: sizing-object\n" + yaml.safe_dump(
                {"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}},
                sort_keys=False),
            encoding="utf-8")
    fm = "title: T\nstatus: executing\n"
    if claims:
        fm += f"register_claims:\n  sizing: {SIZING_REL}\n  rows: [A]\n"
    if stamp:
        fm += STAMP
    plan = tmp_path / "p.md"
    plan.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return plan


def test_resolve_is_empty_resolved(tmp_path: Path) -> None:
    plan = _plan(tmp_path, stamp=False)
    assert m.resolve(plan, {}, tmp_path) == Resolved({})


def test_missing_stamp_calls_mint_with_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, stamp=False)
    seen = {}

    def fake_mint(p, root, **kw):
        seen.update(path=p, root=root, **kw)
        return {}

    monkeypatch.setattr(review_stamp, "mint", fake_mint)
    assert m.write(plan, tmp_path) == Resolved({})
    assert seen == {"path": plan, "root": tmp_path, "build_test_path": None, "repair": True}


def test_mint_refusal_is_review_run_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, stamp=False)

    def boom(*a, **k):
        raise review_stamp.MintRefusal("review-stamp: no run record")

    monkeypatch.setattr(review_stamp, "mint", boom)
    before = plan.read_bytes()
    res = m.write(plan, tmp_path)
    assert res == Refused("review-run-absent", "review-stamp: no run record")
    assert plan.read_bytes() == before


def test_register_written_then_unchanged(tmp_path: Path) -> None:
    plan = _plan(tmp_path, [_row("A")])
    assert m.write(plan, tmp_path) == Resolved({})
    reg = yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])["review_stamp"]["register"]
    assert reg["sizing"] == SIZING_REL and reg["open"] == 0
    after = plan.read_bytes()
    assert m.write(plan, tmp_path) == Resolved({})
    assert plan.read_bytes() == after


def test_register_absent_refuses(tmp_path: Path) -> None:
    plan = _plan(tmp_path, claims=False)
    before = plan.read_bytes()
    res = m.write(plan, tmp_path)
    assert isinstance(res, Refused) and res.reason == "register-absent"
    assert plan.read_bytes() == before


def test_register_unsettled_refuses(tmp_path: Path) -> None:
    plan = _plan(tmp_path, [_row("A", "open", False)])
    before = plan.read_bytes()
    res = m.write(plan, tmp_path)
    assert isinstance(res, Refused) and res.reason == "register-unsettled"
    assert plan.read_bytes() == before


def test_register_no_stamp_mapping() -> None:
    r = m._register_refusal("review-stamp: p.md has no review_stamp; run review-stamp mint")
    assert r.reason == "register-no-stamp"


def test_stamp_required_before_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, [_row("A")], stamp=False)
    calls = []
    monkeypatch.setattr(review_stamp, "mint", lambda *a, **k: calls.append("mint"))
    monkeypatch.setattr(review_stamp, "write_register", lambda *a, **k: calls.append("register"))
    m.write(plan, tmp_path)
    assert calls == ["mint"]
