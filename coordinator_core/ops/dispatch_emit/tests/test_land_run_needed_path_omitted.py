"""A landing file that calls a changed top-level name of a run-edited file the commit omits.

A free file rides the commit; a file a peer or duplicate run holds refuses the landing and leaves HEAD
unmoved; an omitted file whose changed names nothing in the landing reads stays out without a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CALLEE = "pk/m.py"
CALLER = "pk/u.py"
M_BASE = "def f(a):\n    return a\n\n\ndef g():\n    return 0\n"
M_SIGNATURE = M_BASE.replace("def f(a):", "def f(a, base_ref=None):")
M_UNRELATED = M_BASE.replace("return 0", "return 1")
U_BASE = "from pk.m import f\n\n\ndef run():\n    return f(1)\n"
U_NEW = U_BASE.replace("f(1)", 'f(1, base_ref="main")')


def _seed(root: Path) -> None:
    prod._write(root, "pk/__init__.py", "")
    prod._write(root, CALLEE, M_BASE)
    prod._write(root, CALLER, U_BASE)
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "seed pk"], root)


def _run(root: Path, callee_text: str, monkeypatch, *, holder: str | None) -> dict:
    """The run declares and edits ``CALLER`` only; ``CALLEE`` is edited and in the review-prep whole diff."""
    _seed(root)
    prod._author(root, CALLER, U_NEW)
    prod._author(root, CALLEE, callee_text)
    prod._declare(root, CALLER)
    prod._record_dir(root)
    digest = touch_record.compute_content_hash(root / CALLEE)
    monkeypatch.setattr(land_run, "_reconcile_prep", lambda *a, **k: ({CALLEE: digest}, None, True))
    if holder:
        real = land_run._peer_held

        def held(r, paths, *a, **kw):
            out = real(r, paths, *a, **kw)
            out.update({p: [holder] for p in paths if p == CALLEE})
            return out

        monkeypatch.setattr(land_run, "_peer_held", held)
    return prod._land(root, inline_review=reviewed())


def test_free_callee_with_a_changed_signature_rides_the_commit(product_repo, monkeypatch):
    out = _run(product_repo, M_SIGNATURE, monkeypatch, holder=None)

    assert out["committed"] is True and out.get("refused") is None, out
    tree = prod._tree(product_repo)
    assert CALLEE in tree and CALLER in tree, tree


def test_duplicate_run_holding_the_callee_refuses_and_leaves_head(product_repo, monkeypatch):
    out = _run(product_repo, M_SIGNATURE, monkeypatch, holder="dup-run-session")

    assert out["committed"] is False and out["refused"] == land_run.NEEDED_PATH_OMITTED, out
    assert out["paths"] == [CALLEE], out
    entry = out["needed_by"][0]
    assert entry["path"] == CALLEE and entry["needed_by"] == [{"importer": CALLER, "name": "f"}], out
    assert entry["holder"], out
    assert prod._git(["log", "-1", "--format=%s"], product_repo).strip() == "seed pk"


def test_callee_whose_unrelated_name_changed_lands_without_it(product_repo, monkeypatch):
    out = _run(product_repo, M_UNRELATED, monkeypatch, holder="dup-run-session")

    assert out["committed"] is True and out.get("refused") is None, out
    tree = prod._tree(product_repo)
    assert CALLER in tree and CALLEE not in tree, tree


def test_gate_splits_free_from_barred_and_names_the_withhold(product_repo):
    root = product_repo
    _seed(root)
    prod._write(root, CALLER, U_NEW)
    prod._write(root, CALLEE, M_SIGNATURE)
    kw = dict(
        session_id=prod.SELF, rid=prod.RUN, script_path=prod.SCRIPT, plan_rel=None, run_base=None,
        minted_before=None, own_run_ids=(prod.RUN, None),
    )

    carry, refusal = land_run._needed_path_gate(root, [CALLER], [], {CALLEE}, reasons={}, barred=set(), **kw)
    assert carry == [CALLEE] and refusal == []

    carry, refusal = land_run._needed_path_gate(
        root, [CALLER], [], {CALLEE}, reasons={CALLEE: "withheld by run-x"}, barred={CALLEE}, **kw,
    )
    assert carry == [] and refusal[0]["holder"] == "withheld by run-x", refusal
