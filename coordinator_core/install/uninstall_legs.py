"""
coordinator_core.install.uninstall_legs — coordinator uninstall leg functions.

Sourceable/importable library of individual uninstall "legs" — each leg
reverses one surface of the maximalist coordinator install (per
``tasks/coordinator-uninstall/surface-map.md``'s authoritative table). Legs
are composed by this module's own ``orchestrate_uninstall`` (C7) but each is
independently callable for testing.

Every leg resolves its filesystem/registry targets from environment
overrides (``${CLAUDE_HOME:-$HOME}``, ``${COORDINATOR_SETTINGS_HOME:-...}``,
``MACHINE_LOCAL_REGISTRY_DIR``) — NEVER a hardcoded real-user path. This is
what lets a test suite sandbox every leg into a ``mktemp -d $HOME`` without
risk to the real install.

Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (T4a-g3b chunk).
Spec backlink: DoE-claude:pln-first-class-coordinator-uninst-15db2e § C3-C6
Surface source of truth: tasks/coordinator-uninstall/surface-map.md
Identity-key source of truth: coordinator/lib/settings-hook-identity.sh (C2),
    ported at coordinator_core.install._shared.settings_hook_identity_inverse_strip.

Negative-spec: does NOT delete ``<DoE>/coordinator`` source in full-remove
mode — that is a separate, PM-gated decision (anti-scope, matches bash).
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from dataclasses import dataclass, field

from coordinator_core.win_portability import leaf_spawn_creationflags
from coordinator_core.ops import configure_git
from coordinator_core.install import junction

from coordinator_core.install._shared import (
    RequireHomeError,
    ml_set,
    require_home,
    resolve_coordinator_root,
    settings_hook_identity_inverse_strip,
)
from coordinator_core.install.shell_rc_guard import (
    _resolve_rc_path,
    _sentinel_markers,
    _strip_block_text,
    applicable_rc_files,
)
from coordinator_core.install.substrate import (
    _agent_cmd_dest_name,
    _derive_agent_helper_names,
    _resolve_directory_tracked_set,
)
from coordinator_core.install.write_surface import (
    ABSENT_ON_LEGACY_INSTALLS,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.install.receipt import InstallReceipt, load_receipt
from coordinator_core.engine_root import coordinator_engine_root_with_class
from coordinator_core.ops import render_template
from coordinator_core.hooks import platform_localize

# Generator-provenance declaration (generator_provenance.py). Every leg
# resolves its targets from CLAUDE_HOME/COORDINATOR_SETTINGS_HOME env
# overrides per this module's own docstring ("Every leg resolves its
# filesystem/registry targets from environment overrides...NEVER a
# hardcoded real-user path") -- the operator's home tree, not claude-klabauter's own.
GENERATES = []


# ---------------------------------------------------------------------------
# Honest disposition report — what uninstall did, deliberately did not do,
# and cannot safely do (Rule 1 and Rule 2 below).
#
# Spec backlink: pln-writer-declared-write-surface-49d3bd,
#     chunk C8
#
# Purpose: uninstall is an explicitly best-effort courtesy, not a guaranteed
# reversal — the defect this chunk fixes is a doc asserting "uninstall
# reverses every surface." The structural fix is not a better promise, it is
# an uninstall that tells the truth about what it left behind. Every entry
# on a machine's receipt (C5) is assigned to exactly one of three
# dispositions by ``classify_entry_disposition`` below, a TOTAL function —
# ``build_disposition_report`` re-asserts that totality mechanically rather
# than trusting each call site to have covered every case, since a later
# chunk (C9) restates this coverage property to a sibling repo as an
# external commitment.
#
# Two rules decide correctness here, not left to prose alone:
#
#   Rule 1 — uncertainty routes to reported-and-untouched, NEVER to a guess.
#     "Best-effort" describes how much we attempt; it says nothing about
#     lowering our confidence threshold before destroying something. See
#     ohmyzsh#13156 (unconditional snapshot restore cost a user years of
#     `.zshrc`) for the cautionary case this rule exists to avoid.
#
#   Rule 2 — a BEGIN-only marker block is an uncertainty-class hit, and is
#     NEVER heuristically spliced (no scan-to-EOF, scan-to-blank-line, or
#     scan-to-a-body-line-that-looks-like-a-terminator). ``end_marker is
#     None`` and ``end_marker == ABSENT_ON_LEGACY_INSTALLS`` are DIFFERENT
#     states and route differently:
#       - ``ABSENT_ON_LEGACY_INSTALLS`` — a marker-delimited entry whose END
#         marker was added after some installs already existed (C6) and is
#         never retrofitted onto them. Always cannot-reverse-safely,
#         regardless of what the caller-supplied outcome says — an
#         unattempted or even reported-successful strip of a BEGIN-only
#         block is still an uncertainty-class hit, since a precise splice
#         was never possible in the first place.
#       - ``None`` on a ``hook-gate-region`` entry — NOT an uncertainty hit.
#         A gate region's terminator is structural (first blank line, next
#         gate header, or EOF) by design, so ``None`` here means "no literal
#         end marker exists, and none is needed," not "uncertain." This
#         entry is classified from the caller-supplied outcome like any
#         other kind.
#       - ``None`` on ``rc-block`` — not yet declared; routed from the
#         caller-supplied outcome like any other kind, since ``rc-block``
#         (unlike ``hook-gate-region``) has no structural terminator to fall
#         back on and a caller declaring one with ``end_marker=None`` is
#         simply not yet asserting a marker pair, not asserting BEGIN-only.
#
# Negative spec — this section does NOT:
#   - execute or attempt any removal (the existing leg functions above/below
#     do that; this section only classifies and reports);
#   - import a receipt module or any writer module to discover entries —
#     callers pass ``WriteSurfaceEntry``-shaped entries in (C5's receipt
#     shape, once it lands, satisfies this duck type; this module holds no
#     import edge on it, per the invocation-budget constraint on eager
#     op-module imports documented in
#     ``coordinator_core/ops/_registry_map.py``);
#   - decide *whether* an attempt was made — that is the caller's (a leg
#     function's) job; this section only classifies the caller-supplied
#     outcome into one of the three externally-load-bearing dispositions.
# ---------------------------------------------------------------------------

DISPOSITION_REVERSED = "reversed"
DISPOSITION_DELIBERATE = "deliberately-not-reversed"
DISPOSITION_CANNOT_SAFELY = "cannot-reverse-safely"

UNINSTALL_DISPOSITIONS: tuple[str, ...] = (
    DISPOSITION_REVERSED,
    DISPOSITION_DELIBERATE,
    DISPOSITION_CANNOT_SAFELY,
)
"""The frozen three-way disposition set every receipt entry must land in
exactly one of — see module-section docstring above. C9 restates coverage
of this set over the receipt to DoE as a correspondence property; do not
add a fourth value without updating that restatement."""

_MARKER_KINDS = frozenset({"rc-block", "hook-gate-region"})


@dataclass(frozen=True)
class DispositionRecord:
    """One receipt entry's final disposition: which of the three
    ``UNINSTALL_DISPOSITIONS`` it landed in, the human-readable reason, and
    — for ``cannot-reverse-safely`` only — the exact manual command the
    operator would run (e.g. an elevated ``Remove-MpPreference`` for
    Windows Defender exclusions, the standing example this chunk's spec
    names)."""

    entry: WriteSurfaceEntry
    disposition: str
    reason: str
    manual_command: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in UNINSTALL_DISPOSITIONS:
            raise ValueError(
                f"disposition {self.disposition!r} is not one of {UNINSTALL_DISPOSITIONS}"
            )


def classify_entry_disposition(
    entry: WriteSurfaceEntry,
    *,
    attempted_ok: bool | None,
    reason: str,
    manual_command: str | None = None,
) -> DispositionRecord:
    """Classify one receipt entry into exactly one of the three
    ``UNINSTALL_DISPOSITIONS`` — a TOTAL function over every
    ``WriteSurfaceEntry`` shape (see Negative spec above for what this
    function does not do).

    ``attempted_ok`` is the caller-supplied outcome of trying to reverse
    this entry, and is only a proposal — the marker tri-state (Rule 2)
    overrides it when applicable, since a BEGIN-only legacy block is
    cannot-reverse-safely regardless of what the leg reports:
      - ``True``  — the leg reversed this entry; proposes ``reversed``.
      - ``False`` — the leg attempted this entry and could not safely
        complete it (e.g. a hand-modified block, per Rule 1); proposes
        ``cannot-reverse-safely``. ``manual_command`` SHOULD be supplied.
      - ``None``  — this entry was deliberately never attempted (a named
        policy choice, e.g. "we do not delete `<DoE>/coordinator` source in
        full-remove mode"); proposes ``deliberately-not-reversed``.
        ``reason`` MUST name the policy, not be a shrug.

    Marker-tristate override (Rule 2), checked before the caller's proposal
    is trusted:
      - ``entry.end_marker == ABSENT_ON_LEGACY_INSTALLS`` on a
        marker-delimited kind (``rc-block``/``hook-gate-region``) always
        forces ``cannot-reverse-safely`` — reported-and-untouched, never
        heuristically spliced, no matter what ``attempted_ok`` says.
      - ``entry.end_marker is None`` on ``hook-gate-region`` is NOT an
        uncertainty hit (the terminator is structural) and falls through to
        the caller-supplied ``attempted_ok`` like any other kind.
    """

    if (
        entry.kind in _MARKER_KINDS
        and entry.end_marker == ABSENT_ON_LEGACY_INSTALLS
    ):
        return DispositionRecord(
            entry=entry,
            disposition=DISPOSITION_CANNOT_SAFELY,
            reason=(
                "BEGIN-only marker block predating the END marker (C6) — "
                "never heuristically spliced, never retrofitted onto an "
                "already-installed machine (Rule 2)."
            ),
            manual_command=manual_command
            or (
                f"Manually locate and remove the block beginning "
                f"'{entry.begin_marker}' in {entry.path or '<unknown path>'} "
                "— no automated precise splice is possible without an END "
                "marker on this install."
            ),
        )

    if attempted_ok is True:
        return DispositionRecord(
            entry=entry,
            disposition=DISPOSITION_REVERSED,
            reason=reason,
        )
    if attempted_ok is False:
        return DispositionRecord(
            entry=entry,
            disposition=DISPOSITION_CANNOT_SAFELY,
            reason=reason,
            manual_command=manual_command,
        )
    if attempted_ok is None:
        # a deliberate, named policy choice not to attempt.
        return DispositionRecord(
            entry=entry,
            disposition=DISPOSITION_DELIBERATE,
            reason=reason,
        )
    # Review: code-reviewer (Finding 3, P2) — the prior bare `else` silently
    # classified any non-bool, non-None `attempted_ok` as a deliberate
    # policy decision, misreporting a caller bug as policy. Fail loud
    # instead — an out-of-domain value must not degrade into a wrong claim.
    raise TypeError(
        f"attempted_ok must be True, False, or None; got {attempted_ok!r} "
        f"({type(attempted_ok).__name__})"
    )


@dataclass(frozen=True)
class UninstallDispositionReport:
    """The full, mechanically-checkable disposition report for one
    uninstall run: every receipt entry's ``DispositionRecord``, grouped by
    disposition. ``assert_total_coverage`` re-checks — on the assembled
    report, not just per-call — that every record supplied landed in
    exactly one of ``UNINSTALL_DISPOSITIONS`` and none was silently
    dropped, since C9 restates this coverage as an external commitment to
    DoE."""

    records: tuple[DispositionRecord, ...] = field(default_factory=tuple)

    def by_disposition(self, disposition: str) -> tuple[DispositionRecord, ...]:
        return tuple(r for r in self.records if r.disposition == disposition)

    def assert_total_coverage(self) -> None:
        """Raise ``UninstallLegError`` if any record's disposition is not
        one of ``UNINSTALL_DISPOSITIONS``, or if the three disposition
        buckets do not partition ``self.records`` exactly (no entry
        double-counted, none dropped). A no-op on a clean report."""

        seen = 0
        for disposition in UNINSTALL_DISPOSITIONS:
            seen += len(self.by_disposition(disposition))
        if seen != len(self.records):
            raise UninstallLegError(
                "disposition report failed total-function coverage: "
                f"{len(self.records)} record(s) supplied, {seen} accounted "
                f"for across {UNINSTALL_DISPOSITIONS} — an entry was either "
                "silently dropped or assigned an unrecognized disposition."
            )


def build_disposition_report(
    records: "list[DispositionRecord] | tuple[DispositionRecord, ...]",
) -> UninstallDispositionReport:
    """Assemble and validate an ``UninstallDispositionReport`` from
    already-classified ``DispositionRecord``s (see
    ``classify_entry_disposition``). Raises ``UninstallLegError`` if
    coverage is not total — see ``assert_total_coverage``."""

    report = UninstallDispositionReport(records=tuple(records))
    report.assert_total_coverage()
    return report


def render_disposition_report(report: UninstallDispositionReport, *, dry_run: bool = False) -> str:
    """Render ``report`` as the human-readable text uninstall prints —
    honest about what it did, deliberately did not do (with the reason),
    and cannot do safely (with the exact manual command). ``dry_run=True``
    labels this as a PREVIEW rather than a completed run's report, per this
    chunk's ``--dry-run``-is-standard requirement."""

    lines: list[str] = []
    header = "coordinator-uninstall: disposition report"
    if dry_run:
        header += " (DRY RUN — preview only, nothing was touched)"
    lines.append(header)

    def _entry_label(entry: WriteSurfaceEntry) -> str:
        # Review: code-reviewer (Finding 5, P3) — the prior fallback
        # dropped `begin_marker` even when the entry carried one, printing
        # an unidentifiable `<kind entry>` line for a marker-delimited
        # entry whose `key`/`path` are both unset.
        return (
            entry.key
            or entry.path
            or entry.begin_marker
            or f"<{entry.kind} entry>"
        )

    reversed_records = report.by_disposition(DISPOSITION_REVERSED)
    lines.append(f"  reversed ({len(reversed_records)}):")
    for record in reversed_records:
        lines.append(f"    - {_entry_label(record.entry)}: {record.reason}")

    deliberate_records = report.by_disposition(DISPOSITION_DELIBERATE)
    lines.append(f"  deliberately-not-reversed ({len(deliberate_records)}):")
    for record in deliberate_records:
        lines.append(f"    - {_entry_label(record.entry)}: {record.reason}")

    cannot_records = report.by_disposition(DISPOSITION_CANNOT_SAFELY)
    lines.append(f"  cannot-reverse-safely ({len(cannot_records)}):")
    for record in cannot_records:
        lines.append(f"    - {_entry_label(record.entry)}: {record.reason}")
        if record.manual_command:
            lines.append(f"        manual command: {record.manual_command}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# git-config reversal leg (C6) — net-new: `uninstall_legs` had NO git-config
# reversal machinery before this chunk (install only ever PUT git-config
# keys on a machine, via `configure_git._SETTINGS`; nothing took them back
# off). Reverses one `unset_group` of `GitSetting` records as an atomic
# unit — today's only caller is the Windows-only help-browser triple
# (`help.format` -> `web.browser` -> `browser.noop.cmd`, `unset_group=
# "help-browser"`) — honouring four reviewer-integrated constraints (see
# spec backlink) all at once:
#
#   1. SCOPE — every unset runs in the scope the `GitSetting` record itself
#      declares (`configure_git._resolve_scope`, `is_global=False` — a
#      `scope="global"` record is ALWAYS global regardless of invocation),
#      never a literal `--global`/`` in this module. `git config --unset`
#      with no scope flag defaults to LOCAL, so a caller-supplied
#      `config_get`/`config_unset` that received the wrong scope would
#      silently target the wrong config file — this leg always threads the
#      declared scope tuple through to both seams, never omits it.
#   2. ORDER — the group is walked in `configure_git._SETTINGS`'s own
#      declaration order, never re-sorted: `help.format` -> `web.browser`
#      -> `browser.noop.cmd`, which makes the unsafe `web.browser=noop`
#      without `browser.noop.cmd` intermediate unreachable at every prefix.
#   3. ATOMICITY x VALUE-MATCH — unset only when the live value still
#      matches what the installer set (`current == setting.value`);
#      otherwise SKIP (`attempted_ok=None`, `deliberately-not-reversed`) —
#      a SKIP does NOT abort the group. A non-SKIP failure (any exit other
#      than 0, or exit 5 outside the declared scope — see constraint 4)
#      DOES abort every remaining group member as `cannot-reverse-safely`
#      with a `manual_command`, never `reversed`.
#   4. EXIT-CODE — `git config --unset` on an already-absent key exits 5;
#      since this leg always calls `config_unset` with the record's own
#      declared scope (constraint 1), an observed exit 5 here is
#      necessarily "absent in the declared scope" and is treated as
#      success (`reversed`). Any other non-zero exit aborts the group
#      (constraint 3).
#
# `config_get`/`config_unset` are injectable seams (default to
# `configure_git._git_config_get` / this module's own
# `_git_config_unset_scoped`) purely so tests can stub the git subprocess
# boundary without touching a real git config anywhere, machine-local or
# repo-local.
#
# Spec backlink: pln-windows-git-help-browser-tripl-b21b5d § C6
# ---------------------------------------------------------------------------


def _git_config_unset_scoped(scope: Sequence[str], key: str) -> int:
    """Run ``git config <scope> --unset <key>`` and return its raw exit
    code (never raises; an ``OSError`` spawning git itself is reported as
    exit 1, a genuine failure, never conflated with git's own exit 5
    already-absent signal)."""
    try:
        res = subprocess.run(
            ["git", "config", *scope, "--unset", key],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **leaf_spawn_creationflags(),
        )
    except OSError:
        return 1
    return res.returncode


def uninstall_reverse_git_config_group(
    unset_group: str,
    *,
    settings: "Sequence[configure_git.GitSetting]" = configure_git._SETTINGS,
    config_get: Callable[[Sequence[str], str], "str | None"] = configure_git._git_config_get,
    config_unset: Callable[[Sequence[str], str], int] = _git_config_unset_scoped,
) -> tuple[DispositionRecord, ...]:
    """Reverse every ``GitSetting`` in ``settings`` sharing
    ``unset_group``, honouring unset-as-a-unit (see module-section
    docstring above for the four constraints this enforces). Returns one
    ``DispositionRecord`` per group member, in the group's own declared
    order — always classified via ``classify_entry_disposition`` (this
    leg is a caller of that TOTAL function, not a fourth disposition
    source)."""

    group_settings = [s for s in settings if s.unset_group == unset_group]

    records: list[DispositionRecord] = []
    aborted = False
    for setting in group_settings:
        scope, scope_label = configure_git._resolve_scope(setting, False)
        entry = WriteSurfaceEntry(
            kind="git-config-key", key=setting.key, unset_group=unset_group
        )

        if aborted:
            records.append(
                classify_entry_disposition(
                    entry,
                    attempted_ok=False,
                    reason=(
                        f"skipped — an earlier member of unset_group "
                        f"{unset_group!r} failed non-recoverably; the remainder "
                        "of the group is aborted rather than left partially "
                        "reversed (ATOMICITY)."
                    ),
                    manual_command=(
                        f"git config {' '.join(scope)} --unset {setting.key}".strip()
                    ),
                )
            )
            continue

        current = config_get(scope, setting.key)
        if current != setting.value:
            records.append(
                classify_entry_disposition(
                    entry,
                    attempted_ok=None,
                    reason=(
                        f"value-match SKIP — current {scope_label} value "
                        f"{current!r} does not match what this installer set "
                        f"({setting.value!r}); leaving the operator's own value "
                        "in place."
                    ),
                )
            )
            continue

        rc = config_unset(scope, setting.key)
        if rc in (0, 5):
            records.append(
                classify_entry_disposition(
                    entry,
                    attempted_ok=True,
                    reason=(
                        f"unset {scope_label} {setting.key}"
                        + (" (already absent — exit 5, success)" if rc == 5 else "")
                    ),
                )
            )
        else:
            aborted = True
            records.append(
                classify_entry_disposition(
                    entry,
                    attempted_ok=False,
                    reason=(
                        f"git config --unset exited {rc} for {scope_label} "
                        f"{setting.key}"
                    ),
                    manual_command=(
                        f"git config {' '.join(scope)} --unset {setting.key}".strip()
                    ),
                )
            )

    return tuple(records)


_NO_RECEIPT_MESSAGE = (
    "coordinator-uninstall: --dry-run — no install receipt found on this "
    "machine, so I cannot tell you what was installed here. C5's receipt "
    "module (coordinator_core.install.receipt) exists but nothing wires "
    "receipt emission into a live install run yet — this is an honest "
    "'unknown', not an empty report (an empty report would read as "
    "'nothing to remove', which would be false)."
)


def _load_install_receipt() -> "InstallReceipt | None":
    """Load this machine's install receipt, if one has ever been recorded.

    Design note: docs/research/2026-08-06-install-receipt-persistence-design.md,
    chunk C3 (the read side). Persistence itself (``persist_receipt``/
    ``load_receipt``, the on-disk JSON shape, atomic write) is C2's
    (``coordinator_core.install.receipt``) — this function is a thin
    delegation, not a second implementation of loading/degradation.

    ``load_receipt()`` already degrades to ``None`` — never raises — for
    every absent/malformed/unrecognised-schema shape (see its own
    docstring's exhaustive list), which is exactly the honest-unknown
    behaviour ``orchestrate_uninstall``'s ``--dry-run`` branch and
    ``render_uninstall_dry_run_report`` depend on: a machine that has never
    installed (or whose receipt is corrupt) must keep printing
    ``_NO_RECEIPT_MESSAGE``, never a confident "nothing to remove". Callers
    go through this function rather than importing ``load_receipt`` directly
    so that this module's receipt-loading policy (today: exactly
    ``load_receipt()``, no override) stays a one-function change.
    """

    return load_receipt()


def render_uninstall_dry_run_report(receipt: "InstallReceipt | None") -> str:
    """Render the ``--dry-run`` preview text for ``receipt`` (or the honest
    "no receipt" message if ``receipt`` is ``None`` or empty).

    Dry-run never executes a leg (see ``orchestrate_uninstall``), so no
    entry's actual outcome is ever known here. Every ordinary receipt entry
    IS something a real run would attempt to reverse — the receipt only
    ever records what an install wrote, and this module has no per-entry
    signal (yet) distinguishing "would be reversed" from "policy says never
    touch this" — so each is classified with ``attempted_ok=True``
    (optimistic: dry-run previews what WOULD happen, not what did), landing
    it in the ``reversed`` bucket with a reason that says "would reverse."
    ``classify_entry_disposition``'s Rule 2 marker-tristate override still
    fires ahead of that (a BEGIN-only legacy block is
    ``cannot-reverse-safely`` regardless of whether any run even attempts
    it, and regardless of the optimistic ``attempted_ok=True`` proposal
    below), so a dry-run preview still surfaces the structurally
    unreversible entries as such.

    # Review: code-reviewer (Finding 1, P1) — this previously passed
    # ``attempted_ok=None`` for every ordinary entry, which
    # ``classify_entry_disposition`` maps to ``deliberately-not-reversed``:
    # every entry a real run WOULD reverse was mislabeled identically to an
    # entry coordinator has permanently decided never to touch (e.g.
    # ``<DoE>/coordinator`` source), contradicting
    # ``docs/wiki/uninstall-agentic-judgment.md``'s "Dry-run is standard,
    # not optional" promise that "the only difference is that `reversed`
    # becomes 'would reverse'." Landing every ordinary entry in the
    # ``reversed`` bucket (via ``attempted_ok=True``) with reason text
    # spelling out "would reverse" honors that promise at render time
    # without adding a fourth classifier disposition — the classifier's
    # three-way totality (Negative spec, module docstring) is unchanged.

    Chunk C3 (design note: docs/research/2026-08-06-install-receipt-persistence-design.md)
    — an ``unreported_writer_ids`` entry is NOT "this writer wrote nothing"
    (that would collapse exactly the distinction the design note's negative
    spec exists to prevent). It is coverage we cannot vouch for, which is
    the same epistemic class ``classify_entry_disposition`` already models
    as ``cannot-reverse-safely`` — so each unreported writer is classified
    via ``attempted_ok=False`` against a synthetic placeholder entry
    (there is no real ``WriteSurfaceEntry`` to report — the writer never
    told us one), landing it in the SAME bucket a BEGIN-only marker entry
    lands in, but with reason text naming the writer and saying coverage is
    unknown — distinguishable at render time from both the "would reverse"
    entries above and any genuinely-deliberate policy exclusion, since the
    reason text (not the disposition alone) is what a reader keys off to
    tell "hand-modified, refuse to touch" apart from "we never even heard
    from this writer this run."
    """

    if receipt is None or not (receipt.entries or receipt.unreported_writer_ids):
        return _NO_RECEIPT_MESSAGE

    records = [
        classify_entry_disposition(
            entry,
            attempted_ok=True,
            reason=(
                "dry-run preview — a real run would reverse this entry; "
                "--dry-run does not execute any leg, so this reflects what "
                "WOULD happen, not what did happen"
            ),
        )
        for entry in receipt.entries
    ]

    for writer_id in sorted(receipt.unreported_writer_ids):
        placeholder = WriteSurfaceEntry(
            kind="file-path",
            path=f"<writer:{writer_id}>",
            # Review: code-reviewer (Finding 3, P3) -- structural signal
            # (not just reason-text convention) that this record is a
            # synthesized stand-in, not a genuine declared surface.
            synthetic=True,
        )
        records.append(
            classify_entry_disposition(
                placeholder,
                attempted_ok=False,
                reason=(
                    f"writer {writer_id!r} did not report this run — coverage "
                    "unknown, NOT the same as this writer having written "
                    "nothing; cannot safely determine what (if anything) to "
                    "reverse"
                ),
            )
        )

    report = build_disposition_report(records)
    return render_disposition_report(report, dry_run=True)


class UninstallLegError(RuntimeError):
    """Raised by a leg on unrecoverable failure — CLI entry converts to a
    non-zero exit code, matching the bash function-return contract."""


def _settings_home_from(resolved_home: str) -> str:
    """Derive the settings-home root for an uninstall leg from an ALREADY
    require_home()-resolved home.

    Takes the resolved home as an argument rather than reading the env itself:
    each leg's `require_home()` call is the single point where a home is
    validated (present, absolute), and a helper that re-read `CLAUDE_HOME`/`HOME`
    would reintroduce exactly the bug this replaced — two legs derived
    `${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings` inline, which on native
    Windows (no HOME) built a drive-relative `/.coordinator-claude-settings` and
    pointed the registry-clearing and rmtree targets at the wrong tree.

    COORDINATOR_SETTINGS_HOME still wins, and is validated absolute here for the
    same reason require_home validates its own result: these paths feed
    destructive operations, and a relative override anchors them at the process
    cwd.
    """
    override = os.environ.get("COORDINATOR_SETTINGS_HOME") or ""
    if override:
        candidate = Path(override)
        if not (candidate.is_absolute() or candidate.root):
            raise RequireHomeError(
                f"$COORDINATOR_SETTINGS_HOME is set to a non-absolute path {override!r} — "
                "refusing to derive destructive targets from a cwd-relative settings home. "
                "(On Windows, a drive letter alone is insufficient — use 'C:\\...' form.)"
            )
        return override
    return os.path.join(resolved_home, ".coordinator-claude-settings")


def _atomic_write_text(target: Path, content: str) -> None:
    out_dir = target.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + target.name + ".uninstall-strip.", dir=str(out_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass  # best-effort cleanup on an already-failing path; original exception re-raises below
        raise


def _collapse_trailing_blank_runs(lines: List[str]) -> List[str]:
    """Collapse a run of >=2 blank lines down to at most one — cosmetic
    only, mirrors the bash awk collapse pass."""
    out: List[str] = []
    blank = 0
    for line in lines:
        if line.strip() == "":
            blank += 1
            if blank <= 1:
                out.append(line)
        else:
            blank = 0
            out.append(line)
    return out


def _strip_sentinel_block(file_path: Path, begin_marker: str, end_marker: str) -> None:
    """Remove the block delimited by begin_marker..end_marker (inclusive)
    from file_path. Atomic write. No-op if the sentinel is absent, AND ALSO
    a no-op (leaves the malformed block in place) if `begin_marker` is
    present but `end_marker` never follows it — see
    `shell_rc_guard._strip_block_text`'s docstring (Review: code-reviewer,
    Finding 1, P1) for why an unterminated block must never be silently
    truncated.

    The line-state-machine text transform is deliberately delegated to
    `shell_rc_guard._strip_block_text` rather than reimplemented here — the
    plan's exactly-once negative spec requires the sentinel-strip logic
    exist a single time fleet-wide, so this leg owns only the surrounding
    file I/O (read, transform, collapse, atomic write)."""
    text = file_path.read_text(encoding="utf-8")
    stripped = _strip_block_text(text, begin_marker, end_marker)
    out = _collapse_trailing_blank_runs(stripped.split("\n"))
    _atomic_write_text(file_path, "\n".join(out))


def _strip_legacy_block(file_path: Path, begin_marker: str) -> None:
    """State-machine strip of the hand-authored legacy block (variable end
    marker — see bash header comment on _uninstall_strip_legacy_block)."""
    text = file_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    out: List[str] = []
    st = 0  # 0 = outside, 1 = inside (pre-fn-line), 2 = inside (post-fn-line)
    for line in lines:
        if st == 0 and line == begin_marker:
            st = 1
            continue
        if st == 1 and line.startswith("# --- end coordinator maximalist launch"):
            st = 0
            continue
        if st == 1 and line.startswith("claude() {"):
            st = 2
            continue
        if st == 2 and line.startswith("# --- end coordinator maximalist launch"):
            st = 0
            continue
        if st == 2:
            st = 0
            continue
        if st == 1:
            continue
        out.append(line)
    out = _collapse_trailing_blank_runs(out)
    _atomic_write_text(file_path, "\n".join(out))


def uninstall_strip_settings_hooks(*, coordinator_root_env: Optional[str] = None) -> bool:
    """Reverses surface #2 (settings.json generated hook block).

    No-op (success) if settings.json itself does not exist. Idempotent.
    Returns True on success, False on failure (matches bash 0/non-zero)."""
    try:
        claude_home = require_home("uninstall_strip_settings_hooks")
    except RequireHomeError as exc:
        print(str(exc), file=sys.stderr)
        return False

    settings_json = Path(claude_home) / "settings.json"
    if not settings_json.is_file():
        print(
            f"uninstall_strip_settings_hooks: no settings.json at {settings_json} "
            "— nothing to strip (no-op)",
            file=sys.stderr,
        )
        return True

    try:
        coordinator_root = resolve_coordinator_root(coordinator_root_env)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return False

    out_dir = settings_json.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".settings.json.uninstall-strip.", dir=str(out_dir))
    os.close(fd)
    tmp_out = Path(tmp_name)

    try:
        settings_hook_identity_inverse_strip(str(settings_json), coordinator_root, str(tmp_out))
    except Exception as exc:
        print(f"uninstall_strip_settings_hooks: inverse-strip failed for {settings_json}: {exc}", file=sys.stderr)
        tmp_out.unlink(missing_ok=True)
        return False

    if not tmp_out.exists() or tmp_out.stat().st_size == 0:
        print(
            f"uninstall_strip_settings_hooks: inverse-strip produced empty output "
            f"— refusing to overwrite {settings_json}",
            file=sys.stderr,
        )
        tmp_out.unlink(missing_ok=True)
        return False

    os.replace(tmp_out, settings_json)
    return True


def uninstall_remove_shim() -> bool:
    """Reverses the launch-surface family (#4a/#4b/#4c + #10). Idempotent —
    absent shim/sentinel/legacy-block/wrapper are all no-ops."""
    try:
        resolved_home = require_home("uninstall_remove_shim")
    except RequireHomeError as exc:
        print(str(exc), file=sys.stderr)
        return False
    # `home` (rc files, ~/.local/bin) and `claude_home` (the .claude tree) stay
    # DISTINCT — CLAUDE_HOME redirects the latter without moving the operator's
    # real shell rc files. Both now fall through to require_home()'s resolved
    # value rather than to "", which is what made them cwd-relative — this
    # function strips blocks from `Path(home)/".bashrc"`, so an empty `home`
    # meant mutating a `.bashrc` in whatever directory the process started in.
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or resolved_home
    claude_home = os.environ.get("CLAUDE_HOME") or home

    shim_sentinel_begin = "# --- coordinator claude-doe shim [generated] ---"
    shim_sentinel_end = "# --- end coordinator claude-doe shim ---"
    legacy_marker_begin = "# --- coordinator maximalist launch ---"

    overall_ok = True

    # ---- (a) #4a: shell shim owned file ----
    shim_file = Path(claude_home) / "shell" / "claude-doe-shim.sh"
    if shim_file.is_file() or shim_file.is_symlink():
        try:
            shim_file.unlink()
        except OSError as exc:
            print(f"uninstall_remove_shim: failed to remove shim file {shim_file}: {exc}", file=sys.stderr)
            overall_ok = False

    # ---- resolve target rc for (b), same detection as install ----
    if os.environ.get("COORDINATOR_SHIM_RC_OVERRIDE"):
        target_rc = Path(os.environ["COORDINATOR_SHIM_RC_OVERRIDE"])
    elif os.environ.get("COORDINATOR_SHIM_RC"):
        target_rc = Path(os.environ["COORDINATOR_SHIM_RC"])
    else:
        shell = os.environ.get("SHELL", "")
        if shell.endswith("/zsh"):
            target_rc = Path(home) / ".zshrc"
        elif shell.endswith("/bash"):
            target_rc = Path(home) / ".bashrc"
        else:
            target_rc = Path(home) / ".zshrc"

    # ---- (b) #4b: strip generated sentinel block from the detected rc ----
    if target_rc.is_file():
        text = target_rc.read_text(encoding="utf-8", errors="replace")
        if shim_sentinel_begin in text.split("\n"):
            try:
                _strip_sentinel_block(target_rc, shim_sentinel_begin, shim_sentinel_end)
            except OSError as exc:
                print(
                    f"uninstall_remove_shim: failed to strip generated shim block from {target_rc}: {exc}",
                    file=sys.stderr,
                )
                overall_ok = False

    # ---- (c) #4c: remove legacy block from $HOME/.bashrc ----
    legacy_bashrc = Path(home) / ".bashrc"
    if legacy_bashrc.is_file():
        text = legacy_bashrc.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if legacy_marker_begin in lines:
            # `X/DoE-claude` is a fingerprint of the literal text a prior generator
            # wrote into `~/.bashrc`, matched as a string below -- never resolved as
            # a path. It must stay frozen to keep matching that historical output, so
            # it must NOT be swapped for registry/settings-home resolution. A mismatch
            # fails closed either way: false negative re-strips a hand-edited block,
            # false positive refuses and prints manual-removal instructions.
            expected_claude_bin = f"{home}/X/DoE-claude/coordinator/bin/claude-doe"
            expected_repo = f"{home}/X/DoE-claude"
            expected_line = (
                f'claude() {{ REPO_DOE_CLAUDE="{expected_repo}" command bash '
                f'"{expected_claude_bin}" "$@"; }}'
            )

            body_lines: List[str] = []
            capturing = False
            for line in lines:
                if line == legacy_marker_begin:
                    capturing = True
                    continue
                if capturing and line.startswith("# --- end coordinator maximalist launch"):
                    capturing = False
                    continue
                if capturing:
                    body_lines.append(line)

            # bash's `awk 'NF'` drops blank lines
            # but preserves each surviving line's own content/spacing and
            # multi-line structure. The prior Python collapsed ALL whitespace
            # (including newlines) into single spaces, which was MORE
            # permissive than the oracle -- a hand-edit guard must fail in
            # the strict direction, not the lenient one. Join with "\n" (not
            # " ") to preserve line structure, matching awk's default
            # {print} action on each surviving line.
            body_trimmed = "\n".join(l for l in body_lines if l.strip() != "")
            expected_trimmed = "\n".join(
                l for l in expected_line.split("\n") if l.strip() != ""
            )

            if body_trimmed != expected_trimmed:
                print(
                    f"uninstall_remove_shim: legacy block in {legacy_bashrc} has been "
                    "hand-modified — refusing to clobber.",
                    file=sys.stderr,
                )
                print(f"  Expected body: {expected_trimmed}", file=sys.stderr)
                print(f"  Found body:    {body_trimmed}", file=sys.stderr)
                print(
                    f'  To reset: remove the block between "{legacy_marker_begin}" and its '
                    f"end marker from {legacy_bashrc} and re-run.",
                    file=sys.stderr,
                )
                return False

            try:
                _strip_legacy_block(legacy_bashrc, legacy_marker_begin)
            except OSError as exc:
                print(
                    f"uninstall_remove_shim: failed to strip legacy block from {legacy_bashrc}: {exc}",
                    file=sys.stderr,
                )
                overall_ok = False

    # ---- (d) #10 (the Staff Engineer F0): claude-doe wrapper ----
    wrapper = Path(home) / ".local" / "bin" / "claude-doe"
    if wrapper.is_file() or wrapper.is_symlink():
        try:
            wrapper.unlink()
        except OSError as exc:
            print(
                f"uninstall_remove_shim: failed to remove claude-doe wrapper {wrapper}: {exc}",
                file=sys.stderr,
            )
            overall_ok = False

    # ---- (e) F2 (docs/plans/2026-07-25-posix-bareword-path-provisioning.md
    # C1): strip every rc-guard block coordinator_core.install.shell_rc_guard
    # may have written -- CLAUDE_KLABAUTER_CLONE (single file, same resolver the
    # writer uses), and the claude-CLI / settings-home-bin PATH blocks
    # (every applicable login-profile + interactive-rc file). Uses the SAME
    # sentinel-marker derivation and file-list helpers the writer uses
    # (`_sentinel_markers`, `applicable_rc_files`, `_resolve_rc_path`) so
    # this leg can never drift out of sync with what the writer actually
    # wrote. Idempotent — an absent block in a given file is a no-op for
    # that file; a real gap prior to this leg (verified: zero prior
    # coverage for any of these three sentinel families anywhere in this
    # module) would otherwise leave stale PATH entries in every rc file
    # forever after uninstall.
    # `claude_home` (resolved above, ``CLAUDE_HOME`` or ``$HOME``) is the
    # SAME home substrate.py's writer resolves as `install_base` for the
    # CLAUDE_CLI_PATH / SETTINGS_HOME_BIN legs -- must match here or this
    # leg strips the wrong user's dotfiles under a CLAUDE_HOME override
    # (e.g. test isolation). CLAUDE_KLABAUTER_CLONE's `_resolve_rc_path()` stays
    # keyed on literal `$HOME` only, matching the writer's own legacy
    # single-file resolver (never CLAUDE_HOME-aware, by the writer's own
    # design -- see shell_rc_guard.py's module docstring).
    for sentinel_id, candidate_files in (
        # Review: code-reviewer (Finding 3, P2) — CLAUDE_KLABAUTER_CLONE deliberately
        # stays keyed on literal `$HOME` (`_resolve_rc_path()`, never
        # `CLAUDE_HOME`-aware) here, matching the writer's own legacy
        # single-file resolver, while the other two legs below resolve
        # against `claude_home` (`CLAUDE_HOME` or `$HOME`). This is an
        # intentional, real behavioral asymmetry (see the block comment
        # above), inlined here so an editor changing HOME derivation for
        # the other two legs doesn't reflexively "fix" this one to match
        # and silently change behavior.
        # Review: code-reviewer — `_resolve_rc_path()` is called here with no
        # `os.name == "nt"` guard, asymmetric with the writer
        # (`write_shell_rc_guard_block`, shell_rc_guard.py) which early-returns
        # on native Windows before ever calling it. What makes this harmless
        # TODAY is not a platform check on this leg -- it's that the derived
        # `$HOME/.bashrc`/`.zshrc` essentially never exists on native Windows,
        # so the `rc_file.is_file()` guard below degrades this to a no-op. A
        # future change to `_resolve_rc_path()` that returns something
        # real-but-wrong on native Windows would have no platform check here
        # to catch it before the strip runs.
        ("CLAUDE_KLABAUTER_CLONE", [_resolve_rc_path()]),
        ("CLAUDE_CLI_PATH", applicable_rc_files(Path(claude_home))),
        ("SETTINGS_HOME_BIN", applicable_rc_files(Path(claude_home))),
    ):
        begin_marker, end_marker = _sentinel_markers(sentinel_id)
        for rc_file in candidate_files:
            if not rc_file.is_file():
                continue
            text = rc_file.read_text(encoding="utf-8", errors="replace")
            if begin_marker not in text.split("\n"):
                continue
            try:
                _strip_sentinel_block(rc_file, begin_marker, end_marker)
            except OSError as exc:
                print(
                    f"uninstall_remove_shim: failed to strip {sentinel_id} guard block from {rc_file}: {exc}",
                    file=sys.stderr,
                )
                overall_ok = False

    return overall_ok


def uninstall_strip_cmd_autorun() -> bool:
    """Reverses the cmd.exe `AutoRun`-registry interception leg (DR-285,
    ``coordinator_core.ops.cmd_autorun_guard``) — closes the uninstall gap
    that mechanism's own module docstring names as its residual: applying
    the AutoRun write with no composed strip leg would leave HKCU machine
    state behind with no automated removal path.

    Grouped with ``uninstall_remove_shim`` (same dependency tier, run
    immediately after it) because both reverse `claude`-invocation
    interception surfaces — the shell-function shim and the cmd.exe AutoRun
    macro are the two halves of the same "intercept a bare `claude`" family,
    and neither leg reads state the other produces, so their relative order
    is a grouping choice, not a correctness requirement. Placed before
    ``uninstall_remove_substrate``/``uninstall_set_plugin_endstate`` since
    those still resolve `plugin_root` and machine-local registry state this
    leg has no dependency on either way.

    Idempotent (a no-op on absent-or-foreign AutoRun content — see
    ``strip_cmd_autorun_guard``'s own classification handling) and
    self-gates to a no-op on non-Windows and under
    ``COORDINATOR_DISABLE_MACHINE_MUTATION`` (belt-and-braces
    ``_refuse_machine_mutation`` gate, honoured inside
    ``strip_cmd_autorun_guard`` itself — this leg never needs its own dry-run
    branch because ``orchestrate_uninstall``'s ``--dry-run`` short-circuit
    never calls this function at all, matching every other mutating leg).

    Returns True on success (including the absent/foreign no-op cases),
    False only on an unexpected registry error (fail-loud, matches every
    other leg's bool contract)."""
    from coordinator_core.ops.cmd_autorun_guard import strip_cmd_autorun_guard

    try:
        strip_cmd_autorun_guard(check_only=False)
    except Exception as exc:
        print(f"uninstall_strip_cmd_autorun: failed to strip cmd.exe AutoRun guard: {exc}", file=sys.stderr)
        return False
    return True


def uninstall_strip_host_sampler_task() -> bool:
    """Reverses the host-sampler Windows Task Scheduler registration leg
    (``coordinator_core.install.host_sampler_scheduler``) — closes the
    uninstall gap that a scheduled task, once registered, would otherwise
    survive a full-remove uninstall forever (the residue this repo's
    uninstall surface exists to prevent).

    Grouped with ``uninstall_strip_cmd_autorun`` (same dependency tier, run
    immediately after it): both reverse an OS-level scheduling/interception
    surface with no dependency on `plugin_root` or the substrate registry.

    Idempotent (a no-op on an absent task — see
    ``unregister_host_sampler_task``'s own already-absent-is-success
    handling) and self-gates to a no-op on non-Windows.

    Returns True on success (including the absent-task no-op case), False
    only on an unexpected `schtasks.exe` failure (fail-loud, matches every
    other leg's bool contract)."""
    from coordinator_core.install.host_sampler_scheduler import (
        unregister_host_sampler_task,
    )

    try:
        return unregister_host_sampler_task()
    except Exception as exc:
        print(
            f"uninstall_strip_host_sampler_task: failed to remove host-sampler "
            f"scheduled task: {exc}",
            file=sys.stderr,
        )
        return False


def _unlink_clearing_readonly(path: Path) -> None:
    """Unlink ``path``, clearing a read-only attribute first if that is what
    blocks the delete.

    Windows refuses ``DeleteFile`` on a file carrying FILE_ATTRIBUTE_READONLY
    (surfacing as ``PermissionError``/WinError 5), while POSIX only consults
    the *parent directory's* write bit -- so a residual that deletes fine on
    macOS/Linux can be undeletable here. The case this actually hits is a
    foreign git repo left inside the installed ``setup/`` tree: every loose
    object under ``.git/objects/`` is written read-only by design.

    Clears the write bit and retries once. A genuine permission problem (an
    ACL denial, a file held open) fails the retry and propagates, so a real
    error is still reported rather than swallowed.
    """
    try:
        path.unlink()
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            raise
        path.unlink()


def _rm_target(path: Path, label: str, errors: List[str]) -> None:
    """rm -f semantics: remove a file/symlink if present; append to errors on failure."""
    if path.exists() or path.is_symlink():
        try:
            path.unlink()
        except IsADirectoryError:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                errors.append(f"failed to remove {label} ({path}): {exc}")
        except OSError as exc:
            errors.append(f"failed to remove {label} ({path}): {exc}")


def _rmtree_target(path: Path, label: str, errors: List[str]) -> None:
    if path.exists():
        try:
            shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"failed to remove {label} ({path}): {exc}")


def _rmtree_junction_or_target(path: Path, label: str, errors: List[str]) -> None:
    """``_rmtree_target``'s junction-aware counterpart (2026-08-20,
    ``ensure_venv``'s move to junction-published ``.coordinator-venv``):
    ``shutil.rmtree`` REFUSES outright on a junction/reparse point
    (``OSError: Cannot call rmtree on a symbolic link`` — see
    ``coordinator_core.install.junction``'s module docstring), so a plain
    ``_rmtree_target`` on a junctioned ``venv_dir`` would silently leave the
    junction AND its target generation behind, reporting a failure into
    ``errors`` instead of actually uninstalling. Detects the reparse tag
    first (never ``islink()`` — same trap), removes the link with
    ``junction.remove_junction``, then removes the TARGET generation tree
    with a plain ``shutil.rmtree``. A non-junction ``path`` (a legacy
    real-directory venv, or nothing at all) falls through to
    ``_rmtree_target`` unchanged."""
    if not junction.is_junction(path):
        _rmtree_target(path, label, errors)
        return
    target = junction.junction_target(path)
    try:
        junction.remove_junction(path)
    except OSError as exc:
        errors.append(f"failed to remove {label} junction ({path}): {exc}")
        return
    if target is not None and target.exists():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            errors.append(f"failed to remove {label} target ({target}): {exc}")


def _sweep_orphaned_swap_dirs(venv_dir: Path) -> None:
    """Best-effort reclaim of orphaned generation siblings abandoned by a
    prior venv-rebuild process that crashed mid-rebuild (before cleanup) or
    mid-publish (a Windows deferred-reclaim leftover, or a torn junction
    retarget).

    Sweeps two naming generations. ``.gen-*`` (current, since 2026-08-20's
    junction-publish rewrite — see ``ensure_venv``'s module docstring) is
    swept EXCEPT the generation ``venv_dir`` currently points at, resolved
    via ``junction.junction_target`` — that exclusion matters here in a way
    it never did for the legacy prefixes below, because a ``.gen-*`` sibling
    CAN be the live, published tree; a `.build-*`/`.stale-*` sibling never
    was. ``.build-*``/``.stale-*`` (legacy, pre-junction rename-swap) are
    still swept unconditionally — by construction under the OLD layout
    neither was ever the published tree, and a box mid-migration or
    carrying old debris from before this rewrite may still have them lying
    around.

    Relocated here (docs/plans/2026-08-18-retire-coordinator-venv.md chunk
    C3) from ``coordinator_core.install.ensure_venv``, whose own
    ``_ensure_coordinator_venv_impl``/``_swap_in_new_venv`` still call this
    function to sweep siblings ahead of and after a rebuild — this module
    now owns the definition and ``ensure_venv`` imports it back, inverting
    the prior dependency direction. The uninstall leg below
    (``uninstall_remove_substrate``) must be able to reclaim these siblings
    on both the settings-home and legacy trees even after the venv builder
    itself is retired (C4/C5) — a lazy import of a since-deleted
    ``ensure_venv`` symbol would otherwise break uninstall permanently once
    that retirement lands.

    Safe to run unconditionally: a rebuild caller holds the venv build lock
    before calling this, so no OTHER process is concurrently populating its
    own generation sibling of THIS venv right now — anything matching a
    swept prefix at that point belongs to a run that has already ended
    (except the currently-published ``.gen-*``, excluded above). The
    uninstall leg below calls this with no lock held at all, which is still
    safe for the same underlying reason a rebuild's post-lock call is: by
    the time uninstall runs, no builder is (or, once C4/C5 land, can be)
    concurrently active against the same tree either. Never raises — a
    sweep failure must not block whatever operation is merely tidying up
    after it.
    """
    parent = venv_dir.parent
    build_prefix = f"{venv_dir.name}.build-"
    stale_prefix = f"{venv_dir.name}.stale-"
    gen_prefix = f"{venv_dir.name}.gen-"
    current_target = junction.junction_target(venv_dir)
    current_resolved = current_target.resolve() if current_target is not None else None
    try:
        children = list(parent.iterdir())
    except OSError:
        return
    for child in children:
        name = child.name
        if name.startswith(build_prefix) or name.startswith(stale_prefix):
            shutil.rmtree(child, ignore_errors=True)
        elif name.startswith(gen_prefix):
            if current_resolved is not None and child.resolve() == current_resolved:
                continue
            shutil.rmtree(child, ignore_errors=True)


def _uninstall_remove_setup_dir(claude_home: str, errors: List[str]) -> None:
    """Removal leg for the installed percolation ``setup/`` directory (AC9/
    AC10): ``<claude_home>/.claude/setup`` — the SAME ``.claude/setup``
    suffix appended to the same ``require_home()``-resolved home that
    ``substrate.py``'s ``setup_dest = Path(install_base) / ".claude" /
    "setup"`` writes into (Step 3d), so this leg targets exactly what that
    writer creates.

    Contract (plan C8), evaluated per residual FILE under ``setup/``, using a
    single directory-wide tracked-set probe (`_resolve_directory_tracked_set`,
    one ``git ls-files`` spawn for the whole tree, never per file):

      - ``publish-targets.portable`` specifically is ALWAYS reported, never
        deleted, checked FIRST and independent of tracked-ness — no installer
        writes it (a pure orphan), and it is the exact marker file the
        shared-install root-resolution rung (rung 3) keys on: while it
        survives, a stale installed ``setup/`` directory keeps winning root
        resolution forever. That is stated in the report itself.
      - a residual tracked by a git repo (necessarily foreign — this
        installed tree is never part of a coordinator-owned repo), OR one
        the probe could not classify at all (`_resolve_directory_tracked_set`
        returned ``None`` — no git on PATH, or the spawn failed/timed out:
        the Windows-first-class degrade case), is REPORTED and left in place,
        never deleted. Reporting is the contract — a silent skip repeats the
        original clobber-avoidance failure in reverse, and a silent delete
        makes a legitimate foreign checkout's loss permanent.
      - every other residual (untracked, with a resolvable tracked-set) is
        deleted outright.

    Idempotent / a no-op if ``setup/`` is already absent. Never removes
    ``setup/`` itself (or any subdirectory) while a residual has been
    reported and left inside it — the ``rmdir`` cleanup below is best-effort
    and silently no-ops on a non-empty directory."""
    setup_dir = Path(claude_home) / ".claude" / "setup"
    if not setup_dir.is_dir():
        return

    tracked_set = _resolve_directory_tracked_set(setup_dir)
    residual_files = sorted(
        p for p in setup_dir.rglob("*") if p.is_file() or p.is_symlink()
    )

    for path in residual_files:
        rel = path.relative_to(setup_dir).as_posix()

        if rel == "publish-targets.portable":
            print(
                f"uninstall_remove_substrate: leaving installed setup/ residual "
                f"'{rel}' in place ({path}) — no installer writes this file (it "
                "is a pure orphan), and it is the exact marker file the "
                "shared-install root-resolution rung keys on: while it "
                "survives, a stale installed setup/ directory keeps winning "
                "root resolution forever. Remove it manually once you are sure "
                "nothing else depends on it.",
                file=sys.stderr,
            )
            continue

        if tracked_set is None:
            print(
                f"uninstall_remove_substrate: leaving installed setup/ residual "
                f"'{rel}' in place ({path}) — git identity for this directory "
                "could not be resolved (no git on PATH, or the probe failed) so "
                "tracked-ness cannot be classified; refusing to delete. Remove "
                "it manually if you are sure it is safe to do so.",
                file=sys.stderr,
            )
            continue

        if rel in tracked_set:
            print(
                f"uninstall_remove_substrate: leaving installed setup/ residual "
                f"'{rel}' in place ({path}) — tracked by a (foreign) git repo. "
                "Remove it manually if you are sure it is safe to do so.",
                file=sys.stderr,
            )
            continue

        try:
            _unlink_clearing_readonly(path)
        except OSError as exc:
            errors.append(f"failed to remove setup/ residual {rel} ({path}): {exc}")

    # Best-effort deepest-first cleanup of now-empty subdirectories and the
    # setup/ dir itself -- silently no-ops wherever a residual above was
    # reported and left in place (directory non-empty).
    for d in sorted(
        (p for p in setup_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        setup_dir.rmdir()
    except OSError:
        pass


# Review: code-reviewer (Finding 1) — C6's careful-write backups
# (`substrate.py::_careful_write_backup_path`) are disposable-by-design and
# sit OUTSIDE the git-tracked `setup/` tree, but nothing ever cleaned them
# up: every re-install of a foreign-tracked destination added one more
# timestamped .bak forever, and a full-remove uninstall left the whole
# directory behind. full-remove-only (revert-to-marketplace is not a
# teardown -- these backups are the only recovery path for destinations this
# installer overwrote, so they must survive that mode).
def _uninstall_remove_setup_overwrite_backups(claude_home: str, errors: List[str]) -> None:
    """Removal leg for ``<claude_home>/.claude/setup-overwrite-backups`` (the
    C6 careful-write backup sibling of ``setup/``), full-remove mode ONLY.

    Reuses `_resolve_directory_tracked_set` -- the same tracked-ness check
    `_uninstall_remove_setup_dir` (C8) uses -- rather than assuming the
    directory is untracked: this directory is not expected to be inside any
    git-tracked tree, but the contract stays report-don't-delete for
    anything the probe says IS tracked, or that the probe could not
    classify at all (``None`` -- no git on PATH, or the spawn failed).
    Idempotent / a no-op if the directory is already absent."""
    backups_dir = Path(claude_home) / ".claude" / "setup-overwrite-backups"
    if not backups_dir.is_dir():
        return

    tracked_set = _resolve_directory_tracked_set(backups_dir)
    if tracked_set is None:
        print(
            "uninstall_remove_substrate: leaving setup-overwrite-backups/ in "
            f"place ({backups_dir}) — git identity for this directory could "
            "not be resolved (no git on PATH, or the probe failed) so "
            "tracked-ness cannot be classified; refusing to delete. Remove "
            "it manually if you are sure it is safe to do so.",
            file=sys.stderr,
        )
        return
    if tracked_set:
        print(
            "uninstall_remove_substrate: leaving setup-overwrite-backups/ in "
            f"place ({backups_dir}) — contains paths tracked by a git repo. "
            "Remove it manually if you are sure it is safe to do so.",
            file=sys.stderr,
        )
        return

    _rmtree_target(backups_dir, "setup-overwrite-backups", errors)


def uninstall_remove_substrate(
    mode: str = "full-remove",
    *,
    purge_operator_config: bool = False,
    force: bool = False,
    plugin_root: Optional[str] = None,
) -> bool:
    """Reverses surfaces #3, #5, #6, #7, #8, #9 (settings-home-aware).

    <mode> is one of ``full-remove`` (default) | ``revert-to-marketplace``.
    Operator-config purge (#9) is gated behind purge_operator_config, never
    implied by mode alone. Idempotent — every removal is a no-op if its
    target is already absent."""
    try:
        claude_home = require_home("uninstall_remove_substrate")
        sh = _settings_home_from(claude_home)
    except RequireHomeError as exc:
        print(str(exc), file=sys.stderr)
        return False

    if mode not in ("full-remove", "revert-to-marketplace"):
        print(
            f"uninstall_remove_substrate: unrecognized mode '{mode}' "
            "(expected full-remove | revert-to-marketplace)",
            file=sys.stderr,
        )
        return False

    ml_dir = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR") or f"{sh}/machine-local"

    errors: List[str] = []

    # ---- #3: clear coordinator registry keys (both modes) ----
    from shutil import which as _which

    if os.path.isdir(ml_dir) or _which("machine-local"):
        for key in (
            "plugin.mirrors.coordinator-claude.source_path",
            "plugin.mirrors.coordinator-claude.live_path",
            "plugin.mirrors.coordinator-claude.propagation_mode",
            "coordinator.python",
            "coordinator.whoami_src",
            "repos.doe_claude",
        ):
            if not ml_set(key, "", plugin_root=plugin_root, registry_dir=ml_dir):
                errors.append(f"failed to clear registry key {key}")
    else:
        print(
            f"uninstall_remove_substrate: no machine-local registry dir at {ml_dir} "
            "and no CLI on PATH — nothing to clear (no-op)",
            file=sys.stderr,
        )

    # ---- #5: whoami + venv (durable, settings-home) ----
    _rmtree_target(Path(sh) / "coordinator-whoami", "coordinator-whoami", errors)
    # `.coordinator-venv` is a JUNCTION (2026-08-20, ensure_venv's move to
    # junction-published generations) — `shutil.rmtree` refuses on a
    # junction outright, so this uses the junction-aware helper rather than
    # the plain `_rmtree_target` every other durable target above uses. A
    # legacy real-directory venv (pre-migration, or one this repo has no
    # purpose building any more per the 2026-08-20 PM ruling) still falls
    # through to plain `_rmtree_target` inside that helper.
    _rmtree_junction_or_target(Path(sh) / ".coordinator-venv", ".coordinator-venv", errors)
    # A crashed rebuild or a Windows deferred-reclaim can leave
    # `.coordinator-venv.gen-<pid>-<hex>` generation siblings (or, from a
    # box carrying pre-migration debris, `.build-*`/`.stale-*` ones) on
    # disk. `_sweep_orphaned_swap_dirs` (this module's own — see its
    # docstring above) only otherwise runs on the venv's NEXT rebuild — an
    # uninstall that removes the live dir and never rebuilds again would
    # otherwise leave those siblings behind permanently. Reuse the same
    # sweep here rather than reimplementing the prefix match a second time.
    _sweep_orphaned_swap_dirs(Path(sh) / ".coordinator-venv")
    whoami_compat = Path(claude_home) / "coordinator-whoami"
    _rm_target(whoami_compat, "compat symlink", errors)
    _rmtree_junction_or_target(
        Path(claude_home) / ".coordinator-venv", "legacy .coordinator-venv", errors
    )
    _sweep_orphaned_swap_dirs(Path(claude_home) / ".coordinator-venv")

    # ---- #6: .doe-root pointer (BOTH modes) ----
    _rm_target(Path(claude_home) / ".doe-root", ".doe-root", errors)

    # ---- installed percolation setup/ dir residuals (BOTH modes, C8/AC9/
    # AC10) ----  A stale setup/ affects root resolution (rung 3) the same
    # way regardless of full-remove vs revert-to-marketplace, so this leg is
    # not mode-gated -- see _uninstall_remove_setup_dir's own docstring for
    # the per-residual delete/report contract.
    _uninstall_remove_setup_dir(claude_home, errors)

    if mode == "full-remove":
        # ---- #7: ~/.claude/bin/ coordinator-owned forwarders (individual rm, NEVER rm -rf) ----
        # LEGACY CLEANUP ONLY as of the owns-zero-claude-bin retirement
        # (Gate 6): substrate.py's Step 3c-compat block — which used to mirror
        # every settings-home bin/ forwarder into this dir — was deleted, so a
        # fresh install after that change writes NOTHING here. This leg stays
        # so a full-remove on a machine installed BEFORE the retirement still
        # sweeps its old compat-mirror artifacts; on a post-retirement
        # install every _rm_target call below is simply a no-op (file never
        # existed). Same shape as the "platform-localize.sh: legacy cleanup"
        # note a few lines below.
        bin_dir = Path(claude_home) / "bin"
        # settings-home/bin is the CANONICAL home for these artifacts (DR-071/
        # DR-072) — ~/.claude/bin above is the retired compat mirror. The
        # legacy-name sweep must cover both dirs: settings-home/bin was
        # previously covered only incidentally, by #8's wholesale
        # `_rmtree_target(sh_path / "bin")` below — which still leaves a
        # legacy-named artifact there un-swept whenever that dir sweep is
        # ever skipped or the artifact predates it.
        settings_home_bin_dir = Path(sh) / "bin"
        coord_bin_names = (
            "machine-local", "_machine_local.py", "machine-local.cmd", "python3.cmd",
            "claude-home", "_claude_home.py", "claude-home.cmd",
            "resolve-coordinator-clone", "coordinator-settings-home",
            # platform-localize.sh: legacy cleanup of pre-2026-07-22 installs
            # (DoE commit 6fb5fb37 renamed the source to .py/.cmd) — kept so a
            # full-remove on a machine installed before the rename still
            # sweeps the old artifact.
            "platform-localize.sh", "platform-localize.py", "platform-localize.cmd",
            "coordinator-settings-home.cmd", "coordinator-settings-home.ps1",
            "claude_machine_local.py", "claude-machine-local.sh", "claude-machine-local.ps1",
            "resolve-coordinator-clone.cmd",
            # _resolve_claude_klabauter.py: the shared resolve-claude-klabauter-bin ladder module
            # installed once alongside every agent-helper forwarder (see
            # coordinator_core.install.substrate._install_bin_resolvers'
            # rm_family) — must be swept the same as the forwarders it backs.
            "_resolve_claude_klabauter.py",
            # hook-sitepackages.txt: the site-packages pointer
            # ensure_coordinator_venv publishes for a sibling repo's hook
            # bootstrap (2026-08-10-interpreter-surface-four-asks.md chunk
            # C5) — see coordinator_core.install.ensure_venv.
            # SITEPACKAGES_POINTER_NAME.
            "hook-sitepackages.txt",
        )
        # Agent-helper forwarders (_install_bin_resolvers writes both the bare
        # name via _write_agent_forwarder and a "<name>.cmd" via _install_one,
        # for every derived name) — derived here from the SAME
        # coordinator/bin/ directory listing substrate.py installs from
        # (_derive_agent_helper_names), rather than a second hardcoded
        # literal list, so the two can't drift apart again the way the
        # machine-local/claude-home family did (see module-header regression
        # note above this leg's test). Best-effort: a claude-klabauter checkout that
        # is already gone/broken must not abort the rest of uninstall — an
        # empty derived set just means this sub-leg is a no-op.
        try:
            _claude_klabauter_root_str, _resolution_class = coordinator_engine_root_with_class()
            agent_bin = Path(_claude_klabauter_root_str) / "coordinator" / "bin"
            derived_names = _derive_agent_helper_names(agent_bin)
        except RuntimeError:
            derived_names = ()
        agent_helper_bin_names = tuple(derived_names) + tuple(
            _agent_cmd_dest_name(name) for name in derived_names
        )
        for coord_bin_name in coord_bin_names + agent_helper_bin_names:
            _rm_target(bin_dir / coord_bin_name, coord_bin_name, errors)
            _rm_target(
                settings_home_bin_dir / coord_bin_name,
                coord_bin_name,
                errors,
            )

        # ---- #8: coordinator-authored artifacts under settings-home (full-remove only) ----
        sh_path = Path(sh)
        if sh_path.is_dir():
            _rmtree_target(sh_path / "machine-local", "settings-home machine-local", errors)
            _rmtree_target(sh_path / "bin", "settings-home bin", errors)
            _rmtree_target(sh_path / "state" / "handoffs", "settings-home state/handoffs", errors)
            manifest = sh_path / "settings-manifest.md"
            _rm_target(manifest, "settings-manifest.md", errors)
            try:
                (sh_path / "state").rmdir()
            except OSError:
                pass  # routine: non-empty (other state survives blanket-with-provenance) -- not an error
            try:
                sh_path.rmdir()
            except OSError:
                pass  # routine: non-empty for the same reason
        ml_compat = Path(claude_home) / "machine-local"
        _rm_target(ml_compat, "compat symlink machine-local", errors)

        # ---- setup-overwrite-backups/ (full-remove only, C6 backup cleanup) ----
        _uninstall_remove_setup_overwrite_backups(claude_home, errors)

    # ---- #9: operator config (gated, both modes) ----
    if purge_operator_config:
        if not _uninstall_purge_operator_config(claude_home, force, plugin_root=plugin_root):
            errors.append("operator-config purge failed")

    for e in errors:
        print(f"uninstall_remove_substrate: {e}", file=sys.stderr)
    return not errors


def _uninstall_purge_operator_config(
    claude_home: str, force: bool, *, plugin_root: Optional[str] = None
) -> bool:
    """Reverses surface #9 — ONLY when explicitly invoked. Hand-edit detection:
    re-render each file's known-fixed shape and byte-compare against disk;
    any diff is treated as possibly-hand-edited (fail-safe) and refuses
    removal unless force=True."""
    identity_file = Path(claude_home) / "coordinator-identity.yaml"
    working_repos_file = Path(claude_home) / "working-repos.yaml"
    claude_local_file = Path(claude_home) / "CLAUDE.local.md"
    ok = True

    # ---- coordinator-identity.yaml: re-render + byte-compare ----
    if identity_file.is_file():
        text = identity_file.read_text(encoding="utf-8", errors="replace")
        operator_name = ""
        for line in text.split("\n"):
            if line.startswith("operator_name:"):
                operator_name = line.split(": ", 1)[1] if ": " in line else line[len("operator_name:"):].strip()
                break
        expected = (
            "# ~/.claude/coordinator-identity.yaml — operator-local, NEVER a publish target\n"
            "version: 1\n"
            f"operator_name: {operator_name}"
        )
        actual = text.rstrip("\n")
        if force or expected == actual:
            try:
                identity_file.unlink()
            except OSError as exc:
                print(f"_uninstall_purge_operator_config: failed to remove {identity_file}: {exc}", file=sys.stderr)
                ok = False
        else:
            print(
                f"_uninstall_purge_operator_config: {identity_file} differs from a fresh "
                "re-render — possibly hand-edited. Refusing to remove without --force.",
                file=sys.stderr,
            )
            ok = False

    # ---- working-repos.yaml: no static template; fail-safe requires --force ----
    if working_repos_file.is_file():
        if force:
            try:
                working_repos_file.unlink()
            except OSError as exc:
                print(f"_uninstall_purge_operator_config: failed to remove {working_repos_file}: {exc}", file=sys.stderr)
                ok = False
        else:
            print(
                f"_uninstall_purge_operator_config: {working_repos_file} has no fixed "
                "template to re-render against — possibly hand-edited. Refusing to "
                "remove without --force.",
                file=sys.stderr,
            )
            ok = False

    # ---- CLAUDE.local.md: template-rendered; any diff => --force required ----
    if claude_local_file.is_file():
        try:
            coordinator_root = resolve_coordinator_root()
        except RuntimeError:
            coordinator_root = ""
        render_tmpl = Path(coordinator_root) / "templates" / "CLAUDE.local.md.tmpl" if coordinator_root else None

        if force:
            try:
                claude_local_file.unlink()
            except OSError as exc:
                print(f"_uninstall_purge_operator_config: failed to remove {claude_local_file}: {exc}", file=sys.stderr)
                ok = False
        elif coordinator_root and render_tmpl.is_file():
            operator_name = ""
            if identity_file.is_file():
                for line in identity_file.read_text(encoding="utf-8", errors="replace").split("\n"):
                    if line.startswith("operator_name:"):
                        operator_name = line.split(": ", 1)[1] if ": " in line else ""
                        break
            working_repos_block = (
                # bash's $(cat ...) strips all
                # trailing newlines; read_text() preserves them, which
                # mismatched the render-template.sh byte-comparison on
                # every pristine (non-hand-edited) file and forced --force
                # unconditionally. rstrip("\n") matches the oracle's shape.
                working_repos_file.read_text(encoding="utf-8", errors="replace").rstrip("\n")
                if working_repos_file.is_file()
                else ""
            )

            # In-process call to the already-ported render_template.render() —
            # no subprocess/bash spawn: render-template.sh no longer exists on
            # disk (retired in favor of render-template.py), and its logic is
            # this exact module, co-located in this repo. Byte-comparing the
            # in-memory render against the on-disk file reproduces the bash
            # oracle's -o-tempfile-then-byte-compare shape without the
            # tempfile or the external process.
            try:
                rendered_text, render_rc, _render_err = render_template.render(
                    str(render_tmpl),
                    [("PM_NAME", operator_name), ("WORKING_REPOS", working_repos_block)],
                )
            except OSError:
                rendered_text, render_rc = None, 1
            same = (
                render_rc == 0
                and rendered_text is not None
                and rendered_text.encode("utf-8", errors="surrogateescape") == claude_local_file.read_bytes()
            )

            if same:
                try:
                    claude_local_file.unlink()
                except OSError as exc:
                    print(f"_uninstall_purge_operator_config: failed to remove {claude_local_file}: {exc}", file=sys.stderr)
                    ok = False
            else:
                print(
                    f"_uninstall_purge_operator_config: {claude_local_file} differs from "
                    "a fresh template re-render (or re-render failed) — possibly "
                    "hand-edited. Refusing to remove without --force.",
                    file=sys.stderr,
                )
                ok = False
        else:
            print(
                f"_uninstall_purge_operator_config: cannot resolve coordinator "
                f"root/template to re-render {claude_local_file} — possibly "
                "hand-edited. Refusing to remove without --force.",
                file=sys.stderr,
            )
            ok = False

    return ok


def uninstall_set_plugin_endstate(
    mode: str = "full-remove", *, plugin_root: Optional[str] = None
) -> bool:
    """Reverses surface #1 (plugin-source wiring).

    full-remove: clears the mirror wiring keys (does NOT delete <DoE>/coordinator
    source — separate PM-gated decision).
    revert-to-marketplace: re-registers the flat
    ${CLAUDE_HOME:-$HOME}/.claude/plugins/coordinator-claude plugin AND clears
    live_path/propagation_mode, then invokes
    coordinator_core.hooks.platform_localize in-process so settings.local.json
    + known_marketplaces.json agree (CHECK 5 tri-file contract)."""
    try:
        claude_home = require_home("uninstall_set_plugin_endstate")
        sh = _settings_home_from(claude_home)
    except RequireHomeError as exc:
        print(str(exc), file=sys.stderr)
        return False

    if mode not in ("full-remove", "revert-to-marketplace"):
        print(
            f"uninstall_set_plugin_endstate: unrecognized mode '{mode}' "
            "(expected full-remove | revert-to-marketplace)",
            file=sys.stderr,
        )
        return False

    ml_dir = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR") or f"{sh}/machine-local"

    errors: List[str] = []
    from shutil import which as _which

    if mode == "revert-to-marketplace" and (os.path.isdir(ml_dir) or _which("machine-local")):
        for key in (
            "plugin.mirrors.coordinator-claude.live_path",
            "plugin.mirrors.coordinator-claude.propagation_mode",
        ):
            if not ml_set(key, "", plugin_root=plugin_root, registry_dir=ml_dir):
                errors.append(f"failed to clear registry key {key}")
    elif mode == "revert-to-marketplace":
        print(
            f"uninstall_set_plugin_endstate: no machine-local registry dir at {ml_dir} "
            "and no CLI on PATH — nothing to clear (no-op)",
            file=sys.stderr,
        )

    if mode == "full-remove":
        for e in errors:
            print(f"uninstall_set_plugin_endstate: {e}", file=sys.stderr)
        return not errors

    # ---- revert-to-marketplace: re-register the flat plugin tree ----
    flat_plugin_dir = Path(claude_home) / "plugins" / "coordinator-claude"

    if not flat_plugin_dir.is_dir():
        try:
            coordinator_root = resolve_coordinator_root()
        except RuntimeError:
            coordinator_root = ""

        if not coordinator_root:
            errors.append(
                f"cannot resolve coordinator root — cannot re-register the flat "
                f"marketplace plugin at {flat_plugin_dir}"
            )
        elif not (Path(coordinator_root) / ".claude-plugin").is_dir():
            errors.append(
                f"resolved coordinator root {coordinator_root} has no .claude-plugin/ "
                "manifest — cannot re-register the flat marketplace plugin"
            )
        else:
            try:
                flat_plugin_dir.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                errors.append(f"failed to create {claude_home}/plugins")
            try:
                shutil.copytree(coordinator_root, flat_plugin_dir)
            except OSError as exc:
                errors.append(f"failed to copy {coordinator_root} -> {flat_plugin_dir}: {exc}")

    # ---- satisfy CHECK 5's tri-file agreement postcondition ----
    # In-process call to the already-ported coordinator_core.hooks.platform_localize
    # — no subprocess/bash spawn; platform-localize.sh no longer exists (DoE
    # 6fb5fb37, replaced by platform-localize.py). The env-var save/mutate/restore
    # below reproduces the former subprocess env exactly (CLAUDE_HOME popped,
    # HOME pointed at the derived home) so
    # platform_localize.main()'s own CLAUDE_HOME/HOME resolution resolves to the
    # identical settings.local.json / known_marketplaces.json / plugins paths
    # the bash oracle's subprocess env produced.
    prior_env = dict(os.environ)
    try:
        os.environ.pop("CLAUDE_HOME", None)
        # Folded to "/" first: claude_home may arrive backslash-spelled on
        # native Windows shells (`C:\Users\me\.claude`) — an unfolded
        # endswith would silently skip the strip and leave HOME pointed at
        # the .claude dir itself rather than its parent.
        _folded_claude_home = claude_home.replace("\\", "/")
        os.environ["HOME"] = (
            claude_home[: -len("/.claude")] if _folded_claude_home.endswith("/.claude") else claude_home
        )
        localize_rc = platform_localize.main([])
    finally:
        os.environ.clear()
        os.environ.update(prior_env)

    if localize_rc != 0:
        errors.append(
            "platform-localize failed — settings.local.json / "
            "known_marketplaces.json may not agree with the flat plugin tree "
            "(CHECK 5 may fail)"
        )

    for e in errors:
        print(f"uninstall_set_plugin_endstate: {e}", file=sys.stderr)
    return not errors


_ORCHESTRATE_USAGE = """\
Usage: coordinator-uninstall.sh [OPTIONS]

Reverses the maximalist coordinator install's out-of-repo surfaces
(settings.json generated hooks, shell shim/wrapper, machine-local registry
keys, whoami/venv, .doe-root pointer, ~/.claude/bin forwarders, plugin
wiring). All filesystem/registry targets are resolved from environment
overrides (CLAUDE_HOME, COORDINATOR_SETTINGS_HOME, MACHINE_LOCAL_REGISTRY_DIR)
— never a hardcoded real-user path.

Options:
  (none)                    Full-remove (default, PM-ratified): reverses
                             every surface, leaves no coordinator tree
                             resolvable (check-install-singularity.sh ->
                             "no coordinator tree resolved").
  --keep-marketplace         Revert-to-marketplace: re-registers the flat
                             ~/.claude/plugins/coordinator-claude tree and
                             clears live_path, instead of removing wiring
                             entirely. Machine-local dir and ~/.claude/bin
                             forwarders are preserved (other surfaces may
                             still depend on them post-revert); .doe-root is
                             REMOVED (it is a resolution-shadowing pointer
                             that would otherwise outrank the re-registered
                             flat tree and defeat the revert).
  --purge-operator-config    Also purge operator-config surface #9
                             (coordinator-identity.yaml, working-repos.yaml,
                             CLAUDE.local.md). Conservative preserve-by-
                             default; fails loud on a hand-edited file
                             unless combined with --force.
  --force                    Fail-safe override for --purge-operator-config
                             — required to remove a file that differs from
                             its fresh re-render (possibly hand-edited).
  --dry-run                  Print the ordered leg plan and perform ZERO
                             filesystem writes and ZERO registry-key clears.
                             Not merely "prints the plan" — every mutating
                             call is skipped entirely, not run in some
                             preview mode.
  -h, --help                  Show this help and exit 0.

Examples:
  coordinator-uninstall.sh                         # full-remove
  coordinator-uninstall.sh --keep-marketplace       # revert-to-marketplace
  coordinator-uninstall.sh --dry-run                # print plan, no writes
  coordinator-uninstall.sh --purge-operator-config --force
"""


def orchestrate_uninstall(argv: Optional[List[str]] = None) -> int:
    """Top-level orchestration: CLI-flag parsing, ordered-plan printing,
    --dry-run short-circuit (ZERO filesystem writes / registry-key clears —
    the mutating leg functions below are never even called on that path),
    and fail-loud sequencing of the five legs in dependency order (strip
    settings hooks -> remove shim -> strip cmd.exe AutoRun guard -> remove
    substrate -> set plugin end-state — see module-level leg functions for
    the per-surface rationale this order depends on; the AutoRun leg sits
    immediately after remove-shim because both reverse `claude`-invocation
    interception surfaces and neither leg depends on the other or on
    `plugin_root`).

    Calls the leg functions in-process (direct Python call, not a
    subprocess re-exec of this module) — the orchestrator and the legs
    already live in the same process/module, so there is no IPC/subprocess
    hop to preserve from the bash oracle (which shelled out to a second
    `python3 -m` invocation per leg only because bash itself cannot call a
    Python function in-process).

    Returns 0 on success, 1 on a parse error or the first leg failure
    (fail-loud-on-ambiguity: does not continue sequencing past a failed
    leg), matching the bash oracle's exit-code contract.

    Port of: coordinator-uninstall.sh (DoE b5a4192c, 2026-07-20, C7).
    Spec backlink: DoE-claude:pln-first-class-coordinator-uninst-15db2e § C7
    """
    mode = "full-remove"
    purge_operator_config = False
    force = False
    dry_run = False

    args = list(argv) if argv is not None else sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--keep-marketplace":
            mode = "revert-to-marketplace"
        elif a == "--purge-operator-config":
            purge_operator_config = True
        elif a == "--force":
            force = True
        elif a == "--dry-run":
            dry_run = True
        elif a in ("-h", "--help"):
            sys.stdout.write(_ORCHESTRATE_USAGE)
            return 0
        else:
            print(f"coordinator-uninstall.sh: unrecognized option '{a}'", file=sys.stderr)
            print("Run with -h/--help for usage.", file=sys.stderr)
            return 1
        i += 1

    print(
        f"coordinator-uninstall: mode={mode} purge-operator-config="
        f"{1 if purge_operator_config else 0} force={1 if force else 0} "
        f"dry-run={1 if dry_run else 0}"
    )
    print("coordinator-uninstall: ordered plan:")
    print("  1. strip settings.json generated hooks (resolves coordinator-root)")
    print("  2. remove shell shim + wrapper (#4a/#4b/#4c/#10)")
    print("  3. strip cmd.exe AutoRun guard (HKCU Command Processor\\AutoRun)")
    print("  4. remove host-sampler scheduled task (Windows Task Scheduler)")
    print(
        "  5. remove substrate (registry keys, whoami/venv, .doe-root, "
        "~/.claude/bin forwarders, settings-home tree)"
    )
    if purge_operator_config:
        print(f"     (+ purge operator config, force={1 if force else 0})")
    print(f"  6. set plugin end-state ({mode})")

    if dry_run:
        print(
            "coordinator-uninstall: --dry-run — performing ZERO filesystem "
            "writes and ZERO registry-key clears."
        )
        print(render_uninstall_dry_run_report(_load_install_receipt()))
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        try:
            plugin_root = resolve_coordinator_root()
        except RuntimeError:
            plugin_root = None

    def fail_loud(surface: str, remediation: str) -> int:
        print(f"coordinator-uninstall: FAILED at surface: {surface}", file=sys.stderr)
        print(f"  Remediation: {remediation}", file=sys.stderr)
        print(
            "coordinator-uninstall: stopping — not continuing past a "
            "fail-loud leg (fail-loud-on-ambiguity doctrine).",
            file=sys.stderr,
        )
        return 1

    if not uninstall_strip_settings_hooks():
        return fail_loud(
            "settings.json generated hooks (surface #2)",
            "Resolve coordinator root explicitly (COORDINATOR_ROOT env, machine-local "
            "repos.doe_claude, REPO_DOE_CLAUDE env, or ${CLAUDE_HOME:-$HOME}/.doe-root "
            "pointer) and re-run.",
        )

    if not uninstall_remove_shim():
        return fail_loud(
            "shell shim / wrapper (surfaces #4a/#4b/#4c/#10)",
            "Check stderr above for a hand-modified legacy ~/.bashrc block or a removal "
            "failure; resolve manually then re-run (this leg is idempotent — re-running "
            "after a manual fix is safe).",
        )

    if not uninstall_strip_cmd_autorun():
        return fail_loud(
            "cmd.exe AutoRun guard (HKCU Command Processor\\AutoRun)",
            "Check stderr above for the specific registry error; this leg is idempotent "
            "(re-running after a manual fix is safe), and non-Windows/foreign-AutoRun "
            "content are always clean no-ops, never a failure source.",
        )

    if not uninstall_strip_host_sampler_task():
        return fail_loud(
            "host-sampler scheduled task (Windows Task Scheduler)",
            "Check stderr above for the specific schtasks.exe error; this leg is "
            "idempotent (re-running after a manual fix is safe), and non-Windows/"
            "already-absent-task are always clean no-ops, never a failure source.",
        )

    if not uninstall_remove_substrate(
        mode,
        purge_operator_config=purge_operator_config,
        force=force,
        plugin_root=plugin_root,
    ):
        return fail_loud(
            "substrate (registry keys / whoami / venv / .doe-root / ~/.claude/bin "
            "forwarders / settings-home tree, surfaces #3/#5/#6/#7/#8/#9)",
            "Check stderr above for the specific removal/registry-clear failure. If it "
            "names a hand-edited operator-config file, re-run with "
            "--purge-operator-config --force to override, or omit "
            "--purge-operator-config to preserve it.",
        )

    if not uninstall_set_plugin_endstate(mode, plugin_root=plugin_root):
        return fail_loud(
            "plugin end-state (surface #1)",
            "Check stderr above — likely an unresolvable coordinator root (for "
            "revert-to-marketplace re-registration) or a platform-localize.sh failure "
            "(CHECK 5 tri-file agreement not guaranteed). Resolve and re-run "
            "(idempotent).",
        )

    print(f"coordinator-uninstall: complete (mode={mode}).")
    return 0


# ---------------------------------------------------------------------------
# CLI trampoline entry point — one subcommand per leg.
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="coordinator-uninstall-legs")
    sub = parser.add_subparsers(dest="leg", required=True)

    sub.add_parser("strip-settings-hooks")
    sub.add_parser("remove-shim")
    sub.add_parser("strip-cmd-autorun")
    sub.add_parser("strip-host-sampler-task")

    p_substrate = sub.add_parser("remove-substrate")
    p_substrate.add_argument("mode", nargs="?", default="full-remove")
    p_substrate.add_argument("--purge-operator-config", action="store_true")
    p_substrate.add_argument("--force", action="store_true")

    p_endstate = sub.add_parser("set-plugin-endstate")
    p_endstate.add_argument("mode", nargs="?", default="full-remove")

    args = parser.parse_args(argv)
    # bash oracle (_uninstall_ml_set) self-locates
    # via BASH_SOURCE, independent of any env var. CLAUDE_PLUGIN_ROOT is an
    # explicit-override rung, honored first, but its absence (e.g. this CLI
    # invoked directly, bypassing the coordinator-uninstall.sh wrapper that
    # normally exports it) must not silently degrade ml_set/resolve_machine_local_cli
    # to PATH-only resolution. resolve_coordinator_root() carries the same
    # self-locating fallback chain (machine-local registry -> REPO_DOE_CLAUDE
    # env -> .doe-root pointer) already used elsewhere in this module for the
    # analogous CLAUDE.local.md render seam.
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        try:
            plugin_root = resolve_coordinator_root()
        except RuntimeError:
            plugin_root = None

    if args.leg == "strip-settings-hooks":
        ok = uninstall_strip_settings_hooks()
    elif args.leg == "remove-shim":
        ok = uninstall_remove_shim()
    elif args.leg == "strip-cmd-autorun":
        ok = uninstall_strip_cmd_autorun()
    elif args.leg == "strip-host-sampler-task":
        ok = uninstall_strip_host_sampler_task()
    elif args.leg == "remove-substrate":
        ok = uninstall_remove_substrate(
            args.mode,
            purge_operator_config=args.purge_operator_config,
            force=args.force,
            plugin_root=plugin_root,
        )
    elif args.leg == "set-plugin-endstate":
        ok = uninstall_set_plugin_endstate(args.mode, plugin_root=plugin_root)
    else:  # pragma: no cover — argparse enforces the choice set above
        ok = False

    return 0 if ok else 1


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="uninstall-legs",
    source_module="coordinator_core.install.uninstall_legs",
    clauses=(
        # Clause 1 — `uninstall_set_plugin_endstate`'s revert-to-marketplace
        # branch: `shutil.copytree(coordinator_root, flat_plugin_dir)`
        # writes a FRESH copy of the plugin tree to
        # `<claude-home>/plugins/coordinator-claude` when that flat
        # directory is not already present. This manifest describes what
        # INSTALL puts on a machine, and this specific write is the one
        # place this module deviates from that frame — install (see
        # `maximalist.py`'s Step 5 `register-coordinator-mirror`) never
        # populates this flat path itself; it belongs to Claude Code's own
        # marketplace-install mechanism. `--keep-marketplace` uninstall
        # re-creates it as a courtesy so a reverted machine still resolves
        # a working coordinator plugin tree without a marketplace re-fetch
        # — a genuine write this module performs, not a reversal of
        # anything install wrote.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<claude-home>/plugins/coordinator-claude",
                    reason=(
                        "uninstall_set_plugin_endstate (revert-to-marketplace "
                        "mode, --keep-marketplace): shutil.copytree(coordinator_root, "
                        "flat_plugin_dir) re-creates the flat marketplace plugin "
                        "tree if absent, so a reverted machine still resolves a "
                        "working coordinator plugin without an external "
                        "marketplace re-fetch. Only runs when the directory does "
                        "not already exist."
                    ),
                ),
            ),
        ),
    ),
)
"""Every OTHER filesystem/registry mutation this module performs (unlink,
rmtree, os.replace-atomic block-strips in `uninstall_strip_settings_hooks`/
`uninstall_remove_shim`, registry-key clears in `uninstall_remove_substrate`/
`uninstall_set_plugin_endstate`'s full-remove branch, the HKCU AutoRun strip
in `uninstall_strip_cmd_autorun`) is a REVERSAL of a
surface `install` itself wrote — this manifest describes what install PUTS
on a machine, so a leg that only ever deletes/clears what install created is
a CONSUMER of the manifest, not a contributor to it: declaring those
removals here would add entries to the denominator install never produces,
corrupting the count a sibling repo's lockstep test quantifies over (per
this dispatch's own framing).

The disposition-report API (`classify_entry_disposition`,
`UninstallDispositionReport`, `render_disposition_report`,
`render_uninstall_dry_run_report` — landed earlier today) persists nothing
to disk: `render_disposition_report`/`render_uninstall_dry_run_report`
return a `str` that callers `print()`; `_load_install_receipt` returns
`None` unconditionally (C5's receipt module exists but nothing wires
emission into a live install run yet, per its own docstring) — no file is
read or written by any of it. None of that API is this writer's surface
either.

Clause 1 above is the one exception: a genuine write not traceable to
reversing any install-side surface, so it is this writer's own to declare.
"""


if __name__ == "__main__":
    sys.exit(main())
