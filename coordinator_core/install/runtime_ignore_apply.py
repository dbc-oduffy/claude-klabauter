"""Apply the repo-setup template's runtime ignore set to a repo.

`apply_runtime_ignore` appends the template rules the root `.gitignore` does not cover and
untracks (`git rm --cached`, index only) the tracked runtime files they match. Paths under
`UNTRACK_EXEMPT_PREFIXES` (memo-outbox) are never untracked. `UNREAL_ONLY_RULES` apply only to a
repo root carrying a `.uproject`/`.uplugin`. Never commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coordinator_core.git.run import run_git
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.workday_start.runtime_ignore_set import (
    load_template_rules,
    resolve_template_path,
    runtime_ignore_findings,
)

HEADER = "# coordinator — runtime ignore set (applied from repo-setup template)"

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="runtime-ignore-apply",
    source_module="coordinator_core.install.runtime_ignore_apply",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<repo-root>/.gitignore",
                    reason="_append_rules appends the template rules the root .gitignore lacks, under HEADER",
                ),
            ),
        ),
        ShapedClause(
            effect="delete",
            discovered_by="runtime_ignore_findings (tracked files matching the template rules, git rm --cached)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<repo-root>/<tracked-runtime-path>",
                effect="delete",
                reason="index entry only (--cached); the working-tree file is kept and nothing is committed",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class ApplyResult:
    appended: tuple[str, ...]
    untracked: tuple[str, ...]
    skipped: str | None = None


UNREAL_ONLY_RULES = frozenset({"Intermediate/"})
_UNREAL_SUFFIXES = frozenset({".uproject", ".uplugin"})


def _is_unreal_repo(root: Path) -> bool:
    try:
        return any(p.suffix.lower() in _UNREAL_SUFFIXES for p in root.iterdir())
    except OSError:
        return False


def _detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _append_rules(root: Path, rules: tuple[str, ...]) -> None:
    target = root / ".gitignore"
    try:
        text = target.read_bytes().decode("utf-8", errors="surrogateescape")
    except FileNotFoundError:
        text = ""
    eol = _detect_eol(text)
    if text and not text.endswith(("\n", "\r")):
        text += eol
    if text.strip():
        text += eol
    text += eol.join([HEADER, *rules]) + eol
    target.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def apply_runtime_ignore(repo_root: Path, template: Path | None = None) -> ApplyResult:
    """Raises `IndexParseError` on an unreadable index and `RuntimeError` on a failed untrack."""
    root = Path(repo_root)
    template = template or resolve_template_path()
    if template is None:
        return ApplyResult((), (), "template not found")

    rules = load_template_rules(template)
    if not _is_unreal_repo(root):
        rules = tuple(r for r in rules if r not in UNREAL_ONLY_RULES)
    findings = runtime_ignore_findings(root, rules)
    if findings.missing:
        _append_rules(root, findings.missing)
    if findings.tracked:
        payload = ("\n".join(findings.tracked) + "\n").encode("utf-8")
        done = run_git(
            ["--literal-pathspecs", "rm", "--cached", "--force", "--quiet", "--pathspec-from-file=-"],
            cwd=str(root),
            input=payload,
        )
        if done.returncode != 0:
            raise RuntimeError(f"git rm --cached failed ({done.returncode}): {done.stderr.strip()}")
    return ApplyResult(findings.missing, findings.tracked)
