"""
coordinator_core.ops.run_pip_audit — JSON-RPC "ci.run_pip_audit" operation:
run pip-audit against a lock file, optionally passing --extra-index-url for
a detected non-PyPI wheel source (e.g. PyTorch CUDA wheels).

Purpose: replaces the fence at DoE-claude agents/dep-cve-auditor.md:68, which
shelled out to a bare `pip-audit -r <lockfile> [--extra-index-url <url>]`
invocation. This op is a direct external-tool wrapper — spawning the
`pip_audit` package as a subprocess of the CURRENT interpreter IS the op's
purpose (the bash/coreutils gate forbids shells and coreutils, not the
audited tool itself); it never shells to bash/sh.

Contract (op-classification.tsv row run-pip-audit-with-extra-index,
op-key ci.run_pip_audit):
    params: {lockfile_path: str, extra_index_url: str | None}
        lockfile_path    — path to the requirements/lock file to audit
                            (pip-audit's ``-r``). Required.
        extra_index_url  — optional non-PyPI wheel index (e.g. a CUDA wheel
                            index) forwarded as pip-audit's
                            ``--extra-index-url``. Absent/empty/None means
                            "no extra index" — pip-audit runs against PyPI
                            only.
    -> {findings: list[dict], vulnerable_count: int, extra_index_detected: bool}
        findings           — one entry per (package, vulnerability) pair,
                              flattened out of pip-audit's ``--format json``
                              dependency-nested payload: {package, version,
                              id, fix_versions, description}.
        vulnerable_count   — len(findings).
        extra_index_detected — whether the caller supplied a non-empty
                              extra_index_url (echoes the input, not a
                              re-derivation — this op does not sniff the
                              lockfile for CUDA-suffix version strings; that
                              detection is the caller's job per the oracle
                              row's own rationale: "surrounding detection
                              logic ... is real logic worth scripting
                              alongside the bare invocation" — a separate
                              concern from the invocation itself).

Keying scope: none — ``lockfile_path`` is an explicit caller-supplied param.
The oracle row's scope-verdict note is that pip-audit "audits the caller's
own lock file, which is worktree-specific (differs by branch/checkout); must
not default to makima's own clone" — this op never resolves a lockfile path
on the caller's behalf, so there is no worktree-injection surface to get
wrong.

Idempotency (AC7): pip-audit is a read-only query against the lock file and
the public vulnerability advisory database — it mutates no local state.
Two invocations with identical inputs (same lockfile contents, same
extra_index_url) against an unchanged advisory database produce the same
finding set; the manifest rates this hazard "none". No DEC-7 docstring
block is required (that block is mandated only for medium/high hazard
rows), but AC7's explicit double-invocation test still applies — see
test_run_pip_audit.py.

Self-registration: importing this module calls register_op("ci.run_pip_audit",
...) as a side effect. Registration across the remaining three surfaces
(_EAGER_OP_MODULES / _OP_KEY_SCOPE / _registry_map.py) lands in the separate
EM-serial registration pass, per this chunk's write-scope restriction.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 2 (run cluster)
Port source: DoE-claude agents/dep-cve-auditor.md:68

Negative-spec (hard-won):
  - Does NOT resolve lockfile_path itself (no worktree/repo-root default) —
    caller-supplied only, per the oracle's show_top scope-verdict rationale.
  - Does NOT shell out to bash/sh — pip-audit is invoked as
    [sys.executable, "-m", "pip_audit", ...], a direct list-argv subprocess
    of the running interpreter (shell=False), never a shell string.
  - Does NOT treat a non-zero pip-audit exit code as a hard failure by
    itself — pip-audit exits non-zero when vulnerabilities are found, which
    is the expected/common case, not an error. A JSON-unparseable stdout,
    OR empty stdout paired with a non-zero exit code (tool absent/crashed,
    e.g. "No module named pip_audit"), is treated as an invocation failure
    — the latter previously fell through to a false "clean scan" result
    (Review: code-reviewer, P1 finding).
"""

