"""close_out_and_stamp imports with `lib` pre-cached and bin/lib off sys.path."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_with_lib_cached_and_bin_lib_absent():
    code = textwrap.dedent(
        """
        import sys
        import os
        bin_dir = sys.argv[1]
        bin_lib = os.path.realpath(os.path.join(bin_dir, "lib"))
        def _on_path():
            return any(os.path.realpath(p) == bin_lib for p in sys.path if p)
        sys.path.insert(0, bin_dir)
        import lib
        sys.path[:] = [p for p in sys.path if not (p and os.path.realpath(p) == bin_lib)]
        sys.modules.pop("plan_completeness", None)
        assert not _on_path(), "bin/lib still on sys.path before the import"
        import coordinator_core.execute_plan_assemble.close_out_and_stamp as m
        assert _on_path(), "bin/lib not on sys.path after the import"
        assert m._plan_completeness.__name__ == "plan_completeness"
        import coordinator_core.ops as ops
        ops._eager_import_all()
        assert not ops.get_poisoned_modules(), ops.get_poisoned_modules()
        from coordinator_core import ipc
        assert "ceremony.update_docs_scan" in ipc._REGISTRY
        """
    )
    r = subprocess.run(
        [sys.executable, "-c", code, str(_ROOT / "coordinator" / "bin")],
        cwd=_ROOT, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert r.returncode == 0, r.stderr
