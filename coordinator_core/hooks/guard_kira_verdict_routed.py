"""
coordinator_core.hooks.guard_kira_verdict_routed — Stop-hook engine op,
the Kira (overengineering-reviewer) verdict-routing hard-stop.

Purpose: warm command/native-door counterpart of DoE-claude's
`coordinator/hooks/scripts/guard-kira-verdict-routed.py` — verbatim port of
its frontmatter-only decision logic (no YAML dependency, column-zero-only
line-scan). See the source script's own module docstring for the full
worked THE PROBLEM / TRIGGER SCOPE / THE DECISION write-up this module
implements without re-deriving it; kept in lockstep with that file's helper
names so a future diff against the source is mechanical.

Extraction note (W4-C14, docs/plans/2026-09-18-doe-holds-no-scripts.md):
this decision logic previously lived ONLY inline inside
`coordinator_core.hooks.stop_dispatch` (docs/plans/2026-08-31-six-hook-
scripts-become-engine-ops.md chunk C3), composed as a private, unregistered
helper (`@register_op` deliberately removed there — see that module's own
git-blame comment) because no independent DoE-side caller existed for the
op key at the time. This chunk gives it its own module and its own
registered op (`hooks.guard_kira_verdict_routed`) so `hook-run` (W4-C16) can
dial it directly per the W4-C1 verdict (command/native-door for every hook
in this row) — `stop_dispatch` now imports `_guard_kira_verdict_routed_handler`
from here instead of defining it inline, so the fan-in composition is
unchanged, just no longer duplicated.

Op contract: `params` reaches this op in either shape a `hooks.*` handler
receives — wrapped as `params["payload"]` by both engine doors, flat by the
cold chain; `_envelope.payload_of` reads both, supplying the Stop payload
dict (`session_id`, `cwd`, `agent_id`, `stop_hook_active`, ...) — never
`os.environ` or this process's own `cwd`. Returns `deny("Stop", <reasons>)`
when it fires,
`post_advisory(<text>)` on a fail-OPEN could-not-evaluate path (mirrors the
source script's own stdout breadcrumb, never blocking on its own inability
to read a fact), `no_advisory()` otherwise.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/guard-kira-verdict-routed.py
"""

from __future__ import annotations

import os
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, post_advisory
from coordinator_core.ipc import register_op
from coordinator_core.session.machinery_paths import share_dirs as _share_dirs

# script's own CONTRACT_EPOCH section. Delete this constant and
_KIRA_CONTRACT_EPOCH_ISO = "2026-08-30T00:00:00Z"

_KIRA_AGENT_TYPE = "overengineering-reviewer"


def _kira_repo_root(payload: dict) -> "Optional[str]":
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return None
    return show_toplevel(cwd)


