"""dispatch.land_run judges the delivery note's reach claims against the landing tree before the commit."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR
from coordinator_core.ops.dispatch_emit.reach_claims import REACH_CLAIM_REFUSED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_TREE = {
    "pkg/__init__.py": "",
    "pkg/lib.py": "def load_hookimpl(q):\n    return q\n",
    "pkg/wired.py": "from pkg.lib import load_hookimpl\n\n\ndef project_referencers(q):\n    return load_hookimpl(q)\n",
    "pkg/loose.py": "def project_trace(q):\n    return q\n",
}


def _stage(repo, note: str | None) -> list:
    for rel, text in _TREE.items():
        prod._author(repo, rel, text)
    prod._ask_script(repo, *_TREE)
    if note is not None:
        prod._write(repo, f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md", note)
    return list(_TREE)


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


def test_an_unreachable_claim_refuses_with_head_unmoved_and_the_token_redeemable(repo):  # noqa: F811
    _stage(repo, "`project_trace` reaches `load_hookimpl`\n")
    token = run_token.mint(repo, prod.RUN, prod.SCRIPT, session_id=prod.SELF)
    head = prod._git(["rev-parse", "HEAD"], repo).strip()

    out = prod._land(repo, run_token=token)

    assert out["committed"] is False and out["refused"] == REACH_CLAIM_REFUSED, out
    assert out["reach_claims"]["unreachable"], out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head
    assert run_token.admits(repo, prod.RUN, token, prod.SCRIPT)


def test_the_wired_twin_lands_and_reports_the_checked_claim(repo):  # noqa: F811
    _stage(repo, "`project_referencers` reaches `load_hookimpl`\n")

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert out["reach_claims"] == {"checked": 1, "unreachable": [], "undecidable": []}


def test_an_undecidable_claim_lands_and_is_listed(repo):  # noqa: F811
    _stage(repo, "`project_referencers` reaches `no_such_symbol`\n")

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert len(out["reach_claims"]["undecidable"]) == 1 and not out["reach_claims"]["unreachable"]


@pytest.mark.parametrize("note", [None, "no claims here\n"], ids=["no-note", "no-claim"])
def test_no_note_and_no_claim_land_unchanged(repo, note):  # noqa: F811
    _stage(repo, note)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert "reach_claims" not in out


def test_the_judge_receives_the_landing_set_and_the_withheld_keys(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import reach_claims_tree

    _stage(repo, "`project_referencers` reaches `load_hookimpl`\n")
    seen: dict = {}
    real = reach_claims_tree.judge_reach_claims

    def spy(root, claims, **kw):
        seen.update(kw)
        return real(root, claims, **kw)

    monkeypatch.setattr(reach_claims_tree, "judge_reach_claims", spy)
    out = prod._land(repo)

    assert out["committed"] is True, out
    assert set(_TREE) <= set(seen["landing_paths"])
    assert list(seen["deleted_paths"]) == []
    assert set(seen["withheld_paths"]).isdisjoint(_TREE)
