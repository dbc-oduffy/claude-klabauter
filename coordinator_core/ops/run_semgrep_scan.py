"""
coordinator_core.ops.run_semgrep_scan — JSON-RPC "ci.run_semgrep_scan" operation.

Purpose: replaces the example-doctrine-repo security-audit-worker fence (agents/security-audit-worker.md)
that shelled out to `semgrep --config=auto --json` over a diff scope. This is the
Tier-1 (preferred) scanner in that fence's documented severity-mapping / fallback-tier
strategy: run semgrep over the files a branch actually changed (relative to
``diff_base``), summarize findings by severity, and report which tier actually ran so
a caller can distinguish "clean scan" from "scanner unavailable" from "nothing changed."

Diff-scoping, not whole-tree scanning: the fence's whole point was to bound the scan
to what the branch touched, not the entire repo — a full-tree semgrep run on every
CI/review invocation would be both slower and noisier (flags pre-existing findings in
untouched code as if they were new). ``git diff --name-only`` against ``diff_base``
supplies that scope.

External-tool wrapper (plan-sanctioned): spawning the `semgrep`/`git` binaries directly
via list-argv subprocess (shell=False) IS this op's purpose, not a bash/coreutils
violation — the naked-Python gate forbids shells and coreutils, not the audited tool
itself (docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 2
chunk design note).

Contract (op-classification.tsv row run-semgrep-scan, op-key ci.run_semgrep_scan):
    params: {diff_base: str, config: str}
        diff_base — required; the git ref/commit semgrep's scan scope is diffed
                    against (e.g. "main", a merge-base SHA). Empty/missing is a
                    structured ValueError — there is no whole-tree default.
        config    — optional; forwarded to `semgrep --config`. Defaults to "auto"
                    (the fence's documented Tier-1 invocation).
    -> {findings: list[dict], tier_used: str, severity_counts: dict}
        findings        — one dict per semgrep result: check_id, path, start_line,
                          end_line, severity, message. Empty list on empty diff scope
                          or scanner-unavailable fallback.
        tier_used       — "semgrep" (tier 1 ran), "empty_scope" (diff scope had no
                          files — semgrep was never invoked, not merely empty output),
                          or "unavailable" (semgrep not on PATH — fallback fired).
        severity_counts — {severity_string: count}, derived from findings. {} when
                          findings is empty.

Fallback-tier strategy (oracle: "fallback-tier logic ... is part of the same op and
should live in the same script" — kept here, not split out): the oracle's
platform-hazard note requires this branch on a native `shutil.which` check, never a
shelled `command -v`. The oracle's own body does not name a concrete Tier-2 scanner,
and this op does not invent one — inventing an unaudited secondary linter to satisfy a
tier the source fence never specified would be a scope guess, not a port. The honest,
minimal fallback is: report `tier_used="unavailable"` with empty findings, so a caller
can distinguish "clean scan" from "no scan ran" rather than silently returning a clean
result. If/when a concrete Tier-2 tool is named, it is an additive change to this same
module (module-owned, per the oracle), not a new op.

Idempotency (AC7): read-only — an identical (diff_base, config) pair against an
unchanged working tree re-derives the same diff scope and the same semgrep findings.
No disk state is written. idempotency-hazard is rated "none" in op-classification.tsv,
so no DEC-7 docstring block is required (that block is reserved for medium/high rows).

Self-registration: importing this module calls register_op("ci.run_semgrep_scan", ...)
as a side-effect. Registration across the other three surfaces (_EAGER_OP_MODULES /
_OP_KEY_SCOPE / _registry_map.py) lands in the separate EM-serial registration pass.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 2
Port source: example-doctrine-repo agents/security-audit-worker.md:53 (bash fence)

Negative-spec:
    - Does NOT scan the whole tree — always diff-scoped against `diff_base`.
    - Does NOT invent a Tier-2 scanner the source fence never named (see fallback
      strategy above) — reports "unavailable" rather than fabricating coverage.
    - Does NOT shell to `command -v` / any shell builtin to detect semgrep — uses
      `shutil.which`, a native Python check (platform-hazard note in the manifest).
    - Does NOT treat a git-diff resolution failure (bad `diff_base`) the same as an
      empty scope — an unresolvable ref is a structured ValueError, not a clean skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DEFAULT_CONFIG = "auto"
_STDERR_LOG_TAIL = 2000

# Review: code-reviewer — neither the git-diff scoping call nor the semgrep
# scan itself carried a timeout; a stuck semgrep run or an unresponsive git
# invocation wedged this op's worker thread forever. Two separate constants
# since semgrep (an externally-invoked scanner over caller-supplied files)
# can legitimately run far longer than a local `git diff`.
_GIT_TIMEOUT_SECONDS = 30
_SEMGREP_TIMEOUT_SECONDS = 300


def _diff_scoped_files(repo_root: Path, diff_base: str) -> List[str]:
    """Return the repo-relative paths `git diff --name-only` reports as changed
    (added/copied/modified/renamed — deletions excluded, there is nothing on disk
    for semgrep to scan) between the working tree and *diff_base*.

    Raises ValueError with git's own stderr when *diff_base* does not resolve
    (unknown ref, corrupt repo) — an unresolvable scope is a caller error, not a
    silent empty-scope result.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_base],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ci.run_semgrep_scan: 'git diff --name-only {diff_base}' timed "
            f"out after {_GIT_TIMEOUT_SECONDS}s in {repo_root}"
        ) from exc
    if proc.returncode != 0:
        raise ValueError(
            f"ci.run_semgrep_scan: 'git diff --name-only {diff_base}' failed "
            f"(exit {proc.returncode}) in {repo_root} — is diff_base a valid "
            f"ref/commit? stderr: {(proc.stderr or '').strip()[-_STDERR_LOG_TAIL:]}"
        )
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    # Defensive: a rename/checkout race can list a path that no longer exists on
    # disk by the time this runs — semgrep would error on a missing target.
    return [name for name in names if (repo_root / name).exists()]


