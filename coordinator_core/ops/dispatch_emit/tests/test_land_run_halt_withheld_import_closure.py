"""A halt landing commits only run docs; the importer-of-withheld cascade names `import-closure-untracked`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run_halt, landing_integrity
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as halt
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import repo as halt_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REASON = landing_integrity.IMPORT_CLOSURE_UNTRACKED


def _author(root, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


def _callee_chunk_withheld_by_the_integrity_pass(root) -> None:
    halt._record(root)
    halt._chunks(root, {"C1": ["a.py"], "C2": ["pkg/b.py", "broken.py"]})
    _author(root, "a.py", "from pkg.b import f\n")
    _author(root, "pkg/__init__.py", "")
    _author(root, "pkg/b.py", "def f():\n    return 1\n")
    _author(root, "broken.py", "def (:\n")


def test_importer_of_a_chunk_the_integrity_pass_withholds_is_withheld(halt_repo):
    repo = halt_repo
    _callee_chunk_withheld_by_the_integrity_pass(repo)

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    for p in ("a.py", "pkg/b.py", "broken.py"):
        assert out["withheld"][p] == land_run_halt.HALT_RECORD_ONLY, out
    names = halt._names(repo, out["sha"])
    assert not {"a.py", "pkg/b.py", "broken.py"} & set(names), names
    assert any(n.endswith("/halted.json") for n in names), names


def test_cascade_withholds_the_importer_of_a_withheld_callee_by_reason(halt_repo):
    repo = halt_repo
    _callee_chunk_withheld_by_the_integrity_pass(repo)
    withheld = {"pkg/b.py": "peer-held", "broken.py": REASON}
    owner = {"a.py": "C1", "pkg/b.py": "C2", "broken.py": "C2"}

    changed = land_run_halt._withhold_importers_of_withheld(repo, ["a.py", "pkg/b.py", "broken.py"], [], owner, withheld)

    assert changed is True and withheld["a.py"] == REASON, withheld


def test_cascade_pass_is_a_no_op_when_no_finding_names_a_withheld_path(halt_repo):
    repo = halt_repo
    _author(repo, "a.py", "from wt_pkg.mod import X\n")
    _author(repo, "wt_pkg/__init__.py", "")
    _author(repo, "wt_pkg/mod.py", "X = 1\n")
    withheld = {"other.py": "peer-held"}

    changed = land_run_halt._withhold_importers_of_withheld(repo, ["a.py"], [], {"a.py": "C1"}, withheld)

    assert changed is False and withheld == {"other.py": "peer-held"}


def test_halt_landing_withholds_an_importer_and_its_callee_chunk_alike(halt_repo):
    repo = halt_repo
    halt._record(repo)
    halt._chunks(repo, {"C1": ["a.py"], "C2": ["pkg/b.py"]})
    _author(repo, "a.py", "from pkg.b import f\n")
    _author(repo, "pkg/__init__.py", "")
    _author(repo, "pkg/b.py", "def f():\n    return 1\n")

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["committed"] is True, out
    assert out["withheld"]["a.py"] == land_run_halt.HALT_RECORD_ONLY, out
    assert out["withheld"]["pkg/b.py"] == land_run_halt.HALT_RECORD_ONLY, out
    assert not {"a.py", "pkg/b.py"} & set(halt._names(repo, out["sha"]))


def test_product_landing_carries_an_importer_with_a_name_in_a_landing_chunk(product_repo):
    prod._author(product_repo, "a.py", "from pkg.b import f\n")
    prod._author(product_repo, "pkg/__init__.py", "")
    prod._author(product_repo, "pkg/b.py", "def f():\n    return 1\n")
    prod._declare(product_repo, "a.py", "pkg/b.py", "pkg/__init__.py")
    prod._record_dir(product_repo)

    out = prod._land(product_repo, inline_review=reviewed())

    assert out["committed"] is True and out.get("refused") is None, out
    assert "a.py" not in out.get("withheld", {}), out
    tree = prod._tree(product_repo)
    assert "a.py" in tree and "pkg/b.py" in tree
