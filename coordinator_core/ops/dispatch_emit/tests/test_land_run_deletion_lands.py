"""A run whose declared write is a deletion lands in one commit: the measured-tree gate treats a path absent
from both the record and the landing as equal, and the run-dirt withhold never names a deleted path."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.halted_record_eviction import RUN_DIRT_UNCOMMITTED, _withhold_run_dirt
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _record(root, removed):
    rec = measured_rev_record_path(root, RUN)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({"sha": "0" * 40, "base_sha": "0" * 40, "paths": [], "removed": list(removed), "tree": "0" * 40,
                    "blobs": {}}),
        encoding="utf-8",
    )


def _delete_declared_a(root):
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "track a.py")
    (root / "a.py").unlink()


def test_a_declared_deletion_the_record_removes_lands_in_one_commit(repo):  # noqa: F811
    _delete_declared_a(repo)
    _record(repo, ["a.py"])

    out = _call(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert RUN_DIRT_UNCOMMITTED not in json.dumps(out.get("withheld", {}))
    assert "a.py" in _git(repo, "show", "--name-status", "--format=", out["sha"])
    assert _git(repo, "ls-tree", "-r", "--name-only", out["sha"]).count("a.py") == 0


def test_a_declared_deletion_the_record_never_lists_refuses_while_head_holds_it(repo):  # noqa: F811
    _delete_declared_a(repo)
    _record(repo, [])

    out = _call(repo)

    assert out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == ["a.py"]


def test_a_deletion_absent_from_the_record_and_head_is_equal_while_one_head_holds_still_differs(repo):  # noqa: F811
    record = {"blobs": {}, "removed": []}

    assert land_run._measured_mismatch(repo, record, [], ["gone.md"], {}) == []
    assert land_run._measured_mismatch(repo, record, [], ["README.md"], {}) == ["README.md"]


def test_the_withhold_skips_a_landed_deletion_and_names_other_run_dirt():
    withheld: dict = {}

    _withhold_run_dirt(withheld, ["gone.md", "dirty.md"], {"gone.md", "dirty.md"}, {"gone.md"})

    assert withheld == {"dirty.md": RUN_DIRT_UNCOMMITTED}
