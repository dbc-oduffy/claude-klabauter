"""A halted run records, per kept path, HEAD plus only its own LF change: peer dirt present at stage time is subtracted."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.ops.dispatch_emit import halt_refire, halted_record_commit, kept_work, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

KEPT = "src/kept.py"
RECORD = f"{RECORD_DIR_ROOT}/{RUN}"


def _text(edits: dict) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11)).encode("utf-8")


def _put(root, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _kept_run(root, *, pre: bytes, tree: bytes) -> str:
    """HEAD holds ``line1..10``; the run staged KEPT against ``pre`` (uncommitted peer dirt), the tree
    holds ``tree``, and ``kept.json`` records the raw tree blob as the kept chunk's path."""
    _put(root, KEPT, _text({}))
    _git(["add", KEPT], root)
    _git(["commit", "-q", "-m", "kept"], root)
    _put(root, KEPT, pre)
    baseline = _git(["hash-object", "-w", "--no-filters", KEPT], root).strip()
    _put(root, KEPT, tree)
    raw = _git(["hash-object", "-w", "--no-filters", KEPT], root).strip()
    _put(root, f"{RECORD}/halted.json", json.dumps({"status": "halted", "halted": "ledger-check"}).encode("utf-8"))
    _put(root, f"{RECORD}/manifest.json", json.dumps({"rows": [], "write_baseline": {KEPT: baseline}}).encode("utf-8"))
    kept_work.write(root / RECORD, {"C1": {"paths": {KEPT: raw}}}, None)
    return raw


def _recorded(root) -> dict:
    return json.loads((root / RECORD / halt_refire.WITHHELD_OWN_DELTA_FILE).read_text(encoding="utf-8"))


def _blob(root, sha: str) -> bytes:
    kind, data = read_object(resolve_git_common_dir(root), sha)
    assert kind == "blob"
    return data


def _record_directly(root) -> list:
    paths: list = []
    halted_record_commit._record_withheld_own_delta(
        root, RECORD, {}, paths, RUN, kept=halted_record_commit._kept_paths(root, RECORD),
    )
    return paths


def test_kept_paths_name_chunk_and_post_checkpoint_paths(repo) -> None:
    _kept_run(repo, pre=_text({}), tree=_text({8: "OWN"}))
    kept_work.write(repo / RECORD, {"C1": {"paths": {KEPT: "a" * 40}}}, None, {"other.py": "b" * 40})

    assert halted_record_commit._kept_paths(repo, RECORD) == ["other.py", KEPT]


def test_a_kept_path_staged_over_peer_dirt_records_an_own_delta_without_the_peer_hunk(repo) -> None:
    raw = _kept_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "PEER", 8: "OWN"}))

    paths = _record_directly(repo)

    assert paths == [f"{RECORD}/{halt_refire.WITHHELD_OWN_DELTA_FILE}"]
    sha = _recorded(repo)["paths"][KEPT]["blob"]
    assert sha != raw
    assert _blob(repo, sha) == _text({8: "OWN"})
    assert b"PEER" not in _blob(repo, sha)
    assert _blob(repo, raw) == _text({2: "PEER", 8: "OWN"})


def test_a_kept_crlf_path_records_its_delta_in_lf(repo) -> None:
    _kept_run(repo, pre=_text({}), tree=_text({8: "OWN"}).replace(b"\n", b"\r\n"))

    _record_directly(repo)

    assert _blob(repo, _recorded(repo)["paths"][KEPT]["blob"]) == _text({8: "OWN"})


def test_a_kept_path_whose_hunk_collides_with_head_is_named_unlandable(repo) -> None:
    _kept_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "OWN"}))

    _record_directly(repo)

    doc = _recorded(repo)
    assert doc["unlandable"] == [KEPT]
    assert KEPT not in doc["paths"]


def test_a_kept_path_without_a_baseline_records_nothing(repo) -> None:
    _kept_run(repo, pre=_text({}), tree=_text({8: "OWN"}))
    _put(repo, f"{RECORD}/manifest.json", json.dumps({"rows": [], "write_baseline": {}}).encode("utf-8"))

    paths = _record_directly(repo)

    assert paths == []
    assert not (repo / RECORD / halt_refire.WITHHELD_OWN_DELTA_FILE).exists()


def test_the_halted_record_commit_records_the_kept_path_own_delta(repo) -> None:
    _kept_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "PEER", 8: "OWN"}))

    out = terminal_commit._handler({"run_id": RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=repo / ".git")

    assert out.get("committed") is True
    sha = _recorded(repo)["paths"][KEPT]["blob"]
    assert _blob(repo, sha) == _text({8: "OWN"})
    assert b"PEER" not in _blob(repo, sha)
