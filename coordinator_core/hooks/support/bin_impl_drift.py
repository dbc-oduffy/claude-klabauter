"""Daily drift check: refresh `<settings-home>/bin/` files whose source is the
doctrine plane's `templates/bin/`, when the installed copy has fallen behind.

Ported from DoE-claude `coordinator/hooks/scripts/_bin_impl_drift.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C4. ADAPTATION (the
class-1 site named in this package's own `__init__.py` docstring): DoE's
`_templates_bin()` anchored on `Path(__file__).resolve().parents[2] /
"templates" / "bin"` -- correct there because that module lived three levels
under the doctrine root inside the doctrine-plane `coordinator/hooks/scripts/`
tree, and `templates/bin` is doctrine-plane content this engine does not own
(`docs/reference/boundary-and-data-planes.md`). This module now lives INSIDE
the engine, whose own `__file__`-relative ancestor is this claude-klabauter checkout's
root, not the doctrine-plane root -- porting the old computation verbatim
would silently point the "source" side of this diff at claude-klabauter's own tree
(which has no `templates/bin`) instead of the doctrine plane's. Replaced with
`CLAUDE_PLUGIN_ROOT`-anchored resolution (the harness-supplied doctrine-plane
root at runtime), returning no source directory when that env var is absent
or does not resolve -- matching `message_envelope.resolve_wiki_citation`'s
already-landed convention for the identical displacement (see that module's
own `# ---` comment block). Everything else (the stamp-file cadence, the
normalise-for-compare Windows-bake handling, the native-image refusal, the
atomic copy) is unchanged from the source.

WHY THIS EXISTS. `<settings-home>/bin/` is written at INSTALL time. Between
installs it is a snapshot, so a template that gains a feature ships to nobody
until an operator re-runs the installer on that machine -- a source-vs-install
lag no consumer can see and none can fix from their side. Observed live: a new
`machine-local` verb landed in the template, and a downstream consumer found
the installed impl had no such verb -- on the very machine that authored it.
The consumer's capability-probe fallback held, which is exactly why nothing
surfaced.

COST. The steady-state path is ONE `os.stat` of a stamp file. The real
comparison runs at most once per `_INTERVAL_SECONDS` per machine, and copies
only files that actually differ. This is deliberately a daily check and not a
per-session one: the thing it watches changes when someone edits a template,
not when a session starts, and this machine boots sessions by the dozen.

NEGATIVE-SPEC -- this never CREATES a file in `bin/`. It refreshes only names
that are already installed there. Seeding the family, pruning orphans, and the
bin manifest belong to the install substrate; a hook that added names would
fight that manifest and silently resurrect what the installer just pruned.
Falling behind and being absent are different failures, and only the first
one is this module's.

NEGATIVE-SPEC -- this never overwrites a COMPILED NATIVE IMAGE. A cut-over
door installs as `<name>.exe` on Windows and as the extensionless bare name on
POSIX, so a template name that ever collides with a cut-over name would
otherwise un-cut-over that door: a Mach-O/ELF/PE entry replaced by Python
source, with the door's execute bit carried over, so every caller that execs
it directly gets a file the loader cannot run. Name-only matching is what
makes that reachable, and the refusal is on the file's own bytes because those
are the only honest answer.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

_INTERVAL_SECONDS = 24 * 60 * 60

_STAMP_BASENAME = ".impl-drift-checked"

# `set "_py=<absolute interpreter path>"` — the line the Windows installer bakes
# over the `__PYTHON_BIN__` token. Normalised away before comparing so a BAKED
# install never reads as drifted against its own unbaked template (see
# _normalise_for_compare).
_BAKED_PY_LINE = re.compile(r'^set "_py=.*"$', re.MULTILINE)

_TOKEN_PY_LINE = 'set "_py=__PYTHON_BIN__"'

# Leading bytes of every native-image format a door install can produce: Mach-O
# (both endiannesses plus the fat/universal header), ELF, and PE. Mirrors the
# engine's `coordinator_core.install.door_install.NATIVE_IMAGE_MAGIC`, pinned
# against it by test_bin_impl_drift; carried locally because this module must
# stay stdlib-only (hot-ish daily-check path, no reason to pull the install
# package in just for a byte tuple).
_NATIVE_IMAGE_MAGIC = (
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\x7fELF",
    b"MZ",
)


def _templates_bin() -> "Path | None":
    """The doctrine plane's `templates/bin/`, anchored on `CLAUDE_PLUGIN_ROOT`
    (the harness-supplied doctrine-plane root at runtime), never on
    `__file__` -- see this module's own ADAPTATION note. `None` when
    `CLAUDE_PLUGIN_ROOT` is unset or does not resolve to a real directory;
    `check_and_refresh` treats that the same as "nothing to refresh" and
    stays silent, per this module's fail-open contract.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return None
    root = Path(plugin_root)
    if not root.is_dir():
        return None
    return root / "templates" / "bin"


