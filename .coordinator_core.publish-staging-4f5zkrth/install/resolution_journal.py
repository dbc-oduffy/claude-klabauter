"""
coordinator_core.install.resolution_journal — the cross-process recording
leg for install-time `ShapedClause` resolutions.

Spec backlink: docs/research/2026-08-06-install-receipt-persistence-design.md,
chunk C1

Purpose: `receipt.derive_receipt_entries` needs a `ClauseResolution` for
every `ShapedClause` a writer declares — the concrete `WriteSurfaceEntry`
values that clause resolved to on THIS run. Writers execute across process
boundaries (`maximalist.py` runs phases as subprocesses, and as in-process
callables that return only an `int` return code), so a module-level
in-memory collector cannot carry a runtime resolution back to the
orchestrator that eventually calls `receipt.build_receipt`. Instead,
whichever process performs the write journals what it resolved, at the
write site, into an append-only file; the orchestrator reads the whole
journal back at the end of the run.

Append-only JSONL is the concurrency story: two writer processes racing to
record different rows never risk clobbering each other, because neither
ever reads-modifies-writes the file — each call to `record_resolution`
performs exactly one `os.write` of one already-serialized line, via
`coordinator_core.atomic_append.append_line` (plain `O_APPEND` on POSIX,
genuinely atomic there for a single small write; `CreateFileW` with
`FILE_APPEND_DATA` on Windows, where the CRT's `O_APPEND` emulation is
NOT atomic across processes — see that module's docstring for the
reproduced failure). This module never opens the journal for anything but
a single append or a single full read.

The journal path is resolved from `RESOLUTION_JOURNAL_ENV_VAR` so a
subprocess phase inherits the SAME run-scoped path its parent orchestrator
set, rather than each process independently deriving (and disagreeing on)
a default. Absent that env var, the default lives under settings-home,
resolved the same way `uninstall_legs._settings_home_from` resolves it
(via `_shared.require_home` first) — this module does not invent a second
settings-home resolution ladder.

This module writes to the operator's machine (the journal file itself), so
it declares its own `WRITE_SURFACE` and honours
`COORDINATOR_DISABLE_MACHINE_MUTATION` via a local `_refuse_machine_mutation`
guard, gated ABOVE the append — matching `substrate.py`'s delete-leg
pattern. `coordinator_core/install/tests/test_write_reaching_modules_declare.py`
enforces every write-reaching module declares; this module's entry in that
enforcement carries no allowlist bypass.

Negative spec — this module does NOT:
  - build or persist an `InstallReceipt` (C2/C4's job) — it only carries
    the raw per-writer, per-clause resolutions from write site to
    orchestrator;
  - validate a `WriteSurfaceDeclaration`'s shape (`write_surface.validate`
    already does that) or check a resolution against its clause's
    `entry_template` (`receipt._check_resolution_matches_template` already
    does that, at derivation time, once `read_journal`'s output reaches
    `build_receipt`);
  - collapse "this writer did not report" into "this writer wrote
    nothing" — a malformed, truncated, or unparseable row is skipped
    entirely rather than partially applied, and skipping a row leaves the
    affected writer/clause absent from `read_journal`'s return value, not
    present with an empty resolution (see `read_journal`'s docstring).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from coordinator_core import atomic_append
from coordinator_core.install._shared import require_home
from coordinator_core.install.receipt import ClauseResolution
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

RESOLUTION_JOURNAL_ENV_VAR = "COORDINATOR_INSTALL_RESOLUTION_JOURNAL"
"""The env var the orchestrator (`maximalist.py`, a later chunk) sets at run
start so every child process — subprocess phase or in-process callable
alike — inherits the SAME run-scoped journal path. Named clearly (not
`_JOURNAL` or similarly generic) since it crosses process boundaries and a
collision with an unrelated env var would silently misroute every writer's
resolutions into the wrong file, or into none."""

_JOURNAL_RELATIVE_PATH = os.path.join("install", "resolution-journal.jsonl")
"""Default journal location, relative to settings-home — used only when
`RESOLUTION_JOURNAL_ENV_VAR` is unset. `os.path.join` (not a literal
forward-slash string) so the on-disk relative path this module actually
opens matches `WRITE_SURFACE`'s declared path on every platform."""

_ENTRY_FIELDS = tuple(f.name for f in dataclasses.fields(WriteSurfaceEntry))
"""`WriteSurfaceEntry`'s own field names, read off the dataclass rather than
re-typed here — a future field addition/rename to `WriteSurfaceEntry`
changes this set automatically instead of silently going stale."""


