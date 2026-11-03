"""A halt landing withholds a chunk whose .py imports a name only a withheld path binds."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
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

    assert out["withheld"]["a.py"] == REASON, out
    assert "pkg/b.py" in out["withheld"] and "broken.py" in out["withheld"], out
    assert "a.py" not in halt._names(repo, out["sha"])


def test_cascade_pass_is_a_no_op_when_no_finding_names_a_withheld_path(halt_repo):
    from coordinator_core.ops.dispatch_emit import land_run_halt

    repo = halt_repo
    _author(repo, "a.py", "from wt_pkg.mod import X\n")
    _author(repo, "wt_pkg/__init__.py", "")
    _author(repo, "wt_pkg/mod.py", "X = 1\n")
    withheld = {"other.py": "peer-held"}

    changed = land_run_halt._withhold_importers_of_withheld(repo, ["a.py"], [], {"a.py": "C1"}, withheld)

    assert changed is False and withheld == {"other.py": "peer-held"}


def test_importer_of_a_name_in_a_landing_chunk_lands(halt_repo):
    repo = halt_repo
    halt._record(repo)
    halt._chunks(repo, {"C1": ["a.py"], "C2": ["pkg/b.py"]})
    _author(repo, "a.py", "from pkg.b import f\n")
    _author(repo, "pkg/__init__.py", "")
    _author(repo, "pkg/b.py", "def f():\n    return 1\n")

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["committed"] is True, out
    assert "a.py" not in out.get("withheld", {}), out
    names = halt._names(repo, out["sha"])
    assert "a.py" in names and "pkg/b.py" in names
