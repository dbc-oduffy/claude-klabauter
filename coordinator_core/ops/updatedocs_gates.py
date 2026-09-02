"""
coordinator_core.ops.updatedocs_gates — JSON-RPC "updatedocs.gates" operation.

Purpose: run the `/update-docs` gate battery (the six per-phase shell-outs formerly
known as Phases 11f / 11g / 11h / 11h2 / 11i / 11j, plus the pre-flight/threshold
probes ported from `coordinator/bin/update-docs-probes.py`) and return ONE
structured per-gate verdict array instead of six independent exit-code dialects.

Source: cross-repo/inbox/2026-08-06-doe-claude-em-updatedocs-gates-structured-
verdicts.md (kind: proposal, from doe-claude-em; ADOPTED). The measured cost this
op removes: each of the six gates re-taught its own exit-code dialect in prose
(11h alone spent a paragraph on why exit 2 != exit 1, a rule earned after a July
2026 incident where that gate silently no-opped for a week), and severity lived on
the PHASE rather than the FINDING — 11h2 halted a whole ceremony over 49
naming-invariant orphans at a measured 0% true-positive rate, while genuinely
reader-facing link breakage was merely informational.

The verdict enum IS the fix, not a convenience wrapper around one:
  - CLEAN         — the gate ran and found nothing to report.
  - FINDING       — the gate ran and found something; severity lives on the
                     GateResult, not on which gate produced it (§ below).
  - UNAVAILABLE   — the gate could NOT run (missing CLI, missing manifest,
                     unresolvable input). "Found nothing" and "looked at
                     nothing" are different verdicts and MUST NOT collapse —
                     collapsing them is the exact July-2026 incident.
  - CONTRADICTION — the gate's own output asserts something exists (a
                     nonzero warning/count) while its headline result claims
                     CLEAN. Modeled as a value so "never report found nothing
                     when the input asserted there was something" does not
                     need re-teaching per gate (memo's example: sync-plugin-
                     wiki's "clean (161 validated, 31 missing-bundled
                     warnings)" is this trap firing unmarked).

Severity rides the FINDING (`Severity.BLOCKING` / `Severity.INFORMATIONAL`), not
the phase — this is the highest-value item in the memo and the only version that
does not require re-litigating each phase's blocking-ness by hand every time a
gate's behavior changes.

Gate coverage this dispatch (7 of the historical battery — see Deferred below):
  fresh-scaffold-probe, repomap-gate, distill-threshold (pre-flight / cadence
  probes ported from update-docs-probes.py), 11f (lens-orthogonality), 11g
  (plugin-bundled-wiki), 11h (skill-anchor-links), 11i (queue-prune-sweep),
  11j (reap-stale-subagent-sidecars).

Deferred, NOT wired here: 11h2 (cross-reference coverage / verify-coverage).
Its backing implementation is `coordinator_core/ops/verify_coverage.py` /
`coverage_gate.py`, explicitly out of scope for this dispatch (other sessions
were editing those files concurrently) — wiring it is a follow-up chunk, not a
gap in this op's own contract; `updatedocs.gates` runs a NAMED SUBSET of gates
via `params["gates"]`, so adding 11h2 later is additive, not a reshape.

Repo-root resolution: this op is keyed "common_dir" in
coordinator_core/op_scopes.py, so the injected `repo_root` is the git COMMON
DIR (`<worktree>/.git`), not a worktree. Every gate below reads worktree-rooted
corpora (`docs/`, `archive/specs/`, `cross-repo/archive/`), so the handler
derives the worktree via `main_worktree_root()` exactly as its keying-table
peers `memo.triage` and `distill.scope` do. `params["repo_root"]` still
overrides, and `main_worktree_root` is ergonomically widened to accept a
worktree root unchanged, so an explicit worktree-shaped param stays correct.

NEGATIVE SPEC — do not "simplify" this back to using the injected root
directly. Joining a corpus onto `<worktree>/.git` yields a path that is absent
for most gates (UNAVAILABLE, merely useless) but REAL for some, and a gate whose
corpus resolved to a real-but-wrong directory reports CLEAN over an empty walk.
That happened: `distill-threshold` returned CLEAN with total 0 against a
DoE-claude tree holding 634 files in `archive/specs/`, because it counted
`.git/archive/specs`. UNAVAILABLE-on-missing-corpus tests do NOT cover this —
they assert on a path that is absent, and this failure needs a path that exists.
`test_gates_resolve_corpora_under_the_worktree_not_the_git_dir` pins the root.

Every external CLI this op shells out to (the DoE-claude-owned `bin/verify-*`,
`bin/sync-plugin-wiki`, `bin/reap-stale-subagent-sidecars` scripts) is resolved
under `--settings-home` (default: `$COORDINATOR_SETTINGS_HOME` or
`~/.coordinator-claude-settings`) and is READ-ONLY from this op's perspective —
this file never edits a DoE-claude-owned path, it only invokes already-shipped
CLIs there, exactly as `update-docs-probes.py` already did for
`check-rag-state.py` / `generate-repomap.py` / `prune-resolved-queue-entries.py`.

No silent caps: `params["gates"]` selects a named subset explicitly; an unknown
gate name is a ValueError, never a silent skip.

Negative-spec: this module does NOT decide `/update-docs` control flow (whether a
BLOCKING finding halts the ceremony) — that stays the calling skill's own
responsibility, reading `rollup()["halt"]`. It does NOT define "harvest debt" —
`distill-threshold`'s fire/no-fire arithmetic is ported unchanged from
`update-docs-probes.py`, not redesigned (memo's "adjacent gap" is explicitly a
separate, unshaped workstream). It does NOT touch `verify_coverage.py` /
`coverage_gate.py` (11h2) — see Deferred above.

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-updatedocs-gates-
structured-verdicts.md
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.external_tool_budget import bound_for
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.directory_md import compute_directory_md_drift
from coordinator_core.updatedocs.memo_prune import compute_memo_prune_candidates
from coordinator_core.updatedocs.plan_prune import compute_plan_prune_candidates
from coordinator_core.updatedocs.readme_index import compute_readme_index_drift
from coordinator_core.win_portability import no_console_creationflags


class GateVerdict(str, Enum):
    """The transport IS the fix — see module docstring. A plain integer exit
    code has no room for "looked at nothing" vs "found nothing"; this enum
    does, and CONTRADICTION gives "the gate's own evidence disagrees with its
    headline" somewhere to live besides prose."""

    CLEAN = "clean"
    FINDING = "finding"
    UNAVAILABLE = "unavailable"
    CONTRADICTION = "contradiction"


