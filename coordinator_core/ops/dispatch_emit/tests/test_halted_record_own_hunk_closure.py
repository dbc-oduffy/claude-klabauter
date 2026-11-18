"""The halt commit withholds what leaves with a withheld product path: a consumer of a definition only the
withheld worktree content holds (case 7) and a test of a withheld module (case 5)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import own_hunk_candidate
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _git,
    _landed,
    repo,
)
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PRODUCER = "pkg/producer.py"
CONSUMER = "pkg/consumer.py"
TEST = "pkg/tests/test_producer.py"
OTHER = "pkg/other.py"

PRODUCER_HEAD = "".join(f"line{i}\n" for i in range(1, 9))
PRODUCER_PEER = PRODUCER_HEAD.replace("line2\n", "PEER\n")
PRODUCER_TREE = PRODUCER_PEER + "\n\ndef fresh():\n    return 1\n"
CONSUMER_HEAD = "from pkg import producer\n\nproducer.verify(1)\n"
CONSUMER_TREE = "from pkg import producer\n\nproducer.fresh()\n"
TEST_HEAD = "def test_x():\n    pass\n"
TEST_TREE = "def test_x():\n    assert True\n"
OTHER_HEAD = "X = 1\n"
OTHER_TREE = "X = 2\n"


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _blob(root, rel: str, text: str) -> str:
    _write(root, rel, text)
    return _git(["hash-object", "-w", rel], root).strip()


def _halted_run(root) -> None:
    """HEAD holds the four paths; the run's baseline for PRODUCER already carries a peer hunk, so PRODUCER is
    withheld whole; the run edited the other three. The marker names all four as the run's land-only product."""
    heads = {PRODUCER: PRODUCER_HEAD, CONSUMER: CONSUMER_HEAD, TEST: TEST_HEAD, OTHER: OTHER_HEAD}
    for rel, text in heads.items():
        _write(root, rel, text)
    _git(["add", *heads], root)
    _git(["commit", "-q", "-m", "pkg"], root)
    baseline = {
        PRODUCER: _blob(root, PRODUCER, PRODUCER_PEER),
        CONSUMER: _blob(root, CONSUMER, CONSUMER_HEAD),
        TEST: _blob(root, TEST, TEST_HEAD),
        OTHER: _blob(root, OTHER, OTHER_HEAD),
    }
    for rel, text in {PRODUCER: PRODUCER_TREE, CONSUMER: CONSUMER_TREE, TEST: TEST_TREE, OTHER: OTHER_TREE}.items():
        _write(root, rel, text)
    record = f"{RECORD_DIR_ROOT}/{RUN}"
    _write(root, f"{record}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check",
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in baseline}},
    }))
    _write(root, f"{record}/manifest.json", json.dumps({"rows": [], "write_baseline": baseline}))


def _land_only(root) -> dict:
    return terminal_commit._handler({"run_id": RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=root / ".git")


def test_case7_a_consumer_of_a_definition_only_the_withheld_path_holds_is_withheld(repo):
    _halted_run(repo)

    out = _land_only(repo)

    assert out["withheld"][CONSUMER].startswith("consumer of a withheld producer: ")
    assert PRODUCER in out["withheld"][CONSUMER]
    assert CONSUMER not in _landed(repo, out, "--name-only")
    assert (repo / CONSUMER).read_text(encoding="utf-8") == CONSUMER_TREE


def test_case5_a_test_of_a_withheld_module_is_withheld(repo):
    _halted_run(repo)

    out = _land_only(repo)

    assert out["withheld"][TEST] == own_hunk_candidate.TEST_OF_WITHHELD_REASON
    assert TEST not in _landed(repo, out, "--name-only")
    assert (repo / TEST).read_text(encoding="utf-8") == TEST_TREE


def test_a_path_that_needs_nothing_withheld_still_lands(repo):
    _halted_run(repo)

    out = _land_only(repo)

    assert OTHER not in out.get("withheld", {})
    assert OTHER in _landed(repo, out, "--name-only")
    assert _git(["show", f"HEAD:{OTHER}"], repo) == OTHER_TREE


def test_peer_hunk_pass_skips_a_path_candidate_refuses_as_coarse(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit

    monkeypatch.setattr(
        halted_record_commit, "_candidate_for", lambda *a, **k: {"refused": own_hunk_candidate.COARSE}
    )

    refused, named = halted_record_commit._peer_hunks_excluded(repo, RUN, [OTHER], {})

    assert (refused, named) == ([], {})
