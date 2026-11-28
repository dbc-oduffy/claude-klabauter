"""A verify whose overlay cannot import a name that lives only in a peer's uncommitted hunk runs the selected tests
in the real tree and classifies the reds by ownership; a genuine import failure keeps its overlay verdict."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

LIB = "pkgx/lib.py"
SEED = "t/test_uses.py"
_PEER_LIB = "def committed():\n    return 1\n\n\ndef peer_sym():\n    return 2\n"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _repo(tmp_path, seed_src: str):
    """A repo whose HEAD holds `pkgx.lib` without `peer_sym` and a seed importing it, and whose worktree holds
    a peer's uncommitted `peer_sym`."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_creationflags())
    _write(root, LIB, "def committed():\n    return 1\n")
    _write(root, SEED, seed_src)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    head = _git(root, "rev-parse", "HEAD").strip()
    _write(root, LIB, _PEER_LIB)
    return root, head


_OK = "from pkgx.lib import peer_sym\n\n\ndef test_uses_peer_sym():\n    assert peer_sym() == 2\n"
_ONE_RED = _OK + "\n\ndef test_other_red():\n    assert False, 'boom'\n"


def test_an_import_of_a_peers_uncommitted_symbol_gets_a_real_green_verdict_not_a_zero_run(tmp_path):
    root, head = _repo(tmp_path, _OK)

    out = cov.verify(root, "run1", head, [SEED])

    assert out.verdict == "green", out
    assert out.cause == "" and "real-tree run" in out.detail and LIB in out.detail
    assert "peer_sym" not in _git(root, "show", f"{head}:{LIB}")


def test_a_red_naming_no_own_path_is_the_peers_and_lands_as_pre_existing(tmp_path, monkeypatch):
    root, head = _repo(tmp_path, _ONE_RED)
    monkeypatch.setattr(cov, "_own_write_set", lambda _root, _run: frozenset({"elsewhere/own.py"}))

    out = cov.verify(root, "run1", head, [SEED])

    assert out.verdict == "pre_existing", out
    assert out.failing == (f"{SEED}::test_other_red",)


def test_a_red_in_the_runs_own_test_file_stays_the_runs_red(tmp_path, monkeypatch):
    root, head = _repo(tmp_path, _ONE_RED)
    monkeypatch.setattr(cov, "_own_write_set", lambda _root, _run: frozenset({SEED}))

    out = cov.verify(root, "run1", head, [SEED])

    assert out.verdict == "red", out
    assert out.failing == (f"{SEED}::test_other_red",)


def test_a_name_defined_nowhere_is_not_a_foreign_dependency(tmp_path):
    root, head = _repo(tmp_path, "from pkgx.lib import nowhere\n\n\ndef test_x():\n    assert nowhere\n")

    out = cov.verify(root, "run1", head, [SEED])

    assert out.verdict == "red", out
    assert "real-tree run" not in out.detail


def test_a_name_missing_from_the_runs_own_module_is_not_a_foreign_dependency(tmp_path, monkeypatch):
    root, head = _repo(tmp_path, _OK)
    monkeypatch.setattr(cov, "_own_write_set", lambda _root, _run: frozenset({LIB}))

    out = cov.verify(root, "run1", head, [SEED])

    assert out.verdict != "green" and "real-tree run" not in out.detail, out
