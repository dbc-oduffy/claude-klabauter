"""In-process check that the DoE repo-setup template's runtime ignore set is ignored and untracked.

`watch_state_ignore(repo_root) -> (exit_code, text)`. The checked set is the template's rule
lines (`runtime_ignore_set.load_template_rules`), never a copy. Report-only: writes no
`.gitignore`, touches no index. Calling repo only. Tracked-ness is index membership and wins
over ignore coverage; `.coordinator-local/memo-outbox/` paths are exempt. Empty text means
healthy.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.git.git_state import IndexParseError
from coordinator_core.workday_start import runtime_ignore_set as _set


def watch_state_ignore(repo_root: Path) -> tuple[int, str]:
    template = _set.resolve_template_path()
    if template is None:
        return 0, f"runtime-ignore: template not found ({_set.TEMPLATE_REL.as_posix()} under the plugin root)"
    try:
        findings = _set.runtime_ignore_findings(Path(repo_root), _set.load_template_rules(template))
    except IndexParseError:
        return 0, "runtime-ignore: tracked-ness could not be determined (index unreadable)"
    lines = [f"{p}: tracked (fix: git rm --cached {p})" for p in findings.tracked]
    lines.extend(f"{r}: not ignored (fix: add to .gitignore)" for r in findings.missing)
    return 0, "\n".join(lines)
