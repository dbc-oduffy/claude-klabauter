"""
coordinator_core.reconcile.policy_loader -- coordinator-claude-owned auto-reconcile policy reader.

Purpose: reads the coordinator-claude-owned `coordinator/auto-reconcile-policy.yaml` (coordinator-claude
authors it; claude-klabauter NEVER writes it) and validates it against claude-klabauter's grammar
pin (`coordinator_core/contract/auto-reconcile-policy.grammar.md`) before
handing it to C2's commit_reality matcher / C3's gate_eval evaluator. Mirrors
the `subagent-sandbox-policy.yaml` <- `coordinator_core/subagent_sandbox`
read pattern (DR-047 contract-vs-engine split): coordinator-claude owns the policy DATA,
Claude-klabauter owns the reading/validating MACHINE and the grammar it validates
against.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C9
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
    is the expected steady state until coordinator-claude authors the yaml; logging a
    warning every workday-start during the pre-ratification period is noise.
  - policy file PRESENT-but-MALFORMED (grammar-pin validation fails) ->
    conservative no-auto-ship policy + a surfaced data-defect warning. This
    is a real defect coordinator-claude should hear about, distinct from the expected-absent
    case above.

`policy_report_fields(result)` (§ C10 / AC16) flattens `PolicyResult.source`
and `.resolved_path` into the two fields a downstream reconcile report must
surface -- landed because coordinator-claude named the un-reported `source` split as
the cheap engine-side fix that would have told a starvation-report reader
which of "absent"/"malformed"/"loaded" a run was in, one line, no cross-repo
round-trip (`cross-repo/inbox/2026-07-28-coordinator-claude-em-handoff-terminal-
starvation-answers.md`).

Repo-resident overlay (route 3 of 4, see § Overlay in the grammar pin):
  `_resolve_policy_path` also discovers `<repo-root>/auto-reconcile-policy.
  local.yaml`, repo root found via a spawn-free `.git` walk-up from the
  process cwd -- never a `git rev-parse` subprocess, since this loader sits
  on the session-boot and pickup hot paths. When discovered, the overlay is
  merged over the route-4 floor key by key (a top-level `dict.update`, not a
  deep merge) and grammar validation runs against the MERGED result, not the
  overlay alone -- a partial overlay restating a single key is valid because
  the floor supplies the rest. `policy.setdefault("auto_ship_enabled",
  False)` still runs LAST, after the merge, so no overlay can arm auto-ship
  by omission. If the floor file exists but fails to read/parse (or its root
  isn't a mapping), the merge does NOT silently fall back to an absent-
  equivalent `{}` -- that would risk a merged-and-validated "loaded" result
  built from only the overlay's own keys, losing the malformed-floor signal
  `source` exists to carry. This case reports `source="malformed"` with a
  floor-specific warning instead, same loudness as any other malformed
  branch.

Negative-spec:
  - Does NOT vendor or copy the policy YAML -- reads it fresh from disk (or
    the injected path) on every call, matching the subagent_sandbox precedent.
  - Does NOT auto-ship on any fail-closed branch (absent OR malformed) -- both
    return `dry_run: True` / `auto_ship_enabled: False`; only the warning
    differs between the two branches.
  - Does NOT write the policy file -- coordinator-claude is the sole author. This includes
    the repo-resident overlay: claude-klabauter discovers and reads it, never authors
    or scaffolds it.
  - Does NOT deep-merge the overlay over the floor -- the merge is a
    top-level `dict.update`; `three_signal`'s sub-keys are one key's value,
    not independently merged.
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

#: Env var a coordinator-claude-side caller may inject the primary policy path through,
#: mirroring subagent_sandbox.engine.POLICY_ENV_VAR.
POLICY_ENV_VAR = "AUTO_RECONCILE_POLICY"

#: Best-effort default relative path when no explicit path/env var resolves,
#: matching the coordinator-claude-side repo layout cited in the plan
#: (`coordinator/auto-reconcile-policy.yaml`).
_DEFAULT_POLICY_ENV_CANDIDATES = ("CLAUDE_PLUGIN_ROOT",)
_DEFAULT_POLICY_RELATIVE = "auto-reconcile-policy.yaml"

#: Repo-resident overlay filename, discovered at the consuming repo's own
#: root (never under `coordinator/`, never `CLAUDE_PLUGIN_ROOT`-relative).
#: See § Overlay in the grammar pin.
_OVERLAY_RELATIVE = "auto-reconcile-policy.local.yaml"

#: Grammar-pinned top-level keys (see the .grammar.md doc for full shape).
_REQUIRED_KEYS = ("three_signal", "mechanical_commit_denylist", "cross_handoff_attribution", "dry_run")


@dataclass(frozen=True)
class PolicyResult:
    """The outcome of a `load_policy` call.

    ``policy`` is always a usable dict (conservative fail-closed default when
    absent/malformed, or the validated coordinator-claude-authored data on success).
    ``warning`` is populated ONLY on the malformed branch -- absent-file and
    successful-load both leave it ``None`` (see module docstring fail-closed
    split).
    ``resolved_path`` is the policy path this call resolved and looked at,
    stringified, for ALL THREE ``source`` values -- including "absent", where
    it is "the path we looked for and did not find" (the actionable half of
    an absent report; see `policy_report_fields` below). ``None`` only when
    resolution itself found no candidate (no explicit path, no env var, and
    the `CLAUDE_PLUGIN_ROOT` default did not resolve to any path at all).

    Spec backlink: pln-handoff-close-path-fail-loud-b-db23e8 § C10
    (AC16) -- coordinator-claude's reply (`cross-repo/inbox/2026-07-28-coordinator-claude-em-
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


