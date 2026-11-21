"""`own_hunk_candidate.close_over`: the staged paths that leave with a withheld path (case 7: a consumer of
a definition only the withheld worktree content has; case 5: a test of a withheld module)."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import own_hunk_candidate as ohc


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


def test_an_unreadable_root_returns_empty_and_never_raises(tmp_path):
    assert ohc.close_over(tmp_path / "missing", [CONSUMER], [PRODUCER]) == {}


def test_candidate_refuses_a_file_past_the_match_bound_as_coarse(tmp_path):
    head = "".join(f"line{i}\n" for i in range(1, 1501)).encode()
    tree = head.replace(b"line1\n", b"EDIT1\n", 1).replace(b"line1500\n", b"EDIT1500\n", 1)

    res = ohc.candidate(tmp_path, "ask-20261114T100000-aaaaaa", "pkg/big.py", head, tree, declared=True)

    assert res["refused"] == ohc.COARSE and "bytes" not in res
