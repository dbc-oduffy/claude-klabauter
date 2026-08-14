"""Deprecated location — forwards to coordinator/bin/distill-delete-guard.py.

DEC-3 (2026-07-23 claude-klabauter-driven-ceremony-redesign): the distill CLIs relocated to
coordinator/bin/ conventions (discoverability + Windows .cmd twins). This forwarder
keeps `bin/distill-delete-guard.py` working for one release while DoE's PIPELINE.md
is repointed (M1); delete once that repoint lands.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.stderr.write(
    "bin/distill-delete-guard.py is deprecated — use "
    "coordinator/bin/distill-delete-guard.py instead.\n"
)
_target = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "distill-delete-guard.py"
_spec = importlib.util.spec_from_file_location("_distill_delete_guard_relocated", _target)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod  # register before exec — see time_transform.py's _import_seed_module for why
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

if __name__ == "__main__":
    sys.exit(_mod.main(sys.argv[1:]))
