"""Shared probe for the checkout-engine ratchets: spawn a coordinator/bin wrapper the way a test
does and report which `coordinator_core` and dispatch engine root the child resolved."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

# Loads the real wrapper by path and runs its own engine-binding entry points. The wrapper must
# expose `_import_session` and `_bootstrap_engine` (coordinator-safe-commit.py does).
_PROBE = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("wrapper_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod._import_session()
import coordinator_core
print("CORE_FILE=" + os.path.abspath(coordinator_core.__file__))
mod._bootstrap_engine()
from cc_invoke import require_dispatch_engine_on_path
print("DISPATCH_ROOT=" + os.path.abspath(require_dispatch_engine_on_path()))
"""


def checkout_root() -> pathlib.Path:
    """The checkout this module lives in (`coordinator_core/testing/` -> repo root)."""
    return pathlib.Path(__file__).resolve().parents[2]


def wrapper_path(name: str = "coordinator-safe-commit.py") -> pathlib.Path:
    """A `coordinator/bin` wrapper of the checkout."""
    return checkout_root() / "coordinator" / "bin" / name


def is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    """`path` is `root` or inside it, compared case- and separator-normalised."""
    p, r = os.path.normcase(str(path.resolve())), os.path.normcase(str(root.resolve()))
    return p == r or p.startswith(r + os.sep)


def _run_probe(
    tmp_path: pathlib.Path, env: dict[str, str], wrapper: pathlib.Path | None = None
) -> tuple[dict[str, pathlib.Path], subprocess.CompletedProcess]:
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(probe), str(wrapper or wrapper_path())],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = {}
    for ln in proc.stdout.splitlines():
        key, sep, val = ln.partition("=")
        if sep and key in ("CORE_FILE", "DISPATCH_ROOT"):
            out[key] = pathlib.Path(val)
    return out, proc


def loaded_roots(
    tmp_path: pathlib.Path, env: dict[str, str], wrapper: pathlib.Path | None = None
) -> tuple[pathlib.Path, pathlib.Path]:
    """(coordinator_core location, dispatch engine root) one spawned wrapper resolved under `env`."""
    out, proc = _run_probe(tmp_path, env, wrapper)
    assert len(out) == 2, f"probe incomplete (rc={proc.returncode}): {proc.stderr[-800:]}"
    return out["CORE_FILE"], out["DISPATCH_ROOT"]


def assert_unpinned_child_loads_core_from_checkout(
    tmp_path: pathlib.Path, *, may_refuse_without_published_engine: bool = False
) -> None:
    """No engine-root pin in the child env: `coordinator_core` still comes from the checkout.

    A tree whose conftest quarantines the settings home (coordinator_core/tests) leaves the child
    no published-engine pointer to resolve; there the child may refuse loudly instead of resolving,
    but a `coordinator_core` it did load is still the checkout's.
    """
    env = dict(os.environ)
    env.pop("COORDINATOR_ENGINE_ROOT", None)
    env.pop("CLAUDE_KLABAUTER_ROOT", None)
    out, proc = _run_probe(tmp_path, env)
    root = checkout_root()
    if may_refuse_without_published_engine and len(out) < 2:
        assert proc.returncode != 0 and "no published claude-klabauter engine" in proc.stderr, (
            f"probe neither resolved nor refused loudly (rc={proc.returncode}): {proc.stderr[-800:]}"
        )
        assert all(is_under(p, root) for p in out.values()), f"child loaded roots outside the checkout: {out}"
        return
    assert len(out) == 2, f"probe incomplete (rc={proc.returncode}): {proc.stderr[-800:]}"
    assert is_under(out["CORE_FILE"], root), (
        f"test child loaded coordinator_core from {out['CORE_FILE']}, outside the checkout {root}"
    )


def assert_inherited_pin_child_resolves_checkout(tmp_path: pathlib.Path) -> None:
    """The env a tree's conftest chain leaves in `os.environ`, no pin added by the test: both the
    loaded `coordinator_core` and the dispatch engine root are the checkout's."""
    env = dict(os.environ)
    assert env.get("PYTEST_CURRENT_TEST"), "the ratchet must run as a test"
    core, dispatch = loaded_roots(tmp_path, env)
    root = checkout_root()
    assert is_under(core, root), f"test child loaded coordinator_core from {core}, outside the checkout {root}"
    assert is_under(dispatch, root), (
        f"test child resolved dispatch engine root {dispatch}, outside the checkout {root}"
    )
