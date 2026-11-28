"""A halted run records, per withheld path, HEAD plus only its own LF change: the blob a hand land commits."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.ops.dispatch_emit import halt_refire, halted_record_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _git,
    repo,
)
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED = "src/shared.py"
RECORD = f"{RECORD_DIR_ROOT}/{RUN}"


def _text(edits: dict) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11)).encode("utf-8")


def _put(root, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _halted_run(root, *, pre: bytes, tree: bytes) -> None:
    """HEAD holds ``line1..10``; the run staged against ``pre`` (uncommitted peer dirt) and the tree
    holds ``tree``; the marker withholds SHARED."""
    _put(root, SHARED, _text({}))
    _git(["add", SHARED], root)
    _git(["commit", "-q", "-m", "shared"], root)
    _put(root, SHARED, pre)
    blob = _git(["hash-object", "-w", "--no-filters", SHARED], root).strip()
    _put(root, SHARED, tree)
    _put(root, f"{RECORD}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check",
        "run_artifacts": {"withheld": {SHARED: "halt-record-only"}},
    }).encode("utf-8"))
    _put(root, f"{RECORD}/manifest.json", json.dumps({"rows": [], "write_baseline": {SHARED: blob}}).encode("utf-8"))


def _land_only(root) -> dict:
    return terminal_commit._handler({"run_id": RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=root / ".git")


def _recorded(root) -> dict:
    return json.loads((root / RECORD / halt_refire.WITHHELD_OWN_DELTA_FILE).read_text(encoding="utf-8"))


def _blob(root, sha: str) -> bytes:
    kind, data = read_object(resolve_git_common_dir(root), sha)
    assert kind == "blob"
    return data


def test_a_peer_dirty_crlf_withheld_path_records_head_plus_only_the_runs_lf_hunk(repo) -> None:
    peer = _text({2: "PEER"})
    own_crlf = _text({2: "PEER", 8: "OWN"}).replace(b"\n", b"\r\n")
    _halted_run(repo, pre=peer, tree=own_crlf)

    out = _land_only(repo)

    assert SHARED in out["withheld"]
    doc = _recorded(repo)
    image = _blob(repo, doc["paths"][SHARED]["blob"])
    assert image == _text({8: "OWN"})
    assert b"\r" not in image and b"PEER" not in image
    assert (repo / SHARED).read_bytes() == own_crlf


def _record_directly(root) -> list:
    paths: list = []
    halted_record_commit._record_withheld_own_delta(root, RECORD, {SHARED: "halt-record-only"}, paths, RUN)
    return paths


def test_a_withheld_path_the_run_staged_clean_records_its_post_image_in_lf(repo) -> None:
    _halted_run(repo, pre=_text({}), tree=_text({8: "OWN"}).replace(b"\n", b"\r\n"))

    paths = _record_directly(repo)

    assert paths == [f"{RECORD}/{halt_refire.WITHHELD_OWN_DELTA_FILE}"]
    assert _blob(repo, _recorded(repo)["paths"][SHARED]["blob"]) == _text({8: "OWN"})


def test_a_run_hunk_colliding_with_head_names_the_path_unlandable_and_records_no_blob(repo) -> None:
    # The run rewrote line 2 over a pre that differs from HEAD on that same line: no clean delta exists.
    _halted_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "OWN"}))

    _record_directly(repo)

    doc = _recorded(repo)
    assert doc["unlandable"] == [SHARED]
    assert SHARED not in doc["paths"]


def test_a_refire_that_finds_nothing_to_commit_still_leaves_the_own_delta_record(repo) -> None:
    _halted_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "PEER", 8: "OWN"}).replace(b"\n", b"\r\n"))
    _land_only(repo)
    rel = f"{RECORD}/{halt_refire.WITHHELD_OWN_DELTA_FILE}"
    # The record landed without its own delta (an earlier engine); the refire has no new bytes to land.
    (repo / rel).unlink()
    assert not (repo / rel).exists()

    out = _land_only(repo)

    assert not out.get("error")
    assert (repo / rel).is_file()
    assert SHARED in _recorded(repo)["paths"]
    assert out["nothing_to_commit"] is True
    assert out["withheld_own_delta"] == rel


def test_a_no_op_refire_with_the_own_delta_already_recorded_still_names_it(repo) -> None:
    _halted_run(repo, pre=_text({2: "PEER"}), tree=_text({2: "PEER", 8: "OWN"}).replace(b"\n", b"\r\n"))
    _land_only(repo)
    rel = f"{RECORD}/{halt_refire.WITHHELD_OWN_DELTA_FILE}"
    assert (repo / rel).is_file()

    out = _land_only(repo)

    assert not out.get("error")
    assert out["nothing_to_commit"] is True
    assert out["withheld_own_delta"] == rel
    assert SHARED in out["withheld"]


def _record_withheld(root, withheld: dict) -> dict:
    paths: list = []
    halted_record_commit._record_withheld_own_delta(root, RECORD, withheld, paths, RUN)
    return _recorded(root)


def test_a_withheld_new_file_off_the_run_baseline_records_its_whole_post_image(repo) -> None:
    _halted_run(repo, pre=_text({}), tree=_text({}))
    fresh = "src/fresh_product.py"
    _put(repo, fresh, b"print('own')\r\n")

    doc = _record_withheld(repo, {SHARED: "halt-record-only", fresh: "run-dirt-uncommitted"})

    assert _blob(repo, doc["paths"][fresh]["blob"]) == b"print('own')\n"
    assert fresh not in doc.get("unlandable", [])


def test_a_withheld_head_file_off_the_run_baseline_is_named_unlandable(repo) -> None:
    _halted_run(repo, pre=_text({}), tree=_text({8: "OWN"}))
    other = "src/tracked_other.py"
    _put(repo, other, b"a\n")
    _git(["add", other], repo)
    _git(["commit", "-q", "-m", "other"], repo)
    _put(repo, other, b"a\nb\n")

    doc = _record_withheld(repo, {SHARED: "halt-record-only", other: "run-dirt-uncommitted"})

    assert other in doc["unlandable"]
    assert other not in doc["paths"]
    assert SHARED in doc["paths"]


def test_run_state_paths_are_not_recorded_as_withheld_product(repo) -> None:
    _halted_run(repo, pre=_text({}), tree=_text({8: "OWN"}))
    state = "state/warp/ask-x/run.json"
    _put(repo, state, b"{}\n")

    doc = _record_withheld(repo, {SHARED: "halt-record-only", state: "halt-record-only"})

    assert state not in doc["paths"] and state not in doc.get("unlandable", [])


def test_the_halt_em_rule_names_the_own_delta_record_not_the_raw_blob() -> None:
    rule = halt_refire.EM_ACTION_AFTER_HALT_RULE

    assert halt_refire.WITHHELD_OWN_DELTA_FILE in rule
