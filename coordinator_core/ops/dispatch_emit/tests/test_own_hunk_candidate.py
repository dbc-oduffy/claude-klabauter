"""`own_hunk_candidate.close_over`: the staged paths that leave with a withheld path (case 7: a consumer of
a definition only the withheld worktree content has; case 5: a test of a withheld module)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import own_hunk_candidate as ohc
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

PRODUCER = "pkg/producer.py"
CONSUMER = "pkg/consumer.py"
OUTER = "pkg/outer.py"
TEST = "pkg/tests/test_producer.py"
ROW_TEST = "pkg/tests/test_rowcheck.py"
PLAN = "docs/plans/p.md"
SPINE = """# plan

## Tasks

```yaml plan-tasks
- id: R1
  title: row one
  disposition: open
  writes:
    - pkg/producer.py
    - pkg/tests/test_rowcheck.py
```
"""
PRODUCER_HEAD = "def verify(x):\n    return x\n"
PRODUCER_LIVE = "def verify(x, leg=None):\n    return x\n\n\ndef fresh():\n    return 1\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, "pkg/__init__.py", "")
    _write(root, PRODUCER, PRODUCER_HEAD)
    _write(root, CONSUMER, "from pkg import producer\n\nproducer.verify(1)\n")
    _write(root, TEST, "def test_x():\n    pass\n")
    _write(root, ROW_TEST, "def test_y():\n    pass\n")
    _write(root, PLAN, SPINE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    _write(root, PRODUCER, PRODUCER_LIVE)
    return root


def test_case7_a_call_without_its_definition_leaves_with_the_producer(repo):
    _write(repo, CONSUMER, "from pkg import producer\n\nproducer.verify(1, leg=2)\n")
    found = ohc.close_over(repo, [CONSUMER], [PRODUCER])
    assert set(found) == {CONSUMER}
    assert found[CONSUMER].startswith("consumer of a withheld producer: ")
    assert PRODUCER in found[CONSUMER]


def test_case7_a_consumer_of_a_head_definition_stays(repo):
    _write(repo, CONSUMER, "from pkg import producer\n\nproducer.verify(1)\n# edited\n")
    assert ohc.close_over(repo, [CONSUMER], [PRODUCER]) == {}


def test_case7_the_closure_chains_through_a_withheld_consumer(repo):
    _write(repo, CONSUMER, "from pkg import producer\n\nproducer.fresh()\n\n\ndef relay():\n    return 1\n")
    _write(repo, OUTER, "from pkg import consumer\n\nconsumer.relay()\n")
    found = ohc.close_over(repo, [CONSUMER, OUTER], [PRODUCER])
    assert set(found) == {CONSUMER, OUTER}


def test_case7_a_supplied_blob_is_judged_not_the_worktree(repo):
    _write(repo, CONSUMER, "from pkg import producer\n\nproducer.fresh()\n")
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=b"from pkg import producer\n\nproducer.verify(1)\n", capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout.decode().strip()
    assert ohc.close_over(repo, [CONSUMER], [PRODUCER], {CONSUMER: blob}) == {}


def test_case5_a_test_of_a_withheld_module_leaves_with_it(repo):
    _write(repo, TEST, "def test_x():\n    assert True\n")
    found = ohc.close_over(repo, [TEST], [PRODUCER])
    assert found == {TEST: ohc.TEST_OF_WITHHELD_REASON}
    assert ohc.TEST_OF_WITHHELD_REASON == "test of a row whose code is withheld: the row lands whole or waits"


def test_case5_stem_pairing_off_keeps_the_test_for_the_checkpoint_path(repo):
    _write(repo, TEST, "def test_x():\n    assert True\n")
    assert ohc.close_over(repo, [TEST], [PRODUCER], pair_by_stem=False) == {}


def test_case5_a_test_beside_the_module_pairs_by_stem(repo):
    _write(repo, "pkg/test_producer.py", "def test_z():\n    pass\n")
    found = ohc.close_over(repo, ["pkg/test_producer.py"], [PRODUCER])
    assert set(found) == {"pkg/test_producer.py"}


def test_case5_an_unrelated_test_stays(repo):
    _write(repo, "pkg/tests/test_other.py", "def test_o():\n    pass\n")
    assert ohc.close_over(repo, ["pkg/tests/test_other.py"], [PRODUCER]) == {}


def test_case5_a_plan_row_test_leaves_with_its_rows_withheld_code_even_unpaired(repo):
    message = f"checkpoint(wave 1): 1 row — R1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {'a' * 40}\n"
    found = ohc.close_over(repo, [ROW_TEST], [PRODUCER], message=message, pair_by_stem=False)
    assert found == {ROW_TEST: ohc.TEST_OF_WITHHELD_REASON}


def test_a_withheld_test_does_not_pull_a_path_with_no_dependency(repo):
    _write(repo, "pkg/unrelated.py", "X = 1\n")
    assert ohc.close_over(repo, ["pkg/unrelated.py"], [PRODUCER]) == {}


def test_nothing_withheld_judges_nothing(repo):
    assert ohc.close_over(repo, [CONSUMER, TEST], []) == {}


def test_an_unreadable_root_returns_empty_and_never_raises(tmp_path):
    assert ohc.close_over(tmp_path / "missing", [CONSUMER], [PRODUCER]) == {}
