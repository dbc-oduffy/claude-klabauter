"""
coordinator_core.ops.run_shellcheck_sweep — JSON-RPC "ci.run_shellcheck_sweep"
operation.

Purpose: lint every git-tracked `*.sh` file in a worktree with `shellcheck`,
replacing the `workweek-complete` fence's inline pipeline (`git ls-files
'*.sh' | while read f; do tr -d '\\r' <"$f" | shellcheck -f gcc -s bash;
done` plus a `sed` prefix-rewrite — see `commands/workweek-complete.md:737`,
`state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv` row
`run-shellcheck-sweep`). Spawning the `shellcheck` binary itself is this
op's entire purpose (list-argv, `shell=False`) and is the audited external
tool, not a bash/coreutils bypass — the naked-Python mandate's bash/coreutils
gate forbids shells and coreutils, not the subject tool under lint.

Op-key / contract (frozen — see
`state/audits/2026-07-22-command-payload-inventory/op-classification.tsv`,
row `run-shellcheck-sweep`):
    ci.run_shellcheck_sweep
    params:   {repo_root: str | None}
    response: {findings: list[dict], files_checked: int}

`findings` items: {file (str, path relative to repo_root, POSIX-style),
line (int | None), column (int | None), level (str | None — shellcheck's
"error"/"warning"/"info"/"style"), code (str | None — e.g. "SC2086"),
message (str | None)} — shellcheck's own `-f json` field names, with `file`
rewritten from the temp-copy path back to the tracked repo-relative path
(the port of the fence's trailing `sed` prefix-rewrite step).

Scope-verdict `show_top` (per the oracle): `git ls-files '*.sh'` is scoped
to the CALLING worktree's own tracked files, never a shared `common_dir`
key — a repo with multiple linked worktrees never gets one worktree's
findings misapplied to another.

CRLF normalization: the fence's `tr -d '\\r'` step exists so a Windows
checkout with CRLF line endings doesn't spuriously trip shellcheck's own
CRLF detector (SC1017) on every line. Ported natively as `str.replace("\\r",
"")` on the file's in-memory content before handing a normalized COPY to
shellcheck (via a temp file) — the tracked file on disk is never mutated.

Negative-spec:
    - Does NOT shell out to `tr`, `sed`, `bash`, or any shell — CRLF
      normalization is a Python string op; the `sed` prefix-rewrite is a
      dict-field reassignment on parsed JSON.
    - Does NOT mutate the tracked `.sh` file being linted — normalization
      writes to a throwaway temp file; the checked-out original is
      untouched.
    - Does NOT run a mutating git command — `git ls-files` only.
    - Does NOT treat "shellcheck missing from PATH" as "zero findings" —
      raises RuntimeError naming the remediation, so an unconfigured
      machine fails loud instead of silently reporting a clean sweep.

Idempotency (AC7): manifest row rates idempotency-hazard "none" — a lint
sweep is read-only (tracked-file listing + read-only shellcheck subprocess
per file); rerunning against identical repo state reproduces the same
`findings`/`files_checked` byte-for-byte. Platform-hazard is rated "low"
(the `tr -d '\\r'` CRLF step is the one Windows-shaped concern, closed by
the native `str.replace` above); neither hazard is medium/high, so no DEC-7
docstring block is mandated, but the reasoning is recorded here per the
plan's own "honest note, not asserted pass" standard.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op

_GIT_TIMEOUT_SECONDS = 15
_SHELLCHECK_TIMEOUT_SECONDS = 30
_SHELLCHECK_DIALECT = "bash"

_SHELLCHECK_NOT_FOUND_MESSAGE = (
    "ci.run_shellcheck_sweep: 'shellcheck' binary not found on PATH — "
    "install it (e.g. `brew install shellcheck` on macOS, "
    "`choco install shellcheck` on Windows, or your Linux distro's "
    "package manager) and re-run."
)


def _run_git(repo_root: Path, args: List[str]) -> Optional[str]:
    """Run a read-only `git` subprocess in `repo_root`; return stdout on
    success, None on any failure (not a git repo, `git` missing, timeout).
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(
            f"skip: _run_git: proc = subprocess.run(...) failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def tracked_shell_files(repo_root: Path) -> List[str]:
    """List every git-tracked `*.sh` path under `repo_root` (repo-relative,
    forward-slash, sorted ascending). Empty when `repo_root` is not a git
    worktree or tracks no `.sh` files.
    """
    stdout = _run_git(repo_root, ["ls-files", "--", "*.sh"])
    if stdout is None:
        return []
    return sorted({path for path in stdout.splitlines() if path})


def _lint_one_file(repo_root: Path, rel_path: str) -> List[dict]:
    """Lint a single tracked `.sh` file: read its content, CRLF-normalize a
    throwaway copy, run `shellcheck -f json` against the copy, and rewrite
    each finding's `file` field back to `rel_path` (repo-relative).

    Returns an empty list when the file cannot be read, or when shellcheck
    finds nothing / emits no parseable JSON. Raises RuntimeError when the
    `shellcheck` binary itself is not on PATH (a systemic setup problem,
    not a per-file finding).
    """
    abs_path = repo_root / rel_path
    try:
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    normalized = raw.replace("\r", "")

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(normalized)
            tmp_path = Path(tmp.name)

        try:
            proc = subprocess.run(
                ["shellcheck", "-s", _SHELLCHECK_DIALECT, "-f", "json", str(tmp_path)],
                capture_output=True,
                text=True,
                timeout=_SHELLCHECK_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_SHELLCHECK_NOT_FOUND_MESSAGE) from exc
        except subprocess.TimeoutExpired:
            return []
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []
    try:
        raw_findings = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_findings, list):
        return []

    findings: List[dict] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "file": rel_path,
                "line": item.get("line"),
                "column": item.get("column"),
                "level": item.get("level"),
                "code": item.get("code"),
                "message": item.get("message"),
            }
        )
    return findings


def run_shellcheck_sweep(repo_root: Path) -> dict:
    """Lint every git-tracked `.sh` file under `repo_root`. See module
    docstring for the full contract.
    """
    files = tracked_shell_files(repo_root)
    findings: List[dict] = []
    for rel_path in files:
        findings.extend(_lint_one_file(repo_root, rel_path))
    return {"findings": findings, "files_checked": len(files)}


@register_op("ci.run_shellcheck_sweep")
def _run_shellcheck_sweep(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ci.run_shellcheck_sweep" handler (sync — offloaded to a
    worker thread by dispatch, so the blocking subprocess.run calls here
    never park the event loop).

    `params["repo_root"]` (contractual, optional per the frozen contract)
    takes priority when caller-supplied; falls back to the dispatch-supplied
    `repo_root` kwarg (scope-verdict `show_top` — the calling worktree),
    then `Path.cwd()` for the standalone `python3 -m` invocation path.
    """
    param_repo_root = params.get("repo_root")
    if param_repo_root:
        root = Path(param_repo_root)
    elif repo_root is not None:
        root = Path(repo_root)
    else:
        root = Path.cwd()

    return run_shellcheck_sweep(root)
