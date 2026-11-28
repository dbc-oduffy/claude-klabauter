"""The default `--export-run` clean export holds a run's own declared paths at worktree content even when a live peer also declares them; a peer-only path stays at HEAD."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runInc"
SEED = "tests/test_seed.py"
NEW = "plugin/game-dev/check.json"
EDITED = "plugin/game-dev/staff.md"
PEER_ONLY = "tests/peer.md"  # beside the seed: the scoped export carries a seed directory's data files at HEAD
NEW_BYTES = b"run authored"
EDITED_HEAD = b"staff head"
EDITED_RUN = b"staff head run edit"
PEER_HEAD = b"peer head"
PEER_DIRTY = b"peer head peer edit"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, data):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _read_expr(rel):
    """A probe expression reading `rel` through os.path.join, so the `-c` token carries no separator for the export closure to read as a test path."""
    return f"open(__import__('os').path.join(*{tuple(rel.split('/'))!r}), 'rb').read()"


@pytest.fixture
def incident(tmp_path, monkeypatch):
    """Nothing is committed past the base: the run declares an untracked new file and an edited tracked file,
    a live peer declares both, and a third tracked file is dirty under the peer alone."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    for rel, data in ((SEED, b"def test_seed(): pass"), (EDITED, EDITED_HEAD), (PEER_ONLY, PEER_HEAD)):
        _put(root, rel, data)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")

    _put(root, EDITED, EDITED_RUN)
    _put(root, NEW, NEW_BYTES)
    _put(root, PEER_ONLY, PEER_DIRTY)

    record = cov.measured_rev_record_path(root, RUN)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"paths": [SEED]}), encoding="utf-8")
    record.with_name("manifest.json").write_text(
        json.dumps({"review_declared_paths": [NEW, EDITED]}), encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {NEW: "peer", EDITED: "peer", PEER_ONLY: "peer"})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    monkeypatch.setattr(cov, "_snapshot_scope", lambda *_a, **_k: [])
    return root


def _export(root, probe):
    return cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--", sys.executable, "-c", probe])


def test_own_delta_keeps_the_untracked_new_file_and_the_edited_file_a_peer_also_declares(incident):
    paths, _overrides = cov.own_delta(incident, RUN, [SEED])

    assert NEW in paths and EDITED in paths and PEER_ONLY not in paths


def test_the_default_export_holds_the_new_file_and_the_edited_file_at_worktree_bytes_uncommitted(incident):
    probe = f"raise SystemExit(0 if ({_read_expr(NEW)}, {_read_expr(EDITED)}) == ({NEW_BYTES!r}, {EDITED_RUN!r}) else 3)"

    assert _export(incident, probe) == 0
    assert _git(incident, "status", "--porcelain", "--", NEW).startswith("??"), "the export must not commit or stage the run's paths"


def test_a_tracked_shared_path_whose_split_fails_reaches_the_export_at_worktree_bytes_not_head(incident, monkeypatch):
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {})
    probe = f"raise SystemExit(0 if {_read_expr(EDITED)} == {EDITED_RUN!r} else 3)"

    paths, overrides = cov.own_delta(incident, RUN, [SEED])

    assert EDITED in paths and EDITED not in overrides
    assert _export(incident, probe) == 0


def test_a_path_only_a_halted_run_names_is_split_to_own_hunks_and_an_unnamed_path_is_not(incident, monkeypatch):
    from coordinator_core.ops.dispatch_emit import co_tenant_owner

    seen = []
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(co_tenant_owner, "peer_hunk_runs", lambda _r, p, *, exclude_run_id: ("haltedRun",) if p == EDITED else ())
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda _r, _run, paths: seen.append(list(paths)) or {p: b"own" for p in paths})

    paths, overrides = cov.own_delta(incident, RUN, [SEED])

    assert seen == [[EDITED]]
    assert overrides == {EDITED: b"own"} and NEW in paths and NEW not in overrides


def test_a_path_only_a_peer_declares_stays_at_head_in_the_default_export(incident):
    probe = f"raise SystemExit(0 if {_read_expr(PEER_ONLY)} == {PEER_HEAD!r} else 3)"

    assert _export(incident, probe) == 0
