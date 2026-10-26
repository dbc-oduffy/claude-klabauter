"""The prep slice id of a review run: one stdlib-only definition shared by the driver prep and
the reviewer-side prep. Never import execute_review here; this module stays import-cheap."""
import re
from pathlib import Path
from typing import Optional

RUN_PREP_PLAN_STEM = "manifest"


def prep_slice_id_for(
    plan_path: str, run_base_sha: Optional[str], run_key: Optional[str] = None
) -> str:
    """The frozen-diff slice id of one run's prep: plan stem, `run_key` (the run's own unique id)
    and run base, so concurrent runs never share `state/review-trail/diffs/<id>.diff`. Every
    caller passes its run key: a plan-less route (queue grind, ask manifest) has no distinguishing
    stem, so the run base alone would collide."""
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]", "-", s)  # noqa: E731
    parts = [safe(Path(plan_path.replace("\\", "/")).stem), safe(run_key or ""), (run_base_sha or "nobase")[:12]]
    return "-".join(p for p in parts if p) + "-prep"


def run_prep_slice_prefix(run_id: str, base: str) -> str:
    """The canonical slice prefix of a run-mode prep (the ask route's manifest.json prep):
    `manifest-<safe run_id>-<base[:12]>-prep`."""
    return prep_slice_id_for(f"{RUN_PREP_PLAN_STEM}.json", base, run_id)