def _default_journal_path() -> Path:
    """The default journal path when `RESOLUTION_JOURNAL_ENV_VAR` is unset:
    `<settings-home>/install/resolution-journal.jsonl`, settings-home
    resolved exactly as `uninstall_legs._settings_home_from` resolves it —
    `_shared.require_home` first, `COORDINATOR_SETTINGS_HOME` override
    honoured inside that helper.

    `_settings_home_from` is imported at call time, not module level.
    `uninstall_legs` eagerly pulls in `coordinator_core.ops`, which reaches
    back into this package's writers, so a module-level import here closes a
    cycle the moment a writer imports this module — which every writer now
    does, to journal its resolutions. Deferring it HERE fixes that once, at
    the root: the alternative is every one of the twelve writers deferring
    its own import of this module, which is the same fix repeated twelve
    times and forgotten on the thirteenth."""
    from coordinator_core.install.uninstall_legs import _settings_home_from

    resolved_home = require_home("resolution_journal")
    settings_home = _settings_home_from(resolved_home)
    return Path(settings_home) / "install" / "resolution-journal.jsonl"


def _journal_path() -> Path:
    """Resolve the run-scoped journal path: `RESOLUTION_JOURNAL_ENV_VAR`
    wins unconditionally when set (including across a subprocess boundary,
    since env vars are inherited); otherwise falls back to
    `_default_journal_path`."""
    override = os.environ.get(RESOLUTION_JOURNAL_ENV_VAR) or ""
    if override:
        return Path(override)
    return _default_journal_path()


def _refuse_machine_mutation(
    path_being_written: str, *, what: str, check_temp_path: bool = False
) -> str | None:
    """Guard in front of this module's real-machine-state mutations (the
    journal append and clear).

    Delegates to `substrate.py`'s implementation rather than carrying a
    second one. The kill switch it enforces
    (`COORDINATOR_DISABLE_MACHINE_MUTATION`) needs exactly one definition:
    the 2026-08-06 chain review's slice D found it extended across
    `substrate.py`'s eight legs while never reaching `substrate_migrate.py`
    or `shell_rc_guard.py`, leaving a real `rmtree` and an IPC-reachable
    `$HOME` rc-file write ungated. A second copy of the predicate makes that
    class of divergence cheaper to create, so there isn't one.

    The import is deferred to call time — the same pattern
    `shell_rc_guard.py`, `substrate_migrate.py` and `receipt.py` use for
    their own back-imports of `substrate.py` — because `substrate.py` will
    import THIS module to journal its own resolutions, and a module-level
    import would close that cycle at load time.

    `check_temp_path` defaults to `False` here where `substrate.py`'s is
    `True`: a journal append is a plain filesystem write, correctly
    sandboxed by a `tmp_path`-rooted `COORDINATOR_INSTALL_RESOLUTION_JOURNAL`
    override — the same reasoning `substrate.py`'s own file/dir call sites
    use when they pass `check_temp_path=False`. The env-var trigger still
    refuses every call regardless.
    """
    from coordinator_core.install import substrate as _substrate_mod

    return _substrate_mod._refuse_machine_mutation(
        path_being_written, what=what, check_temp_path=check_temp_path,
    )


def _entry_to_dict(entry: WriteSurfaceEntry) -> dict:
    return dataclasses.asdict(entry)


def _entry_from_dict(data: dict) -> WriteSurfaceEntry:
    """Reconstruct a `WriteSurfaceEntry` from a decoded JSON object,
    rejecting anything carrying an unrecognized key or missing the
    dataclass's required shape — raises `(TypeError, ValueError)` on any
    mismatch, which `read_journal` catches and treats as an unparseable
    entry (see that function's tolerant-reader contract)."""
    if not isinstance(data, dict):
        raise TypeError(f"entry is not an object: {data!r}")
    unknown = set(data) - set(_ENTRY_FIELDS)
    if unknown:
        raise ValueError(f"entry carries unrecognized field(s): {sorted(unknown)!r}")
    return WriteSurfaceEntry(**data)


def record_resolution(
    writer_id: str, clause_index: int, entries: Sequence[WriteSurfaceEntry]
) -> None:
    """Append one journal row recording that `writer_id`'s `ShapedClause`
    at `clause_index` resolved, on THIS run, to `entries`.

    Called at the write site by the module that owns the surface — the
    same module whose `WRITE_SURFACE` declares the clause being resolved,
    mirroring the declaration protocol's "the module performing the write
    declares it" symmetry.

    Writes exactly one line: a JSON object with `writer_id`, `clause_index`,
    and `entries` (each entry serialized via `dataclasses.asdict`),
    appended via `coordinator_core.atomic_append.append_line` — the shared
    atomic-append primitive (real `O_APPEND` on POSIX, `CreateFileW`/
    `FILE_APPEND_DATA` on Windows) so concurrent callers in different
    processes cannot interleave or clobber each other's rows. Silently
    refuses (prints the reason to stderr, does not raise, does not write)
    when `_refuse_machine_mutation` says so — matching `substrate.py`'s
    delete-leg pattern of failing loud-but-non-fatal rather than aborting
    the writer's own run over a journal-write refusal.
    """
    path = _journal_path()
    blocked = _refuse_machine_mutation(str(path), what="append an install resolution-journal row")
    if blocked:
        print(f"resolution_journal.record_resolution: {blocked}", file=sys.stderr)
        return

    row = {
        "writer_id": writer_id,
        "clause_index": clause_index,
        "entries": [_entry_to_dict(entry) for entry in entries],
    }
    line = json.dumps(row, sort_keys=True) + "\n"
    data = line.encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_append.append_line(path, data)


