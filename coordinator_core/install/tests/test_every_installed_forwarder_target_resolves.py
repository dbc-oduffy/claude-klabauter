"""
The detector: for EVERY installed forwarder, does its exec target resolve
under the root its own resolution class picks?

WHY A PROBE AND NOT ANOTHER LIST. Three counts were wrong on 2026-08-28 and
all three were censuses — of `coordinator/bin/`, of the mirror, of
`PUBLISHER_ONLY_TARGETS`. A census cannot see a rename: publish ships
``check-claude-klabauter-doctor-sentinel.sh`` as
``check-claude-klabauter-doctor-sentinel.py``, so grepping either tree for
the other tree's spelling reports a false absence, and every list built that
way agrees with every other one while four CLIs are dead on most of the
fleet. This file maintains no list of any kind. It asks the door the question
the operator's shell asks it, once per installed forwarder.

BOTH CLASSES, BECAUSE THE TWO REPAIRS ARE INVERSE. A publisher-only target
must resolve live-tree-only (the mirror never carries it, and diverting there
yields a program that cannot run); a renamed target must resolve under the
mirror (it ships and works and is merely misaddressed, so pinning it
live-tree-only would break it on every box WITHOUT a checkout). A fix that
repairs one class by breaking the other passes a single-class probe and goes
red here. That is the near-outage this file exists to catch — it very nearly
happened, and the correction that prevented it arrived by message, not by a
test.

SCOPE, STATED RATHER THAN ASSUMED — THIS COVERS THE DOOR, NOT THE ENGINE
IMPORT. Measured across five `coordinator/bin/` targets: not one imports
`coordinator_core` at module level; every engine import is deferred inside a
function. So a forwarder can resolve its door, parse arguments, exit 0, and
still be dead the moment it does real work. Nothing here sees that. Covering
it needs a probe that imports each target module without invoking `main()`,
which is a different instrument with a different cost, and it is NOT this
one. Read a green run from this file as "the door finds the file", never as
"the CLI works".

AND IT NEVER RUNS A TARGET. `--help` is a valid probe for the door and a
false green for the engine import — it exercises the argument parser, which
is exactly the part that runs before the deferred import. The resolution is
driven directly instead, with the invocation stubbed out.

THE VERDICT CARRIES THE RESOLVER'S IDENTITY. Every failure message names the
mtime and content hash of the `_resolve_claude_klabauter.py` this run actually loaded,
and whether the installed copy differs from the repo's. On a box where a
dozen sessions install into a shared settings-home, the instrument is
rewritten underneath a measurement in progress: a green before an install and
a red after read as a difference between TARGETS when they are a difference
in WHEN. That happened during this plan's own authoring and cost six messages
across two repos to unwind. A measurement is only stable if the thing doing
the measuring is.

NEGATIVE SPEC: this file adds no membership list and modifies none. It is a
detector. If it ever grows an "and therefore add these names to X", that is
the conflation it was written against — the fix belongs at the seam that
resolves, not in another list that agrees with the last one.

Spec backlink: pln-the-currency-signal-exists-and-918d50 C4.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from pathlib import Path

import pytest

#: READ-ONLY ORACLE AGAINST THE LIVE INSTALL, which is the whole subject: the
#: defect is what the forwarders ON THIS BOX ask for, and a quarantined home
#: has no forwarders to ask about. Nothing here writes anything, and the
#: machine-mutation kill switch stays set regardless (see
#: `coordinator_core/conftest.py::_quarantine_real_home`).
pytestmark = [pytest.mark.real_home]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_DOOR = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"

#: `exec_cli("<target>")` — the one thing a forwarder body says, and the only
#: thing this probe needs from it.
_EXEC_CLI_CALL = re.compile(r"""exec_cli\(\s*['"]([^'"]+)['"]""")


def _settings_home_bin() -> "Path | None":
    """The installed forwarder directory, resolved the way the door's own
    ladder resolves it — `COORDINATOR_SETTINGS_HOME`, else the default. `None`
    when it does not exist, which is a box with no install, not a failure."""
    raw = os.environ.get("COORDINATOR_SETTINGS_HOME", "").strip()
    root = Path(raw) if raw else Path.home() / ".coordinator-claude-settings"
    bin_dir = root / "bin"
    return bin_dir if bin_dir.is_dir() else None


def _resolver_identity(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"{path} <unreadable: {exc}>"
    return (
        f"{path} mtime={path.stat().st_mtime_ns} "
        f"sha256={hashlib.sha256(raw).hexdigest()[:16]}"
    )


@pytest.fixture(scope="module")
def door():
    """The door as the OPERATOR runs it — the installed copy when there is
    one, falling back to the repo's.

    Loading the repo copy when an installed one exists would test a file no
    forwarder on this box executes, which is the single most plausible way for
    this probe to go green about the wrong thing.
    """
    bin_dir = _settings_home_bin()
    installed = bin_dir / "_resolve_claude_klabauter.py" if bin_dir else None
    path = installed if installed and installed.is_file() else _REPO_DOOR
    spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._probe_loaded_from = path  # type: ignore[attr-defined]
    return module


@pytest.fixture(scope="module")
def provenance(door) -> str:
    """Which resolver answered, and whether the installed and repo copies
    agree. Prepended to every failure in this file."""
    loaded = door._probe_loaded_from
    lines = [f"resolver: {_resolver_identity(loaded)}"]
    if loaded != _REPO_DOOR:
        lines.append(f"repo copy: {_resolver_identity(_REPO_DOOR)}")
        try:
            if loaded.read_bytes() != _REPO_DOOR.read_bytes():
                lines.append(
                    "INSTALLED AND REPO COPIES DIFFER — a verdict here is about "
                    "the installed one, and a repo-side fix is not deployed."
                )
        except OSError:
            pass
    return "\n".join(lines)


@pytest.fixture(scope="module")
def forwarder_targets() -> "dict[str, str]":
    """`{forwarder name: exec_cli target}` for every installed forwarder.

    Read out of the generated bodies rather than derived from
    `coordinator/bin/`, deliberately: the defect class is a forwarder ASKING
    for a name that no longer exists, and a derivation from the current bin
    listing would regenerate the correct name and see nothing.
    """
    bin_dir = _settings_home_bin()
    if bin_dir is None:
        pytest.skip("no installed settings-home bin/ on this box")
    found: "dict[str, str]" = {}
    for path in sorted(bin_dir.iterdir()):
        # `_`-prefixed files are the door's own support modules living beside
        # the forwarders (`_resolve_claude_klabauter.py`, `_machine_local.py`), not
        # forwarders — `_resolve_claude_klabauter.py` names targets in its own docstrings
        # and would otherwise enumerate itself as one.
        if path.name.startswith("_") or path.name.startswith("."):
            continue
        if not path.is_file() or path.suffix in (".cmd", ".ps1", ".exe", ".json"):
            continue
        try:
            match = _EXEC_CLI_CALL.search(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if match:
            found[path.name] = match.group(1)
    if not found:
        pytest.skip("settings-home bin/ carries no forwarders that call exec_cli")
    return found


def _resolved_target(door, bin_dir: str, target: str, resolution_class: str) -> "str | None":
    """The path ``exec_cli`` would run for *target*, or `None` if it fails
    loud (127) instead — driven through ``exec_cli`` itself, never a
    reimplementation of its composition, and with the invocation stubbed so
    nothing is executed."""
    import contextlib
    import io
    import sys

    seen: "dict[str, str]" = {}

    class _OSNameProxy:
        def __init__(self) -> None:
            object.__setattr__(self, "name", "nt")

        def __getattr__(self, attr):
            return getattr(os, attr)

    saved = {
        "resolve_claude_klabauter_root_with_class": door.resolve_claude_klabauter_root_with_class,
        "_resolve_publisher_root": door._resolve_publisher_root,
        "_validate_bin_dir": door._validate_bin_dir,
        "_run_target_in_process": door._run_target_in_process,
        "os": door.os,
    }
    door.resolve_claude_klabauter_root_with_class = lambda: (bin_dir, resolution_class)
    door._resolve_publisher_root = lambda: bin_dir
    door._validate_bin_dir = lambda root: root
    door._run_target_in_process = lambda tp, argv, root: seen.setdefault("t", tp) and 0
    door.os = _OSNameProxy()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                door.exec_cli(target, [])
            except SystemExit:
                pass
    finally:
        for name, value in saved.items():
            setattr(door, name, value)
        sys.stderr = sys.__stderr__
    return seen.get("t")


def _unresolvable(door, bin_dir: Path, targets, resolution_class: str, *, skip_publisher_only: bool):
    out = []
    for name, target in sorted(targets.items()):
        if skip_publisher_only and door._is_publisher_only_target(target):
            continue
        if _resolved_target(door, str(bin_dir), target, resolution_class) is None:
            out.append(f"{name} -> {target}")
    return out


# ---------------------------------------------------------------------------


@pytest.mark.pending_fix
def test_every_forwarder_target_resolves_under_the_class_this_box_picks(
    door, provenance, forwarder_targets
):
    """THE OPERATOR'S ACTUAL QUESTION, asked with nothing stubbed: the door
    resolves its own root and class, and every installed forwarder's target
    must be found under it.

    RED ON THIS BOX, for two causes this probe found and no census had:

    1. The settings-home carries BOTH SPELLINGS OF FOUR CLIS. Four forwarders
       ask for the published name (`check-claude-klabauter-doctor-sentinel.py`)
       and four ask for the claude-klabauter name (`check-claude-klabauter-doctor-sentinel.sh`),
       so whichever class resolves, four of the eight are dead. The install is
       a union of two vintages and nothing removed the older half. C3a's map
       retry covers the claude-klabauter->published direction only; the reverse
       direction has no retry and is a fresh finding.
    2. TWENTY-TWO FORWARDERS NAME A TARGET THAT EXISTS IN NEITHER TREE —
       `migrate-*` for migrations long since run, `render-*`/`query-*` for
       deleted CLIs, and one generated against a stray
       `coordinator-safe-commit.py.your-wip.573859.bak`. Uninstall never
       removed them, so every one is a `127` waiting for whoever types it.

    Neither is this plan's subject and neither is invented by this test; both
    are routed rather than swallowed (see the bug-backlog entry this commit
    files). `pending_fix` comes off when they are, and the assertion is not
    to be weakened to reach green — the failure list IS the deliverable here.
    """
    root, resolution_class = door.resolve_claude_klabauter_root_with_class()
    if not root:
        pytest.skip("the door resolves no root on this box")
    bin_dir = Path(root) / "coordinator" / "bin"
    missing = _unresolvable(
        door, bin_dir, forwarder_targets, resolution_class, skip_publisher_only=False
    )
    assert not missing, (
        f"{provenance}\n"
        f"class={resolution_class} root={root}\n"
        f"{len(missing)} of {len(forwarder_targets)} installed forwarders name a target "
        f"that root cannot serve:\n  " + "\n  ".join(missing)
    )


def test_publisher_only_targets_stay_pinned_to_the_live_tree(
    door, provenance, forwarder_targets
):
    """THE INVERSE-REPAIR CONTROL. These exist nowhere but the live tree, so a
    fix that made them resolvable under the mirror would have converted a name
    bug into an outage on every box without a checkout. They must resolve at
    `RESOLUTION_LIVE_WORKING_TREE` even when the door is handed a
    resolved-engine class — `exec_cli` diverts them upstream of the class
    ladder, and this asserts that divert still happens."""
    publisher_only = {
        name: target
        for name, target in forwarder_targets.items()
        if door._is_publisher_only_target(target)
    }
    if not publisher_only:
        pytest.skip("no publisher-only forwarder installed on this box")
    bin_dir = _REPO_ROOT / "coordinator" / "bin"
    missing = _unresolvable(
        door, bin_dir, publisher_only, door.RESOLUTION_RESOLVED_ENGINE,
        skip_publisher_only=False,
    )
    assert not missing, (
        f"{provenance}\n"
        "a publisher-only forwarder failed to resolve against the live tree while "
        "the class said resolved-engine — the carve-out is not diverting:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.pending_fix
def test_every_forwarder_target_resolves_under_the_published_engine(
    door, provenance, forwarder_targets
):
    """The class a box WITHOUT a claude-klabauter checkout takes — most of the fleet.

    RED UNTIL A PUBLISH ROUND RUNS, and that is the finding, not a defect in
    this test. Four targets are renamed by publish's identity transform, so
    they are absent under the only name their forwarder asks for. C3a retries
    through the map C2 emits; C2's map reaches a mirror only when a round
    lands it. Until then this fails naming exactly the CLIs that are broken on
    the fleet right now, which is the worklist.

    REMOVE `pending_fix` on the first round that carries C2 — do not weaken
    the assertion, and do not enumerate the four here. A list of the names
    that currently fail is the census this whole file was written against.
    """
    mirror = door._resolve_published_engine(door._ml_dir())
    if not mirror:
        pytest.skip("no published engine mirror registered on this box")
    bin_dir = Path(mirror) / "coordinator" / "bin"
    if not bin_dir.is_dir():
        pytest.skip(f"registered mirror carries no coordinator/bin: {bin_dir}")
    missing = _unresolvable(
        door, bin_dir, forwarder_targets, door.RESOLUTION_RESOLVED_ENGINE,
        skip_publisher_only=True,
    )
    assert not missing, (
        f"{provenance}\n"
        f"{len(missing)} of {len(forwarder_targets)} installed forwarders name a target "
        f"the published engine at {mirror} cannot serve:\n  " + "\n  ".join(missing)
    )
