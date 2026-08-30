"""
coordinator_core.install.door_serving_census — measures SERVING, not presence.

Purpose: `docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-
thoroughly-dead.md`'s C4. The prior census (`forwarder_door_census.py`)
answers a DR-365 eligibility question over generator-known names; it never
asked "does the installed `.exe` actually resolve", which is exactly how the
extensionless twelve and the killed three stayed invisible. This module asks
that question directly, by RESOLVING both candidate targets a real door
image would dial (`<name>.py` then extensionless `<name>`, matching
`invoke_from_argv._resolve_entrypoint_script`'s own two-candidate order) —
never by spawning the image and reading its refusal.

WHY THIS IS A NEW FILE AND NOT AN EDIT TO `forwarder_door_census.py`. That
module answers a different question — DR-365 door ELIGIBILITY on two axes
(op-equivalent, warm-loadable) over generator-known names. It knows about
neither the extensionless twelve nor the six static-family shims, which is
exactly why both classes stayed invisible. It is also owned by a reviewed
peer plan (`docs/plans/2026-08-30-the-repo-launcher-class-stops-shadowing-
the-door.md`) — consolidating the two censuses is a real follow-up and
belongs to whoever holds that file, not duplicated here.

RESOLUTION, NOT PROBING. Probing every settings-home `.exe` spawns one
process per name — 375 processes on a box already running ~50 concurrent
sessions is a load-norm breach on its own (CLAUDE.md § Load norm). This
module resolves both candidate targets from tracked repo/engine state; the
only process this module ever spawns is the explicit, single-name
`--probe NAME` an operator opts into.

FOUR POPULATIONS, REPORTED SEPARATELY, because collapsing any two of them
is what broke this twice:

    SERVES              — settings-home carries an image for the name AND
                           the engine resolves it (via `<name>.py` or the
                           extensionless `<name>`).
    DEFECT              — settings-home carries an image AND the engine
                           does NOT resolve it. The image answers with the
                           door's "no matching coordinator/bin CLI exists"
                           refusal.
    DELIBERATE_NO_IMAGE — no image, the engine does not resolve it, but
                           THIS repo's own `coordinator/bin/` does carry the
                           name (as `<name>.py` or extensionless `<name>`).
                           This is the publisher-side/renamed population
                           `door_install.launcher_is_installable` already
                           carves out — correct, and must never be reported
                           as a gap.
    PENDING_CUTOVER     — no image, the engine does not resolve it via
                           `coordinator/bin/`, and it is not one of the
                           publisher-side names above either: it is a
                           static-family name the generator installs via a
                           non-`coordinator/bin` mechanism (see
                           `substrate._static_bin_family_names`) or one the
                           engine WOULD resolve once cut over. A gap, and
                           NOT the same thing as DELIBERATE_NO_IMAGE — see
                           `_classify` for how the two are told apart
                           without a hardcoded name list.

Negative-spec (RAG-bait):
    This module does NOT hardcode "the twelve", "the six", or "the three"
    as name lists anywhere. Every bucket is derived by reading actual
    on-disk state (settings-home images, this repo's `coordinator/bin/`,
    the resolved engine root's `coordinator/bin/`, and
    `substrate._static_bin_family_names()`, which is itself generator
    state, not a census-invented list) — a literal list here would be a
    second source of truth that goes stale the first time any of those
    three populations changes membership.

    This module does NOT treat settings-home `.exe` presence as anything
    but the SERVES/DEFECT split's corroborating signal for a name the
    engine already resolves or fails to resolve — never as the sole
    predicate for DELIBERATE_NO_IMAGE vs. PENDING_CUTOVER (Anti-scope,
    origin plan: settings-home content is last-install-run state, not
    tracked repo state, and moved 368->375 mid-session with no code
    change).

    This module does NOT probe by default. `render_census` never spawns a
    process; only `--probe NAME` does, and it spawns exactly one.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-thoroughly-dead.md, chunk C4
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.install import door_install
from coordinator_core.install.engine_root_for_install import resolve_engine_root_for_install
from coordinator_core.install.substrate import _static_bin_family_names

#: This file lives at `<repo_root>/coordinator/bin/door-serving-census.py`
#: (once installed there) as well as being importable from
#: `coordinator_core/install/`. Callers pass their own `generator_bin_dir`
#: rather than this module re-deriving one from `__file__`, since the two
#: on-disk locations are not the same depth.
DEFAULT_GENERATOR_BIN_DIR = Path(__file__).resolve().parents[2] / "coordinator" / "bin"

SERVES = "SERVES"
DEFECT = "DEFECT"
DELIBERATE_NO_IMAGE = "DELIBERATE_NO_IMAGE"
PENDING_CUTOVER = "PENDING_CUTOVER"


@dataclass(frozen=True)
class CensusRow:
    name: str
    bucket: str
    has_image: bool
    resolves_in_engine: bool
    in_generator_bin: bool


def _settings_home_root() -> Path:
    """Same env-var convention every forwarder in this codebase honors —
    `COORDINATOR_SETTINGS_HOME`, falling back to the documented default.
    Not a new resolver: mirrors `forwarder_door_census._settings_home_root`
    (private to that module, so duplicated here rather than imported)."""
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if env:
        return Path(env)
    return Path.home() / ".coordinator-claude-settings"


def _resolves_two_candidate(bin_dir: Path, name: str) -> bool:
    """True when `bin_dir/<name>.py` or the extensionless `bin_dir/<name>`
    exists as a regular file — the same two-candidate order
    `invoke_from_argv._resolve_entrypoint_script` and the cold `door.c` /
    `door_posix.c` fall-through both resolve against (`.py` first, always
    wins when both exist)."""
    if (bin_dir / f"{name}.py").is_file():
        return True
    extensionless = bin_dir / name
    return extensionless.is_file()


def _generator_bin_names(generator_bin_dir: Path) -> "set[str]":
    """Every user-facing CLI name this repo's own `coordinator/bin/`
    carries, as either `<name>.py` or an extensionless `<name>`. Excludes
    directories, dotfiles, and leading-underscore implementation modules
    (`_queue_append_locator.py` and kin) — none of those is a door image's
    basename."""
    names: "set[str]" = set()
    if not generator_bin_dir.is_dir():
        return names
    for entry in generator_bin_dir.iterdir():
        if entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.suffix == ".py":
            names.add(entry.stem)
        elif entry.suffix == "":
            names.add(entry.name)
    return names


def _static_family_bare_names() -> "set[str]":
    """Bare (extensionless, non-underscore) names drawn from
    `substrate._static_bin_family_names()` — the generator's own record of
    every filename `_install_bin_resolvers` writes for the static launcher
    families (claude-home, machine-local, resolve-coordinator-clone, and
    kin). That set mixes bare names, `.cmd`/`.ps1`/`.py` twins, and
    implementation modules; this filters to the bare, user-invocable
    subset, the same shape a door image's basename takes.

    Not a substitute for reading `coordinator/bin/` — this is the ONLY
    source that can see a name whose CLI is generated from
    `coordinator/lib/claude-home/` or DoE's `templates/bin/` rather than
    from `coordinator/bin/` at all, which is exactly the six static-family
    shims' shape before they are cut over (C5)."""
    names: "set[str]" = set()
    try:
        raw = _static_bin_family_names()
    except Exception:
        return names
    for entry in raw:
        if entry.startswith("_") or entry.startswith("."):
            continue
        if "." in entry:
            continue
        names.add(entry)
    return names


