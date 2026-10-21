"""
coordinator_core.ops.plan_mise_prep_probe — JSON-RPC "plan.mise_prep_probe",
"plan.mise_upgrade_apply" and "plan.mise_certify_probe" operations.

Purpose: the three per-plan ops a client-side ``mise-prep-run`` / ``mise-certify`` loop
issues, so a full-set run is N short ops and never one long one. Each is a thin composition of reads that
already exist; this module's only original code is param validation, the HEAD-keyed
corpus cache and the census/upgrade gating rules.

``plan.mise_prep_probe`` (COMPUTE_ONLY) answers, for ONE plan, in one reply: the gate
verdict, the census outcome and the upgrade derivation.
``plan.mise_upgrade_apply`` (MUTATING) writes the derivable fields of ONE plan.

Wire params, probe:
    plan           (str, required)   — repo-relative or absolute; inside the main worktree.
    check_census   (bool, default F) — re-run the plan's census commands.
    upgrade        (bool, default F) — derive what ``mise-prep-upgrade`` would write.
    census_timeout (int, default 30) — seconds one census command may run.

Reply, probe:
    {"gate": <plan.prep_gate reply, same shape>, "census": <revalidate outcome>|None,
     "upgrade": <upgrade_plan dict>|{"skipped": "not upgradeable"}|None, "process_ms": float}

Wire params, apply: ``plan`` (str, required).
Reply, apply: ``{"plan", "error", "derived", "residue", "changed"}`` — ``upgrade_plan``'s dict.

``plan.mise_certify_probe`` (COMPUTE_ONLY) is ``mise-certify.py :: certify`` for ONE plan:
the sha leg, then the census leg only when the stamp reads CERTIFIED (the opposite of the
probe's PREPPED-and-not-CERTIFIED census rule, hence a separate op).
Wire params, certify: ``plan`` (str, required), ``census_timeout`` (int, default 30).
Reply, certify: ``{"plan", "sha", "sha_detail"?, "stamp": <read_stamp dict>|None,
"census": <revalidate outcome>|None, "process_ms"}``. An unreadable plan replies sha
MALFORMED with the OSError text; a plan absent from the worktree raises ValueError.

Census deadline: both census-running ops bound the census leg by
``_CENSUS_DEADLINE_SHARE`` (0.5) of their own ``ipc._timeout_for`` guard. Each entry runs for
``min(census_timeout, remaining)`` seconds; an entry reached past the deadline reads UNRUNNABLE
without a spawn, so the census rolls up UNDECIDABLE, never DRIFT. DR-344 standing: an op that
returns at the deadline is still over 500ms; that cost is the plan's authored census, and this
bound stops the op dying, it does not make that plan fit.

Negative-spec:
  - Takes ONE plan, never a list. ``roadmap.prep_gate.gate_plan`` records one-plan-per-call
    as a measured budget decision; a batch probe would reopen it.
  - Does NOT write. The probe runs ``upgrade_plan(check=True)``; only
    ``plan.mise_upgrade_apply`` writes.
  - Does NOT re-implement the census executor or the deriver: both are loaded by file path
    from THIS engine's ``coordinator/bin/`` and called. Never a foreign root.
  - Does NOT serve a corpus across a HEAD move. ``CorpusInputs`` is cached on
    (repo_root, HEAD sha); an unreadable HEAD is never cached. A top-level directory
    created without a commit, or a ``.gitignore`` edit made without one, is therefore not seen
    until HEAD moves.
  - ``process_ms`` is this process's CPU time over the op body. Census shell children are
    separate processes and report their own cost in the census outcome.
"""

from __future__ import annotations

import functools
import importlib.util
import time
from pathlib import Path
from typing import Any, Optional

MUTATES = ["docs/plans/*.md"]  # plan.mise_upgrade_apply writes the derivable fields of the caller-named plan only

from coordinator_core import ipc
from coordinator_core.bin_lib_binding import exec_module_bin_bound
from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.engine_version import engine_build
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops.plan_prep_gate import _resolve_plan
from coordinator_core.roadmap.prep_gate import (
    CERTIFIED,
    PREPPED,
    CorpusInputs,
    corpus_inputs,
    gate_plan_with_corpus,
    read_stamp,
)

GENERATES: list = []

#: Failing gate classes the upgrade path cannot reach: a schema class refuses a value the
#: plan already states, so there is nothing derivable to write.
_NOT_UPGRADEABLE = ("schema",)

_BIN_DIR = ENGINE_CHECKOUT_ROOT / "coordinator" / "bin"

_CORPUS_CACHE: dict = {}

#: Share of the op's own wall guard the census leg may spend. It leaves the gate leg, the
#: upgrade leg and the transport inside the guard under load; the guard is a runaway catch,
#: never a target.
_CENSUS_DEADLINE_SHARE = 0.5


def _census_deadline(op: str) -> float:
    """A `time.monotonic()` deadline for the census leg.

    Read through `ipc._timeout_for`, never a literal: the env knob only narrows the guard.
    """
    return time.monotonic() + _CENSUS_DEADLINE_SHARE * ipc._timeout_for(op)


@functools.cache
def _load_bin(filename: str, modname: str):
    """A sibling ``coordinator/bin`` script of THIS engine, loaded once by file path."""
    path = _BIN_DIR / filename
    if not path.is_file():
        raise ValueError(f"engine has no {path}")
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    exec_module_bin_bound(spec.loader, mod, str(_BIN_DIR))
    return mod


def _upgrader():
    return _load_bin("mise-prep-upgrade.py", "mise_prep_upgrade")


