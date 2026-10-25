"""A kept.json carrying only `post_checkpoint` is read by every kept-fingerprint reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import sibling_claims
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_halted_hold_release as thr
from coordinator_core.ops.dispatch_emit.tests import test_halted_hunk_attribution as tha
from coordinator_core.ops.dispatch_emit.tests.test_halted_hunk_attribution import root  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261024T000000-aaaaaa"


def _kept(record: Path, post: dict, chunks: dict | None = None) -> None:
    record.mkdir(parents=True, exist_ok=True)
    (record / "kept.json").write_text(
        json.dumps({"chunks": chunks or {}, "plan_gate": None, "post_checkpoint": post}), encoding="utf-8"
    )


def test_rule_2b_holds_a_path_whose_blob_is_only_in_post_checkpoint(root):
    tha._tree(root, "landing wrote this\n")
    tha._halted_run(root, tha._sha("old\n"))
    tha._landing(root, tha._sha("old\n"))
    assert tha._held(root) == set()
    _kept(root / "state" / "warp" / tha.HALTED, {tha.PATH: tha._sha("landing wrote this\n")})
    assert tha._held(root) == {tha.PATH}
    _kept(root / "state" / "warp" / tha.HALTED, {tha.PATH: tha._sha("some other bytes\n")})
    assert tha._held(root) == set()


def test_own_dirt_true_only_for_the_post_checkpoint_blob(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "a.py").write_bytes(b"review hunk\n")
    rec = tmp_path / "state" / "warp" / RUN
    _kept(rec, {"p/a.py": blob_sha(b"review hunk\n")})
    assert sibling_claims._own_dirt(tmp_path, RUN, "p/a.py") is True
    _kept(rec, {"p/a.py": blob_sha(b"something else\n")})
    assert sibling_claims._own_dirt(tmp_path, RUN, "p/a.py") is False


def test_hold_released_once_post_checkpoint_blob_is_at_head(tmp_path):
    h, _c = thr._kept_repo(tmp_path, thr.KEPT, thr.KEPT)
    rec = tmp_path / "state/warp" / h
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    kept_blob = thr._git(tmp_path, "hash-object", "-w", "--stdin", stdin=thr.KEPT)
    _kept(rec, {"a.py": kept_blob})
    assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py"}


def test_chunk_blob_wins_over_post_checkpoint_blob_for_the_hold_release(tmp_path):
    h, _c = thr._kept_repo(tmp_path, thr.KEPT, thr.KEPT)
    rec = tmp_path / "state/warp" / h
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    kept_blob = thr._git(tmp_path, "hash-object", "-w", "--stdin", stdin=thr.KEPT)
    _kept(rec, {"a.py": kept_blob}, chunks={"X1": {"paths": {"a.py": blob_sha(b"other\n")}}})
    # chunk blob wins over post_checkpoint: today's single-blob semantics, and it is not at HEAD
    assert hr.kept_at_head_paths(tmp_path, rec, halted) == set()