class Severity(str, Enum):
    """Attaches to a GateResult, never to a gate identity — the whole point
    of "severity rides the finding, not the phase" (memo's highest-value
    item, evidenced by 11h2's 0%-true-positive halts vs informational
    link-breakage findings)."""

    BLOCKING = "blocking"
    INFORMATIONAL = "informational"


@dataclass
class GateResult:
    """One gate's structured verdict.

    `severity` is only meaningful when `verdict` is FINDING or CONTRADICTION —
    CLEAN and UNAVAILABLE carry `severity=None` because "how bad is it" does
    not apply to "nothing was found" or "nothing could be checked".
    """

    gate_id: str
    verdict: GateVerdict
    summary: str
    severity: Optional[Severity] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "verdict": self.verdict.value,
            "severity": self.severity.value if self.severity else None,
            "summary": self.summary,
            "detail": self.detail,
        }


def rollup(results: list[GateResult]) -> dict[str, Any]:
    """Pure function over the verdict array — no gate-by-gate special-casing.

    This is what lets a maintainer's Phase-14-style negative-spec ("do not
    re-add these eleven lines") degrade into an absence in a renderer instead
    of hand-maintained prose (memo's second bullet): the rollup only ever asks
    "what verdict, what severity", never "which gate is this".
    """
    blocking = [r.gate_id for r in results if r.severity == Severity.BLOCKING]
    informational = [r.gate_id for r in results if r.severity == Severity.INFORMATIONAL]
    unavailable = [r.gate_id for r in results if r.verdict == GateVerdict.UNAVAILABLE]
    contradiction = [r.gate_id for r in results if r.verdict == GateVerdict.CONTRADICTION]
    clean = [r.gate_id for r in results if r.verdict == GateVerdict.CLEAN]
    return {
        "halt": bool(blocking),
        "blocking": blocking,
        "informational": informational,
        "unavailable": unavailable,
        "contradiction": contradiction,
        "clean": clean,
        "gate_count": len(results),
    }


# Review: coordinator:code-reviewer — log-size ceiling for the JSON-RPC
# response body; every gate function truncated stdout/stderr to the same
# literal magic number at 5+ call sites with no named rationale.
_DETAIL_TAIL_CHARS = 2000

# Review: coordinator:code-reviewer — CLAUDE.md: "every op is held to an
# end-to-end invocation budget," but _run passed no timeout= to
# subprocess.run, so one hung external CLI could stall updatedocs.gates
# indefinitely. The per-CLI ceiling is now the named external-tool carve-out
# rather than this module's own literal; `_run`'s `timeout=` param narrows
# only — a caller passing a larger value is clamped back to the ceiling.
_CLI_SITE = "coordinator_core/ops/updatedocs_gates.py :: _run"
_DEFAULT_CLI_TIMEOUT_SECONDS = bound_for(_CLI_SITE)


def _windows_exec_argv(cli_path: Path, args: list[str]) -> Optional[list[str]]:
    """Resolves the Windows-executable argv form of an extensionless CLI.
    `CreateProcess` (what `subprocess.run` uses under the hood on Windows)
    never honours a `#!` shebang line the way POSIX `execve` does — exec'ing
    `cli_path` directly there raises OSError (WinError 193 / ENOEXEC) before
    the CLI ever runs, which is exactly the gap the `.cmd`/`.ps1` launcher
    shim (`coordinator/bin/gen-launcher-shim.py`) exists to close for every
    `bin/` entrypoint. Checked in the shim's own precedence order — first
    co-located sibling that exists wins, no cascading past it even if a
    later candidate would also resolve, so the fallback to a raw shebang
    read stays a last resort rather than competing with an already-generated
    shim. Returns None (never raises) when nothing resolves; the caller
    treats that exactly like today's direct-exec OSError — UNAVAILABLE."""
    cmd_sibling = Path(str(cli_path) + ".cmd")
    if cmd_sibling.is_file():
        return [str(cmd_sibling), *args]

    ps1_sibling = Path(str(cli_path) + ".ps1")
    if ps1_sibling.is_file():
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return None
        # Mirrors gen_claude_doe_launcher.smoke_launcher's own .ps1
        # invocation shape — the tree's one other site that spawns a
        # generated .ps1 launcher rather than merely rendering its text.
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_sibling), *args]

    py_sibling = Path(str(cli_path) + ".py")
    if py_sibling.is_file():
        return [sys.executable, str(py_sibling), *args]

    if cli_path.suffix:
        return None
    try:
        with cli_path.open("r", encoding="utf-8", errors="replace") as f:
            shebang = f.readline()
    except OSError:
        return None
    # A shebang naming anything other than a Python interpreter (sh/bash/
    # node/...) is not something this op can safely run in-process or via
    # sys.executable — that stays a genuine UNAVAILABLE, not an invented
    # interpreter substitution.
    if shebang.startswith("#!") and "python" in shebang.lower():
        return [sys.executable, str(cli_path), *args]
    return None