from __future__ import annotations

import json
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.external_tool_budget import bound_for
from coordinator_core.ipc import register_op

#: Max characters of a failing pip-audit invocation's stderr replayed into
#: the raised error message.
_STDERR_TAIL = 2000

# Review: code-reviewer — pip-audit is a live network call (vulnerability
# advisory endpoint); an unresponsive network wedges this op's worker thread
# forever without a cap. The cap is no longer this module's to choose: DR-349
# grants a network leg no standing carve-out, so it lives inside the
# external-tool ceiling like any other third-party spawn.
_PIP_AUDIT_SITE = "coordinator_core/ops/run_pip_audit.py :: _run_pip_audit"
_TIMEOUT_SECONDS = bound_for(_PIP_AUDIT_SITE)


@register_op("ci.run_pip_audit")
def _run_pip_audit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ci.run_pip_audit" handler (sync — offloaded to a worker
    thread by dispatch, so the blocking subprocess.run call here never parks
    the event loop).

    See module docstring for the full contract. Raises ValueError on a
    missing/invalid lockfile_path param, and RuntimeError if pip-audit's
    stdout cannot be parsed as JSON, or if stdout is empty and the exit code
    is non-zero (tool absent/crashed) — a non-zero exit alone, with a
    genuine JSON payload, is not an error.
    """
    lockfile_path_raw = (params.get("lockfile_path") or "").strip()
    if not lockfile_path_raw:
        raise ValueError(
            "ci.run_pip_audit: missing required param 'lockfile_path' "
            "(explicit caller-supplied lock/requirements file path — this "
            "op never resolves one off a worktree)"
        )

    lockfile = Path(lockfile_path_raw)
    if not lockfile.is_file():
        raise ValueError(
            f"ci.run_pip_audit: lockfile_path does not exist or is not a "
            f"file: {lockfile}"
        )

    extra_index_url_raw = params.get("extra_index_url")
    extra_index_url = (
        str(extra_index_url_raw).strip() if extra_index_url_raw else ""
    ) or None

    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(lockfile),
        "--format",
        "json",
    ]
    if extra_index_url:
        cmd += ["--extra-index-url", extra_index_url]

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ci.run_pip_audit: pip-audit timed out after {_TIMEOUT_SECONDS:.0f}s "
            f"invoking {cmd!r}"
        ) from exc

    stdout = (proc.stdout or "").strip()
    # Review: code-reviewer — empty stdout + non-zero exit (e.g. "No module
    # named pip_audit") previously fell into the falsy-stdout branch below
    # and silently substituted a clean-scan payload, identical to a genuine
    # zero-vuln result. Treat that shape as an invocation failure, same as
    # unparseable stdout.
    if not stdout and proc.returncode != 0:
        raise RuntimeError(
            "ci.run_pip_audit: pip-audit produced no stdout and exited "
            f"non-zero (exit {proc.returncode}) — treating as an invocation "
            "failure (tool absent/crashed), not a clean scan; stderr tail: "
            f"{(proc.stderr or '')[-_STDERR_TAIL:]}"
        )
    try:
        payload: Dict[str, Any] = json.loads(stdout) if stdout else {"dependencies": []}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "ci.run_pip_audit: pip-audit produced non-JSON stdout "
            f"(exit {proc.returncode}); stderr tail: "
            f"{(proc.stderr or '')[-_STDERR_TAIL:]}"
        ) from exc

    findings: List[Dict[str, Any]] = []
    for dependency in payload.get("dependencies", []) or []:
        package = dependency.get("name")
        version = dependency.get("version")
        for vuln in dependency.get("vulns", []) or []:
            findings.append(
                {
                    "package": package,
                    "version": version,
                    "id": vuln.get("id"),
                    "fix_versions": vuln.get("fix_versions", []),
                    "description": vuln.get("description"),
                }
            )

    return {
        "findings": findings,
        "vulnerable_count": len(findings),
        "extra_index_detected": extra_index_url is not None,
    }
