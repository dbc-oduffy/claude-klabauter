#!/usr/bin/env python3
"""Launch codex as a coordinator EM. See codex-coordinator.py."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("codex-coordinator.py")), run_name="__main__")