def _run(
    cli_path: Path,
    args: list[str],
    *,
    timeout: Optional[float] = None,
) -> Optional[subprocess.CompletedProcess]:
    """Run an external CLI, returning None (never raising) when it cannot even
    be spawned, OR when it hangs past `timeout` seconds — both are exactly
    what UNAVAILABLE exists to carry rather than an exception escaping the
    op (a timeout must never read as CLEAN; every call site already treats
    None as, at minimum, UNAVAILABLE — see module docstring's verdict enum).

    Interpreter selection is by file extension, never `os.access(X_OK)` —
    Review: coordinator:code-reviewer — os.access(path, os.X_OK) is not a
    reliable executability signal cross-platform: on Windows it returns True
    for effectively any existing file, so a POSIX-only "exec directly" branch
    would fire there too and every shelled-out gate would go UNAVAILABLE; and
    on POSIX, a shell/extensionless CLI that lost its exec bit (a known
    git-on-Windows-checkout hazard) would fall into the sys.executable branch
    and hand a non-Python script to the interpreter instead of failing with a
    spawn OSError. `.py` CLIs always get the explicit `sys.executable` prefix
    (the pre-existing contract for check-rag-state.py / generate-repomap.py /
    prune-*.py); every other CLI (the extensionless DoE-claude bin/ scripts)
    is exec'd directly on POSIX and relies on its own shebang, exactly as
    before this diff introduced those gates. On Windows, direct exec of an
    extensionless file cannot work at all (CreateProcess does not read
    shebangs) — see `_windows_exec_argv` for the launcher-shim-shaped
    resolution used there instead; POSIX behaviour below is unchanged.

    Deliberate isolation boundary — do not convert to an in-process import.
    Mechanism: crash containment — runs project-declared gate commands,
    which are external/third-party CLIs whose crash must not take down the
    caller. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    if cli_path.suffix == ".py":
        argv = [sys.executable, str(cli_path), *args]
    elif sys.platform == "win32":
        resolved = _windows_exec_argv(cli_path, args)
        if resolved is None:
            return None
        argv = resolved
    else:
        argv = [str(cli_path), *args]
    # Resolved at call time (not a bound default) so a test/override can
    # tighten _DEFAULT_CLI_TIMEOUT_SECONDS without needing every caller to
    # thread an explicit timeout= through. The min() is what makes the caller's
    # `timeout=` narrow-only (DR-349 § 3): applied AFTER resolution, so a gate
    # can ask for less than the ceiling and can never ask for more.
    effective_timeout = min(
        _DEFAULT_CLI_TIMEOUT_SECONDS,
        _DEFAULT_CLI_TIMEOUT_SECONDS if timeout is None else timeout,
    )
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=effective_timeout,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _settings_home(override: Optional[str]) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if env:
        return Path(env)
    return Path.home() / ".coordinator-claude-settings"


# ---------------------------------------------------------------------------
# Gate: fresh-scaffold-probe (pre-flight, ported from update-docs-probes.py)
# ---------------------------------------------------------------------------


def _gate_fresh_scaffold_probe(repo_root: Path, _settings: Path, _overrides: dict) -> GateResult:
    """3-axis conjunctive freshly-scaffolded-repo probe. A FINDING (not a
    CLEAN) when it fires — "the repo is too fresh to run the rest of the
    battery" is itself the thing the caller needs to act on."""
    if not (repo_root / "CLAUDE.md").is_file() and not (repo_root / ".git" / "HEAD").is_file():
        return GateResult(
            "fresh-scaffold-probe",
            GateVerdict.UNAVAILABLE,
            "not at a resolvable repo root",
        )

    axis1 = not (repo_root / "docs" / "DIRECTORY.md").is_file() and not (repo_root / "DIRECTORY.md").is_file()
    completed_dir = repo_root / "archive" / "completed"
    axis2 = not completed_dir.is_dir() or not any(completed_dir.iterdir())
    tasks_dir = repo_root / "tasks"
    axis3 = not tasks_dir.is_dir() or not any(tasks_dir.rglob("*.md"))

    if axis1 and axis2 and axis3:
        return GateResult(
            "fresh-scaffold-probe",
            GateVerdict.FINDING,
            "freshly-scaffolded repo — no DIRECTORY.md, no completed work, no distillable tasks/ artifacts",
            severity=Severity.INFORMATIONAL,
            detail={"axis1_no_index": axis1, "axis2_no_archive": axis2, "axis3_no_tasks": axis3},
        )
    return GateResult("fresh-scaffold-probe", GateVerdict.CLEAN, "repo has real content; proceed with the pipeline")


# ---------------------------------------------------------------------------
# Gate: repomap-gate (Phase 9b)
# ---------------------------------------------------------------------------


