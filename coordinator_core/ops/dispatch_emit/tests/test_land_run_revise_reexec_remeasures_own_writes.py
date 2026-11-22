"""A path the run's own executor rewrote after dispatch.measured_rev (a revise re-execution) or that
differs only by its EOF newline lands; a peer's rewrite of a measured path still mismatches."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import checkin_blob_sha
from coordinator_core.ops.dispatch_emit import kept_work, land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

RUN = "ask-20261121T180041-81d49a"
CODE, DOC = "pkg/gnm_register.py", "docs/synthesis.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()


def _put(root: Path, rel: str, data: bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _git(root, "hash-object", "-w", rel)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.autocrlf", "false")
    return tmp_path


def _mismatch(root: Path, measured: dict, post_blobs: dict) -> list:
    rdir = root / RECORD_DIR_ROOT / RUN
    rdir.mkdir(parents=True, exist_ok=True)
    kept_work.write(rdir, {}, None, post_blobs)
    owned = land_run._own_bytes_now(root, rdir, None, None, None, post_measure=True)
    return land_run._measured_mismatch(
        root, {"blobs": measured, "removed": []}, [CODE, DOC], [], {}, owned_now=owned,
    )


def test_own_rewrite_after_measure_and_eof_newline_delta_land(repo: Path) -> None:
    measured = {
        CODE: _put(repo, CODE, b"x = 1\n"),
        DOC: _put(repo, DOC, b"# synthesis"),
    }
    post = {CODE: _put(repo, CODE, b"x = 2\n")}
    _put(repo, DOC, b"# synthesis\n")
    assert checkin_blob_sha(repo, CODE) != measured[CODE] and checkin_blob_sha(repo, DOC) != measured[DOC]

    assert _mismatch(repo, measured, post) == []


def test_peer_rewrite_of_a_measured_path_still_mismatches(repo: Path) -> None:
    measured = {CODE: _put(repo, CODE, b"x = 1\n"), DOC: _put(repo, DOC, b"# synthesis\n")}
    _put(repo, CODE, b"x = 99  # peer\n")
    _put(repo, DOC, b"# synthesis, peer words\n")
    own_post = {CODE: _put(repo, "scratch/other.py", b"x = 2\n")}

    assert _mismatch(repo, measured, own_post) == sorted([CODE, DOC])


def test_eof_delta_does_not_hide_a_content_change(repo: Path) -> None:
    measured = {DOC: _put(repo, DOC, b"# synthesis")}
    _put(repo, DOC, b"# synthesis changed\n")

    assert land_run._measured_mismatch(repo, {"blobs": measured, "removed": []}, [DOC], [], {}) == [DOC]


def test_interior_crlf_with_trailing_delta_is_not_an_eof_delta(repo: Path) -> None:
    measured = {DOC: _put(repo, DOC, b"# a\n\nbody")}
    _put(repo, DOC, b"# a\r\n\nbody\r\n")

    assert land_run._measured_mismatch(repo, {"blobs": measured, "removed": []}, [DOC], [], {}) == [DOC]


def test_trailing_crlf_only_delta_lands(repo: Path) -> None:
    measured = {DOC: _put(repo, DOC, b"# a\n\nbody")}
    landing = _put(repo, DOC, b"# a\n\nbody\r\n")

    assert land_run._measured_mismatch(repo, {"blobs": measured, "removed": []}, [DOC], [], {DOC: landing}) == []


def test_whole_file_crlf_rewrite_is_not_an_eof_delta(repo: Path) -> None:
    measured = {DOC: _put(repo, DOC, b"# a\n\nbody\n")}
    _put(repo, DOC, b"# a\r\n\r\nbody\r\n")

    assert land_run._measured_mismatch(repo, {"blobs": measured, "removed": []}, [DOC], [], {}) == [DOC]


from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_land_run_own_hunks_clean_tree_differs import (  # noqa: E402
    _record,
    _stored_blob,
)
from coordinator_core.ops.dispatch_emit.tests import test_land_run_unclaimed_peer_dirt_own_hunks as _peer  # noqa: E402

land_repo = _peer.repo
BOTH, PEER, TARGET, RUN_ID = _peer.BOTH, _peer.PEER, _peer.TARGET, _peer.RUN


REWRITTEN = BOTH.replace("own10\n", "own10-revised\n")


def test_own_rewrite_recorded_after_measure_lands(land_repo):
    token = _peer._stage(land_repo, PEER, BOTH, baselined=False)
    _record(land_repo)
    _peer._write(land_repo, TARGET, REWRITTEN)
    kept_work.write(measured_rev_record_path(land_repo, RUN_ID).parent, {}, None, {TARGET: _stored_blob(land_repo, TARGET)})

    out = _peer._land(land_repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out


def test_unrecorded_rewrite_after_measure_still_refuses(land_repo):
    token = _peer._stage(land_repo, PEER, BOTH, baselined=False)
    _record(land_repo)
    _peer._write(land_repo, TARGET, REWRITTEN)

    out = _peer._land(land_repo, token)

    assert out.get("refused") == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [TARGET]
    (entry,) = out["refused_paths"]
    assert entry["path"] == TARGET and entry["measured"] and entry["landing"]
    assert entry["measured"] != entry["landing"]


def test_real_landing_refusal_reaches_halted_json_through_commit_stage_halt(land_repo, tmp_path):
    import json

    from coordinator_core.ops.dispatch_emit.commit_stage_halt import halt_run_on_commit_stage_end
    from coordinator_core.ops.dispatch_emit.halted_run import HALT_REFUSED_PATHS_FIELD, HALTED_FILE

    token = _peer._stage(land_repo, PEER, BOTH, baselined=False)
    _record(land_repo)
    _peer._write(land_repo, TARGET, REWRITTEN)
    out = _peer._land(land_repo, token)
    assert out.get("refused") == land_run.LANDING_TREE_DIFFERS, out

    rd = tmp_path / "record"
    rd.mkdir()
    journal = tmp_path / "journal.jsonl"
    journal.write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "started", "agentId": "a1", "label": "commit", "key": "k1"},
        {"type": "result", "agentId": "a1", "key": "k1", "result": out},
    ]), encoding="utf-8")

    assert halt_run_on_commit_stage_end(
        rd, str(journal), terminal_status="failed", sizing_rel=None, launched_at=1.0,
    ) is not None
    data = json.loads((rd / HALTED_FILE).read_text(encoding="utf-8"))
    assert [(e["path"], e["measured"], e["landing"]) for e in data[HALT_REFUSED_PATHS_FIELD]] == [
        (e["path"], e["measured"], e["landing"]) for e in out["refused_paths"]
    ]
