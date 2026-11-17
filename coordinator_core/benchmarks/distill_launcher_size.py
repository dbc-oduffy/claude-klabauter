"""Falsifier for distill.launch_args: the bytes a Workflow call carries inline and the launcher size, per prepared.json.

Each prepared.json is copied into a temp repo root and `distill.launch_args` runs on it. Per file it
prints `args_bytes` (the launch-args.json size: what rode inline at base), `inline_args_bytes` (the reply's
`args` payload, 0 once the op returns a scriptPath) and `launcher_bytes` (the launcher the op wrote) against the runner cap.
Exit 0 when every file fits under the cap with no inline args, else 1.

Usage: python coordinator_core/benchmarks/distill_launcher_size.py <prepared.json> [<prepared.json> ...]
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.distill.manifest_schema import SCHEMA_VERSION
from coordinator_core.ops.dispatch_emit.emit import _WORKFLOW_SCRIPT_BYTE_CAP
from coordinator_core.ops.distill_launch_args import _handler
from coordinator_core.ops.distill_scope import RUN_DIR_REL

RUN_ID = "launcher-size-bench"
_HARVEST_NAME = "distill-harvest.workflow.js"


def measure(prepared: Path, scratch: Path) -> dict[str, Any]:
    """Run the op over a copy of `prepared` under `scratch` and return the row printed for it."""
    root = scratch / "repo"
    run_dir = root / RUN_DIR_REL / RUN_ID
    run_dir.mkdir(parents=True)
    shutil.copyfile(prepared, run_dir / "prepared.json")
    manifest = {"schema_version": SCHEMA_VERSION, "run_id": RUN_ID, "batches": [], "wiki_dirs": ["docs/wiki"], "wiki_slugs": []}
    (run_dir / "input.json").write_text(json.dumps(manifest), encoding="utf-8")
    harvest = scratch / _HARVEST_NAME
    harvest.write_text("// stand-in for the harvest script; the launcher only names its path\n", encoding="utf-8")
    params = {
        "repo_root": str(root),
        "run_id": RUN_ID,
        "prepared_path": f"{RUN_DIR_REL}/{RUN_ID}/prepared.json",
        "workflow_script": harvest.as_posix(),
    }
    reply = _handler(params, None)
    inline = reply.get("args")
    args_bytes = (run_dir / "launch-args.json").stat().st_size
    return {
        "prepared_bytes": prepared.stat().st_size,
        "args_bytes": args_bytes,
        "inline_args_bytes": 0 if inline is None else len(json.dumps(inline, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
        "launcher_bytes": reply["launcher_bytes"],
        "cap": _WORKFLOW_SCRIPT_BYTE_CAP,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    ok = True
    scratch_root = Path(tempfile.mkdtemp(prefix="distill-launcher-size-"))
    try:
        for i, name in enumerate(argv):
            row = measure(Path(name), scratch_root / str(i))
            ok = ok and row["inline_args_bytes"] == 0 and row["launcher_bytes"] <= _WORKFLOW_SCRIPT_BYTE_CAP
            print(f"{name}: " + " ".join(f"{k}={v}" for k, v in row.items()))
    finally:
        rmtree_or_raise(scratch_root, label="distill-launcher-size")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
