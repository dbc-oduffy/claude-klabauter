"""Apply the repo-setup template's runtime ignore set to a repo.

`apply_runtime_ignore` appends the template rules the root `.gitignore` does not cover and
untracks (`git rm --cached`, index only) the tracked runtime files they match. Paths under
`UNTRACK_EXEMPT_PREFIXES` (memo-outbox) are never untracked. Never commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coordinator_core.git.run import run_git
from coordinator_core.workday_start.runtime_ignore_set import (
    load_template_rules,
    resolve_template_path,
    runtime_ignore_findings,
)

HEADER = "# coordinator — runtime ignore set (applied from repo-setup template)"


@dataclass(frozen=True)
class ApplyResult:
    appended: tuple[str, ...]
    untracked: tuple[str, ...]
    skipped: str | None = None


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

    findings = runtime_ignore_findings(root, load_template_rules(template))
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
