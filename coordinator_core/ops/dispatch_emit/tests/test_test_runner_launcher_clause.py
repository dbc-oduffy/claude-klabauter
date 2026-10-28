"""The emitted scoped-test prompt runs pytest only through the scoped_pytest launcher, scope as allow-list."""
from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import emit

MODULE = "coordinator_core.testing.scoped_pytest"
SCOPE = ["tests/test_a.py", "tests/test_b.py"]


def _steps(scope=SCOPE, base=None):
    return emit._scoped_test_steps(list(scope), None, None, base, None, None, None)


def test_launcher_string_and_every_allow_entry_appear():
    text = _steps()
    assert f"-m {MODULE} --allow tests/test_a.py --allow tests/test_b.py -- tests/test_a.py tests/test_b.py" in text


def test_no_bare_pytest_instruction():
    text = _steps()
    assert not re.search(r"`(python3?|python) -m pytest", text.replace("never bare `pytest` or `python -m pytest`", ""))


def test_empty_scope_carries_no_launcher():
    assert MODULE not in _steps(scope=[])


def test_review_edits_targets_use_the_launcher_with_diff_limited_allow():
    text = _steps(base="a" * 40)
    assert text.count(f"-m {MODULE}") >= 2
    assert "only for targets covering files in that edited set" in text


def test_foreign_repo_gets_claude_klabauters_interpreter(tmp_path):
    import sys

    cmd = emit._launcher_command(["t.py"], "t.py", tmp_path)
    assert cmd.startswith(sys.executable.replace("\\", "/"))
