"""
bin/tests/test_launch_chain_probe_gating.py

Regression guard for `claude-klabauter.launch.shim_chain`'s gating contract.

The probe sets `required` PER RESULT, and both halves of that are load-bearing:

- Real failures (shim absent, wrong shell dialect) return `required=True` so
  `--step-zero` exits 1. On a box that HAS this launch chain, a shim that cannot
  define `claude()` means every session runs without the coordinator plugin.
  An install that ends by calling itself healthy is the exact failure the probe
  exists to end — it is what happened on 2026-08-14.

- The SKIP path (no DoE clone resolves — the marketplace population, which never
  has this chain) returns `required=False`. A skipped REQUIRED probe reduces to
  DEGRADED in `_local_reduce_overall`, so a blanket `required=True` would degrade
  every marketplace install for lacking something it should not have.

Collapsing either half is a silent regression: flipping failures to
`required=False` makes the probe advisory again and the install self-certifies;
flipping the skip to `required=True` degrades installs that are fine. Neither
shows up as a test failure anywhere else.

Spec backlink: docs/reference/interactive-launch-chain.md.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _require_module() -> ModuleType:
    """Import bin/claude-klabauter-doctor-probe.py, mirroring test_generator_staleness_probe.

    Registered in sys.modules BEFORE exec so dataclass annotation resolution
    (sys.modules[cls.__module__]) finds a valid namespace.
    """
    if not _BIN_PROBE.exists():
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")
    key = "claude_klabauter_doctor_probe_launch_chain_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - environment-dependent
        sys.modules.pop(key, None)
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable")
    return mod


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """A sandboxed CLAUDE_HOME with a resolvable DoE clone."""
    shell_dir = tmp_path / ".claude" / "shell"
    shell_dir.mkdir(parents=True)
    doe = tmp_path / "doe"
    (doe / "coordinator").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe))
    return shell_dir


def _shim_path(mod, shell_dir: Path) -> Path:
    import os

    return shell_dir / ("claude-doe-shim.ps1" if os.name == "nt" else "claude-doe-shim.sh")


def _healthy_body() -> str:
    import os

    return (
        "function claude {\n  & claude-doe\n}\n"
        if os.name == "nt"
        else "claude() {\n  claude-doe \"$@\"\n}\n"
    )


def _wrong_dialect_body() -> str:
    import os

    # The OTHER family's definition — the shape maximalist used to install.
    return "claude() {\n  echo bash\n}\n" if os.name == "nt" else "function claude {\n  x\n}\n"


def test_absent_shim_gates(probe_env):
    mod = _require_module()
    _shim_path(mod, probe_env).unlink(missing_ok=True)

    r = mod._run_probe_launch_chain()

    assert r.required is True, "an absent shim must gate — every session loses coordinator"
    assert r.skipped is False
    assert mod._local_reduce_overall([r]) in (mod._DEGRADED, mod._BROKEN)


def test_wrong_dialect_shim_gates(probe_env):
    mod = _require_module()
    _shim_path(mod, probe_env).write_text(_wrong_dialect_body(), encoding="utf-8")

    r = mod._run_probe_launch_chain()

    assert r.status == mod._BROKEN
    assert r.required is True, "a wrong-dialect shim must gate — claude() is never defined"
    assert r.skipped is False


def test_healthy_shim_passes(probe_env):
    mod = _require_module()
    _shim_path(mod, probe_env).write_text(_healthy_body(), encoding="utf-8")

    r = mod._run_probe_launch_chain()

    assert r.status == mod._PASS
    assert mod._local_reduce_overall([r]) == mod._PASS


def test_no_doe_clone_skips_without_degrading(probe_env, monkeypatch):
    """The marketplace population must not be degraded for lacking this chain."""
    mod = _require_module()
    import coordinator_core.ops.coordinator_doe_root as cdr

    monkeypatch.setattr(cdr, "coordinator_doe_root", lambda *a, **k: None)

    r = mod._run_probe_launch_chain()

    assert r.skipped is True
    assert r.required is False, (
        "a skipped REQUIRED probe reduces to DEGRADED — this would degrade every "
        "install that legitimately has no DoE clone"
    )
    assert mod._local_reduce_overall([r]) == mod._INFO