def _severity_counts(findings: List[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for finding in findings:
        severity = finding.get("severity") or "UNKNOWN"
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _parse_semgrep_json(stdout: str) -> List[dict]:
    """Map raw `semgrep --json` output to this op's flat finding-dict shape."""
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ci.run_semgrep_scan: semgrep --json produced unparseable output: {exc}"
        ) from exc

    findings: List[dict] = []
    for result in payload.get("results", []):
        extra = result.get("extra") or {}
        start = result.get("start") or {}
        end = result.get("end") or {}
        findings.append(
            {
                "check_id": result.get("check_id", ""),
                "path": result.get("path", ""),
                "start_line": start.get("line"),
                "end_line": end.get("line"),
                "severity": extra.get("severity", "UNKNOWN"),
                "message": extra.get("message", ""),
            }
        )
    return findings


def _run_semgrep(repo_root: Path, config: str, files: List[str]) -> List[dict]:
    """Invoke `semgrep --config=<config> --json <files>` and return parsed findings.

    Semgrep's own exit-code contract: 0 = clean scan, 1 = findings reported (both
    are successful scans, not errors); any other code is a genuine scanner error
    (bad config, internal crash) and is raised loud rather than swallowed.
    """
    try:
        proc = subprocess.run(
            ["semgrep", f"--config={config}", "--json", *files],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_SEMGREP_TIMEOUT_SECONDS,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ci.run_semgrep_scan: semgrep timed out after "
            f"{_SEMGREP_TIMEOUT_SECONDS}s (config={config!r})"
        ) from exc
    if proc.returncode not in (0, 1):
        raise ValueError(
            f"ci.run_semgrep_scan: semgrep exited {proc.returncode} (config="
            f"{config!r}) — stderr: {(proc.stderr or '').strip()[-_STDERR_LOG_TAIL:]}"
        )
    return _parse_semgrep_json(proc.stdout)


@register_op("ci.run_semgrep_scan")
def _run_semgrep_scan(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ci.run_semgrep_scan" handler (sync — offloaded to a worker thread
    by dispatch; the blocking git/semgrep subprocess calls here never park the
    event loop).

    See module docstring for the full contract, scope-derivation, and fallback-
    tier strategy. Raises ValueError on a missing repo_root, a missing/blank
    diff_base param, an unresolvable diff_base ref, or a genuine semgrep error.
    """
    if repo_root is None:
        raise ValueError(
            "ci.run_semgrep_scan requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope is 'show_top' and _origin_worktree must be "
            "present in the JSON-RPC envelope."
        )
    diff_base = (params.get("diff_base") or "").strip()
    if not diff_base:
        raise ValueError(
            "ci.run_semgrep_scan: missing required param 'diff_base' (the git ref/"
            "commit the scan scope is diffed against — there is no whole-tree default)"
        )
    if diff_base.startswith("-"):
        # Review: code-reviewer (F5, nit) — diff_base is passed positionally
        # to `git diff` with no `--` separator; a value beginning with `-`
        # would be misparsed as a git flag rather than a revision.
        raise ValueError(
            f"ci.run_semgrep_scan: diff_base {diff_base!r} looks like a git "
            "option (starts with '-'), not a ref/commit — refusing"
        )
    config = (params.get("config") or "").strip() or _DEFAULT_CONFIG

    repo_root_path = Path(repo_root)
    files = _diff_scoped_files(repo_root_path, diff_base)

    if not files:
        return {"findings": [], "tier_used": "empty_scope", "severity_counts": {}}

    if shutil.which("semgrep") is None:
        # Fallback tier: semgrep is not installed. See module docstring
        # "Fallback-tier strategy" — no invented Tier-2 scanner, honest signal.
        print(
            "ci.run_semgrep_scan: semgrep not found on PATH — falling back to "
            "tier_used='unavailable' (no findings scanned)",
            file=sys.stderr,
        )
        return {"findings": [], "tier_used": "unavailable", "severity_counts": {}}

    findings = _run_semgrep(repo_root_path, config, files)
    return {
        "findings": findings,
        "tier_used": "semgrep",
        "severity_counts": _severity_counts(findings),
    }