def _gate_repomap(repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    bin_dir = settings_home / "bin"
    rag_state = overrides.get("rag_state")
    if not rag_state:
        check_rag_state_cli = Path(overrides["check_rag_state_cli"]) if overrides.get("check_rag_state_cli") else bin_dir / "check-rag-state.py"
        proc = _run(check_rag_state_cli, [])
        rag_state = proc.stdout.strip() if proc and proc.returncode == 0 else "unknown"
        if not rag_state:
            rag_state = "unknown"

    if rag_state == "fresh":
        return GateResult("repomap-gate", GateVerdict.CLEAN, "repomap skipped (RAG present + fresh)")

    generate_repomap_cli = Path(overrides["generate_repomap_cli"]) if overrides.get("generate_repomap_cli") else bin_dir / "generate-repomap.py"
    if not generate_repomap_cli.is_file():
        return GateResult(
            "repomap-gate",
            GateVerdict.UNAVAILABLE,
            f"generate-repomap.py unresolvable at {generate_repomap_cli}",
        )

    proc = _run(generate_repomap_cli, [])
    if proc is None:
        return GateResult("repomap-gate", GateVerdict.UNAVAILABLE, "generate-repomap.py could not be spawned")
    if proc.returncode != 0:
        return GateResult(
            "repomap-gate",
            GateVerdict.FINDING,
            f"repomap regeneration failed (exit {proc.returncode})",
            severity=Severity.INFORMATIONAL,
            detail={"stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )
    fallback = rag_state != "absent"
    return GateResult(
        "repomap-gate",
        GateVerdict.CLEAN,
        f"repomap generated (rag_state={rag_state}{', fallback' if fallback else ''})",
    )


# ---------------------------------------------------------------------------
# Gate: distill-threshold (Phase 13, steps 1-2)
# ---------------------------------------------------------------------------

_RUN_HEADER_RE = re.compile(r"^##\s+Run\s+(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_LEGACY_ROW_DATE_RE = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|", re.MULTILINE)


def _last_distillation_days_ago(log_path: Path) -> Optional[int]:
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    dates = _RUN_HEADER_RE.findall(text) + _LEGACY_ROW_DATE_RE.findall(text)
    if not dates:
        return None
    latest = max(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) for d in dates)
    delta = datetime.now(timezone.utc) - latest
    return max(delta.days, 0)


def _gate_distill_threshold(repo_root: Path, _settings: Path, overrides: dict) -> GateResult:
    plans = len(list((repo_root / "docs" / "plans").glob("*.md"))) if (repo_root / "docs" / "plans").is_dir() else 0
    handoffs = (
        len(list((repo_root / "archive" / "handoffs").rglob("*.md")))
        if (repo_root / "archive" / "handoffs").is_dir()
        else 0
    )
    completed = (
        len(list((repo_root / "archive" / "completed").rglob("*.md")))
        if (repo_root / "archive" / "completed").is_dir()
        else 0
    )
    tasks_dir = repo_root / "tasks"
    tasks = 0
    if tasks_dir.is_dir():
        for p in tasks_dir.rglob("*.md"):
            try:
                rel_parts = p.relative_to(tasks_dir).parts
            except ValueError:
                continue
            if len(rel_parts) >= 2:
                tasks += 1
    total = plans + handoffs + completed + tasks

    log_path = Path(overrides["log_path"]) if overrides.get("log_path") else repo_root / "state" / "distillation-log.md"
    days_ago = _last_distillation_days_ago(log_path)

    if total >= 50:
        reason, fire = f"total count {total} >= 50", True
    elif days_ago is not None and days_ago > 14:
        reason, fire = f"last distillation {days_ago} days ago (> 14)", True
    elif days_ago is None and total >= 20:
        reason, fire = f"no distillation log and total count {total} >= 20", True
    else:
        reason = f"total count {total}, last distillation {days_ago if days_ago is not None else 'never'} days ago"
        fire = False

    detail = {"total": total, "days_ago": days_ago, "reason": reason}
    if fire:
        return GateResult(
            "distill-threshold", GateVerdict.FINDING, f"harvest threshold met — {reason}", Severity.INFORMATIONAL, detail
        )
    return GateResult("distill-threshold", GateVerdict.CLEAN, f"harvest threshold not met — {reason}", detail=detail)


# ---------------------------------------------------------------------------
# Gate 11f: Parallel-review lens-orthogonality check
# ---------------------------------------------------------------------------


def _gate_lens_orthogonality(_repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    cli = Path(overrides["cli"]) if overrides.get("cli") else settings_home / "bin" / "verify-parallel-review-lens-orthogonality"
    if not cli.exists():
        return GateResult("11f-lens-orthogonality", GateVerdict.UNAVAILABLE, f"CLI not found at {cli}")
    proc = _run(cli, [])
    if proc is None:
        return GateResult("11f-lens-orthogonality", GateVerdict.UNAVAILABLE, "CLI could not be spawned")
    if proc.returncode == 0:
        return GateResult("11f-lens-orthogonality", GateVerdict.CLEAN, "lens-domain orthogonality clean")
    return GateResult(
        "11f-lens-orthogonality",
        GateVerdict.FINDING,
        "lens-domain collision detected — rename the colliding domain or drop the reviewer from the parallel pool",
        Severity.INFORMATIONAL,
        detail={"exit_code": proc.returncode, "stdout": proc.stdout[-_DETAIL_TAIL_CHARS:]},
    )


# ---------------------------------------------------------------------------
# Gate 11g: Plugin-bundled wiki validate
# ---------------------------------------------------------------------------

_MISSING_BUNDLED_RE = re.compile(r"(\d+)\s+missing-bundled", re.IGNORECASE)
_VALIDATED_RE = re.compile(r"(\d+)\s+validated", re.IGNORECASE)


def _gate_plugin_wiki(_repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    cli = Path(overrides["cli"]) if overrides.get("cli") else settings_home / "bin" / "sync-plugin-wiki"
    if not cli.exists():
        return GateResult("11g-plugin-wiki", GateVerdict.UNAVAILABLE, f"CLI not found at {cli}")
    proc = _run(cli, [])
    if proc is None:
        return GateResult("11g-plugin-wiki", GateVerdict.UNAVAILABLE, "CLI could not be spawned")

    stdout = proc.stdout or ""
    missing_match = _MISSING_BUNDLED_RE.search(stdout)
    missing_count = int(missing_match.group(1)) if missing_match else 0
    validated_match = _VALIDATED_RE.search(stdout)
    validated_count = int(validated_match.group(1)) if validated_match else None

    if proc.returncode == 5:
        return GateResult(
            "11g-plugin-wiki",
            GateVerdict.FINDING,
            "dev-side wiki mirror exists at ~/.claude/docs/wiki/ — resolve or override COORDINATOR_OVERRIDE_WIKI_MIRROR=1",
            Severity.BLOCKING,
            detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]},
        )
    if proc.returncode != 0:
        return GateResult(
            "11g-plugin-wiki",
            GateVerdict.UNAVAILABLE,
            f"sync-plugin-wiki exited unexpectedly (exit {proc.returncode})",
            detail={"exit_code": proc.returncode, "stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )

    # exit 0. Never collapse "0 missing-bundled warnings present" into CLEAN —
    # this is the memo's own worked example of the trap.
    #
    # Review: coordinator:code-reviewer — regex-over-freetext is the sole
    # CONTRADICTION-detection mechanism and is fragile to upstream wording
    # drift (a DoE-claude-owned CLI this repo does not control). The memo's
    # own worked example ("clean (161 validated, 31 missing-bundled
    # warnings)") always carries a "N validated" count on exit 0 — if that
    # shape stops matching, missing_count silently defaults to 0 and this
    # gate reports the exact CLEAN-when-not-clean trap it exists to prevent.
    # Canary: on exit 0, the "N validated" count MUST parse; if it doesn't,
    # surface UNAVAILABLE-with-parse-warning rather than trust a silent
    # zero-default into CLEAN.
    if validated_match is None:
        return GateResult(
            "11g-plugin-wiki",
            GateVerdict.UNAVAILABLE,
            "sync-plugin-wiki exited 0 but output did not match the expected "
            "'N validated' shape — parse may be stale against upstream wording",
            detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]},
        )
    if missing_count > 0:
        return GateResult(
            "11g-plugin-wiki",
            GateVerdict.CONTRADICTION,
            f"reported clean but carries {missing_count} missing-bundled warning(s) — validated={validated_count}",
            Severity.INFORMATIONAL,
            detail={"validated": validated_count, "missing_bundled": missing_count},
        )
    return GateResult(
        "11g-plugin-wiki",
        GateVerdict.CLEAN,
        f"plugin-bundled wiki clean (validated={validated_count})",
        detail={"validated": validated_count},
    )


# ---------------------------------------------------------------------------
# Gate 11h: Super-skill anchor-link check
# ---------------------------------------------------------------------------

_UNRESOLVED_RE = re.compile(r"(\d+)\s+unresolved", re.IGNORECASE)


def _gate_skill_anchor_links(_repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    """Never collapse exit 1 into exit 2 — "found dead anchors" and "could not
    check" are different verdicts (the incident this whole op exists to
    prevent from recurring)."""
    cli = Path(overrides["cli"]) if overrides.get("cli") else settings_home / "bin" / "verify-skill-anchor-links"
    if not cli.exists():
        return GateResult("11h-skill-anchor-links", GateVerdict.UNAVAILABLE, f"CLI not found at {cli}")
    proc = _run(cli, [])
    if proc is None:
        return GateResult("11h-skill-anchor-links", GateVerdict.UNAVAILABLE, "CLI could not be spawned")

    stdout = proc.stdout or ""
    if proc.returncode == 2:
        return GateResult(
            "11h-skill-anchor-links",
            GateVerdict.UNAVAILABLE,
            "could not check anchor links — coverage you do not have, not a finding",
            detail={"stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )
    if proc.returncode == 1:
        return GateResult(
            "11h-skill-anchor-links",
            GateVerdict.FINDING,
            "dead anchor citation(s) found — repoint, lift the section, or qualify as global",
            Severity.BLOCKING,
            detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]},
        )
    if proc.returncode != 0:
        return GateResult(
            "11h-skill-anchor-links",
            GateVerdict.UNAVAILABLE,
            f"unexpected exit code {proc.returncode}",
            detail={"stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )

    unresolved_match = _UNRESOLVED_RE.search(stdout)
    # Review: coordinator:code-reviewer — same regex-brittleness concern as
    # 11g's _VALIDATED_RE (see that gate's canary comment). Unlike 11g, there
    # is no documented worked example proving "N unresolved" always appears
    # on a clean exit 0 here, so a silent-absence default to 0 is left as-is
    # (a false UNAVAILABLE on legitimately clean output would be worse) —
    # but the word "unresolved" appearing WITHOUT a parseable count is a
    # strong, unambiguous signal the wording drifted underneath the regex,
    # and that failure mode must be loud, not silent.
    if unresolved_match is None and "unresolved" in stdout.lower():
        return GateResult(
            "11h-skill-anchor-links",
            GateVerdict.UNAVAILABLE,
            "verify-skill-anchor-links mentions 'unresolved' but the count "
            "did not parse — parse may be stale against upstream wording",
            detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]},
        )
    unresolved = int(unresolved_match.group(1)) if unresolved_match else 0
    if unresolved > 0:
        return GateResult(
            "11h-skill-anchor-links",
            GateVerdict.FINDING,
            f"anchor links clean of dead citations but {unresolved} unresolved (non-fatal, must still be surfaced)",
            Severity.INFORMATIONAL,
            detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]},
        )
    return GateResult("11h-skill-anchor-links", GateVerdict.CLEAN, "super-skill anchor links clean", detail={"stdout": stdout[-_DETAIL_TAIL_CHARS:]})


# ---------------------------------------------------------------------------
# Gate 11i: Prune resolved-state bloat from queues
# ---------------------------------------------------------------------------

_DEFAULT_QUEUES = (
    "state/coordinator-improvement-queue.md",
    "state/improvement-queue.md",
    "state/bug-backlog.md",
)

_YAML_QUEUE_FAMILIES = (
    ("state/bug-backlog", "prune-closed-bugs.py"),
    ("state/improvement-queue", "prune-closed-improvements.py"),
)


def _gate_queue_prune_sweep(repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    bin_dir = settings_home / "bin"
    override_cli = overrides.get("prune_cli")
    prune_cli = Path(override_cli) if override_cli else bin_dir / "prune-resolved-queue-entries.py"
    queues = overrides.get("queues") or list(_DEFAULT_QUEUES)

    overall_fail = False
    any_legacy_present = False
    total_pruned = 0
    lines: list[str] = []

    existing_queues = [q for q in queues if (repo_root / q).is_file()]
    any_legacy_present = bool(existing_queues)
    before_counts = {
        q: sum(1 for _ in (repo_root / q).open("r", encoding="utf-8", errors="replace"))
        for q in existing_queues
    }

    if existing_queues:
        if override_cli:
            # A caller-substituted CLI (overrides["prune_cli"]) is not
            # guaranteed to accept multiple positionals the way our own
            # prune-resolved-queue-entries.py now does — keep the original
            # one-spawn-per-queue contract for this seam rather than
            # assuming multi-arg support it may not have.
            for queue in existing_queues:
                proc = _run(prune_cli, [str(repo_root / queue)])
                if proc is None or proc.returncode != 0:
                    overall_fail = True
                    lines.append(f"prune FAILED for {queue}")
        else:
            # Our own CLI's single-subject arity was self-imposed
            # (prune_resolved_queue_entries.main took exactly one
            # positional); it now prunes N queue-files independently in one
            # process, still emitting a per-file stderr line on failure —
            # so the whole default-callee sweep collapses to one spawn.
            proc = _run(prune_cli, [str(repo_root / q) for q in existing_queues])
            if proc is None:
                overall_fail = True
                lines.append("prune FAILED for all queues (CLI could not be spawned)")
            elif proc.returncode != 0:
                overall_fail = True
                stderr_lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
                lines.extend(stderr_lines or ["prune FAILED (nonzero exit, no stderr detail)"])

    for queue in existing_queues:
        queue_path = repo_root / queue
        after = sum(1 for _ in queue_path.open("r", encoding="utf-8", errors="replace"))
        pruned = before_counts[queue] - after
        total_pruned += pruned
        if pruned:
            lines.append(f"pruned {pruned} lines from {queue}")

    any_yaml_present = False
    for family_dir, cli_name in _YAML_QUEUE_FAMILIES:
        dir_path = repo_root / family_dir
        if not dir_path.is_dir():
            continue
        any_yaml_present = True
        cli_path = bin_dir / cli_name
        if not cli_path.is_file():
            overall_fail = True
            lines.append(f"YAML prune CLI missing for {family_dir}: {cli_path}")
            continue
        # Review: coordinator:code-reviewer — the YAML leg's exit code was
        # previously discarded, defeating the F9 fix's whole purpose (a
        # caller must be able to branch on prune-closed-*.py's non-zero
        # exit on partial-archive failure). Nonzero/unrecognized MUST NOT
        # collapse into CLEAN — fold it into overall_fail like the legacy
        # markdown leg above.
        yaml_proc = _run(cli_path, ["--repo-root", str(repo_root)])
        if yaml_proc is None or yaml_proc.returncode != 0:
            overall_fail = True
            exit_desc = "could not be spawned" if yaml_proc is None else f"exit {yaml_proc.returncode}"
            lines.append(f"YAML prune FAILED for {family_dir} ({exit_desc})")

    if not any_legacy_present and not any_yaml_present:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.UNAVAILABLE,
            f"no legacy markdown queues and no YAML queue families found under {repo_root} — verify repo_root",
        )

    if overall_fail:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.FINDING,
            "queue prune failed loud on unexpected structure — must not be bypassed",
            Severity.BLOCKING,
            detail={"lines": lines},
        )
    if total_pruned:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.FINDING,
            f"pruned {total_pruned} resolved-state line(s)",
            Severity.INFORMATIONAL,
            detail={"lines": lines, "total_pruned": total_pruned},
        )
    return GateResult("11i-queue-prune-sweep", GateVerdict.CLEAN, "queue prune clean (no resolved bloat found)")


