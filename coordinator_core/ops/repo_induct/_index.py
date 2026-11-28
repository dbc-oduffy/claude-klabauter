"""repo.induct index step: verify the repo is in example-retrieval-repo's registry, else launch the detached build for that repo alone."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from coordinator_core.ops.repo_induct._contract import StepRecord, make_step

STEP = "index"
_ENGINE_ROOT = Path(__file__).resolve().parents[3]


def _canon(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser()


def _narrow_plan(rag, slug: str, root: Path, extractor_install_argv, fleet_languages, lite_producers: str) -> dict:
    cli = [rag.interpreter, str(rag.root / "project_rag_cli.py"), "index"]
    return {
        "rag_root": str(rag.root),
        "interpreter": rag.interpreter,
        "roots": {slug: str(root)},
        "register_argv": {slug: cli + ["--register-only", "--project-root", str(root)]},
        "index_argv": {
            slug: cli + ["--project-root", str(root), "--non-interactive", f"--producers={lite_producers}"]
        },
        "index_needs_kind": True,
        "extractor_argv": extractor_install_argv(rag, fleet_languages([root])),
    }


def index_step(
    root: str | Path,
    repo_key: str,
    *,
    spawn: Callable[..., bool] | None = None,
    engine_root: Path | None = None,
) -> StepRecord:
    """Verify `root` is registered with example-retrieval-repo; when absent, launch the one-repo worker detached and never wait.

    Detail carries `state=registered|launched|disabled|<failure reason>`; `changed` holds the plan path on launch.
    `spawn` defaults to `detached_spawn.spawn_detached`.
    """
    from coordinator_core._settings_home import settings_home
    from coordinator_core.install import rag_fleet_registration as reg

    rag = reg.detect_example_retrieval_repo()
    if rag is None:
        return make_step(STEP, "failed", "state=example_retrieval_repo_not_detected: set repos.project_rag")
    canon_root = _canon(root)
    if canon_root in reg.registered_roots(rag):
        return make_step(STEP, "skipped", "state=registered")
    if os.environ.get("COORDINATOR_DISABLE_MACHINE_MUTATION", "").strip() not in ("", "0"):
        return make_step(STEP, "skipped", "state=disabled: COORDINATOR_DISABLE_MACHINE_MUTATION set")

    slug = repo_key.split(".", 1)[1]
    plan = _narrow_plan(rag, slug, canon_root, reg.extractor_install_argv, reg.fleet_languages, reg.LITE_PRODUCERS)
    work_dir = settings_home() / "rag-fleet-registration" / slug
    plan["status_path"] = str(work_dir / "status.json")
    plan["log_path"] = str(work_dir / "worker.log")
    plan_path = work_dir / "plan.json"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    except OSError as exc:
        return make_step(STEP, "failed", f"state=plan_write_failed: {exc}")

    if spawn is None:
        from coordinator_core.ops.ceremony import detached_spawn

        spawn = detached_spawn.spawn_detached
    script = (engine_root or _ENGINE_ROOT) / "scripts" / "rag_fleet_register.py"
    if not spawn(str(engine_root or _ENGINE_ROOT), str(script), [str(plan_path)]):
        return make_step(STEP, "failed", f"state=spawn_failed: run {script} {plan_path}")
    return make_step(STEP, "done", f"state=launched status={plan['status_path']}", [str(plan_path)])
