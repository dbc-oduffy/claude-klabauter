"""
coordinator_core.root_channel_reconcile -- reconcile the channels that each
answer "where is root X?", and report where they disagree.

THE DEFECT SHAPE, three times in one day:

  1. 2026-08-22, install dogfood on `machine-b`. The pointer file
     `<settings-home>/machine-local/.claude-klabauter-root` held a
     drive-lettered Windows path for the published mirror -- on a Mac,
     arrived through a git-synced `~/.claude` meta-repo. The registry key
     `repos.claude_klabauter` held that box's correct POSIX path the whole
     time. The two channels
     disagreed SILENTLY: `cc_invoke`'s rung 1.5
     (`coordinator/bin/lib/engine_bootstrap.py :: _resolve_engine_root`)
     reads the pointer, finds no stamp under a path that does not exist,
     and falls through to a lower rung that resolves the live tree -- so
     every live op from settings-home `bin/` failed with a message about
     an unstamped clone, naming no path and no pointer file.

  2. 2026-08-22, independently. DoE's `_engine_root.resolve_claude_klabauter_root`
     answers `repos.claude_klabauter` (classed `resolved-engine`) while
     claude-klabauter's `trusted_root_guard._claude_klabauter_root` answers
     `repos.claude_klabauter` and never reads that key at all -- one ladder
     hands out a root the other refuses, and an operator was handed an
     unrunnable remediation path out of it.
     Filed: `state/bug-backlog/2026-08-22-published-mirror-resolves-as-
     claude-klabauter-root-but-is-never-trusted.yaml`.

  3. 2026-08-22, same day again. `require_dispatch_engine_on_path()`
     resolves the published mirror, so a script running FROM the live tree
     imports `coordinator_core` from the mirror instead -- measured against
     `coordinator/scripts/chain-walk.py --preflight`, which kept reporting
     its `coordinator-claude` dependency `[FAIL]` after the fix for it had
     landed in the live tree and its unit tests passed. The tree and the
     mirror are two channels for "which code is running", and nothing said
     they had diverged.

All three are the same defect: N channels answer one question, a resolver
picks one by precedence, and the disagreement is never surfaced. Precedence
is correct and is NOT what this module changes -- resolvers keep their
ladders. What was missing is a reader that looks at every channel at once and
says which ones disagree, and which name a path that is not there.

GENERAL, NOT PER-FILENAME (this module's reason to exist): the fix asked for
after incident 1 was "detect a pointer file that disagrees with its registry
key". Written for `.claude-klabauter-root` alone it would have seen neither
of the other two. `_ROOTS` is therefore a table of logical roots, each with
the full set of channels that claim to answer for it; adding a root or a
channel is a row, not a code path.

WHAT THIS DOES AND DOES NOT REACH. Incidents 1 and 2 are both channels
holding a PATH, and this module reconciles them directly. Incident 3 is one
rung further out -- the divergence is between two trees' CONTENTS, not two
recorded paths -- so what lands here for it is the `claude_klabauter` row
proving the mirror path every importer resolves to. Closing incident 3
properly needs a content/stamp comparison between the live tree and the
mirror it publishes to, which belongs with `publish_lag` (already the
publish-staleness reader) rather than here; the backlog row filed alongside
this module carries that.

Negative-spec:
  - Does NOT resolve a root for any caller and does NOT change any ladder's
    precedence. `engine_root`, `engine_bootstrap`, and `trusted_root_guard`
    keep their own orderings; this module only reports on them.
  - Does NOT write, repair, or normalise any channel. A disagreement is an
    operator decision (which of two paths is the real one), never something
    to guess at -- and one of the channels here lives in an operator's
    git-synced meta-repo, shared across machines.
  - Does NOT spawn a subprocess and does NOT import `coordinator_core.ops`
    (the same bound `engine_root_census` documents: this is reachable from
    diagnostics on the dispatch path, and an `ops` import there costs an
    eager walk of ~206 op modules).
  - Never raises out of `reconcile()` / `reconcile_all()`: an unreadable
    channel is reported as unreadable, which is itself the finding.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

__all__ = [
    "Channel",
    "RootReconciliation",
    "reconcile",
    "reconcile_all",
    "disagreement_message",
]


@dataclass(frozen=True)
class _ChannelSpec:
    """One place a root's path can be written down. `kind` is either
    `"registry"` (a dotted machine-local registry key) or `"pointer"` (a
    machine-local pointer file's basename)."""

    kind: str
    locator: str


@dataclass(frozen=True)
class _RootSpec:
    name: str
    purpose: str
    channels: Tuple[_ChannelSpec, ...]


#: The logical roots this box writes down more than once, and every channel
#: that claims to answer for each. Rows, not code paths -- see the module
#: docstring's "GENERAL, NOT PER-FILENAME".
#:
#: `publish.mirrors.claude_klabauter.path` is a genuine third channel for the
#: published mirror, not a near-duplicate: `publish.py` writes the mirror to
#: the path THAT key names, while dispatch resolves through
#: `repos.claude_klabauter`. The two pointing at different trees means
#: publishing lands somewhere nothing dispatches to, which is exactly the
#: class of silent split this module exists to name.
_ROOTS: Tuple[_RootSpec, ...] = (
    _RootSpec(
        name="claude_klabauter",
        purpose="the published engine build every warm dispatch resolves to (DR-326)",
        channels=(
            _ChannelSpec("registry", "repos.claude_klabauter"),
            _ChannelSpec("pointer", ".claude-klabauter-root"),
            _ChannelSpec("registry", "publish.mirrors.claude_klabauter.path"),
        ),
    ),
    _RootSpec(
        name="claude_klabauter",
        purpose="the live claude-klabauter working tree (source, never a dispatch target)",
        channels=(
            _ChannelSpec("registry", "repos.claude_klabauter"),
            _ChannelSpec("pointer", ".claude-klabauter-root"),
            _ChannelSpec("registry", "engine.working_repos.claude_klabauter"),
        ),
    ),
)


@dataclass(frozen=True)
class Channel:
    """One channel's answer for one root.

    `value` is the raw text the channel held, `None` when the channel is
    silent (key absent, pointer file absent) and the empty string never
    appears -- an empty registry value is "not found" by
    `machine_resolver.registry_get`'s own contract and an empty pointer file
    is treated the same way here.

    `origin` names the exact key or file path the value came from, so a
    reader is sent to the thing they have to edit rather than to a concept.
    `unreadable` distinguishes "this channel said nothing" from "this
    channel could not be read", which are different operator problems.
    """

    name: str
    origin: str
    value: Optional[str]
    exists: bool
    unreadable: bool = False


@dataclass(frozen=True)
class RootReconciliation:
    """Every channel's answer for one root, plus what is wrong with the set."""

    root: str
    purpose: str
    channels: Tuple[Channel, ...]
    #: Channel names whose values are not the same path, when two or more
    #: channels spoke. Empty when the speaking channels agree.
    disagreeing: Tuple[str, ...]
    #: Channel names whose value names a path that is not a directory on
    #: this box. A pointer carried over from another machine lands here.
    absent_targets: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.disagreeing and not self.absent_targets


def _normalise(raw: str) -> str:
    """Compare-form for a channel value: whitespace-stripped, trailing
    separators removed, `os.path.normpath`'d.

    Deliberately NOT `Path.resolve()`. A channel value naming a path that
    does not exist here is the case this module was built for, and
    `resolve()` on a foreign-platform path (a drive-lettered one on a Mac)
    silently anchors it to the current working directory -- turning the
    finding into a plausible-looking absolute path that differs per caller.
    """
    text = raw.strip().rstrip("/\\")
    if not text:
        return ""
    return os.path.normpath(text)


def _read_pointer(machine_local: Path, basename: str) -> Tuple[Optional[str], bool]:
    """Return `(value, unreadable)` for a machine-local pointer file."""
    path = machine_local / basename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, False
    except (OSError, UnicodeDecodeError):
        return None, True
    return (text or None), False


def _read_registry(key: str) -> Tuple[Optional[str], bool]:
    """Return `(value, unreadable)` for a dotted machine-local registry key."""
    try:
        from coordinator_core.machine_resolver import registry_get

        value = registry_get(key)
    except Exception:
        return None, True
    if value is None:
        return None, False
    value = value.strip()
    return (value or None), False


def _channel_for(spec: _ChannelSpec, machine_local: Path) -> Channel:
    if spec.kind == "pointer":
        origin = str(machine_local / spec.locator)
        value, unreadable = _read_pointer(machine_local, spec.locator)
        name = f"pointer {spec.locator}"
    else:
        origin = spec.locator
        value, unreadable = _read_registry(spec.locator)
        name = f"registry {spec.locator}"
    exists = False
    if value:
        try:
            exists = Path(value).is_dir()
        except OSError:
            exists = False
    return Channel(name=name, origin=origin, value=value, exists=exists, unreadable=unreadable)


def reconcile(root_name: str, machine_local: Optional[Path] = None) -> RootReconciliation:
    """Reconcile every channel for one named root in `_ROOTS`.

    `machine_local` overrides where pointer files are read from; it exists
    for tests and for a caller that already resolved settings-home, not as a
    second resolver -- the default comes from
    `coordinator_core._settings_home.machine_local_dir()`.

    Raises `KeyError` for a name not in `_ROOTS`. That is a caller bug (a
    typo'd row name), not a box condition, and is the one thing here that is
    allowed to raise.
    """
    spec = next((r for r in _ROOTS if r.name == root_name), None)
    if spec is None:
        raise KeyError(root_name)

    if machine_local is None:
        from coordinator_core._settings_home import machine_local_dir

        machine_local = machine_local_dir()

    channels = tuple(_channel_for(c, Path(machine_local)) for c in spec.channels)

    speaking = [c for c in channels if c.value]
    forms = {_normalise(c.value or "") for c in speaking}
    disagreeing: Tuple[str, ...] = ()
    if len(forms) > 1:
        disagreeing = tuple(c.name for c in speaking)

    absent = tuple(c.name for c in speaking if not c.exists)

    return RootReconciliation(
        root=spec.name,
        purpose=spec.purpose,
        channels=channels,
        disagreeing=disagreeing,
        absent_targets=absent,
    )


def reconcile_all(machine_local: Optional[Path] = None) -> Tuple[RootReconciliation, ...]:
    """Reconcile every root in `_ROOTS`."""
    return tuple(reconcile(spec.name, machine_local) for spec in _ROOTS)


def disagreement_message(reports: Sequence[RootReconciliation]) -> Optional[str]:
    """One operator-facing block naming every disagreeing or absent channel,
    or `None` when every root reconciles.

    Guard-messaging register (`docs/wiki/guard-messaging.md` § Register):
    one fact per line, each line naming the exact key or file to edit, and a
    single terse alternative at the end. No decision-record citation --
    channels disagreeing is a data condition, and citing a ruling for it
    sends the reader to a decision record instead of to their pointer file.
    """
    lines: list[str] = []
    for report in reports:
        if report.ok:
            continue
        for channel in report.channels:
            if channel.name in report.disagreeing or channel.name in report.absent_targets:
                state = "path does not exist" if not channel.exists else "disagrees"
                lines.append(
                    f"  {report.root}: {channel.origin} = {channel.value!r} ({state})"
                )
    if not lines:
        return None
    return (
        "root-resolution channels disagree; each line is a place the root is "
        "written down:\n"
        + "\n".join(lines)
        + "\nCorrect the channel that is wrong; resolvers read them in their own "
        "precedence order and will not reconcile this for you."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: `python3 -m coordinator_core.root_channel_reconcile`.

    Exit 0 when every root reconciles, 1 when any channel disagrees or names
    an absent path. Runnable before any Claude Code session exists -- this is
    the remediation `warm.client` and `skew` name on the cold path, where a
    slash command cannot run (`coordinator/tests/
    test_cold_path_remediation_is_runnable.py`).
    """
    import argparse

    parser = argparse.ArgumentParser(prog="root_channel_reconcile")
    parser.add_argument(
        "--machine-local",
        default=None,
        help="machine-local directory to read pointer files from (default: resolved settings-home)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    reports = reconcile_all(Path(args.machine_local) if args.machine_local else None)
    for report in reports:
        print(f"{report.root} -- {report.purpose}")
        for channel in report.channels:
            if channel.unreadable:
                shown = "<unreadable>"
            elif channel.value is None:
                shown = "<silent>"
            else:
                shown = f"{channel.value} ({'exists' if channel.exists else 'ABSENT'})"
            print(f"  {channel.name}: {shown}")

    message = disagreement_message(reports)
    if message is None:
        print("all root-resolution channels agree")
        return 0
    print(message)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
