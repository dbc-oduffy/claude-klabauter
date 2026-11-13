"""needed_path: changed top-level symbols vs HEAD and the landing files that read them."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import needed_path as np_
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

CALLEE = "pkg/callee.py"
CALLER = "pkg/caller.py"
_CALLEE_V1 = "def anchor_verdicts(plan):\n    return plan\n\n\ndef other():\n    return 1\n\n\nLIMIT = 3\n"
_CALLER_FROM = "from pkg.callee import anchor_verdicts\n\n\ndef go():\n    return anchor_verdicts('p', base_ref='x')\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, CALLEE, _CALLEE_V1)
    _write(root, "pkg/__init__.py", "")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root


def test_an_added_keyword_parameter_changes_only_that_function(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("(plan)", "(plan, base_ref=None)"))

    assert np_.changed_symbols(repo, CALLEE) == frozenset({"anchor_verdicts"})


def test_an_untouched_file_has_no_changed_symbols(repo):
    assert np_.changed_symbols(repo, CALLEE) == frozenset()


def test_a_new_and_a_removed_name_are_changed(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("def other", "def renamed") + "class K:\n    pass\n")

    assert np_.changed_symbols(repo, CALLEE) == frozenset({"other", "renamed", "K"})


def test_assignment_target_change_is_seen(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("LIMIT = 3", "LIMIT = 4"))

    assert np_.changed_symbols(repo, CALLEE) == frozenset({"LIMIT"})


def test_a_file_head_lacks_reports_every_worktree_name(repo):
    _write(repo, "pkg/fresh.py", "def a():\n    pass\n\n\nB = 1\n")

    assert np_.changed_symbols(repo, "pkg/fresh.py") == frozenset({"a", "B"})


def test_an_unparseable_head_side_reports_every_worktree_name(repo):
    _write(repo, CALLEE, "def broken(:\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "broken")
    _write(repo, CALLEE, _CALLEE_V1)

    assert np_.changed_symbols(repo, CALLEE) == frozenset({"anchor_verdicts", "other", "LIMIT"})


def test_a_missing_or_unparseable_worktree_side_reports_nothing(repo):
    _write(repo, CALLEE, "def broken(:\n")
    assert np_.changed_symbols(repo, CALLEE) == frozenset()
    assert np_.changed_symbols(repo, "pkg/absent.py") == frozenset()


def test_from_import_of_a_changed_name_is_reported(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("(plan)", "(plan, base_ref=None)"))
    _write(repo, CALLER, _CALLER_FROM)

    assert np_.needed_by(repo, [CALLER], [CALLEE]) == {CALLEE: [(CALLER, "anchor_verdicts")]}


def test_an_unrelated_name_change_is_not_needed(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("return 1", "return 2"))
    _write(repo, CALLER, _CALLER_FROM)

    assert np_.needed_by(repo, [CALLER], [CALLEE]) == {}


@pytest.mark.parametrize(
    "src",
    [
        "import pkg.callee as c\n\n\ndef go():\n    return c.anchor_verdicts('p')\n",
        "from pkg import callee\n\n\ndef go():\n    return callee.anchor_verdicts('p')\n",
        "import pkg.callee\n\n\ndef go():\n    return pkg.callee.anchor_verdicts('p')\n",
    ],
)
def test_a_module_attribute_read_is_reported(repo, src):
    _write(repo, CALLEE, _CALLEE_V1.replace("(plan)", "(plan, base_ref=None)"))
    _write(repo, CALLER, src)

    assert np_.needed_by(repo, [CALLER], [CALLEE]) == {CALLEE: [(CALLER, "anchor_verdicts")]}


def test_a_relative_import_is_resolved(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("(plan)", "(plan, base_ref=None)"))
    _write(repo, CALLER, "from .callee import anchor_verdicts\n")

    assert np_.needed_by(repo, [CALLER], [CALLEE]) == {CALLEE: [(CALLER, "anchor_verdicts")]}


def test_a_reader_of_an_unchanged_name_and_a_non_importer_are_not_needed(repo):
    _write(repo, CALLEE, _CALLEE_V1.replace("(plan)", "(plan, base_ref=None)"))
    _write(repo, CALLER, "from pkg.callee import other\n\nimport os\n")
    _write(repo, "pkg/bystander.py", "def anchor_verdicts():\n    pass\n")

    assert np_.needed_by(repo, [CALLER, "pkg/bystander.py"], [CALLEE]) == {}


def test_an_empty_omitted_set_or_a_non_py_path_is_empty(repo):
    _write(repo, CALLER, _CALLER_FROM)

    assert np_.needed_by(repo, [CALLER], []) == {}
    assert np_.needed_by(repo, [CALLER], ["pkg/data.json"]) == {}
