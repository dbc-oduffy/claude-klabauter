"""`dispatch.objective_write`: write the run's objective.json once at sizing.

Never pins: the script's sha256 is recorded as a candidate and `falsifier_sha256` / `pinned_at` stay
null until calibration (`dispatch.objective_calibrate`) passes.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.win_portability import no_console_creationflags

_FIELDS = (
    Field("run_id", "nonempty_str", True),
    Field("falsifier_script", "nonempty_str", True),
    Field("axes", "list", True),
    Field("verbatim", "str"),
)
_BASE_FAILURES = frozenset({
    oc.REASON_UNPARSEABLE, oc.REASON_NO_SUMMARY, oc.REASON_SUMMARY_DISAGREES,
    oc.REASON_MISSING_AXIS, oc.REASON_DUPLICATE_AXIS, oc.REASON_BAD_PASS,
})


def _refuse(error: str) -> dict:
    return {"written": False, "error": error}


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(root: Path, rel: str) -> bool:
    from coordinator_core.git.run import run_git

    res = run_git(["--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel], cwd=str(root))
    return res.returncode == 0


def _sizing_verbatim(root: Path, dir_: Path) -> Optional[str]:
    """The PM's newest `pm_verbatims` text, else the first register row's `source_text`; None when neither exists."""
    import yaml

    from coordinator_core.ops import requirement_register as rr
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    run = rl.read_run(dir_)
    rel = run.get("sizing_rel") if run else None
    if not isinstance(rel, str) or not rel:
        return None
    try:
        text = rl._sizing_path(root, rel).read_text(encoding="utf-8")
        sizing = yaml.safe_load(text)
    except (OSError, rl.LedgerRefused, yaml.YAMLError):
        return None
    if not isinstance(sizing, dict):
        return None
    quotes = rl.pm_words(sizing)
    if quotes:
        return quotes[0]
    register = rr.read_register(text)
    for row in (register.rows if register is not None else []):
        source = row.get(rr.ROW_TEXT) if isinstance(row, dict) else None
        if isinstance(source, str) and source.strip():
            return source.strip()
    return None


def _run_at_base(root: Path, script: Path, axes: list) -> Optional[list]:
    """The script's per-axis lines when it runs at base and prints well-formed output, else None."""
    try:
        proc = subprocess.run(
            oc.falsifier_argv(script), cwd=str(root), capture_output=True, text=True,
            timeout=oc.FALSIFIER_TIMEOUT_S, **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    results = oc.parse_falsifier_lines(proc.stdout, axes)
    if any(r.reason in _BASE_FAILURES for r in results):
        return None
    return [r.to_line() for r in results]


@register_op("dispatch.objective_write")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: ``run_id``, ``verbatim`` (optional), ``falsifier_script``, ``axes``.

    Writes ``state/warp/<run_id>/objective.json`` once; a second call is refused. Returns
    ``{written, path, falsifier_candidate_sha256, base_written}`` or ``{written: False, error}``.
    """
    bad = validate_params(oc.OP_WRITE, params, _FIELDS)
    if bad:
        return _refuse(bad["error"])
    root = Path(repo_root) if repo_root is not None else Path.cwd()

    from coordinator_core.atomic_replace import atomic_write_bytes
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    try:
        dir_ = rl.run_dir(root, params["run_id"])
    except rl.LedgerRefused as exc:
        return _refuse(str(exc))
    target = dir_ / oc.OBJECTIVE_FILE
    if target.exists():
        return _refuse(f"{oc.OBJECTIVE_FILE} already written for {params['run_id']}; it is written once and never rewritten")

    axes = []
    for raw in params["axes"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
            return _refuse("axes must be objects each carrying a non-empty string id")
        axes.append(oc.Axis(**{k: v for k, v in raw.items() if k in oc.Axis.__dataclass_fields__}))
    if not axes or len({a.id for a in axes}) != len(axes):
        return _refuse("axes must be non-empty with unique ids")

    rel = params["falsifier_script"].replace("\\", "/")
    script = (root / rel).resolve()
    try:
        script.relative_to(root.resolve())
    except ValueError:
        return _refuse(f"falsifier_script {rel!r} escapes the repo root")
    if not script.is_file():
        return _refuse(f"falsifier_script {rel!r} does not exist")
    if not rel.endswith(".py") and not os.access(script, os.X_OK):
        return _refuse(f"falsifier_script {rel!r} is not executable")
    if not _tracked(root, rel):
        return _refuse(f"falsifier_script {rel!r} is not git-tracked")

    verbatim = params.get("verbatim")
    if not isinstance(verbatim, str) or not verbatim.strip():
        verbatim = _sizing_verbatim(root, dir_)
    if not verbatim:
        return _refuse("no verbatim passed and the sizing holds no pm_verbatims or register source_text")

    sha = _sha256(script)
    base_lines = _run_at_base(root, script, axes)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    objective = oc.Objective(
        verbatim=verbatim, falsifier_script=rel, axes=axes,
        falsifier_candidate_sha256=sha, written_at=stamp,
    )
    dir_.mkdir(parents=True, exist_ok=True)
    if base_lines is not None:
        body = {"axes": base_lines, "written_at": stamp}
        atomic_write_bytes(dir_ / oc.BASE_FILE, (json.dumps(body, sort_keys=True) + "\n").encode("utf-8"), preserve_mode=False)
    staged = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    staged.write_bytes((json.dumps(objective.to_json(), sort_keys=True) + "\n").encode("utf-8"))
    try:
        os.link(staged, target)  # fails if a concurrent writer published first: write-once holds
    except FileExistsError:
        return _refuse(f"{oc.OBJECTIVE_FILE} already written for {params['run_id']}; it is written once and never rewritten")
    finally:
        staged.unlink(missing_ok=True)
    return {
        "written": True, "path": str(target), "falsifier_candidate_sha256": sha,
        "base_written": base_lines is not None,
    }
