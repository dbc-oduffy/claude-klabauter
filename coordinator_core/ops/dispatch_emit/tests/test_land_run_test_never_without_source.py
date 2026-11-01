"""A landing never commits a test while the source it exercises stays out of the commit.

A peer-held `m.py` stays out; the test that reads `m.note_ids` is dropped and named withheld, while a
non-test importer of the same `m.py` still lands with its `import-closure-untracked` provenance entry.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, land_run
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REASON = landing_integrity.IMPORT_CLOSURE_UNTRACKED
M_BASE = "def old():\n    pass\n"
M_NEW = M_BASE + "\n\ndef note_ids():\n    return []\n"
USER_BASE = "def run():\n    return 1\n"
USER_NEW = "def run():\n    from pk import m\n\n    return m.note_ids()\n"
TEST_BASE = "def test_run():\n    assert True\n"
TEST_NEW = "def test_run():\n    from pk import m\n\n    assert m.note_ids() == []\n"


def _stage(repo, monkeypatch, *, hold_m: bool) -> dict:
    prod._write(repo, "pk/__init__.py", "")
    prod._write(repo, "pk/m.py", M_BASE)
    prod._write(repo, "pk/u.py", USER_BASE)
    prod._write(repo, "pk/test_u.py", TEST_BASE)
    prod._git(["add", "."], repo)
    prod._git(["commit", "-q", "-m", "seed pk"], repo)
    prod._author(repo, "pk/u.py", USER_NEW)
    prod._author(repo, "pk/test_u.py", TEST_NEW)
    prod._author(repo, "pk/m.py", M_NEW)
    prod._declare(repo, "pk/u.py", "pk/test_u.py")
    prod._record_dir(repo)
    if hold_m:
        real = land_run._peer_held

        def held(root, paths, *a, **kw):
            out = real(root, paths, *a, **kw)
            out.update({p: ["peer-sid"] for p in paths if p == "pk/m.py"})
            return out

        monkeypatch.setattr(land_run, "_peer_held", held)
    return prod._land(repo, inline_review=reviewed())


def test_test_importing_a_held_source_is_dropped_and_named(product_repo, monkeypatch):
    out = _stage(product_repo, monkeypatch, hold_m=True)

    assert out["committed"] is True and out.get("refused") is None, out
    tree = prod._tree(product_repo)
    assert "pk/test_u.py" not in tree and "pk/m.py" not in tree
    assert out["withheld"]["pk/test_u.py"] == f"{land_run.TEST_WITHOUT_SOURCE}: pk/m.py", out
    assert not [e for e in out.get("provenance") or [] if e["path"] == "pk/test_u.py"], out


def test_non_test_importer_of_the_held_source_still_lands_with_provenance(product_repo, monkeypatch):
    out = _stage(product_repo, monkeypatch, hold_m=True)

    assert "pk/u.py" in prod._tree(product_repo)
    entries = [e for e in out.get("provenance") or [] if e["reason"] == REASON and e["path"] == "pk/u.py"]
    assert len(entries) == 1 and "pk/m.py" in entries[0]["detail"], out


def test_unheld_source_is_carried_so_the_test_lands(product_repo, monkeypatch):
    out = _stage(product_repo, monkeypatch, hold_m=False)

    assert out["committed"] is True, out
    tree = prod._tree(product_repo)
    assert {"pk/test_u.py", "pk/m.py", "pk/u.py"} <= set(tree)
    assert "pk/test_u.py" not in (out.get("withheld") or {})


@pytest.mark.parametrize("rel,expect", [
    ("pk/test_a.py", True), ("pk/a_test.py", True), ("pk/tests/helper.py", True),
    ("pk/a.py", False), ("pk/contest.py", False), ("pk/test_a.txt", False),
])
def test_is_test_path(rel, expect):
    assert land_run._is_test_path(rel) is expect
