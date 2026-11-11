"""A served entrypoint's bare import of a `<bin>/lib` module resolves on every call in a worker.

`coordinator/bin/lib/__init__.py` puts its own directory on `sys.path` only when `lib` is first
imported, and `_run_entrypoint` restores `sys.path` after each call. A warm pool worker that already
imported `lib` (an earlier op), and had its `sys.path` restored, served `sizing-assemble --intent`
with `ModuleNotFoundError: No module named 'entry_point_shim'` on about 1 call in 3 at r23.

Hermetic: a throwaway bin dir holds its own `lib` package and a probe entrypoint.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from coordinator_core.ops import invoke_from_argv as ifa

_LIB_INIT = (
    "import os, sys\n"
    "_SELF = os.path.dirname(os.path.abspath(__file__))\n"
    "if _SELF not in sys.path:\n"
    "    sys.path.insert(0, _SELF)\n"
)

_PROBE = (
    "import sys\n"
    "def main(argv=None):\n"
    "    import lib  # noqa: F401\n"
    "    from rebind_probe_shim import VALUE\n"
    "    sys.stdout.write(VALUE)\n"
    "    return 0\n"
)


def _bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "lib" / "__init__.py").write_text(_LIB_INIT, encoding="utf-8")
    (bin_dir / "lib" / "rebind_probe_shim.py").write_text("VALUE = 'bound'\n", encoding="utf-8")
    (bin_dir / "rebind-probe.py").write_text(_PROBE, encoding="utf-8")
    return bin_dir


def test_cached_lib_with_its_dir_off_sys_path_still_resolves_its_modules(monkeypatch, tmp_path):
    bin_dir = _bin(tmp_path)
    monkeypatch.setattr(ifa, "_resolve_entrypoint_script", lambda _name: bin_dir / "rebind-probe.py")
    monkeypatch.delitem(sys.modules, "lib", raising=False)
    monkeypatch.delitem(sys.modules, "rebind_probe_shim", raising=False)
    monkeypatch.setattr(sys, "path", [str(bin_dir)] + [p for p in sys.path])
    importlib.import_module("lib")
    # The worker state at fault: `lib` cached, its directory restored off sys.path, shim never imported.
    sys.path[:] = [p for p in sys.path if p not in (str(bin_dir), str(bin_dir / "lib"))]

    result = ifa._invoke_from_argv({"argv": [], "cwd": str(tmp_path), "entrypoint": "rebind-probe"})

    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"] == "bound"
    assert str(bin_dir / "lib") not in sys.path, "the per-call bind must not outlive the call"