def _kira_read_frontmatter(path: str) -> dict:
    """Flat, stdlib-only top-level `key: value` line-scan of the YAML
    frontmatter block — verbatim port of the source script's own
    `_read_frontmatter`. Only COLUMN-ZERO keys are read; returns `{}` on any
    read/shape failure — a guard that cannot prove a fact must never block
    on it."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    body = lines[1:end]
    meta: dict = {}
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0].isspace():
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if "#" in rest:
            rest = rest.split("#", 1)[0].strip()
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1]
            meta[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
            i += 1
            continue
        if rest in ("", "{}"):
            items: list = []
            j = i + 1
            while j < len(body):
                candidate = body[j]
                if not candidate.strip() or candidate.lstrip().startswith("#"):
                    j += 1
                    continue
                if not candidate.lstrip().startswith("- "):
                    break
                items.append(candidate.strip()[2:].strip().strip("'\""))
                j += 1
            if items:
                meta[key] = items
                i = j
                continue
            meta[key] = rest
            i += 1
            continue
        meta[key] = rest.strip("'\"")
        i += 1
    return meta


def _kira_postdates_epoch(meta: dict) -> bool:
    spawned = meta.get("spawned_at")
    if not isinstance(spawned, str) or not spawned:
        return True
    return spawned >= _KIRA_CONTRACT_EPOCH_ISO


def _kira_to_int(value) -> "Optional[int]":
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kira_stem(filename: str) -> str:
    return filename[:-3] if filename.endswith(".md") else filename


def _kira_normalize_agent_type(agent_type) -> "Optional[str]":
    if not isinstance(agent_type, str) or not agent_type:
        return None
    if ":" in agent_type:
        return agent_type.split(":", 1)[1]
    return agent_type


def _kira_is_kira(filename: str, meta: dict) -> bool:
    return _kira_normalize_agent_type(meta.get("agent_type")) == _KIRA_AGENT_TYPE


def _kira_is_review_activity(filename: str, meta: dict) -> bool:
    if "findings_count" in meta:
        return True
    kind = meta.get("kind")
    if kind in ("review-findings", "staff-eng-review"):
        return True
    agent_type = meta.get("agent_type", "")
    if isinstance(agent_type, str) and "review" in agent_type.lower():
        return True
    return "review" in filename.lower()


def _kira_block_condition_1(in_scope: list) -> bool:
    kira_present = any(_kira_is_kira(f, m) for f, m in in_scope)
    if kira_present:
        return False
    return any(
        _kira_is_review_activity(f, m) and not _kira_is_kira(f, m) for f, m in in_scope
    )


def _kira_find_answers(kira_filename: str, in_scope: list) -> list:
    stem = _kira_stem(kira_filename)
    answers: list = []
    for f, m in in_scope:
        if f == kira_filename:
            continue
        integrated = m.get("integrated_from")
        if isinstance(integrated, str):
            integrated = [integrated] if integrated.strip() else []
        if not isinstance(integrated, list):
            continue
        if stem not in integrated and kira_filename not in integrated:
            continue
        answers.append(f)
    return answers


def _kira_unstamped_integrators(in_scope: list, plan: "Optional[str]" = None) -> list:
    """Sidecars carrying a SPAWN-time `integrator_receipt` with no
    `integrated_from` of their own, paired against the reviewer's OWN
    `plan:` field rather than the whole session.

    This is a "an integrator was born" signal ONLY, never a "this verdict
    is being handled" one — `_receipt_block`'s splice fires at spawn, before
    the child has done or reported any work (module docstring of
    `provision_report.py`), and a receipt directory is shared by every
    review the session touches, misfiled sidecars included (issue #47).
    Naming this verdict is the only thing `_kira_find_answers` already
    checks for via `integrated_from`; a caller MUST NOT read this list as
    evidence that any one of these sidecars answers a PARTICULAR verdict —
    see `_guard_kira_verdict_routed`'s own reasons loop, which surfaces this
    list as an FYI count only, never as a "do not re-dispatch" claim.

    `plan` is the reviewer (Kira) sidecar's own `plan:` value. A session
    routinely runs multiple unrelated plans concurrently; an integrator
    spawned for a DIFFERENT plan is not evidence about THIS verdict, so it
    is excluded when `plan` is a non-empty string. `plan is None` (the
    field could not be read) falls back to the old session-wide count
    rather than silently reporting zero."""
    candidates = [
        (f, m)
        for f, m in in_scope
        if "integrator_receipt" in m and not m.get("integrated_from")
    ]
    if not isinstance(plan, str) or not plan.strip():
        return [f for f, m in candidates]
    return [f for f, m in candidates if m.get("plan") == plan]


_KIRA_BLOCK_HEADER = (
    "[guard] This close carries an unrouted Kira (overengineering-reviewer) "
    "verdict.\n"
)


def _guard_kira_verdict_routed(payload: dict) -> dict:
    """Stop guard: hard-stop a close whose Kira (overengineering-reviewer)
    verdict was never routed anywhere. Verbatim decision port of
    `guard-kira-verdict-routed.py::main()` — see that script's module
    docstring for the full THE PROBLEM / TRIGGER SCOPE / THE DECISION
    write-up this function implements without re-deriving it.

    Returns `deny("Stop", <reasons>)` when it fires, `post_advisory(<text>)`
    on a fail-OPEN could-not-evaluate path (mirrors the source script's own
    stdout breadcrumb, never blocking on its own inability to read a fact),
    `no_advisory()` otherwise.
    """
    if not isinstance(payload, dict):
        return no_advisory()

    if payload.get("agent_id"):
        return no_advisory()
    if payload.get("stop_hook_active"):
        return no_advisory()

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return post_advisory(
            "[guard] guard-kira-verdict-routed could not evaluate: "
            "no session_id in the Stop payload"
        )

    repo_root = _kira_repo_root(payload)
    if repo_root is None:
        return post_advisory(
            "[guard] guard-kira-verdict-routed could not evaluate: "
            "could not resolve repo root from cwd"
        )

    share_dirs = _share_dirs(repo_root, session_id)
    entries = []
    listed_any = False
    unreadable: list = []
    for share_dir in share_dirs:
        try:
            filenames = [
                f
                for f in os.listdir(share_dir)
                if f.endswith(".md") and not f.endswith(".blocks.md")
            ]
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(share_dir)
            continue
        listed_any = True
        for fname in filenames:
            meta = _kira_read_frontmatter(os.path.join(share_dir, fname))
            entries.append((fname, meta))

    if unreadable and not listed_any:
        return post_advisory(
            "[guard] guard-kira-verdict-routed could not evaluate: "
            "could not list share dir " + ", ".join(unreadable)
        )

    if not entries:
        return no_advisory()

    in_scope = [(f, m) for f, m in entries if _kira_postdates_epoch(m)]
    if not in_scope:
        return no_advisory()

    reasons: list = []

    if _kira_block_condition_1(in_scope):
        reasons.append(
            "- Other review activity ran this session, but no Kira "
            "(overengineering-reviewer) sidecar is present. Kira fires on "
            "every close (SKILL.md); dispatch her before closing."
        )

    kira_entries = [(f, m) for f, m in in_scope if _kira_is_kira(f, m)]
    for kira_file, kira_meta in kira_entries:
        findings_count = _kira_to_int(kira_meta.get("findings_count"))
        answers = _kira_find_answers(kira_file, in_scope)

        if findings_count is not None and findings_count > 0 and not answers:
            unstamped_count = len(
                _kira_unstamped_integrators(in_scope, plan=kira_meta.get("plan"))
            )
            unstamped_note = (
                f" ({unstamped_count} integrator sidecar(s) in this session "
                "carry a spawn-time integrator_receipt with no integrated_from "
                "naming this verdict — spawned is not evidence of routed; do "
                "not treat them as already handling it.)"
                if unstamped_count
                else ""
            )
            reasons.append(
                f"- {kira_file} stamps findings_count={findings_count} with no "
                f"sibling sidecar's integrated_from naming it. No integrator "
                f"has claimed this verdict.{unstamped_note} Owed route: "
                f"review-integrator, or a refactor executor if the verdict "
                f"recommended a rebuild."
            )

    if not reasons:
        return no_advisory()

    return deny("Stop", _KIRA_BLOCK_HEADER + "\n".join(reasons))


@register_op("hooks.guard_kira_verdict_routed")
def _guard_kira_verdict_routed_handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    try:
        return _guard_kira_verdict_routed(dict(payload))
    except Exception:
        return no_advisory()
