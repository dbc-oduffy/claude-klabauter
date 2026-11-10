"""`find_commit_module_attr_violations` resolves `<module>.<attr>` reads against
the blobs of a commit, never the worktree: the r20 shape, a caller committed
whose callee definition sits only in the dirty worktree."""

import subprocess
import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.import_closure import find_commit_module_attr_violations  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_CALLER = "from coordinator_core import ledger\n\ndef gate():\n    return ledger.advisory_text()\n"


def _write(root: Path, rel: str, src: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, caller: str = _CALLER, ledger: str = "def revise():\n    pass\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "coordinator_core/__init__.py", "")
    _write(repo, "coordinator_core/ledger.py", ledger)
    _write(repo, "coordinator_core/caller.py", caller)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _violations(repo: Path) -> list[tuple[str, str]]:
    return find_commit_module_attr_violations(repo, _git(repo, "rev-parse", "HEAD"))[1]


def test_attr_missing_from_committed_module_is_refused_naming_file_and_attr(tmp_path):
    repo = _repo(tmp_path)
    examined, violations = find_commit_module_attr_violations(repo, _git(repo, "rev-parse", "HEAD"))
    assert examined == 1
    assert violations == [("coordinator_core/caller.py", "coordinator_core.ledger.advisory_text")]


def test_attr_defined_only_in_dirty_worktree_is_still_refused(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "coordinator_core/ledger.py", "def revise():\n    pass\n\ndef advisory_text():\n    pass\n")
    assert _violations(repo) == [("coordinator_core/caller.py", "coordinator_core.ledger.advisory_text")]


def test_attr_defined_in_committed_module_passes(tmp_path):
    repo = _repo(tmp_path, ledger="def advisory_text():\n    pass\n")
    assert _violations(repo) == []


def test_import_as_alias_form_is_checked(tmp_path):
    caller = "import coordinator_core.ledger as lg\n\nlg.nothing\n"
    assert _violations(_repo(tmp_path, caller)) == [("coordinator_core/caller.py", "coordinator_core.ledger.nothing")]


def test_package_submodule_attr_resolves(tmp_path):
    caller = "from coordinator_core import pkg\n\npkg.leaf\npkg.absent\n"
    repo = _repo(tmp_path, caller)
    _write(repo, "coordinator_core/pkg/__init__.py", "")
    _write(repo, "coordinator_core/pkg/leaf.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pkg")
    assert _violations(repo) == [("coordinator_core/caller.py", "coordinator_core.pkg.absent")]


@pytest.mark.parametrize(
    "caller",
    [
        "from coordinator_core import ledger\nhasattr(ledger, 'advisory_text') and ledger.advisory_text\n",
        "from coordinator_core import ledger\nledger.advisory_text = 1\n",
        "from coordinator_core import ledger\n\ndef f(ledger):\n    return ledger.advisory_text\n",
        "from coordinator_core import ledger\nledger.__name__\n",
        "def a():\n    from coordinator_core import ledger\n\ndef b():\n    from coordinator_core import other as ledger\n    ledger.advisory_text\n",
    ],
)
def test_exempt_shapes_never_flag(tmp_path, caller):
    repo = _repo(tmp_path, caller)
    _write(repo, "coordinator_core/other.py", "advisory_text = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "other")
    assert _violations(repo) == []


def test_module_defining_dunder_getattr_never_flags(tmp_path):
    caller = "from coordinator_core import ledger\nledger.anything\nledger.late\n"
    ledger = "def __getattr__(name):\n    return name\n"
    assert _violations(_repo(tmp_path, caller, ledger)) == []


def test_global_declared_name_resolves(tmp_path):
    caller = "from coordinator_core import ledger\nledger.late\n"
    ledger = "def init():\n    global late\n    late = 1\n"
    assert _violations(_repo(tmp_path, caller, ledger)) == []


def test_test_modules_are_not_examined(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "coordinator_core/tests/test_x.py", _CALLER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test")
    assert [v[0] for v in _violations(repo)] == ["coordinator_core/caller.py"]