def _normalise_for_compare(text: str) -> str:
    """Collapse the differences an INSTALL is allowed to introduce.

    Only one exists: the Windows installer substitutes `__PYTHON_BIN__` with an
    absolute interpreter path. Comparing raw bytes would read every correctly-baked
    `.cmd` as drifted, and refreshing it would UNBAKE it -- this module would then
    undo the install-time optimisation it is meant to protect, once a day, forever.

    Line endings are normalised too: `.cmd` files are written CRLF on Windows and
    the template is stored LF, which is a checkout artifact and not drift. Every CR
    is stripped rather than only the CRLF pair -- a file that has been through both
    a CRLF-translating write and a CRLF-checkout carries `\\r\\r\\n`, and a
    pair-only replacement leaves a stray CR that defeats the `$` anchor below,
    which is exactly how a baked shim reads as drifted.
    """
    text = text.replace("\r", "")
    return _BAKED_PY_LINE.sub(_TOKEN_PY_LINE, text)


def _is_native_image(path: Path) -> bool:
    """True iff `path` opens as a compiled native image; unreadable is False.

    Asks the file's own bytes, never its name: under settings-home a cut-over
    door is the extensionless bare name on POSIX and `<name>.exe` on Windows, so
    the name carries no information about which of the two shapes is on disk.
    Unreadable answers False because the copy that follows would fail on the same
    file anyway, and this predicate's job is to name one refusal, not to
    adjudicate every I/O failure.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(8).startswith(_NATIVE_IMAGE_MAGIC)
    except OSError:
        return False


def _differs(src: Path, dst: Path) -> bool:
    try:
        src_text = src.read_text(encoding="utf-8")
        dst_text = dst.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Unreadable or non-text: not something this module can reason about, so
        # it is not something it should overwrite.
        return False
    return _normalise_for_compare(src_text) != _normalise_for_compare(dst_text)


def _copy_atomic(src: Path, dst: Path) -> bool:
    """Replace dst's CONTENT with src's, preserving dst's mode; True on success.

    Atomic because a dozen live sessions share this install surface and any of them
    may invoke `machine-local` mid-write. `os.replace` gives every reader either the
    old file or the new one, never a truncated interpreter script.

    The mode carry-over is load-bearing, not tidiness. `shutil.copyfile` writes
    content only, so the temp file is born at the umask default -- no execute bit.
    Replacing an installed `bin/` entry with it would strip the execute bit from
    `machine-local` and `_machine_local.py` (both tracked 100755, both invoked
    directly off PATH via their shebang), so this sweep would break the very
    invocation path it exists to keep working, on the first refresh, on every POSIX
    machine.

    dst's mode is copied, NOT src's: the installer sets each file's execute bit
    deliberately and per-file (its `exec_bit` argument), so the installed file is
    the authority on what the mode should be. src's mode is a property of how the
    plugin tree happened to be checked out, which is not the same question.
    """
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    try:
        mode = dst.stat().st_mode
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _claim_interval(stamp: Path, now: float) -> bool:
    """True if this process owns this interval's check.

    The stamp is written BEFORE the work, not after: a dozen sessions booting
    together would otherwise all see a stale stamp and all do the same scan. The
    trade is that a crash mid-refresh skips one interval -- acceptable for a
    once-a-day freshness sweep, where the failure mode is 'still stale tomorrow',
    not data loss.
    """
    try:
        if now - stamp.stat().st_mtime < _INTERVAL_SECONDS:
            return False
    except OSError:
        pass  # absent or unreadable → treat as due

    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}.tmp")
        tmp.write_text(f"{now}\n", encoding="utf-8")
        os.replace(tmp, stamp)
    except OSError:
        return False  # read-only settings home: never block the session
    return True


def check_and_refresh(bin_dir: Path, now: float | None = None) -> str | None:
    """Refresh drifted `bin/` files at most once per interval; return a banner or None.

    Returns None on the overwhelmingly common path (checked recently, nothing
    drifted, or `CLAUDE_PLUGIN_ROOT` unresolved -- see `_templates_bin`) so the
    caller emits nothing. Any failure is silent by design: this is a
    freshness convenience, and a session must never fail to start because a refresh
    could not run.
    """
    now = time.time() if now is None else now
    if not _claim_interval(bin_dir / _STAMP_BASENAME, now):
        return None

    src_dir = _templates_bin()
    if src_dir is None or not src_dir.is_dir():
        return None

    refreshed = []
    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        dst = bin_dir / src.name
        # Refresh-only, never seed — see this module's negative-spec.
        if not dst.is_file():
            continue
        # Never un-cut-over a door — see this module's negative-spec.
        if _is_native_image(dst):
            continue
        if _differs(src, dst) and _copy_atomic(src, dst):
            refreshed.append(src.name)

    if not refreshed:
        return None
    return (
        f"── Refreshed {len(refreshed)} stale coordinator bin file(s) from the "
        f"doctrine plane's templates: {', '.join(refreshed)} ──"
    )