def _installed_image_names(settings_home_bin: Path) -> "set[str]":
    names: "set[str]" = set()
    if not settings_home_bin.is_dir():
        return names
    for entry in settings_home_bin.iterdir():
        if entry.is_file() and entry.suffix == ".exe":
            names.add(entry.stem)
    return names


def _installed_cmd_names(settings_home_bin: Path) -> "set[str]":
    """Corroboration only (Anti-scope) — surfaced so an operator can see
    which PENDING_CUTOVER names are still served by a `.cmd` shim today,
    never consulted by `_classify` to decide a bucket."""
    names: "set[str]" = set()
    if not settings_home_bin.is_dir():
        return names
    for entry in settings_home_bin.iterdir():
        if entry.is_file() and entry.suffix == ".cmd":
            names.add(entry.stem)
    return names


def _classify(
    name: str,
    *,
    has_image: bool,
    resolves_in_engine: bool,
    in_generator_bin: bool,
    is_static_family_bare_name: bool,
) -> str:
    if has_image and resolves_in_engine:
        return SERVES
    if has_image and not resolves_in_engine:
        return DEFECT
    if resolves_in_engine:
        return PENDING_CUTOVER
    if in_generator_bin:
        return DELIBERATE_NO_IMAGE
    if is_static_family_bare_name:
        return PENDING_CUTOVER
    raise ValueError(f"door_serving_census: {name!r} is not a coordinator/bin candidate")


