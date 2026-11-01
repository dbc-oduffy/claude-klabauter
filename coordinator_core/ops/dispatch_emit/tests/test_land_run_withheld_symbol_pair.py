"""A landing reading `m.note_ids` while only an uncommitted `m.py` defines it.

A product landing carries `m.py` into the commit unless another run or a peer holds it; a halted
landing whose `m.py` a peer holds lands `u.py` with an `import-closure-untracked` provenance entry.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as halt
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import repo as halt_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REASON = landing_integrity.IMPORT_CLOSURE_UNTRACKED
M_BASE = "def old():\n    pass\n"
M_NEW = M_BASE + "\n\ndef note_ids():\n    return []\n"
U_BASE = "def run():\n    return 1\n"
U_NEW = "def run():\n    from pk import m\n\n    return m.note_ids()\n"


def _seed(root, write, git) -> None:
    write(root, "pk/__init__.py", "")
    write(root, "pk/m.py", M_BASE)
    write(root, "pk/u.py", U_BASE)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed pk")


def _pair_entry(out: dict) -> dict:
    entries = [e for e in out.get("provenance") or [] if e["reason"] == REASON and e["path"] == "pk/u.py"]
    assert len(entries) == 1, out
    return entries[0]


def test_product_landing_carries_the_withheld_symbol_file(product_repo):
    _seed(product_repo, prod._write, lambda r, *a: prod._git(list(a), r))
    prod._author(product_repo, "pk/u.py", U_NEW)
    prod._author(product_repo, "pk/m.py", M_NEW)
    prod._declare(product_repo, "pk/u.py")
    prod._record_dir(product_repo)

    out = prod._land(product_repo, inline_review=reviewed())

    assert out["committed"] is True and out.get("refused") is None, out
    entry = _pair_entry(out)
    assert "note_ids" in entry["detail"] and "pk/m.py" in entry["detail"], entry
    assert "pk/m.py" in prod._tree(product_repo) and "pk/u.py" in prod._tree(product_repo)


def test_halted_record_landing_records_the_pair_naming_both_paths(halt_repo, monkeypatch):
    repo = halt_repo
    _seed(repo, halt._write, halt._git)
    halt._record(repo)
    halt._chunks(repo, {"C1": ["pk/u.py"], "C2": ["pk/m.py"]})
    halt._write(repo, "pk/u.py", U_NEW)
    halt._write(repo, "pk/m.py", M_NEW)

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        holds.extend((p, "peer-sid") for p in paths if p == "pk/m.py")
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["committed"] is True, out
    names = halt._names(repo, out["sha"])
    assert "pk/u.py" in names and "pk/m.py" not in names, out
    entry = _pair_entry(out)
    assert "note_ids" in entry["detail"] and "pk/m.py" in entry["detail"], entry


def test_product_landing_does_not_carry_a_peer_held_symbol_file(product_repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import land_run

    _seed(product_repo, prod._write, lambda r, *a: prod._git(list(a), r))
    prod._author(product_repo, "pk/u.py", U_NEW)
    prod._author(product_repo, "pk/m.py", M_NEW)
    prod._declare(product_repo, "pk/u.py")
    prod._record_dir(product_repo)
    real = land_run._peer_held

    def held(root, paths, *a, **kw):
        out = real(root, paths, *a, **kw)
        out.update({p: ["peer-sid"] for p in paths if p == "pk/m.py"})
        return out

    monkeypatch.setattr(land_run, "_peer_held", held)

    out = prod._land(product_repo, inline_review=reviewed())

    assert out["committed"] is True, out
    assert "pk/m.py" not in prod._tree(product_repo)
    _pair_entry(out)
