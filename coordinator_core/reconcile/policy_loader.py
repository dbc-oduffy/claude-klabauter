"""
coordinator_core.reconcile.policy_loader -- example-doctrine-repo-owned auto-reconcile policy reader.

Purpose: reads the example-doctrine-repo-owned `coordinator/auto-reconcile-policy.yaml` (example-doctrine-repo
authors it; claude-klabauter NEVER writes it) and validates it against claude-klabauter's grammar
pin (`coordinator_core/contract/auto-reconcile-policy.grammar.md`) before
handing it to C2's commit_reality matcher / C3's gate_eval evaluator. Mirrors
the `subagent-sandbox-policy.yaml` <- `coordinator_core/subagent_sandbox`
read pattern (DR-047 contract-vs-engine split): example-doctrine-repo owns the policy DATA,
Claude-klabauter owns the reading/validating MACHINE and the grammar it validates
against.

Spec backlink: docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C9
Grammar pin: coordinator_core/contract/auto-reconcile-policy.grammar.md
Consumers: coordinator_core/reconcile/commit_reality.py (C2, reads
  three_signal / mechanical_commit_denylist / cross_handoff_attribution),
  coordinator_core/reconcile/gate_eval.py (C3),
  coordinator_core/ops/handoff_reconcile.py (C4/D2, reads dry_run via
  _resolve_dry_run -- see that module's D2(a) docstring section; landed
  2026-07-27, retiring dry_run's former required-but-unconsumed status)
Reference precedent: coordinator_core/subagent_sandbox/engine.py load_policy()

Fail-closed behavior (absent-vs-malformed split, the Staff Engineer review finding 5):
  - policy file ABSENT -> conservative no-auto-ship policy, NO warning. This
    is the expected steady state until example-doctrine-repo authors the yaml; logging a
    warning every workday-start during the pre-ratification period is noise.
  - policy file PRESENT-but-MALFORMED (grammar-pin validation fails) ->
    conservative no-auto-ship policy + a surfaced data-defect warning. This
    is a real defect example-doctrine-repo should hear about, distinct from the expected-absent
    case above.

`policy_report_fields(result)` (§ C10 / AC16) flattens `PolicyResult.source`
and `.resolved_path` into the two fields a downstream reconcile report must
surface -- landed because example-doctrine-repo named the un-reported `source` split as
the cheap engine-side fix that would have told a starvation-report reader
which of "absent"/"malformed"/"loaded" a run was in, one line, no cross-repo
round-trip (`cross-repo/inbox/2026-07-28-example-doctrine-repo-em-handoff-terminal-
starvation-answers.md`).

Negative-spec:
  - Does NOT vendor or copy the policy YAML -- reads it fresh from disk (or
    the injected path) on every call, matching the subagent_sandbox precedent.
  - Does NOT auto-ship on any fail-closed branch (absent OR malformed) -- both
    return `dry_run: True` / `auto_ship_enabled: False`; only the warning
    differs between the two branches.
  - Does NOT write the policy file -- example-doctrine-repo is the sole author.
  - `policy_report_fields` does NOT decide report FORMAT (JSON key names,
    Markdown layout, log-line shape) -- it only supplies the two-field
    payload. Wiring into `handoff.reconcile_open`'s returned dict and the
    C12a Markdown report lives at the call site
    (`coordinator_core/ops/handoff_reconcile.py`'s `_handler` /
    `_build_dry_run_report`, per C10/AC16), outside this module's
    write-scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

import yaml

#: Env var a example-doctrine-repo-side caller may inject the primary policy path through,
#: mirroring subagent_sandbox.engine.POLICY_ENV_VAR.
POLICY_ENV_VAR = "AUTO_RECONCILE_POLICY"

#: Best-effort default relative path when no explicit path/env var resolves,
#: matching the example-doctrine-repo-side repo layout cited in the plan
#: (`coordinator/auto-reconcile-policy.yaml`).
_DEFAULT_POLICY_ENV_CANDIDATES = ("CLAUDE_PLUGIN_ROOT",)
_DEFAULT_POLICY_RELATIVE = "auto-reconcile-policy.yaml"

#: Grammar-pinned top-level keys (see the .grammar.md doc for full shape).
_REQUIRED_KEYS = ("three_signal", "mechanical_commit_denylist", "cross_handoff_attribution", "dry_run")


@dataclass(frozen=True)
class PolicyResult:
    """The outcome of a `load_policy` call.

    ``policy`` is always a usable dict (conservative fail-closed default when
    absent/malformed, or the validated example-doctrine-repo-authored data on success).
    ``warning`` is populated ONLY on the malformed branch -- absent-file and
    successful-load both leave it ``None`` (see module docstring fail-closed
    split).
    ``resolved_path`` is the policy path this call resolved and looked at,
    stringified, for ALL THREE ``source`` values -- including "absent", where
    it is "the path we looked for and did not find" (the actionable half of
    an absent report; see `policy_report_fields` below). ``None`` only when
    resolution itself found no candidate (no explicit path, no env var, and
    the `CLAUDE_PLUGIN_ROOT` default did not resolve to any path at all).

    Spec backlink: docs/plans/2026-07-28-handoff-close-path-fail-loud.md § C10
    (AC16) -- example-doctrine-repo's reply (`cross-repo/inbox/2026-07-28-example-doctrine-repo-em-
    handoff-terminal-starvation-answers.md`) named the missing report surface
    for this data as the cheap fix that would have told a downstream reader
    which of "absent" / "malformed" / "loaded" a run was in without a
    cross-repo round-trip.
    """

    policy: Dict[str, Any]
    source: str  # "absent" | "malformed" | "loaded"
    warning: Optional[str] = None
    resolved_path: Optional[str] = None


def _conservative_policy() -> Dict[str, Any]:
    """The fail-closed policy: no auto-ship, surface everything."""
    return {
        "three_signal": {},
        "mechanical_commit_denylist": [],
        "cross_handoff_attribution": True,
        "dry_run": True,
        "auto_ship_enabled": False,
    }


def _resolve_default_policy_path() -> Optional[Path]:
    """Best-effort default fallback, mirroring subagent_sandbox's
    ``_resolve_default_policy_path`` -- checks ``CLAUDE_PLUGIN_ROOT`` for a
    co-located policy file. Never raises; absence is absorbed by the caller.
    """
    for env_var in _DEFAULT_POLICY_ENV_CANDIDATES:
        root = os.environ.get(env_var)
        if not root:
            continue
        candidate = Path(root) / _DEFAULT_POLICY_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def _resolve_policy_path(policy_path: Optional[str]) -> Optional[Path]:
    candidates: List[Optional[str]] = [policy_path, os.environ.get(POLICY_ENV_VAR)]
    for candidate in candidates:
        if candidate:
            return Path(candidate)
    return _resolve_default_policy_path()


def _validate_grammar(data: Any) -> List[str]:
    """Validate ``data`` against the grammar pin's required top-level shape.

    Returns a list of human-readable defect strings; empty list means valid.
    Deliberately structural/shallow (dict-of-required-keys + coarse type
    checks) -- the grammar doc is the authorial source of truth for the full
    shape; this is the runtime gate, not a schema-validator reimplementation.
    """
    defects: List[str] = []
    if not isinstance(data, dict):
        return ["policy root is not a mapping"]

    for key in _REQUIRED_KEYS:
        if key not in data:
            defects.append(f"missing required key: {key!r}")

    if "three_signal" in data and not isinstance(data["three_signal"], dict):
        defects.append("'three_signal' must be a mapping")

    if "mechanical_commit_denylist" in data:
        denylist = data["mechanical_commit_denylist"]
        if not isinstance(denylist, list) or not all(isinstance(item, str) for item in denylist):
            defects.append("'mechanical_commit_denylist' must be a list of strings")

    if "cross_handoff_attribution" in data and not isinstance(data["cross_handoff_attribution"], bool):
        defects.append("'cross_handoff_attribution' must be a boolean")

    if "dry_run" in data and not isinstance(data["dry_run"], bool):
        defects.append("'dry_run' must be a boolean")

    return defects


def load_policy(policy_path: Optional[str] = None) -> PolicyResult:
    """Load + grammar-validate the example-doctrine-repo-owned auto-reconcile policy.

    Resolution order mirrors ``subagent_sandbox.engine.load_policy``:
      1. ``policy_path`` (explicit override, e.g. a CLI/test injection).
      2. ``AUTO_RECONCILE_POLICY`` env var.
      3. Best-effort ``CLAUDE_PLUGIN_ROOT``-relative default.

    Fail-closed on any failure to resolve/read/parse/validate -- see module
    docstring for the absent-vs-malformed warning split. Never raises.
    """
    resolved = _resolve_policy_path(policy_path)
    resolved_path_str = str(resolved) if resolved is not None else None

    if resolved is None or not resolved.is_file():
        return PolicyResult(
            policy=_conservative_policy(),
            source="absent",
            warning=None,
            resolved_path=resolved_path_str,
        )

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return PolicyResult(
            policy=_conservative_policy(),
            source="malformed",
            warning=f"auto-reconcile-policy.yaml at {resolved} could not be read: {exc}",
            resolved_path=resolved_path_str,
        )

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return PolicyResult(
            policy=_conservative_policy(),
            source="malformed",
            warning=f"auto-reconcile-policy.yaml at {resolved} is not valid YAML: {exc}",
            resolved_path=resolved_path_str,
        )

    defects = _validate_grammar(data)
    if defects:
        return PolicyResult(
            policy=_conservative_policy(),
            source="malformed",
            warning=(
                f"auto-reconcile-policy.yaml at {resolved} failed grammar-pin "
                f"validation: {'; '.join(defects)}"
            ),
            resolved_path=resolved_path_str,
        )

    policy = dict(data)
    policy.setdefault("auto_ship_enabled", True)
    return PolicyResult(
        policy=policy,
        source="loaded",
        warning=None,
        resolved_path=resolved_path_str,
    )


class PolicyReportFields(TypedDict):
    """The `policy_report_fields` return shape (§ C10 / AC16).

    `policy_source` is always one of the three literal `PolicyResult.source`
    strings ("absent" | "malformed" | "loaded") -- never `None`. Only
    `policy_path` can be `None` (see `PolicyResult.resolved_path`'s
    docstring for the one case that produces it). A plain
    `Dict[str, Optional[str]]` return annotation loses that asymmetry for a
    caller reading the signature; this `TypedDict` self-documents it and
    catches a future key-name typo at type-check time.
    """

    policy_source: str
    policy_path: Optional[str]


def policy_report_fields(result: PolicyResult) -> PolicyReportFields:
    """Flatten a `PolicyResult` into the two fields a reconcile report must
    surface, per plan (§ C10 / AC16): `policy_source` distinguishes "absent"
    (expected steady state until example-doctrine-repo authors the yaml) from "malformed" (a
    real data defect) from "loaded" (example-doctrine-repo's policy, wherever it resolved
    from) -- see module docstring's fail-closed split. `policy_path` is the
    path this call looked for, reported even when `policy_source == "absent"`,
    since "the path we looked for and did not find" is the actionable half
    of an absent report.

    A caller building a reconcile-op report (JSON body, Markdown row, log
    line) merges this dict's two keys in rather than re-deriving them from
    `PolicyResult` by hand -- one call site, not a shape a caller reinvents
    per report format.
    """
    return {"policy_source": result.source, "policy_path": result.resolved_path}