# ---------------------------------------------------------------------------
# Gate 11j: Reap stale subagent-share sidecars
# ---------------------------------------------------------------------------

_REAPED_RE = re.compile(r"reaped\s+(\d+)", re.IGNORECASE)


def _gate_reap_stale_sidecars(repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    cli = Path(overrides["cli"]) if overrides.get("cli") else settings_home / "bin" / "reap-stale-subagent-sidecars"
    if not cli.exists():
        return GateResult("11j-reap-stale-sidecars", GateVerdict.UNAVAILABLE, f"CLI not found at {cli}")
    proc = _run(cli, ["--repo-root", str(repo_root)])
    if proc is None:
        return GateResult("11j-reap-stale-sidecars", GateVerdict.UNAVAILABLE, "CLI could not be spawned")
    if proc.returncode != 0:
        return GateResult(
            "11j-reap-stale-sidecars",
            GateVerdict.FINDING,
            f"reap failed (exit {proc.returncode}) — surface, do not skip",
            Severity.BLOCKING,
            detail={"stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )
    reaped_match = _REAPED_RE.search(proc.stdout or "")
    reaped = int(reaped_match.group(1)) if reaped_match else 0
    if reaped:
        return GateResult(
            "11j-reap-stale-sidecars",
            GateVerdict.FINDING,
            f"reaped {reaped} stale sidecar(s)",
            Severity.INFORMATIONAL,
            detail={"reaped": reaped},
        )
    return GateResult("11j-reap-stale-sidecars", GateVerdict.CLEAN, "subagent-share sidecar reap clean (nothing stale)")


# ---------------------------------------------------------------------------
# Gates: doc-index drift and prune candidates (bucket-2 extraction)
#
# Four detectors backed by coordinator_core.updatedocs. Each compute function
# raises a typed missing-path error rather than returning an empty result, and
# each gate below converts that to UNAVAILABLE — never CLEAN. That mapping is
# the point of these four: the 2026-09-02 boundary audit found "engine
# unreachable" indistinguishable from "found nothing" at nine of ten fallback
# sites in the ceremony this file serves, and a detector that reports an absent
# corpus as "nothing to do" adds a tenth.
#
# All four are INFORMATIONAL. Drift in a doc index is not a reason to halt
# /update-docs; the audit's evidence against Phase 11h2's unconditional halt
# applies here too.
# ---------------------------------------------------------------------------


def _gate_docs_readme_index_drift(repo_root: Path, _settings: Path, _overrides: dict) -> GateResult:
    """Which docs/ artifacts are missing from, or dead in, docs/README.md."""
    try:
        drift = compute_readme_index_drift(repo_root)
    except UpdatedocsTargetMissing as exc:
        return GateResult(
            "docs-readme-index-drift",
            GateVerdict.UNAVAILABLE,
            f"cannot check: {exc.missing_path} does not exist",
            detail={"missing_path": str(exc.missing_path)},
        )

    drifted = [s for s in drift.sections if s.missing or s.dead]
    if not drifted:
        return GateResult(
            "docs-readme-index-drift",
            GateVerdict.CLEAN,
            "docs/README.md indexes every artifact and links nothing absent",
        )

    total_missing = sum(len(s.missing) for s in drifted)
    total_dead = sum(len(s.dead) for s in drifted)
    return GateResult(
        "docs-readme-index-drift",
        GateVerdict.FINDING,
        f"{len(drifted)} section(s) drifted — {total_missing} unindexed, {total_dead} dead link(s)",
        severity=Severity.INFORMATIONAL,
        detail={
            "sections": [
                {
                    "section": s.section,
                    "linked": s.linked,
                    "on_disk": s.on_disk,
                    "missing": s.missing,
                    "dead": s.dead,
                    "missing_titles": s.missing_titles,
                }
                for s in drifted
            ],
        },
    )


def _gate_directory_md_staleness(repo_root: Path, _settings: Path, overrides: dict) -> GateResult:
    """Do a DIRECTORY.md's asserted counts and refresh date still match disk?

    `overrides["directory_md"]` selects which DIRECTORY.md to check; the default
    is this repo's only one. Deliberately a parameter — there is no root-level
    DIRECTORY.md here, and a gate that hardcoded one would report UNAVAILABLE
    forever on a repo that is not missing anything.
    """
    rel = overrides.get("directory_md") or "coordinator_core/DIRECTORY.md"
    target = repo_root / rel
    try:
        drift = compute_directory_md_drift(target)
    except UpdatedocsTargetMissing as exc:
        return GateResult(
            "directory-md-staleness",
            GateVerdict.UNAVAILABLE,
            f"cannot check: {exc.missing_path} does not exist",
            detail={"missing_path": str(exc.missing_path)},
        )

    mismatched = [c for c in drift.count_claims if not c.matches]
    detail = {
        "directory_md": rel,
        "refreshed_on": drift.refreshed_on.isoformat() if drift.refreshed_on else None,
        "age_days": drift.age_days,
        "mismatched_counts": [
            {"claim_site": c.claim_site, "asserted": c.asserted, "actual": c.actual}
            for c in mismatched
        ],
    }

    reasons = []
    if drift.refreshed_on is None:
        # Not the same as "refreshed today" — an unparseable date means the
        # document's own freshness claim could not be read, which is a finding
        # about the document, not a clean bill of health.
        reasons.append("no readable `Last refreshed:` date")
    if mismatched:
        reasons.append(f"{len(mismatched)} count claim(s) disagree with disk")

    if not reasons:
        return GateResult(
            "directory-md-staleness",
            GateVerdict.CLEAN,
            f"{rel} is internally consistent with disk",
            detail=detail,
        )
    return GateResult(
        "directory-md-staleness",
        GateVerdict.FINDING,
        f"{rel}: " + "; ".join(reasons),
        severity=Severity.INFORMATIONAL,
        detail=detail,
    )


def _prune_gate(
    gate_id: str,
    compute_fn: Callable[..., Any],
    repo_root: Path,
    overrides: dict,
) -> GateResult:
    """Shared body for the two prune gates below.

    Both predicates now emit the identical `prunable`/`retained`/
    `indeterminate` list[str] shape (Kira's finding 1/2 unification), so the
    two `_gate_*` functions differed only in gate id, compute function, and
    one CLEAN message string. This is the table that difference collapses to;
    `_gate_plans_prune_candidates` / `_gate_archive_memo_prune_candidates`
    below close over it with their own name and compute function.
    """
    kwargs = {}
    if "age_days" in overrides:
        kwargs["age_days"] = overrides["age_days"]
    try:
        result = compute_fn(repo_root, **kwargs)
    except UpdatedocsTargetMissing as exc:
        return GateResult(
            gate_id,
            GateVerdict.UNAVAILABLE,
            f"cannot check: {exc.missing_path} does not exist",
            detail={"missing_path": str(exc.missing_path)},
        )

    detail = {
        "prunable": list(result.prunable),
        "indeterminate": list(result.indeterminate),
        "retained_count": len(result.retained),
    }
    if not result.prunable and not result.indeterminate:
        return GateResult(
            gate_id,
            GateVerdict.CLEAN,
            f"no candidate is prune-eligible ({len(result.retained)} retained)",
            detail=detail,
        )
    return GateResult(
        gate_id,
        GateVerdict.FINDING,
        f"{len(result.prunable)} prune candidate(s), {len(result.indeterminate)} indeterminate",
        severity=Severity.INFORMATIONAL,
        detail=detail,
    )


def _gate_plans_prune_candidates(repo_root: Path, _settings: Path, overrides: dict) -> GateResult:
    """Which docs/plans/ entries satisfy all three ripeness-safety legs.

    Prunes nothing: the disposal decision stays with the ceremony and its
    existing guards.
    """
    return _prune_gate(
        "plans-prune-candidates", compute_plan_prune_candidates, repo_root, overrides
    )


def _gate_archive_memo_prune_candidates(repo_root: Path, _settings: Path, overrides: dict) -> GateResult:
    """Which cross-repo/archive memos satisfy the prune predicate.

    Zero prunable is a legitimate, expected answer on a young archive — which
    is exactly why an absent archive directory must reach the caller as
    UNAVAILABLE and not as this gate's CLEAN.
    """
    return _prune_gate(
        "archive-memo-prune-candidates", compute_memo_prune_candidates, repo_root, overrides
    )


# ---------------------------------------------------------------------------
# Battery registry
# ---------------------------------------------------------------------------

_GateFn = Callable[[Path, Path, dict], GateResult]

_GATES: dict[str, _GateFn] = {
    "fresh-scaffold-probe": _gate_fresh_scaffold_probe,
    "repomap-gate": _gate_repomap,
    "distill-threshold": _gate_distill_threshold,
    "11f-lens-orthogonality": _gate_lens_orthogonality,
    "11g-plugin-wiki": _gate_plugin_wiki,
    "11h-skill-anchor-links": _gate_skill_anchor_links,
    "11i-queue-prune-sweep": _gate_queue_prune_sweep,
    "11j-reap-stale-sidecars": _gate_reap_stale_sidecars,
    "docs-readme-index-drift": _gate_docs_readme_index_drift,
    "directory-md-staleness": _gate_directory_md_staleness,
    "plans-prune-candidates": _gate_plans_prune_candidates,
    "archive-memo-prune-candidates": _gate_archive_memo_prune_candidates,
}
"""Deferred, deliberately absent: 11h2 (cross-reference coverage) — its backing
`verify_coverage.py` / `coverage_gate.py` was out of scope for this dispatch.
Adding it later is additive (a new key here), not a reshape of this table."""


@register_op("updatedocs.gates")
def _updatedocs_gates(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "updatedocs.gates" handler — runs the named gate subset and
    returns {"gates": [...], "rollup": {...}}.

    Params (all optional):
        repo_root      (str) — repo root to scope gates against; default cwd.
                         Explicit param, NOT the injected `_origin_worktree`
                         (this op is registered scope="none" — see module
                         docstring).
        settings_home  (str) — override for $COORDINATOR_SETTINGS_HOME
                         resolution (external DoE-claude CLI location).
        gates          (list[str]) — named subset to run; default: all gates
                         in the registry. An unknown name is a ValueError,
                         never a silent skip (no silent caps).
        overrides      (dict[str, dict]) — per-gate keyword overrides (CLI
                         paths, injected states) for test/diagnostic use,
                         keyed by gate_id — mirrors update-docs-probes.py's
                         existing --*-cli override flags.
    """
    # Injected `repo_root` is the git COMMON DIR (op_scopes: "common_dir"), and every
    # gate reads worktree-rooted corpora — see the module docstring's negative spec.
    # Only the injected value is translated: an explicit params["repo_root"] is already
    # worktree-shaped by contract, and main_worktree_root() REFUSES a non-git path
    # rather than guessing, so applying it there would turn a diagnostic call against a
    # plain directory into an uncaught ValueError instead of running the gates.
    explicit = params.get("repo_root")
    if explicit:
        root = Path(explicit)
    elif repo_root is not None:
        root = main_worktree_root(Path(repo_root))
    else:
        root = Path.cwd()
    settings_home = _settings_home(params.get("settings_home"))
    requested = params.get("gates") or list(_GATES.keys())
    unknown = [g for g in requested if g not in _GATES]
    if unknown:
        raise ValueError(f"updatedocs.gates: unknown gate id(s) {unknown} — known: {sorted(_GATES)}")

    overrides_by_gate: dict[str, dict] = params.get("overrides") or {}
    results: list[GateResult] = []
    for gate_id in requested:
        fn = _GATES[gate_id]
        results.append(fn(root, settings_home, overrides_by_gate.get(gate_id, {})))

    return {
        "gates": [r.to_dict() for r in results],
        "rollup": rollup(results),
    }
