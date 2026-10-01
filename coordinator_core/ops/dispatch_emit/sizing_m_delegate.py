"""M+ arm of `emit-dispatch-workflow --sizing`: delegate to `emit-wave-fire --from-sizing` in-process.

emit-wave-fire owns every M+ refusal; nothing here re-checks a sizing field. A non-zero exit,
argparse `SystemExit` included, is returned, never raised.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core import engine_root


def _engine_script() -> Path:
    return Path(engine_root.coordinator_engine_root()) / "coordinator" / "bin" / "emit-wave-fire.py"


def _load_emit_wave_fire(path: Path):
    spec = importlib.util.spec_from_file_location("emit_wave_fire_for_sizing_delegate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def delegate_m_plus(*, sizing_rel: str, repo_root: Path, trail_dir: Path | None = None) -> dict:
    """Fire an M+ sizing through emit-wave-fire; returns `scriptPath`, `exit_code`, `batons`, `refusal`.

    `trail_dir` absent mints `<repo_root>/state/plan-blitz/<UTC stamp>/`.
    """
    root = Path(repo_root).resolve()
    if trail_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trail_dir = root / "state" / "plan-blitz" / stamp
    trail = Path(trail_dir).resolve()
    trail.mkdir(parents=True, exist_ok=True)

    script = _engine_script()
    if not script.is_file():
        return {
            "scriptPath": None,
            "exit_code": 2,
            "batons": [],
            "refusal": f"emit-wave-fire not found at {script}",
        }

    argv = ["--repo-root", str(root), "--trail-dir", str(trail),
            "--from-sizing", sizing_rel, "--json"]
    out, err = io.StringIO(), io.StringIO()
    try:
        mod = _load_emit_wave_fire(script)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    code = int(code or 0)

    result = {"scriptPath": None, "exit_code": code, "batons": []}
    if code != 0:
        result["refusal"] = (err.getvalue() + out.getvalue()).strip()
        return result
    try:
        reply = json.loads(out.getvalue())
        fires = reply.get("fires") or []
    except (ValueError, AttributeError):
        result["exit_code"] = 1
        result["refusal"] = f"emit-wave-fire reply is not JSON: {out.getvalue()[:200]!r}"
        return result
    if fires:
        result["scriptPath"] = fires[0].get("scriptPath")
    result["batons"] = [b for f in fires for b in (f.get("batons") or [])]
    return result
