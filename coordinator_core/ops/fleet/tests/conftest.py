from pathlib import Path

import pytest

from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401
from coordinator_core.testing.fleet_pin import assume_an_author_machine  # noqa: F401


@pytest.fixture(autouse=True)
def _resync_sinks_write_only_under_basetemp(monkeypatch, tmp_path_factory):
    """The resync failure sinks (bug-row filer, pending store) write only under pytest's basetemp.

    Trap: many resync tests pass a fictional root like `Path("/repo")`, which on Windows resolves
    under the current drive's root — an unguarded failure path files real bug rows there.
    """
    from coordinator_core.ops.fleet import _common

    base = tmp_path_factory.getbasetemp().resolve()

    def _inside(root) -> bool:
        return Path(root).resolve().is_relative_to(base)

    persist, record = _common._persist_index_resync_failure, _common.record_pending

    def _persist(**kwargs):
        if _inside(kwargs["worktree_root"]):
            persist(**kwargs)

    def _record(worktree_root, records):
        if _inside(worktree_root):
            record(worktree_root, records)

    monkeypatch.setattr(_common, "_persist_index_resync_failure", _persist)
    monkeypatch.setattr(_common, "record_pending", _record)


@pytest.fixture(autouse=True)
def _owning_box_reads_no_live_sessions(monkeypatch, tmp_path_factory):
    """`memo_wire.resolve_owning_box` reads an empty sessions dir, never the box's live mirror.

    Trap: the resolver matches on repo basename, so a live session in any `example-retrieval-repo` checkout
    would refuse every tmp-repo memo send. Tests of the refusal re-patch it with their own dir.
    """
    from coordinator_core.ops.fleet import _memo_owning_box, memo_wire

    empty = tmp_path_factory.mktemp("no_live_sessions")
    real = _memo_owning_box.resolve_owning_box
    monkeypatch.setattr(memo_wire, "resolve_owning_box", lambda repo: real(repo, sessions_dir=str(empty)))
