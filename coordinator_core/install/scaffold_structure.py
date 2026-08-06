"""
coordinator_core.install.scaffold_structure — Port of:
``coordinator/bin/scaffold-canonical-structure.sh`` (example-doctrine-repo 432e3285,
2026-07-22) [example-doctrine-repo repo].

Purpose: parse the example-doctrine-repo-owned ``canonical-structure.yaml`` manifest and idempotently
scaffold every ``creation: eager`` directory (+ README/.gitkeep) and eager
template-backed file entry under a target root, in both a dry-run/check mode
(never mutates disk) and a live mode. Two call sites use this module:
``plugin_health.sentinel.probe_p12`` (dry-run only, advisory doctor probe) and
``install.maximalist`` Step 7 (live, advisory phase).

Three outcomes per declared ``creation: eager`` entry (DR-116,
``docs/decisions/DR-116-creation-eager-declares-lifecycle-not-agency.md``,
Example-doctrine-repo repo): (a) creatable here -- a directory, or template-backed file;
(b) satisfied-elsewhere -- carries a non-empty ``produced_by:`` naming a
producer in another repo's onboarding flow (e.g. a ``/coordinator:repo-setup``
phase); (c) a genuine orphan -- neither ``template:`` nor ``produced_by:``,
a manifest/parser disagreement (this is the case ``d6fa361d`` was built for
and reports exactly as before). DR-116's ruling: ``creation:`` alone
conflated two orthogonal axes -- *when must this exist* (lifecycle) versus
*who creates it* (agency). ``produced_by:`` names the agency half so a
lifecycle-eager, elsewhere-produced entry is neither silently created nor
misreported as an orphan.

Port backlink: docs/plans/2026-07-17-retire-doe-bash-bridges-native-python.md
    (chunk C4 — Port D).

Public surface (pinned contract — do not change without updating consumers):

    class ScaffoldError(RuntimeError): ...
    @dataclass class ScaffoldEntry: path, creation, gitkeep, readme, template,
        produced_by
    @dataclass class ScaffoldResult: created_dirs, created_readmes,
        created_gitkeeps, created_files, skipped, dropped_entries,
        satisfied_elsewhere --
        ``would_create_count()`` sums the four ``created_*`` counters, which
        double as "would create" counts under ``dry_run=True`` (mirrors the
        bash oracle's shared ``CREATED_*`` variable naming — same counters,
        mode-dependent meaning). ``dropped_entries`` and ``satisfied_elsewhere``
        are both separate from all of the above (see AC below) -- neither is
        ever a create, a skip, or a would-create; ``dropped_entries`` is a
        manifest/parser disagreement, ``satisfied_elsewhere`` is a declared
        producer elsewhere (DR-116), surfaced to the caller via distinct
        channels so the one signal (a genuine orphan) never drowns in the
        other (an expected, elsewhere-produced entry).
    @dataclass class ManifestParseResult: entries, dropped, satisfied_elsewhere --
        ``dropped`` is a list of ``(path, reason)`` tuples for declared
        ``creation: eager`` entries that are neither a directory,
        template-backed, nor ``produced_by:``-backed (see "Dropped-entry
        reporting" below). ``satisfied_elsewhere`` is a list of
        ``(path, producer)`` tuples for declared ``creation: eager`` entries
        that carry a non-empty ``produced_by:`` and are not template-backed
        (template-backed wins -- see parse_manifest's docstring for the
        both-fields precedence).
    def parse_manifest(text: str) -> ManifestParseResult
    def locate_manifest(manifest_root: Path) -> Path
    def scaffold_canonical_structure(
        root: str | Path, manifest_root: str | Path, *, dry_run: bool = False,
    ) -> ScaffoldResult

Standalone CLI entry (thin wrapper over scaffold_canonical_structure, mirrors
substrate.py's main()):

    python3 -m coordinator_core.install.scaffold_structure --root <path> \
        [--manifest-root <path>] [--dry-run]

Explicit-params discipline (AC G5): both parameters (target root, manifest
root) are explicit — this module never re-reads ``os.environ`` or re-derives
a plugin/bin-tree location itself. Callers resolve their own root once
(``sibling_bin_dir.parent`` at the probe_p12 call site, ``coord_root`` at the
maximalist Step 7 call site — see AC D5) and pass it in.

Manifest ownership (AC D5): ``canonical-structure.yaml`` is example-doctrine-repo-owned
(``example-doctrine-repo/coordinator/canonical-structure.yaml``) and is NOT vendored
here — this module only reimplements the *parse*, locating the file via the
caller-supplied ``manifest_root``.

Windows/MSYS path normalization (AC D6): the bash oracle normalizes
backslashes to forward slashes in ``--root`` before use
(``ROOT_PATH="${ROOT_PATH//\\//}"``) so a Git-Bash/MSYS caller's
backslash-separated path still resolves. This port applies the equivalent
normalization to ``root`` before constructing a ``Path``.

Exit/error-code parity (AC D4): usage/prereq and manifest-parse failures
raise ``ScaffoldError`` (mirrors bash exit 1 / exit 2) for the caller to
handle per its own disposition (probe_p12: graceful-absent → ``[]``;
maximalist Step 7: advisory WARN, non-fatal). A missing declared template is
fatal (raises ``ScaffoldError``) on a **live** run but warn-and-continue
(logged, counted as neither created nor skipped) on a **dry-run** — this
exact asymmetry is preserved from the bash oracle (AC D2 would-create
parity: a missing-template entry never counts toward "would create").

Doctor-probe sequencing note (Review: code-reviewer, finding 2 -- EM
disposition): ``plugin_health.sentinel.probe_p12`` deliberately does NOT
yet consult ``ScaffoldResult.dropped_entries`` for its amber decision -- it
only ambers on ``would_create_count() >= 1``. Wiring ``dropped_entries``
into probe_p12 today would flip the whole fleet's doctor probe to amber
immediately, because the live manifest currently has six orphan entries
that are not yet an agreed manifest/parser disagreement so much as
Example-doctrine-repo's ``produced_by:`` manifest half not having landed yet. This is
gated on that sibling-repo change landing first, at which point wiring
probe_p12 to ``dropped_entries`` becomes low-blast-radius. A future reader
must not "fix" this silently without knowing the sequencing.

Dropped-entry reporting: a manifest entry declared ``creation: eager`` that
is neither a directory (``path`` ending in ``/``), template-backed (has a
``template:`` value), nor satisfied-elsewhere (has a ``produced_by:``
value) has no creation vector the code below knows how to honour -- this is
a manifest/parser disagreement, not a no-op. Such entries are surfaced,
never silently discarded: ``parse_manifest`` reports them via
``ManifestParseResult.dropped`` (a list of ``(path, reason)``),
``scaffold_canonical_structure`` copies that list onto
``ScaffoldResult.dropped_entries`` and appends one human-readable line per
dropped entry to ``result.lines`` in both dry-run and live mode, so any
caller printing ``result.lines`` (CLI, maximalist Step 7's advisory WARN
path) names the drop and its reason.

Satisfied-elsewhere reporting (DR-116): a manifest entry declared
``creation: eager`` with a non-empty ``produced_by:`` and no ``template:``
is NOT created here and NOT reported as dropped -- it is surfaced via its
own channel, ``ManifestParseResult.satisfied_elsewhere`` /
``ScaffoldResult.satisfied_elsewhere`` (a list of ``(path, producer)``),
with one human-readable line per entry appended to ``result.lines``,
distinct from the ``dropped`` lines. Kept separate deliberately: folding it
into ``dropped`` would recreate the exact noise problem DR-116 exists to
remove -- a drop report so reliably noisy the one line that matters (a real
orphan) goes unread.

Negative-spec:
  - Does NOT vendor a copy of canonical-structure.yaml.
  - Does NOT change the manifest schema or the sentinel probe selection grammar.
  - Does NOT touch probe_p19 / release-currency, or the venv-ensure call sites.
  - Does NOT make a dropped eager-but-templateless-and-unproduced entry start
    materializing on disk -- that would require a new (non-template-copy)
    creation path in ``_scaffold_file_entry`` and is a deliberate
    out-of-scope, cross-repo manifest/parser design question, not something
    this module decides unilaterally.
  - A non-eager templateless entry (e.g. ``creation: lazy``) is NOT reported
    as dropped -- it was never a candidate for creation, so reporting it
    would be noise, not signal. The same holds for a non-eager entry
    carrying ``produced_by:`` -- it is not reported anywhere.
  - ``produced_by:`` is opaque provenance for reporting only -- it documents
    an existing producer (e.g. a named phase of another repo's
    ``/coordinator:repo-setup`` skill), it never dispatches, invokes,
    imports, or shells out to whatever it names, and its value is never
    validated to exist. Doing either would make this module a second
    onboarding driver competing with that producer -- a hard boundary from
    DR-116, not a design choice this module gets to relax.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

# `resolution_journal` is imported lazily (call-time, inside
# scaffold_canonical_structure) rather than at module level:
# resolution_journal.py itself imports uninstall_legs.py, which imports
# `coordinator_core.ops` -- and coordinator_core.ops eagerly imports every
# op module at import time, including bootstrap_repo.py, which imports
# THIS module (ScaffoldError et al.). A module-level import here closes
# that cycle while scaffold_structure is still mid-init, the same
# deferred-import pattern resolution_journal.py itself already uses for
# its own back-import of substrate.py.

_MANIFEST_FILENAME = "canonical-structure.yaml"
_ENTRY_KEY_INDENT = 4  # '    creation:', '    readme:', etc.
_CONTENT_INDENT = _ENTRY_KEY_INDENT + 2  # block-scalar content indent (yaml: indicator + 1)
_GITKEEP_FILENAME = ".gitkeep"
_README_FILENAME = "README.md"

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="scaffold-structure",
    source_module="coordinator_core.install.scaffold_structure",
    clauses=(
        ShapedClause(
            discovered_by="parse_manifest (directory entries, _scaffold_dir_entry)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<manifest-declared-dir>/",
            ),
        ),
        ShapedClause(
            discovered_by="parse_manifest (gitkeep: true directory entries, _scaffold_dir_entry)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<manifest-declared-dir>/{_GITKEEP_FILENAME}",
            ),
        ),
        ShapedClause(
            # Review: coordinator:code-reviewer — prose named only half the
            # real gate (`if readme_text and not entry.gitkeep:`); a
            # gitkeep: true entry never gets a README written even if
            # readme-bearing.
            discovered_by="parse_manifest (readme-bearing, non-gitkeep directory entries, _scaffold_dir_entry)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<manifest-declared-dir>/{_README_FILENAME}",
            ),
        ),
        ShapedClause(
            discovered_by="parse_manifest (template-backed file entries, _scaffold_file_entry)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<manifest-declared-path>",
            ),
        ),
    ),
)
"""This writer's surface is entirely manifest-shaped: which directories and
files get scaffolded depends on what ``canonical-structure.yaml`` declares
at install time (via ``parse_manifest``), not on anything enumerable in this
module's source. Four distinct sub-clauses, not one -- a single manifest
directory entry with ``gitkeep: true`` and a ``readme:`` value produces
THREE distinct on-disk artifacts (the directory itself, ``.gitkeep``,
``README.md``), each with its own removal story on uninstall, plus the
separate template-copy path for file entries (``_scaffold_file_entry``),
driven by a different manifest entry shape (``template:`` rather than
``gitkeep:``/``readme:``). Collapsing these into "the directory" would lose
that distinction. ``.gitkeep``/``README.md`` filenames are read from the
module-level ``_GITKEEP_FILENAME``/``_README_FILENAME`` constants shared
with ``_scaffold_dir_entry`` below, not restated here.
"""


class ScaffoldError(RuntimeError):
    """Usage/prereq, manifest-locate/parse, or live template-missing failure."""


@dataclass
class ScaffoldEntry:
    path: str
    creation: str = ""
    gitkeep: bool = False
    readme: Optional[str] = None
    template: Optional[str] = None
    produced_by: Optional[str] = None

    @property
    def is_dir(self) -> bool:
        return self.path.endswith("/")

    @property
    def is_file(self) -> bool:
        return not self.is_dir and bool(self.template)


@dataclass
class ScaffoldResult:
    created_dirs: int = 0
    created_readmes: int = 0
    created_gitkeeps: int = 0
    created_files: int = 0
    skipped: int = 0
    lines: List[str] = field(default_factory=list)
    dropped_entries: List[Tuple[str, str]] = field(default_factory=list)
    satisfied_elsewhere: List[Tuple[str, str]] = field(default_factory=list)

    def would_create_count(self) -> int:
        """Entries actually created (live) or that would be created (dry-run).

        Excludes skipped-exists entries, missing-declared-template
        warn-continues (AC D2 would-create parity — F7), ``dropped_entries``
        (a manifest/parser disagreement, never a creation candidate in the
        first place), and ``satisfied_elsewhere`` (DR-116: declared
        ``produced_by:``, created by another repo's flow, never a creation
        candidate here either).
        """
        return self.created_dirs + self.created_readmes + self.created_gitkeeps + self.created_files


def locate_manifest(manifest_root: Path) -> Path:
    """Resolve the example-doctrine-repo-owned canonical-structure.yaml under ``manifest_root``.

    Does NOT vendor a copy (AC D5) -- raises ScaffoldError if absent.
    """
    manifest = Path(manifest_root) / _MANIFEST_FILENAME
    if not manifest.is_file():
        raise ScaffoldError(f"manifest not found at: {manifest}")
    return manifest


@dataclass
class ManifestParseResult:
    entries: List[ScaffoldEntry] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)
    satisfied_elsewhere: List[Tuple[str, str]] = field(default_factory=list)


def parse_manifest(text: str) -> ManifestParseResult:
    """Reimplements the bash oracle's inline minimal-YAML entry parser.

    An entry starts with "  - path: <value>" and ends at the next
    "  - path:" or EOF. Fields: path, creation, gitkeep, readme (inline or
    block scalar), template, produced_by. ``ManifestParseResult.entries``
    holds only ``creation: eager`` entries that are either a directory (path
    ends with "/") or a template-backed file.

    Three outcomes for a ``creation: eager`` entry (DR-116 --
    ``docs/decisions/DR-116-creation-eager-declares-lifecycle-not-agency.md``,
    example-doctrine-repo repo -- ``creation:`` alone conflates lifecycle, "when must
    this exist," with agency, "who creates it"; ``produced_by:`` names the
    agency half):

      1. Creatable here -- a directory, or template-backed (``template:``
         non-empty). Included in ``entries``. **Precedence**: an entry with
         BOTH ``template:`` and ``produced_by:`` is treated as case 1, not
         case 2 -- template-backed creation is the concrete capability this
         module owns and can execute; ``produced_by:`` alone is a claim
         about another repo's flow it cannot verify. A template value wins.
         The same precedence applies to a directory entry (``path`` ending
         in ``/``) that also carries a non-empty ``produced_by:`` -- being a
         directory is checked first, so it is treated as case 1 and
         ``produced_by:`` is discarded, identically to the template-wins
         rule above (Review: code-reviewer -- pinned by
         test_parse_manifest_directory_entry_with_produced_by_dir_wins).
      2. Satisfied-elsewhere -- not a directory, no ``template:``, but a
         non-empty ``produced_by:``. Not created, not dropped -- reported
         via ``ManifestParseResult.satisfied_elsewhere`` as a
         ``(path, producer)`` pair. An empty-string or null ``produced_by:``
         (``""``, ``null``, ``~``) is treated as ABSENT, same convention as
         ``template:``/``readme:``, not as case 2.
      3. Genuine orphan -- neither a directory, ``template:``, nor
         ``produced_by:``. A manifest/parser disagreement -- dropped from
         ``entries`` but reported in ``ManifestParseResult.dropped`` as a
         ``(path, reason)`` pair, never silently discarded (this is the
         case ``d6fa361d`` was built for; its reporting is unchanged here).

    A non-eager entry (e.g. ``creation: lazy``) is never a creation
    candidate regardless of ``template:``/``produced_by:`` -- excluded from
    ``entries``, ``dropped``, AND ``satisfied_elsewhere`` alike, since
    reporting it anywhere would be noise, not signal.
    """
    lines = text.splitlines()
    entries: List[dict] = []
    current: Optional[dict] = None
    in_readme_block = False
    readme_lines: List[str] = []

    def flush() -> None:
        nonlocal current, readme_lines
        if current is None:
            return
        if in_readme_block or readme_lines:
            current["readme"] = "\n".join(readme_lines)
        entries.append(current)

    for line in lines:
        stripped = line.rstrip().rstrip("\r")

        if in_readme_block:
            if stripped == "":
                readme_lines.append("")
                continue

            leading_spaces = len(line) - len(line.lstrip())
            is_comment = stripped.lstrip().startswith("#")

            term_new_entry = re.match(r"^  - path:", stripped)
            term_toplevel = leading_spaces == 0 and not is_comment
            term_entry_field = leading_spaces == _ENTRY_KEY_INDENT and not stripped.lstrip().startswith("#")
            term_comment_bleed = is_comment and leading_spaces <= _ENTRY_KEY_INDENT

            if term_new_entry or term_toplevel or term_entry_field or term_comment_bleed:
                in_readme_block = False
                while readme_lines and readme_lines[-1] == "":
                    readme_lines.pop()
                if term_new_entry:
                    flush()
                    readme_lines = []
                    path_val = stripped.split("path:", 1)[1].strip()
                    current = {
                        "path": path_val, "creation": "", "schema": None,
                        "gitkeep": False, "readme": None, "template": None,
                        "produced_by": None,
                    }
                    continue
                elif term_toplevel:
                    flush()
                    current = None
                    readme_lines = []
                    continue
                # else: term_entry_field or term_comment_bleed -- fall through
                # to normal field parsing below.
            else:
                if len(line) > _CONTENT_INDENT and line[:_CONTENT_INDENT] == " " * _CONTENT_INDENT:
                    content = line[_CONTENT_INDENT:].rstrip()
                elif len(line) >= leading_spaces:
                    content = stripped.lstrip()
                else:
                    content = stripped
                readme_lines.append(content)
                continue

        m_entry = re.match(r"^  - path:\s*(.+)", stripped)
        if m_entry:
            flush()
            readme_lines = []
            in_readme_block = False
            path_val = m_entry.group(1).strip()
            current = {
                "path": path_val, "creation": "", "schema": None,
                "gitkeep": False, "readme": None, "template": None,
                "produced_by": None,
            }
            continue

        if current is None:
            continue

        m_creation = re.match(r"^    creation:\s*(.+)", stripped)
        if m_creation:
            current["creation"] = m_creation.group(1).strip()
            continue

        m_schema = re.match(r"^    schema:\s*(.+)", stripped)
        if m_schema:
            val = m_schema.group(1).strip()
            current["schema"] = None if val in ("null", "~", "") else val
            continue

        m_gitkeep = re.match(r"^    gitkeep:\s*(.+)", stripped)
        if m_gitkeep:
            val = m_gitkeep.group(1).strip().lower()
            current["gitkeep"] = val == "true"
            continue

        m_readme_block = re.match(r"^    readme:\s*[>|]$", stripped)
        if m_readme_block:
            in_readme_block = True
            readme_lines = []
            continue

        m_readme_inline = re.match(r"^    readme:\s*(.+)", stripped)
        if m_readme_inline:
            val = m_readme_inline.group(1).strip()
            current["readme"] = None if val in ("null", "~") else val
            continue

        m_template = re.match(r"^    template:\s*(.+)", stripped)
        if m_template:
            val = m_template.group(1).strip()
            current["template"] = None if val in ("null", "~") else val
            continue

        m_produced_by = re.match(r"^    produced_by:\s*(.+)", stripped)
        if m_produced_by:
            val = m_produced_by.group(1).strip()
            current["produced_by"] = None if val in ("null", "~", "") else val
            continue

    flush()

    result: List[ScaffoldEntry] = []
    dropped: List[Tuple[str, str]] = []
    satisfied_elsewhere: List[Tuple[str, str]] = []
    for raw in entries:
        entry = ScaffoldEntry(
            path=raw.get("path", ""),
            creation=raw.get("creation", ""),
            gitkeep=raw.get("gitkeep", False),
            readme=raw.get("readme"),
            template=raw.get("template"),
            produced_by=raw.get("produced_by"),
        )
        if entry.creation != "eager":
            continue
        if entry.is_dir or entry.is_file:
            # Template-backed (or dir) creation wins over produced_by -- see
            # parse_manifest's docstring precedence note.
            result.append(entry)
            continue
        if entry.produced_by:
            satisfied_elsewhere.append((entry.path, entry.produced_by))
            continue
        dropped.append((
            entry.path,
            "creation: eager but neither a directory (no trailing '/') nor "
            "template-backed (no template: value) -- manifest/parser "
            "disagreement, not created",
        ))
    return ManifestParseResult(entries=result, dropped=dropped, satisfied_elsewhere=satisfied_elsewhere)


def _dir_has_content(dir_abs: Path) -> bool:
    if not dir_abs.is_dir():
        return False
    for child in dir_abs.iterdir():
        name = child.name
        if not name.startswith("."):
            return True
        if name in (".", "..", _GITKEEP_FILENAME):
            continue
        return True
    return False


def _scaffold_dir_entry(
    entry: ScaffoldEntry,
    root_path: Path,
    dry_run: bool,
    result: ScaffoldResult,
    journal_entries: dict,
) -> None:
    dir_rel = entry.path.rstrip("/")
    dir_abs = root_path / dir_rel
    readme_abs = dir_abs / _README_FILENAME
    gitkeep_abs = dir_abs / _GITKEEP_FILENAME
    readme_text = entry.readme or ""

    if not dir_abs.is_dir():
        if dry_run:
            result.lines.append(f"[dry-run] would create dir:    {dir_rel}/")
            result.created_dirs += 1
        else:
            dir_abs.mkdir(parents=True, exist_ok=True)
            result.lines.append(f"created dir:    {dir_rel}/")
            result.created_dirs += 1
            journal_entries[0].append(WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/"))
    else:
        if dry_run:
            result.lines.append(f"[dry-run] skip (exists) dir:   {dir_rel}/")
        else:
            # Real machine state: this writer's directory clause resolved
            # to an already-present dir just as much as a freshly created
            # one -- the concrete on-disk fact is identical either way.
            journal_entries[0].append(WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/"))
        result.skipped += 1

    if entry.gitkeep:
        if not gitkeep_abs.is_file():
            if not _dir_has_content(dir_abs):
                if dry_run:
                    result.lines.append(f"[dry-run] would create .gitkeep: {dir_rel}/.gitkeep")
                    result.created_gitkeeps += 1
                else:
                    dir_abs.mkdir(parents=True, exist_ok=True)
                    gitkeep_abs.touch()
                    result.lines.append(f"created .gitkeep: {dir_rel}/.gitkeep")
                    result.created_gitkeeps += 1
                    journal_entries[1].append(
                        WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/{_GITKEEP_FILENAME}")
                    )
            else:
                if dry_run:
                    result.lines.append(f"[dry-run] skip .gitkeep (dir has content): {dir_rel}/")
                result.skipped += 1
                # Dir has content -- the .gitkeep was never written, this
                # run or any prior one; nothing to journal for this entry.
        else:
            if dry_run:
                result.lines.append(f"[dry-run] skip (exists) .gitkeep: {dir_rel}/.gitkeep")
            else:
                journal_entries[1].append(
                    WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/{_GITKEEP_FILENAME}")
                )
            result.skipped += 1

    if readme_text and not entry.gitkeep:
        if not readme_abs.is_file():
            if dry_run:
                result.lines.append(f"[dry-run] would create README: {dir_rel}/README.md")
                result.created_readmes += 1
            else:
                dir_abs.mkdir(parents=True, exist_ok=True)
                content = f"# {dir_rel}\n\n{readme_text}\n"
                readme_abs.write_text(content, encoding="utf-8")
                result.lines.append(f"created README: {dir_rel}/README.md")
                result.created_readmes += 1
                journal_entries[2].append(
                    WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/{_README_FILENAME}")
                )
        else:
            if dry_run:
                result.lines.append(f"[dry-run] skip (exists) README: {dir_rel}/README.md")
            else:
                journal_entries[2].append(
                    WriteSurfaceEntry(kind="file-path", path=f"{dir_rel}/{_README_FILENAME}")
                )
            result.skipped += 1


def _scaffold_file_entry(
    entry: ScaffoldEntry,
    root_path: Path,
    manifest_dir: Path,
    dry_run: bool,
    result: ScaffoldResult,
    journal_entries: dict,
) -> None:
    target = root_path / entry.path
    template_abs = manifest_dir / (entry.template or "")

    if not template_abs.is_file():
        if dry_run:
            result.lines.append(
                f"[dry-run] ERROR: declared template not found: {template_abs} (would exit 2 on live run)"
            )
            return
        raise ScaffoldError(f"declared template not found: {template_abs}")

    if target.is_file():
        if dry_run:
            result.lines.append(f"[dry-run] skip (exists) file:  {entry.path}")
        else:
            journal_entries[3].append(WriteSurfaceEntry(kind="file-path", path=entry.path))
        result.skipped += 1
    else:
        if dry_run:
            result.lines.append(f"[dry-run] would create file:   {entry.path}")
            result.created_files += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template_abs, target)
            result.lines.append(f"created file:   {entry.path}")
            result.created_files += 1
            journal_entries[3].append(WriteSurfaceEntry(kind="file-path", path=entry.path))


def scaffold_canonical_structure(
    root: "str | Path", manifest_root: "str | Path", *, dry_run: bool = False
) -> ScaffoldResult:
    """Parse canonical-structure.yaml under ``manifest_root`` and scaffold ``root``.

    ``dry_run=True`` mutates nothing (AC D2/anti-scope); ``dry_run=False`` is
    the live mode. Raises ``ScaffoldError`` on usage/prereq, manifest-locate/
    parse, or (live-only) missing-declared-template failures.
    """
    root_str = str(root).replace("\\", "/")  # AC D6: Windows Git-Bash/MSYS backslash normalization
    root_path = Path(root_str)
    if not root_path.is_dir():
        raise ScaffoldError(f"root dir does not exist: {root_path}")

    manifest = locate_manifest(Path(manifest_root))
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScaffoldError(f"cannot read manifest: {exc}") from exc

    parse_result = parse_manifest(text)
    entries = parse_result.entries
    result = ScaffoldResult()
    result.dropped_entries = list(parse_result.dropped)
    result.satisfied_elsewhere = list(parse_result.satisfied_elsewhere)

    if dry_run:
        result.lines.append(f"[dry-run] target root: {root_path}")
        result.lines.append(f"[dry-run] manifest:    {manifest}")

    for path, reason in result.dropped_entries:
        result.lines.append(f"dropped (declared eager, not created): {path} — {reason}")

    for path, producer in result.satisfied_elsewhere:
        result.lines.append(
            f"satisfied elsewhere (declared eager, not created here): {path} — produced by: {producer}"
        )

    # Per-clause resolved entries for this run, keyed by WRITE_SURFACE
    # clause index (0=dirs, 1=gitkeeps, 2=readmes, 3=files) -- accumulated
    # across every manifest entry, then journaled once per clause below.
    # Only populated in live mode: a dry-run performs no writes, so there
    # is nothing this module actually resolved to journal (see
    # resolution_journal.record_resolution's "journal what happened, not
    # what was declared" contract).
    journal_entries: dict = {0: [], 1: [], 2: [], 3: []}

    for entry in entries:
        if entry.is_dir:
            _scaffold_dir_entry(entry, root_path, dry_run, result, journal_entries)
        else:
            _scaffold_file_entry(entry, root_path, manifest.parent, dry_run, result, journal_entries)

    if not dry_run:
        from coordinator_core.install import resolution_journal

        for clause_index, resolved in journal_entries.items():
            resolution_journal.record_resolution("scaffold-structure", clause_index, resolved)

    if dry_run:
        result.lines.append("[dry-run] done — no files were created")
    else:
        result.lines.append(
            f"scaffold complete: {result.created_dirs} dir(s) created, "
            f"{result.created_readmes} README(s) created, {result.created_gitkeeps} .gitkeep(s) created, "
            f"{result.created_files} file(s) created, {result.skipped} already-present item(s) skipped, "
            f"{len(result.dropped_entries)} declared-eager entries dropped (manifest/parser disagreement), "
            f"{len(result.satisfied_elsewhere)} declared-eager entries satisfied elsewhere (produced_by)"
        )

    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scaffold-structure")
    parser.add_argument("--root")
    parser.add_argument("--manifest-root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = args.root
    if not root:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"--root not given and not inside a git repo: {exc}", file=sys.stderr)
            return 1
        root = proc.stdout.strip()

    manifest_root = args.manifest_root or root

    try:
        result = scaffold_canonical_structure(root, manifest_root, dry_run=args.dry_run)
    except ScaffoldError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for line in result.lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
