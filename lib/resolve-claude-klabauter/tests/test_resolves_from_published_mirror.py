"""
coordinator/lib/resolve-claude-klabauter/tests/test_resolves_from_published_mirror.py

Chunk C2 (docs/plans/2026-08-16-one-engine-for-the-whole-box.md): proves that
a process whose LIVE-TREE rungs are unreachable — no ``CLAUDE_KLABAUTER_ROOT`` env var,
no resolvable ``repos.claude_klabauter`` registry key, no ``.claude-klabauter-root``
sentinel — can still resolve and EXECUTE a real op sourced entirely from the
published engine mirror (``repos.claude_klabauter``).

"The files are published, therefore it resolves" is exactly the claim this
row exists to refuse. Pointing ``CLAUDE_KLABAUTER_ROOT``/``.claude-klabauter-root`` AT the mirror
would prove resolution but not unreachability — Rung 1 short-circuits before
any gate runs. This test instead builds a synthetic settings-home whose
registry carries ONLY ``repos.claude_klabauter`` (no ``claude_klabauter`` key,
no ``.claude-klabauter-root`` file), points ``MACHINE_LOCAL_REGISTRY_DIR`` at it, and
clears ``CLAUDE_KLABAUTER_ROOT``/``CLAUDE_PROJECT_DIR`` from the child env — so rungs
1/1.5/2 all genuinely miss and only the published rung can answer. It then
loads the MIRROR's own ``coordinator_core/claude_klabauter_root.py`` (never this
repo's) by path in a subprocess, asserts it resolves class
``RESOLUTION_RESOLVED_ENGINE`` at the mirror root, and execs a real,
side-effect-free op (``coordinator/bin/publish-resolve-target.py --help``)
FROM the mirror's own ``coordinator/bin/`` — confirming
``_load_shim()``'s partial-checkout ``RuntimeError`` does not fire and the
op produces its normal (exit-0, non-empty stdout) output.

Negative-spec: does NOT hardcode the mirror's absolute path anywhere in this
file (AC12) — resolved at test-collection time from the live
``repos.claude_klabauter`` registry entry via the shim's own
``_resolve_published_engine`` reader, same as any other caller.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C2
Regression-pin, not a Tier-T gate: the mirror this test exercises is a
machine-local artifact (``repos.claude_klabauter``), absent on a clean box
and in any peer's checkout — every test below SKIPS cleanly (never fails)
when the mirror is unregistered or incomplete, rather than becoming
skipping-test theatre that always reports green without ever running.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_SHIM_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"


def _load_local_shim():
    """Load THIS repo's shim by path — used only to discover the currently
    registered mirror path; never to resolve FROM it (that happens inside
    the subprocess, against the mirror's own copy)."""
    spec = importlib.util.spec_from_file_location("_c2_local_resolve_claude_klabauter_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_mirror_root() -> Optional[str]:
    """The live ``repos.claude_klabauter`` mirror path on THIS box, or
    ``None`` if unregistered/unusable. Never hardcoded — read fresh off the
    real machine-local registry every call."""
    shim = _load_local_shim()
    return shim._resolve_published_engine(shim._ml_dir())


def _mirror_carries_required_files(mirror_root: str) -> bool:
    root = Path(mirror_root)
    return (
        (root / "coordinator_core" / "claude_klabauter_root.py").is_file()
        and (root / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py").is_file()
        and (root / "coordinator" / "bin" / "publish-resolve-target.py").is_file()
    )


def _skip_reason(mirror_root: Optional[str]) -> Optional[str]:
    if mirror_root is None:
        return "no published engine mirror registered (repos.claude_klabauter) on this box"
    if not _mirror_carries_required_files(mirror_root):
        return (
            f"published mirror at '{mirror_root}' is missing coordinator_core/claude_klabauter_root.py, "
            "the resolve-claude-klabauter shim, or coordinator/bin/publish-resolve-target.py — a publish "
            "round is needed: run `python coordinator/bin/publish.py claude-klabauter` (or the "
            "matching lib-target row) from claude-klabauter before this pin can execute"
        )
    return None


_MIRROR_ROOT = _registered_mirror_root()
_SKIP_REASON = _skip_reason(_MIRROR_ROOT)


def _mirror_root() -> str:
    """`_MIRROR_ROOT` narrowed to `str` for the bodies below.

    Every caller sits behind `skipif(_SKIP_REASON is not None)`, and a `None`
    root is one of the conditions that produces a skip reason — so this can
    only fire if that guard is removed, which is exactly when a caller should
    hear about it rather than fail later on a `None` path argument."""
    assert _MIRROR_ROOT is not None, "guarded by _SKIP_REASON; see _skip_reason()"
    return _MIRROR_ROOT

_CHILD_SCRIPT = r"""
import importlib.util
import sys
from pathlib import Path

mirror_root = Path(sys.argv[1])
claude_klabauter_root_path = mirror_root / "coordinator_core" / "claude_klabauter_root.py"

spec = importlib.util.spec_from_file_location("_c2_mirror_claude_klabauter_root", claude_klabauter_root_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

root, resolution_class = module.coordinator_claude_klabauter_root_with_class()
print("RESOLVED_ROOT=" + root)
print("RESOLVED_CLASS=" + resolution_class)
"""


@pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))
def test_repointed_resolution_resolves_class_from_mirror_only():
    """With live-tree rungs unreachable (no CLAUDE_KLABAUTER_ROOT, no resolvable
    repos.claude_klabauter, no .claude-klabauter-root), the mirror's OWN claude_klabauter_root.py
    still resolves — and classifies the answer RESOLUTION_RESOLVED_ENGINE,
    proving the published rung (not an accidental live-tree hit) answered."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        settings_home = scratch / "settings-home"
        ml_dir = settings_home / "machine-local"
        ml_dir.mkdir(parents=True)
        (ml_dir / "registry.local.toml").write_text(
            f'"repos.claude_klabauter" = {_mirror_root()!r}\n',
            encoding="utf-8",
        )
        cwd = scratch / "no-git-here"
        cwd.mkdir()

        script_path = scratch / "_c2_child.py"
        script_path.write_text(_CHILD_SCRIPT, encoding="utf-8")

        env = dict(os.environ)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["MACHINE_LOCAL_REGISTRY_DIR"] = str(ml_dir)

        result = subprocess.run(
            [sys.executable, str(script_path), _mirror_root()],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        assert result.returncode == 0, (
            f"mirror-only resolution failed unexpectedly:\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
        assert "RESOLVED_CLASS=resolved-engine" in result.stdout, result.stdout
        assert f"RESOLVED_ROOT={_mirror_root()}" in result.stdout, result.stdout


@pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))
def test_real_op_executes_from_mirror_under_unreachable_live_tree():
    """Runs a real, side-effect-free op (publish-resolve-target.py --help)
    straight out of the mirror's own coordinator/bin/, under the same
    unreachable-live-tree env as above. Asserts the shim's
    _load_shim()-adjacent partial-checkout RuntimeError does NOT fire (exit
    0, no traceback on stderr) and the op produces its normal output."""
    target = Path(_mirror_root()) / "coordinator" / "bin" / "publish-resolve-target.py"

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        cwd = scratch / "no-git-here"
        cwd.mkdir()

        env = dict(os.environ)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        # Deliberately no MACHINE_LOCAL_REGISTRY_DIR override here — the op
        # is exec'd directly (not via exec_cli's own resolution ladder), so
        # this leg proves the mirror's CODE runs standalone once resolved,
        # not a second resolution pass.

        result = subprocess.run(
            [sys.executable, str(target), "--help"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        assert result.returncode == 0, (
            f"real op from mirror failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "Traceback" not in result.stderr
        assert "partial" not in result.stderr.lower()
        assert result.stdout.strip() != ""
