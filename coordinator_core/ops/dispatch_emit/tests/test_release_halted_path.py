"""dispatch.release_halted_path: a halted run's per-path release, with a reason."""

from __future__ import annotations

import json

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import halted_hold_release, release_halted_path
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261027T000000-aaaaaa"
HELD = "src/held.py"
OTHER = "src/other.py"


def _repo(tmp_path, *, status="halted", manifest=None):
    (tmp_path / ".git").mkdir()
    rdir = record_dir(tmp_path, RUN)
    rdir.mkdir(parents=True)
    marker = {"status": status, "run_artifacts": {"withheld": {HELD: "peer-held", OTHER: "peer-held"}}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    if manifest is not None:
        (rdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _call(root, **params):
    return release_halted_path._handler(params, root / ".git")


def _marker(root):
    return json.loads((record_dir(root, RUN) / "halted.json").read_text(encoding="utf-8"))


def test_op_is_registered():
    assert get_op_handler(release_halted_path.OP_NAME) is not None


def test_release_drops_only_that_path_and_records_the_reason(tmp_path):
    root = _repo(tmp_path)

    out = _call(root, run_id=RUN, path=HELD, reason="criterion met at HEAD")

    assert out == {"ok": True, "run_id": RUN, "path": HELD, "released": True, "reason": "criterion met at HEAD"}
    marker = _marker(root)
    assert marker["status"] == "halted"
    assert marker["run_artifacts"]["withheld"] == {OTHER: "peer-held"}
    assert marker["released_paths"][HELD]["reason"] == "criterion met at HEAD"
    assert halted_hold_release.held_paths(root, RUN, "ask-20261027T000001-bbbbbb") == frozenset({OTHER})


def test_a_repeat_release_is_a_no_op_reply(tmp_path):
    root = _repo(tmp_path)
    _call(root, run_id=RUN, path=HELD, reason="r")

    again = _call(root, run_id=RUN, path=HELD, reason="other words")

    assert again["ok"] is True and again["released"] is False and again["reason"] == "r"


def test_a_blank_reason_is_refused_and_writes_nothing(tmp_path):
    root = _repo(tmp_path)
    before = _marker(root)

    out = _call(root, run_id=RUN, path=HELD, reason="  ")

    assert out["ok"] is False and "reason" in out["error"]
    assert _marker(root) == before


def test_a_path_the_run_neither_withholds_nor_declares_is_refused(tmp_path):
    root = _repo(tmp_path)
    before = _marker(root)

    out = _call(root, run_id=RUN, path="src/unrelated.py", reason="r")

    assert out["ok"] is False
    assert _marker(root) == before


def test_a_manifest_declared_path_released_per_path_is_dropped_from_holds(tmp_path):
    root = _repo(tmp_path, manifest={"rows": [{"id": "C1", "writes": ["docs/declared.md"]}]})

    out = _call(root, run_id=RUN, path="docs/declared.md", reason="dirt is the landing run's")

    assert out["ok"] is True and out["released"] is True
    holds = [("docs/declared.md", RUN), (HELD, RUN)]
    assert halted_hold_release.without_released_paths(root, holds) == [(HELD, RUN)]


def test_a_run_that_is_not_halted_is_refused(tmp_path):
    root = _repo(tmp_path, status="landed")

    out = _call(root, run_id=RUN, path=HELD, reason="r")

    assert out["ok"] is False and "halted" in out["error"]
