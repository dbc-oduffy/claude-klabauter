"""`verify` at default arguments carries the run's own untracked write-set paths (a new module and the new test importing it) into the clean mirror, names them in its verdict, and leaves a peer's untracked file out."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _write
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _isolated_system_temp, _repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runOwnNew"
MODULE = "t/own_new_module.py"
TEST = "t/test_own_new_module.py"
PEER = "t/peer_untracked.py"
PEER_TEST = "t/test_peer_absent.py"

_MODULE_SRC = "VALUE = 7\n"
_TEST_SRC = "from own_new_module import VALUE\n\n\ndef test_value():\n    assert VALUE == 7\n"
_PEER_TEST_SRC = (
    "import pathlib\n\n\ndef test_the_peer_file_is_not_in_the_mirror():\n"
    "    assert not pathlib.Path(__file__).with_name('peer_untracked.py').exists()\n"
)


def _world(tmp_path):
    """HEAD holds the base only; the worktree carries the run's untracked module and test (declared in its manifest) and a peer's untracked file."""
    root, head = _repo(tmp_path)
    _write(root, MODULE, _MODULE_SRC)
    _write(root, TEST, _TEST_SRC)
    _write(root, PEER, "PEER = True\n")
    _write(root, PEER_TEST, _PEER_TEST_SRC)
    manifest = cov.measured_rev_record_path(root, RUN).with_name("manifest.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"review_declared_paths": [MODULE, TEST, PEER_TEST]}), encoding="utf-8")
    return root, head


def test_a_new_untracked_module_and_the_test_importing_it_go_green_and_the_verdict_names_them(tmp_path):
    root, head = _world(tmp_path)

    out = cov.verify(root, RUN, head, [TEST], touched=[TEST])

    assert out.verdict == "green", out
    assert "overlaid own path" in out.detail and MODULE in out.detail and TEST in out.detail


def test_a_peer_untracked_file_outside_the_write_set_stays_absent_from_the_mirror(tmp_path):
    root, head = _world(tmp_path)

    out = cov.verify(root, RUN, head, [PEER_TEST], touched=[PEER_TEST])

    assert out.verdict == "green", out
    assert PEER not in out.detail