def read_journal() -> dict[str, dict[int, ClauseResolution]]:
    """Read every row currently in the journal, grouped
    `writer_id -> clause_index -> ClauseResolution`, ready to hand to
    `receipt.build_receipt` (via each writer's own declaration).

    Absent journal (never written this run, or `clear_journal` ran and
    nothing has been appended since) returns `{}` — never raises.

    Tolerant reader: a truncated final line (the run died mid-append), a
    line that is not valid JSON, a row missing `writer_id`/`clause_index`/
    `entries` or carrying the wrong types, or an entry inside `entries`
    that does not round-trip through `WriteSurfaceEntry` is SKIPPED in its
    entirety — never partially applied, and never crashes the read. A
    skipped row leaves that writer/clause pairing ABSENT from the returned
    mapping, which is the intended "did not report" signal — distinct from
    a present, empty `ClauseResolution(entries=())`, which some OTHER
    (well-formed) row for that writer/clause explicitly wrote.

    Multiple valid rows for the same `(writer_id, clause_index)` pair
    ACCUMULATE — their entries union, de-duplicated on the entry's own
    fields.

    Last-write-wins was the original reading ("an append-only log's most
    recent state") and it is wrong for how writers actually journal. A row
    is a PARTIAL CONTRIBUTION from one call site, not a restatement of the
    clause's whole resolution: `dep_check`'s `visited_set_init` and
    `visited_set_crash_cleanup` both resolve clause 1, and `substrate.py`
    reaches several of its ten clauses from more than one write site.
    Under last-write-wins the earlier site's entries vanish from the
    receipt, so uninstall never learns about a surface that was genuinely
    written — the under-report is silent, and silent under-reporting of
    what to remove is the failure mode this whole receipt exists to
    prevent.

    De-duplication matters for the same reason in reverse: a call site that
    runs twice in one process (or re-journals after a retry) must not
    double an entry into the receipt.

    An empty row still marks its clause REPORTED while contributing no
    entries — that is how "this clause resolved to nothing on this machine"
    stays distinguishable from "this clause never reported", which is the
    module's central invariant.
    """
    path = _journal_path()
    if not path.is_file():
        return {}

    try:
        raw = path.read_bytes()
    except OSError:
        return {}

    text = raw.decode("utf-8", errors="strict") if raw else ""
    lines = text.split("\n")
    # A well-formed file ends in a trailing newline, so the final split
    # element is "". Anything else there is a truncated last line (the run
    # died mid-append) — drop it rather than attempting to parse a partial
    # JSON object.
    if lines and lines[-1] != "":
        lines = lines[:-1]

    accumulated: dict[str, dict[int, list[WriteSurfaceEntry]]] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        writer_id = row.get("writer_id")
        clause_index = row.get("clause_index")
        raw_entries = row.get("entries")
        if not isinstance(writer_id, str) or not writer_id:
            continue
        if not isinstance(clause_index, int) or isinstance(clause_index, bool):
            continue
        if not isinstance(raw_entries, list):
            continue
        try:
            entries = tuple(_entry_from_dict(e) for e in raw_entries)
        except (TypeError, ValueError):
            continue

        # Accumulate rather than replace — see this function's docstring on
        # why a clause's rows are partial contributions, not restatements.
        # Dedupe on the entry's own field tuple: a re-run call site
        # re-journaling an identical entry must not double it in the receipt.
        bucket = accumulated.setdefault(writer_id, {}).setdefault(clause_index, [])
        for entry in entries:
            if entry not in bucket:
                bucket.append(entry)

    return {
        writer_id: {
            clause_index: ClauseResolution(entries=tuple(entry_list))
            for clause_index, entry_list in by_clause.items()
        }
        for writer_id, by_clause in accumulated.items()
    }


def clear_journal() -> None:
    """Reset the journal at run start — the orchestrator's job (a later
    chunk wires this into `maximalist.py`'s run-start sequence), never a
    writer's own job.

    Removes the journal file outright rather than truncating it in place,
    so a stale journal from a prior, unrelated run can never be misread as
    partial data for THIS run. Absent-journal is a no-op, not an error.
    Honours `_refuse_machine_mutation` exactly like `record_resolution` —
    a disabled or temp-sandboxed run refuses the delete just as it refuses
    the append.
    """
    path = _journal_path()
    blocked = _refuse_machine_mutation(str(path), what="clear the install resolution-journal")
    if blocked:
        print(f"resolution_journal.clear_journal: {blocked}", file=sys.stderr)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="resolution-journal",
    source_module="coordinator_core.install.resolution_journal",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=_JOURNAL_RELATIVE_PATH,
                    reason=(
                        "run-scoped install resolution journal, written under "
                        f"settings-home (or {RESOLUTION_JOURNAL_ENV_VAR} if set) "
                        "by record_resolution/clear_journal"
                    ),
                ),
            ),
        ),
    ),
)