def build_census(
    *,
    generator_bin_dir: Optional[Path] = None,
    engine_bin_dir: Optional[Path] = None,
    settings_home_bin: Optional[Path] = None,
) -> "list[CensusRow]":
    """Resolution-based census over the union of every name any of the
    three trackers (installed images, this repo's `coordinator/bin/`, the
    static-family manifest) knows about. Never spawns a process."""
    generator_bin_dir = generator_bin_dir or DEFAULT_GENERATOR_BIN_DIR
    settings_home_bin = settings_home_bin or (_settings_home_root() / "bin")

    if engine_bin_dir is None:
        resolved = resolve_engine_root_for_install()
        engine_bin_dir = (
            (resolved.root / "coordinator" / "bin") if resolved.root is not None else None
        )

    generator_names = _generator_bin_names(generator_bin_dir)
    static_family_names = _static_family_bare_names()
    installed_names = _installed_image_names(settings_home_bin)

    universe = generator_names | static_family_names | installed_names

    rows: "list[CensusRow]" = []
    for name in sorted(universe):
        has_image = name in installed_names
        resolves_in_engine = (
            _resolves_two_candidate(engine_bin_dir, name) if engine_bin_dir is not None else False
        )
        in_generator_bin = name in generator_names
        bucket = _classify(
            name,
            has_image=has_image,
            resolves_in_engine=resolves_in_engine,
            in_generator_bin=in_generator_bin,
            is_static_family_bare_name=name in static_family_names,
        )
        rows.append(
            CensusRow(
                name=name,
                bucket=bucket,
                has_image=has_image,
                resolves_in_engine=resolves_in_engine,
                in_generator_bin=in_generator_bin,
            )
        )
    return rows


def render_census(rows: Iterable[CensusRow]) -> str:
    rows = list(rows)
    lines: "list[str]" = []
    for bucket in (SERVES, DEFECT, DELIBERATE_NO_IMAGE, PENDING_CUTOVER):
        names = sorted(r.name for r in rows if r.bucket == bucket)
        lines.append(f"{bucket} ({len(names)}):")
        for n in names:
            lines.append(f"  {n}")
    total = len(rows)
    defects = sum(1 for r in rows if r.bucket == DEFECT)
    lines.append("")
    lines.append(
        f"total={total} serves={sum(1 for r in rows if r.bucket == SERVES)} "
        f"defect={defects} deliberate_no_image="
        f"{sum(1 for r in rows if r.bucket == DELIBERATE_NO_IMAGE)} "
        f"pending_cutover={sum(1 for r in rows if r.bucket == PENDING_CUTOVER)}"
    )
    return "\n".join(lines)


def _probe(name: str, settings_home_bin: Optional[Path] = None) -> int:
    """Opt-in, single-name process spawn — the ONLY probing path this
    module has. Runs the installed image with `--help` and prints its
    stdout/stderr/exit code verbatim; never called by `build_census`."""
    settings_home_bin = settings_home_bin or (_settings_home_root() / "bin")
    image = settings_home_bin / f"{name}.exe"
    if not image.is_file():
        print(f"door-serving-census: no installed image at {image}", file=sys.stderr)
        return 1
    result = subprocess.run(
        [str(image), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="door-serving-census",
        description=(
            "Census of settings-home door images by whether they actually SERVE "
            "(resolve through the engine), not by file presence. Resolution-based: "
            "never probes unless --probe is given."
        ),
    )
    parser.add_argument(
        "--probe",
        metavar="NAME",
        help="Opt-in: spawn the single named installed image with --help and print its output.",
    )
    args = parser.parse_args(argv)

    if args.probe:
        return _probe(args.probe)

    rows = build_census()
    print(render_census(rows))
    return 1 if any(r.bucket == DEFECT for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
