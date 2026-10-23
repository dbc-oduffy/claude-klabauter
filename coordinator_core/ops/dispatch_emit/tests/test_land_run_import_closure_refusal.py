"""A product landing and a halt landing refuse a .py importing a worktree-only module before commit_paths."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as halt
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import repo as halt_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REASON = landing_integrity.IMPORT_CLOSURE_UNTRACKED
IMPORTER = "from wt_pkg.mod import X\n"


def _worktree_only_module(root) -> None:
    (root / "wt_pkg").mkdir(exist_ok=True)
    (root / "wt_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "wt_pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")


def test_product_landing_refuses_and_head_stays(product_repo):
    prod._author(product_repo, "src/a.py", IMPORTER)
    _worktree_only_module(product_repo)
    prod._declare(product_repo, "src/a.py")
    prod._record_dir(product_repo)
    head = prod._git(["rev-parse", "HEAD"], product_repo).strip()

    out = prod._land(product_repo)

    assert REASON in str(out), out
    assert out.get("committed") is not True, out
    assert prod._git(["rev-parse", "HEAD"], product_repo).strip() == head


def test_product_landing_without_the_import_lands(product_repo):
    prod._author(product_repo, "src/a.py", "X = 1\n")
    _worktree_only_module(product_repo)
    prod._declare(product_repo, "src/a.py")
    prod._record_dir(product_repo)

    out = prod._land(product_repo)

    assert out["committed"] is True, out
    assert "src/a.py" in prod._tree(product_repo)


def test_halt_landing_withholds_the_importing_chunk_and_lands_the_rest(halt_repo):
    repo = halt_repo
    halt._record(repo)
    halt._chunks(repo, {"C1": ["bad.py"], "C2": ["good.py"]})
    (repo / "bad.py").write_text(IMPORTER, encoding="utf-8")
    _worktree_only_module(repo)
    head = halt._git(repo, "rev-parse", "HEAD").strip()

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["withheld"] == {"bad.py": REASON}, out
    assert out["committed"] is True
    names = halt._names(repo, out["sha"])
    assert "good.py" in names and "bad.py" not in names
    assert halt._git(repo, "rev-parse", "HEAD~1").strip() == head


def test_halt_landing_without_the_import_lands(halt_repo):
    repo = halt_repo
    halt._record(repo)
    halt._chunks(repo, {"C1": ["bad.py"]})
    _worktree_only_module(repo)

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["committed"] is True, out
    assert "withheld" not in out
    assert "bad.py" in halt._names(repo, out["sha"])


def _seed_module_and_importer(root, importer_text: str) -> None:
    prod._write(root, "rp/__init__.py", "")
    prod._write(root, "rp/helper.py", "X = 1\n")
    prod._write(root, "rp/user.py", importer_text)
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "seed module and importer"], root)


def _delete_module_leave_importer_dirty(root, importer_text: str) -> None:
    (root / "rp" / "helper.py").unlink()
    prod._author(root, "rp/user.py", importer_text + "# edited\n")
    prod._declare(root, "rp/helper.py", "rp/user.py")
    prod._record_dir(root)


def test_product_landing_refuses_a_deletion_a_residue_file_still_imports(product_repo):
    _seed_module_and_importer(product_repo, "from rp.helper import X\n")
    _delete_module_leave_importer_dirty(product_repo, "from rp.helper import X\n")
    head = prod._git(["rev-parse", "HEAD"], product_repo).strip()

    out = prod._land(product_repo, withhold_paths=[{"path": "rp/user.py", "reason": "peer owns it"}])

    assert REASON in str(out), out
    assert "rp/user.py" in str(out), out
    assert out.get("committed") is not True, out
    assert prod._git(["rev-parse", "HEAD"], product_repo).strip() == head


def test_product_landing_lands_a_deletion_no_residue_file_imports(product_repo):
    _seed_module_and_importer(product_repo, "Y = 2\n")
    _delete_module_leave_importer_dirty(product_repo, "Y = 2\n")

    out = prod._land(product_repo, withhold_paths=[{"path": "rp/user.py", "reason": "peer owns it"}])

    assert out["committed"] is True, out
    assert "rp/helper.py" in prod._tree(product_repo)


def _detector_raises(monkeypatch) -> None:
    from coordinator_core.authoring_leaks import import_closure

    def boom(*_a, **_k):
        raise RuntimeError("detector down")

    monkeypatch.setattr(import_closure, "detect", boom)


def test_product_landing_refuses_when_the_detector_raises(product_repo, monkeypatch):
    prod._author(product_repo, "src/a.py", "X = 1\n")
    prod._declare(product_repo, "src/a.py")
    prod._record_dir(product_repo)
    head = prod._git(["rev-parse", "HEAD"], product_repo).strip()
    _detector_raises(monkeypatch)

    out = prod._land(product_repo)

    assert REASON in str(out), out
    assert out.get("committed") is not True, out
    assert prod._git(["rev-parse", "HEAD"], product_repo).strip() == head


def test_halt_landing_withholds_every_chunk_when_the_detector_raises(halt_repo, monkeypatch):
    repo = halt_repo
    halt._record(repo)
    halt._chunks(repo, {"C1": ["a.py"], "C2": ["b.py"]})
    _detector_raises(monkeypatch)

    out = halt._call(repo, halt.LEDGER, **halt.PASSING)

    assert out["withheld"] == {"a.py": REASON, "b.py": REASON}, out
    names = halt._names(repo, out["sha"]) if out.get("sha") else set()
    assert "a.py" not in names and "b.py" not in names
    assert halt._git(repo, "ls-tree", "-r", "--name-only", "HEAD").count("a.py") == 0
