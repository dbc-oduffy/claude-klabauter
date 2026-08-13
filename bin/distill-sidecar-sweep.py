"""Deprecated location — forwards to coordinator/bin/distill-sidecar-sweep.py.

DEC-3 (2026-07-23 claude-klabauter-driven-ceremony-redesign): the distill CLIs relocated to
coordinator/bin/ conventions (discoverability + Windows .cmd twins). This forwarder
keeps `bin/distill-sidecar-sweep.py` working for one release while example-doctrine-repo's PIPELINE.md
is repointed (M1); delete once that repoint lands.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.stderr.write(
    "bin/distill-sidecar-sweep.py is deprecated — use "
    "coordinator/bin/distill-sidecar-sweep.py instead.\n"
)
_target = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "distill-sidecar-sweep.py"
_spec = importlib.util.spec_from_file_location("_distill_sidecar_sweep_relocated", _target)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod  # register before exec
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

if __name__ == "__main__":
    sys.exit(_mod.main(sys.argv[1:]))