def _resolve_repo_root() -> Optional[Path]:
    """Walk up from the process cwd for a `.git` entry, spawn-free.

    Returns the first ancestor (starting at cwd) containing a `.git` entry,
    or ``None`` if none is found before the filesystem root. Deliberately a
    `pathlib` walk-up rather than a `git rev-parse` subprocess -- this loader
    sits on the session-boot and pickup hot paths under the machine load
    norm; see `_resolve_policy_path`'s overlay route and the module
    docstring's `subagent_sandbox.engine.resolve_git_root` precedent note.
    """
    current = Path.cwd()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _resolve_policy_path(policy_path: Optional[str]) -> tuple[Optional[Path], bool]:
    """Resolve the policy path, plus whether the resolution is the
    repo-resident overlay route (route 3 of 4).

    Resolution order:
      1. ``policy_path`` (explicit override).
      2. ``AUTO_RECONCILE_POLICY`` env var.
      3. `<repo-root>/auto-reconcile-policy.local.yaml`, discovered via a
         spawn-free `.git` walk-up from the process cwd. Absent file, or
         unresolvable repo root, falls through to route 4 unchanged.
      4. Best-effort ``CLAUDE_PLUGIN_ROOT``-relative default (the floor).

    Returns ``(None, False)`` when no route resolves.
    """
    candidates: List[Optional[str]] = [policy_path, os.environ.get(POLICY_ENV_VAR)]
    for candidate in candidates:
        if candidate:
            return Path(candidate), False

    repo_root = _resolve_repo_root()
    if repo_root is not None:
        overlay = repo_root / _OVERLAY_RELATIVE
        if overlay.is_file():
            return overlay, True

    return _resolve_default_policy_path(), False


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

    if "auto_ship_enabled" in data and not isinstance(data["auto_ship_enabled"], bool):
        defects.append("'auto_ship_enabled' must be a boolean")

    return defects


def load_policy(policy_path: Optional[str] = None) -> PolicyResult:
    """Load + grammar-validate the coordinator-claude-owned auto-reconcile policy.

    Resolution order mirrors ``subagent_sandbox.engine.load_policy``, plus a
    fourth repo-resident overlay route (see § Overlay in the grammar pin):
      1. ``policy_path`` (explicit override, e.g. a CLI/test injection).
      2. ``AUTO_RECONCILE_POLICY`` env var.
      3. `<repo-root>/auto-reconcile-policy.local.yaml`, discovered via a
         spawn-free `.git` walk-up from the process cwd. When discovered, it
         is merged over the route-4 floor key by key (a top-level
         `dict.update`, not a deep merge) and grammar validation runs
         against the MERGED result -- a partial overlay restating only one
         key is valid because the floor supplies the rest. A floor that
         exists but fails to read/parse reports `source="malformed"` rather
         than silently merging over `{}` (see module docstring).
      4. Best-effort ``CLAUDE_PLUGIN_ROOT``-relative default (the floor).

    Fail-closed on any failure to resolve/read/parse/validate -- see module
    docstring for the absent-vs-malformed warning split. Never raises.
    """
    resolved, is_overlay = _resolve_policy_path(policy_path)
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

    if is_overlay:
        if not isinstance(data, dict):
            overlay_defects = _validate_grammar(data)
            return PolicyResult(
                policy=_conservative_policy(),
                source="malformed",
                warning=(
                    f"auto-reconcile-policy.yaml at {resolved} failed grammar-pin "
                    f"validation: {'; '.join(overlay_defects)}"
                ),
                resolved_path=resolved_path_str,
            )
        floor_path = _resolve_default_policy_path()
        floor: Dict[str, Any] = {}
        floor_warning: Optional[str] = None
        if floor_path is not None and floor_path.is_file():
            try:
                floor_raw = floor_path.read_text(encoding="utf-8")
            except OSError as exc:
                floor_warning = f"auto-reconcile-policy.yaml floor at {floor_path} could not be read: {exc}"
            else:
                try:
                    floor_data = yaml.safe_load(floor_raw)
                except yaml.YAMLError as exc:
                    floor_warning = f"auto-reconcile-policy.yaml floor at {floor_path} is not valid YAML: {exc}"
                else:
                    if isinstance(floor_data, dict):
                        floor = floor_data
                    elif floor_data is not None:
                        floor_warning = f"auto-reconcile-policy.yaml floor at {floor_path} is not a mapping"
        if floor_warning is not None:
            # The plugin-floor route (route 4) is present but malformed: merging
            # over `{}` here would silently swallow the malformed-floor signal
            # `PolicyResult.source` exists to carry (a merged result could pass
            # grammar validation on the overlay's own keys alone and report
            # "loaded", losing the fact that the floor never actually merged
            # in). Surface it loud, same as any other malformed branch.
            return PolicyResult(
                policy=_conservative_policy(),
                source="malformed",
                warning=floor_warning,
                resolved_path=resolved_path_str,
            )
        merged = dict(floor)
        merged.update(data)
        data = merged

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
    # Fail-closed: absent key must resolve identically to the absent-file and
    # malformed-file branches (both `auto_ship_enabled: False`), so silence
    # never arms auto-ship. See cross-repo/inbox/2026-08-13-coordinator-claude-em-
    # grammar-pin-cannot-express-auto-ship-off.md.
    policy.setdefault("auto_ship_enabled", False)
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
    (expected steady state until coordinator-claude authors the yaml) from "malformed" (a
    real data defect) from "loaded" (coordinator-claude's policy, wherever it resolved
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
