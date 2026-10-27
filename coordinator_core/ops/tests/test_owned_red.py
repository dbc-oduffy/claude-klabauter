from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.owned_red import read_owned_red

RUN = "ask-20261025T000000-aaaaaa"


def _write(repo, text):
    d = rl.run_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / rl.OWNED_RED_FILE).write_text(text, encoding="utf-8")


def test_absent_file_is_empty(tmp_path):
    assert read_owned_red(tmp_path, RUN) == {}


def test_reads_rows_skips_malformed_and_later_wins(tmp_path):
    rows = [
        {"nodeid": "a.py::t1", "owner_run_id": "111", "at": "x"},
        {"nodeid": "a.py::t1", "owner_run_id": "222"},
        {"nodeid": "b.py::t2"},
        ["a.py::t3"],
    ]
    _write(tmp_path, "\n".join(json.dumps(r) for r in rows) + "\n{not json\n")
    assert read_owned_red(tmp_path, RUN) == {"a.py::t1": "222"}


def test_unsafe_run_key_refused(tmp_path):
    with pytest.raises(rl.LedgerRefused):
        read_owned_red(tmp_path, "../x")
