"""Doctor layers for the boot-time banner checks: hook delivery and the hook-plane verdict.

Reuses the SessionStart readers so the on-demand verdict and the boot banner cannot disagree.
Spawns nothing. `doctor.run_doctor` imports this module lazily to avoid an import cycle.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List

from coordinator_core.ops.doctor import Finding, Layer

HOOK_DELIVERY_NAME = "Hook delivery is not duplicated"
HOOK_PLANE_NAME = "Hook plane is armed"


def hook_delivery_layer(config_dir: Path) -> Layer:
    """broken on double-fire or a resurrected retired guard; unknown when the check cannot decide."""
    from coordinator_core.ops.session.guard_settings_integrity import (
        detect_hook_delivery_duplication,
        format_hook_delivery_banner,
    )

    report = detect_hook_delivery_duplication(config_dir)
    if report.double_fire or report.resurrected_decisions:
        status = "broken"
    elif report.indeterminate or report.degraded:
        status = "unknown"
    else:
        status = "ok"
    banner = format_hook_delivery_banner(report).strip()
    findings: List[Finding] = []
    if banner:
        findings.append(Finding("broken" if status == "broken" else "info", banner))
    return Layer(HOOK_DELIVERY_NAME, status, findings)


def hook_plane_layer(config_dir: Path) -> Layer:
    """broken with one finding per `hook_plane_problems` entry; ok carries the status line."""
    from coordinator_core._settings_home import settings_home
    from coordinator_core.data_root import content_root_for
    from coordinator_core.install.hook_plane_verdict import (
        derive_hook_plane,
        hook_plane_problems,
        hook_plane_status_line,
    )
    from coordinator_core.ops.coordinator_content_root import coordinator_content_root

    content_root = coordinator_content_root()
    plugin_root = content_root_for(content_root) if content_root else None
    try:
        settings_home_path: Path | None = settings_home()
    except RuntimeError:
        settings_home_path = None

    plane = derive_hook_plane(
        claude_home=config_dir, plugin_root=plugin_root, settings_home=settings_home_path
    )
    problems = hook_plane_problems(plane)
    if problems:
        return Layer(HOOK_PLANE_NAME, "broken", [Finding("broken", p) for p in problems])
    return Layer(HOOK_PLANE_NAME, "ok", [Finding("info", hook_plane_status_line(plane))])


def boot_banner_layers(config_dir: Path) -> List[Layer]:
    """Both layers in order; a raising layer becomes `unknown` carrying the exception repr."""
    layers: List[Layer] = []
    checks: List[tuple[str, Callable[[Path], Layer]]] = [
        (HOOK_DELIVERY_NAME, hook_delivery_layer),
        (HOOK_PLANE_NAME, hook_plane_layer),
    ]
    for name, check in checks:
        try:
            layers.append(check(config_dir))
        except Exception as exc:  # a doctor that dies cannot report
            layers.append(Layer(name, "unknown", [Finding("broken", f"{name} check raised {exc!r}.")]))
    return layers
