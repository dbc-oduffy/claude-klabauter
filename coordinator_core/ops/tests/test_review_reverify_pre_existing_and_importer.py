"""review.reverify never reverts for a red in its own pre_existing set or a foreign test file, and
never removes a module a kept edited file imports."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    _revert, world,
)

NEW = "tests/test_new.py"
NEW_BODY = "def test_n():\n    assert False\n"


def _create(root, monkeypatch, files):
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {rel: "a1" for rel in files})


def test_id_recorded_pre_existing_by_an_earlier_call_is_never_reverted(world, monkeypatch):
    root, _orig, _ = world
    _create(root, monkeypatch, {NEW: NEW_BODY})
    node = f"{NEW}::test_n"
    first = _revert(root, [node], failing_at_base=[node])
    assert first["pre_existing"] == [node] and first["reverted"] == []
    second = _revert(root, [node])
    assert second["error"] is None
    assert second["reverted"] == [] and second["pre_existing"] == [node]
    assert (root / NEW).read_text(encoding="utf-8") == NEW_BODY
    record = json.loads((root / second["record"]).read_text(encoding="utf-8"))
    assert record["pre_existing"] == [node] and record["reverted"] == []


def test_id_not_in_pre_existing_is_still_reverted(world, monkeypatch):
    root, _orig, _ = world
    _create(root, monkeypatch, {NEW: NEW_BODY})
    out = _revert(root, [f"{NEW}::test_n"], failing_at_base=[f"{NEW}::test_other"])
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [(NEW, "removed")]


def test_failing_id_in_a_foreign_test_file_reverts_nothing_it_names(world, monkeypatch):
    root, _orig, _ = world
    _create(root, monkeypatch, {
        "pkg/newmod.py": "X = 1\n",
        "tests/test_sib.py": 'def test_s():\n    assert "pkg/newmod.py"\n',
    })
    monkeypatch.setattr(
        review_edits, "review_writes",
        lambda *_a, **_k: {"pkg/newmod.py": "mine", "tests/test_sib.py": "sib"},
    )
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {"tests/test_sib.py": "run-b"})
    out = _revert(root, ["tests/test_sib.py::test_s"])
    assert out["reverted"] == []
    assert out["foreign"] == ["tests/test_sib.py::test_s"]
    assert (root / "pkg/newmod.py").exists()


def test_removing_a_created_module_a_kept_edited_file_imports_is_refused(world, monkeypatch):
    root, _orig, _ = world
    _create(root, monkeypatch, {"pkg/newmod.py": "X = 1\n"})
    (root / "pkg/mod.py").write_text("from pkg.newmod import X\nVALUE = X\n", encoding="utf-8")
    out = _revert(root, ["pkg/newmod.py::test_x"])
    assert out["error"] == "revert would leave pkg/mod.py importing removed pkg/newmod.py"
    assert out["reverted"] == []
    assert (root / "pkg/newmod.py").exists()


def test_removing_a_created_module_nothing_kept_imports_still_proceeds(world, monkeypatch):
    root, _orig, _ = world
    _create(root, monkeypatch, {"pkg/newmod.py": "X = 1\n"})
    (root / "pkg/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    out = _revert(root, ["pkg/newmod.py::test_x"])
    assert out["error"] is None
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [("pkg/newmod.py", "removed")]
