"""The guard graph's import of ``commit_tripwires`` does not wait on the git index/divergence
modules, so a request thread importing ``dispatch_checks`` cannot queue behind another thread's
slow import of them. Each case runs in a fresh interpreter: import state is process-global."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE_BUDGET_S = 0.5
_HELD = (
    "coordinator_core.git.divergence",
    "coordinator_core.git.commit_trailers",
)


def _run_probe(body: str) -> str:
    script = f"import sys; sys.path.insert(0, {str(_REPO_ROOT)!r})\n" + textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


_HOLD_PRELUDE = """
    import importlib.abc, importlib.util, sys, threading, time

    HELD = {held!r}
    release = threading.Event()
    entered = threading.Event()

    class _Loader(importlib.abc.Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            entered.set()
            release.wait(30)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name in HELD:
                return importlib.util.spec_from_loader(name, _Loader())
            return None

    sys.meta_path.insert(0, _Finder())
"""


@pytest.mark.parametrize("held", _HELD)
def test_commit_tripwires_imports_while_a_peer_thread_holds_the_git_module_import(held: str) -> None:
    out = _run_probe(
        _HOLD_PRELUDE.format(held=(held,))
        + f"""
    import importlib
    t = threading.Thread(target=lambda: importlib.import_module({held!r}), daemon=True)
    t.start()
    assert entered.wait(10), "peer thread never entered the held import"

    cpu0 = time.process_time()
    from coordinator_core.bash_guards import commit_tripwires
    cpu = time.process_time() - cpu0
    still_held = t.is_alive()
    release.set()
    t.join(10)
    print("held=%s cpu=%.3f" % (still_held, cpu))
    assert still_held, "import returned only after the peer released: it waited"
    assert cpu < {_PROBE_BUDGET_S}, cpu
    """
    )
    assert out.startswith("held=True")


def test_dispatch_checks_facade_imports_while_a_peer_thread_holds_the_git_module_import() -> None:
    out = _run_probe(
        _HOLD_PRELUDE.format(held=_HELD)
        + """
    import importlib
    t = threading.Thread(target=lambda: importlib.import_module(HELD[0]), daemon=True)
    t.start()
    assert entered.wait(10)

    from coordinator_core.bash_guards import dispatch_checks
    still_held = t.is_alive()
    release.set()
    t.join(10)
    print("held=%s" % still_held)
    assert still_held
    assert dispatch_checks.commit_tripwires is sys.modules["coordinator_core.bash_guards.commit_tripwires"]
    """
    )
    assert out == "held=True"


def test_the_deferred_git_symbols_still_resolve_from_their_call_sites() -> None:
    out = _run_probe(
        """
        import sys
        from coordinator_core.bash_guards import commit_tripwires as ct

        assert "coordinator_core.git.divergence" not in sys.modules
        assert "coordinator_core.git.commit_trailers" not in sys.modules
        assert ct._diverging_paths([], None) == []
        from coordinator_core.git.divergence import DivergenceCheckFailed

        assert ct.DivergenceCheckFailed is DivergenceCheckFailed
        demoted = ct.check_trailer_demoted_to_body(
            ["git", "commit", "-m", "subject\\n\\nprose body\\n\\nCo-Authored-By: A <a@b.c>\\n\\nSigned-off-by: A <a@b.c>"]
        )
        print("ok", type(demoted).__name__)
        """
    )
    assert out.startswith("ok")