def _revalidator():
    return _load_bin("mise-census-revalidate.py", "mise_census_revalidate")


def _head_sha(worktree_root: Path) -> Optional[str]:
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.repo_root import _walk_for_repo

    try:
        found = _walk_for_repo(worktree_root)
        return head_sha(found[1]) if found is not None else None
    except OSError:
        return None


def _corpus(worktree_root: Path) -> CorpusInputs:
    """``CorpusInputs`` for the root, derived once per (root, HEAD sha)."""
    head = _head_sha(worktree_root)
    if head is None:
        return corpus_inputs(worktree_root)
    key = (str(worktree_root), head)
    cached = _CORPUS_CACHE.get(key)
    if cached is None:
        _CORPUS_CACHE.clear()
        cached = _CORPUS_CACHE[key] = corpus_inputs(worktree_root)
    return cached


def _rel(plan_path: Path, worktree_root: Path) -> str:
    try:
        return plan_path.relative_to(worktree_root).as_posix()
    except ValueError:  # pragma: no cover - contained_path already refused this
        return plan_path.as_posix()


def _plan_param(params: dict, op: str, repo_root: Optional[Path]) -> Path:
    if repo_root is None:
        raise ValueError(f"{op} requires a resolved repo_root")
    raw = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file"
        )
    return _resolve_plan(raw.strip(), Path(main_worktree_root(repo_root)))


def _bool_param(params: dict, name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _not_upgradeable(gate: dict) -> bool:
    failing = [
        name for name, body in (gate.get("classes") or {}).items()
        if body.get("status") != "PASS"
    ]
    return bool(failing) and all(n.lower().startswith(_NOT_UPGRADEABLE) for n in failing)


def _upgrade_dict(result: dict, worktree_root: Path) -> dict:
    out = dict(result)
    out["plan"] = _rel(Path(result["plan"]), worktree_root)
    return out


@register_op("plan.mise_prep_probe")
def _probe_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_prep_probe" handler. See module docstring."""
    plan_path = _plan_param(params, "plan.mise_prep_probe", repo_root)
    check_census = _bool_param(params, "check_census")
    upgrade = _bool_param(params, "upgrade")
    timeout = params.get("census_timeout", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("census_timeout must be a positive integer")

    deadline = _census_deadline("plan.mise_prep_probe")
    started = time.process_time()
    worktree_root = Path(main_worktree_root(repo_root))
    text = plan_path.read_text(encoding="utf-8", errors="replace")
    report = gate_plan_with_corpus(plan_path, _corpus(worktree_root), text=text)
    stamp = read_stamp(text)
    gate = {
        "plan": _rel(plan_path, worktree_root),
        "verdict": report["verdict"],
        "withheld_rows": report["withheld_rows"],
        "classes": report["classes"],
        "message": report["message"],
        "stamp": stamp,
        "engine_build": engine_build(),
    }

    census = None
    if check_census and report["verdict"] == PREPPED and stamp["state"] != CERTIFIED:
        census = _revalidator().revalidate(
            plan_path, worktree_root, timeout, require_stamp=False, deadline=deadline
        )

    derivation = None
    if upgrade and report["verdict"] != PREPPED:
        if _not_upgradeable(gate):
            derivation = {"skipped": "not upgradeable"}
        else:
            derivation = _upgrade_dict(
                _upgrader().upgrade_plan(plan_path, check=True, text=text), worktree_root
            )

    return {
        "gate": gate,
        "census": census,
        "upgrade": derivation,
        "process_ms": round((time.process_time() - started) * 1000.0, 3),
    }


@register_op("plan.mise_upgrade_apply")
def _apply_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_upgrade_apply" handler. See module docstring."""
    plan_path = _plan_param(params, "plan.mise_upgrade_apply", repo_root)
    worktree_root = Path(main_worktree_root(repo_root))
    upgrader = _upgrader()
    text = plan_path.read_text(encoding="utf-8", errors="replace")
    parsed = upgrader.parse_frontmatter(text)
    reason = upgrader.not_a_plan_reason(plan_path, text, parsed)
    if reason is not None:
        raise ValueError(f"not a plan: {reason}")
    return _upgrade_dict(
        upgrader.upgrade_plan(plan_path, check=False, text=text, parsed=parsed), worktree_root
    )


@register_op("plan.mise_certify_probe")
def _certify_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_certify_probe" handler. See module docstring."""
    plan_path = _plan_param(params, "plan.mise_certify_probe", repo_root)
    timeout = params.get("census_timeout", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("census_timeout must be a positive integer")

    deadline = _census_deadline("plan.mise_certify_probe")
    started = time.process_time()
    worktree_root = Path(main_worktree_root(repo_root))
    rel = _rel(plan_path, worktree_root)
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "plan": rel, "sha": "MALFORMED", "sha_detail": str(exc), "stamp": None,
            "census": None, "process_ms": round((time.process_time() - started) * 1000.0, 3),
        }
    stamp = read_stamp(text)
    reply: dict = {"plan": rel, "sha": stamp["state"]}
    if stamp["state"] == "STALE":
        reply["sha_detail"] = f"recorded {stamp['recorded_sha']}, body {stamp['body_sha']}"
    elif stamp["state"] == "MALFORMED":
        reply["sha_detail"] = f"stamp fields missing: {', '.join(stamp.get('missing') or [])}"
    reply["stamp"] = stamp
    reply["census"] = (
        _revalidator().revalidate(plan_path, worktree_root, timeout, deadline=deadline)
        if stamp["state"] == CERTIFIED
        else None
    )
    reply["process_ms"] = round((time.process_time() - started) * 1000.0, 3)
    return reply
