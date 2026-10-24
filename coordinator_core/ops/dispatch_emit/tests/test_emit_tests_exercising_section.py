"""`emit_script` renders the tests-exercising section into row briefs through the one widening grep."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import dispatch_row_lint
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HEAD = dispatch_row_lint.SECTION_HEAD
OUTSIDE = "Outside your writes: run, do not edit:"


def _git(repo, *args):
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, (args, done.stderr)


def _plan(rows):
    spine = "".join(
        f"- id: {rid}\n  title: {rid} row\n  writes: [{', '.join(writes)}]\n  disposition: open\n"
        f"  body: |\n    Do {rid}.\n"
        for rid, writes in rows
    )
    return (
        "---\nplan_id: pln-x\nstatus: executing\n---\n\n# P\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{spine}```\n"
    )


def _repo(tmp_path, files, rows):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, text in {**files, "p.md": _plan(rows)}.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    return repo


def _emit(repo):
    """The script text with the JS string escapes of newline and quote undone."""
    text = emit_script(repo / "p.md", repo_root=repo, **REVIEW_KW)
    return text.replace("\\n", "\n").replace("\\'", "'").replace("\\\\", "\\")


def test_section_appears_for_py_write_a_test_imports(tmp_path):
    repo = _repo(
        tmp_path,
        {"pkg/foo.py": "x = 1\n", "pkg/tests/test_foo.py": "from pkg.foo import x\n"},
        [("A", ["pkg/foo.py"])],
    )
    script = _emit(repo)
    assert HEAD in script
    assert "pkg/tests/test_foo.py" in script


def test_test_naming_the_file_is_listed_outside_and_not_added_to_writes(tmp_path):
    repo = _repo(
        tmp_path,
        {"pkg/foo.py": "x = 1\n", "pkg/tests/test_named.py": 'PATH = "pkg/foo.py"\n'},
        [("A", ["pkg/foo.py"])],
    )
    script = _emit(repo)
    assert f"{OUTSIDE}\n- `pkg/tests/test_named.py`" in script
    assert "'pkg/tests/test_named.py'" not in script


def test_md_only_row_gets_no_section(tmp_path):
    repo = _repo(
        tmp_path,
        {"pkg/tests/test_foo.py": "from pkg.foo import x\n", "notes.md": "foo.py\n"},
        [("A", ["notes.md"])],
    )
    assert HEAD not in _emit(repo)


def test_cap_message_carries_the_literal_query(tmp_path):
    from coordinator_core.ops.dispatch_emit import emit
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

    files = {"pkg/foo.py": "x = 1\n"}
    files.update({f"pkg/tests/test_n{i:02d}.py": "from pkg.foo import x\n" for i in range(26)})
    repo = _repo(tmp_path, files, [("A", ["pkg/foo.py"])])
    row = WaveRow(id="A", title="A", surface="s", writes=["pkg/foo.py"], reads=[], depends_on=[], body="")
    [[widened]] = emit._widen_waves_with_test_referencers([[row]], repo)
    assert "26 tests in total; the first 25 are listed." in widened.body
    assert "`git grep -lE '" in widened.body


def test_non_doe_layout_still_gets_the_section(tmp_path):
    repo = _repo(
        tmp_path,
        {"lib/foo.py": "x = 1\n", "tests/test_x.py": "from lib.foo import x\n"},
        [("A", ["lib/foo.py"])],
    )
    script = _emit(repo)
    assert HEAD in script
    assert "tests/test_x.py" in script


def test_py_row_without_an_exercising_test_is_byte_identical_to_no_pattern(tmp_path, monkeypatch):
    repo = _repo(
        tmp_path,
        {"pkg/foo.py": "x = 1\n", "pkg/tests/test_other.py": "from pkg.bar import y\n"},
        [("A", ["pkg/foo.py"])],
    )
    with_lint = _emit(repo)
    monkeypatch.setattr(dispatch_row_lint, "exercising_pattern", lambda sources: None)
    without_lint = _emit(repo)
    assert HEAD not in with_lint
    assert with_lint == without_lint
