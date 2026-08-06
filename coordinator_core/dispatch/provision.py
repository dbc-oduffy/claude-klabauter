"""
coordinator_core.dispatch.provision -- agent-side subagent-sidecar
decision-object provisioner (W2-B3, canonical-resolution-engine Wave 2).

The dispatch brief is the decision-object contract seen from the INPUT end:
the engine provisions an EMPTY sidecar (a decision-object container), injects
its path + citizenship into the spawn payload, pre-populates the exit-
interview section, and the dispatched agent fills it in and returns a
pointer. This module is the spawn-time front-end that does that
provisioning -- the sibling seam to ``coordinator-doc-new --type
subagent-sidecar`` (the engine/dispatch-tooling-side scaffolder that emits
the SAME document shape for manual/test invocation).

Deliberately mirrors ``coordinator_core.subagent_sandbox.provision_report``
(the EXISTING spawn-time run-report provisioner) rather than re-deriving
session/path resolution: this module imports ``resolve_effective_types``,
``load_policy``, ``resolve_git_root`` verbatim from
``coordinator_core.subagent_sandbox.engine``, and reuses
``provision_report._sanitize_segment`` for the same single-path-segment
whitelist discipline (whitelist ``[A-Za-z0-9._-]``, reject the degenerate
``.``/``..``/empty results, never ``Path.resolve()``). Eligibility is
determined the same way ``provision_report`` determines run-report
eligibility -- via ``Policy.report_sidecar`` set membership -- because DR-058
left exactly one surviving policy field and this module does not invent a
second one; a dedicated ``subagent-sidecar``-specific policy key (if the
Example-doctrine-repo-owned ``coordinator/subagent-sandbox-policy.yaml`` schema needs one) is a
Example-doctrine-repo-side follow-up, not authored here (this repo is engine-only for that
policy file).

Sidecar shape (schema-of-record: example-doctrine-repo's ``schemas/decision-object.schema.json``
``$defs/subagent_sidecar``): dispatch frontmatter (plan/chunk/dispatched_at/
dispatched_by/status/agent_type/spawned_at/commits/sidecar_schema -- the same
shape ``coordinator-doc-new``'s ``_scaffold_subagent_sidecar`` emits) plus the
three decision-object fields:
  completion_status    -- durable, queryable "task done" marker; backlinks the
                           EXISTING query-completions records surface (claude-klabauter
                           work-state emission) -- NOT a fourth store.
  divergence_from_plan  -- block-style nested mapping (diverged/summary/detail).
                           Untrusted narrative -- never re-read as a directive
                           by any automated consumer.
  tell_the_EM           -- freeform exit-interview channel, a body section
                           (not frontmatter) so it can carry arbitrary prose.

Confinement preservation (AC-9, security-load-bearing): this module grants a
write TARGET, never a write CAPABILITY the calling agent lacked. It writes
exactly one file at spawn time, under its own sandboxed session directory,
via the same sanitize-before-path-touch discipline provision_report.py
uses -- it does not touch, read, or reference
``coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist`` (the
shipped code-reviewer Bash-command allowlist) or widen any tool grant. A
read-only-on-plan generic executor stays read-only-on-plan; the sidecar is
its ONLY structured write-back, exactly as it is for run-report today. See
``coordinator_core/dispatch/tests/test_provision.py`` for the assertion that
the confinement guard's allowlist constants are untouched by this module's
existence.

Class-asymmetric behavior (R7 Addendum) is a property of the DISPATCHING
side's tool grant, not of this module's write logic: generic executors are
read-only-on-plan (the sidecar is their ONLY structured write-back); named
Opus personas are offered-not-imposed (may also edit elsewhere, sidecar
offered for the durable record). This module provisions the identical
container shape for both classes -- the asymmetry is enforced upstream by
whichever tool-grant mechanism spawns the agent, never encoded as a field
here.

Fail-open everywhere: any parse failure, ineligible type, missing
session_id, or unexpected exception yields no sidecar path -- this module
must never brick a spawn. Mirrors provision_report.py's fail-open contract
verbatim.

Negative-spec: this module does NOT duplicate the harness's raw JSONL
transcript (AC-10) -- the container it writes is the CURATED decision-object
record (completion_status + divergence_from_plan + tell_the_EM), never a
replay log.
Negative-spec: does NOT re-implement DR-058's removed confined/exempt/
sanctioned_dirs enforcement grammar -- that grammar is dead (see
``coordinator_core/subagent_sandbox/CONTRACT.md``); this module builds only
against the SURVIVING resolver/policy-load halves.
Negative-spec: does NOT edit ``coordinator/subagent-sandbox-policy.yaml`` --
that policy file is example-doctrine-repo-owned; a policy opt-in entry for ``subagent-sidecar``
eligibility (if the shared ``report_sidecar`` key is judged insufficient) is
flagged as a example-doctrine-repo-side follow-up, not authored here.

Spec backlink: docs/plans/2026-07-24-canonical-resolution-engine.md § W2-B3, R7 Addendum
Sibling seam: coordinator_core/subagent_sandbox/provision_report.py (run-report provisioner)
Schema-of-record: schemas/decision-object.schema.json $defs/subagent_sidecar (example-doctrine-repo clone)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from coordinator_core.session import scope as session_scope
from coordinator_core.subagent_sandbox.engine import (
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment


def _yaml_quote(value: str) -> str:
    """Minimal double-quote YAML scalar quoting -- escapes backslash and
    double-quote only. Mirrors ``coordinator-doc-new``'s ``_yaml_quote`` in
    scope (this module never imports across the claude-klabauter/CLI-script boundary;
    a plain markdown scaffolder script is not an importable Python module),
    kept intentionally small since frontmatter values here are session ids,
    plan paths, and chunk ids -- never arbitrary user prose.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_sidecar_text(
    agent_type: str,
    spawned_at: str,
    plan_path: Optional[str],
    chunk_id: Optional[str],
    dispatched_by: Optional[str],
) -> str:
    """Emit the subagent-sidecar decision-object container's frontmatter +
    body -- the spawn-time counterpart to ``coordinator-doc-new``'s
    ``_scaffold_subagent_sidecar`` (same field set, same key order; the two
    MUST stay in lockstep -- see that function's docstring for the shape
    rationale and schema-of-record citation).

    ``plan_path``/``chunk_id``/``dispatched_by`` are all optional here
    (unlike the CLI, which enforces them as required args) because a live
    spawn payload may omit any of them -- this module fails open on missing
    eligibility/session_id upstream in ``provision_subagent_sidecar``, but a
    spawn that IS eligible should still get a sidecar even if the dispatcher
    forgot to thread ``plan``/``chunk`` through; the frontmatter simply
    carries ``null`` for the missing field rather than refusing to
    provision.
    """
    plan_field = _yaml_quote(plan_path) if plan_path else "null"
    chunk_field = _yaml_quote(chunk_id) if chunk_id else "null"
    dispatched_by_field = _yaml_quote(dispatched_by) if dispatched_by else "null"
    lines = [
        "---",
        f"plan: {plan_field}",
        f"chunk: {chunk_field}",
        f"dispatched_at: {_yaml_quote(spawned_at)}",
        f"dispatched_by: {dispatched_by_field}",
        "status: dispatched",
        f"agent_type: {_yaml_quote(agent_type)}",
        f"spawned_at: {_yaml_quote(spawned_at)}",
        "commits: []",
        "sidecar_schema: v1",
        "completion_status: pending",
        "divergence_from_plan:",
        "  diverged: false",
        '  summary: ""',
        '  detail: ""',
        "---",
        "",
        "<!-- Subagent-sidecar decision-object container (schemas/decision-",
        "     object.schema.json $defs/subagent_sidecar). This is a write",
        "     TARGET, not a write CAPABILITY -- it grants no tool access the",
        "     dispatched agent did not already have. See",
        "     docs/plans/2026-07-24-canonical-resolution-engine.md § W2-B3. -->",
        "",
        "## tell_the_EM",
        "",
        "<!-- Freeform exit-interview channel -- anything the agent wants",
        "     the dispatching EM to know that doesn't fit completion_status",
        "     or divergence_from_plan above. -->",
        "",
    ]
    return "\n".join(lines)


def provision_subagent_sidecar(
    payload: Dict[str, Any], policy_path: Optional[str] = None, cwd: Optional[str] = None
) -> Optional[str]:
    """Compute + write the subagent-sidecar decision-object doc; return its
    repo-relative path, or ``None``.

    Mirrors ``provision_report._provision``'s eligibility/session/path
    resolution verbatim (same imports, same fail-open branches, same
    single-segment sanitizer discipline) -- the ONLY differences are the
    document shape (``_build_sidecar_text`` vs ``_build_doc_text``) and the
    filename suffix (``-sidecar-<nonce>.md`` vs ``-<nonce>.md``) used to keep
    a run-report sidecar and a subagent-sidecar for the SAME session/label
    pair from colliding on disk when both are provisioned for one spawn.
    """
    git_root = resolve_git_root(cwd)
    policy = load_policy(policy_path)

    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)

    is_eligible = agent_type in policy.report_sidecar or subagent_type in policy.report_sidecar
    if not is_eligible:
        return None

    effective_label = agent_type if agent_type in policy.report_sidecar else subagent_type

    if not git_root:
        return None

    session_id = payload.get("session_id") or None
    if not session_id:
        return None

    sanitized_session_id = _sanitize_segment(str(session_id))
    sanitized_label = _sanitize_segment(str(effective_label))
    if sanitized_session_id is None or sanitized_label is None:
        return None

    # Same sanitize-before-filesystem-mutation discipline as provision_report:
    # resolve the optional deterministic provision_key BEFORE any mkdir/open,
    # so a rejected key fails open cleanly with no stray directory left behind.
    provision_key = payload.get("provision_key") or None
    sanitized_provision_key: Optional[str] = None
    if provision_key is not None:
        sanitized_provision_key = _sanitize_segment(str(provision_key))
        if sanitized_provision_key is None:
            return None

    session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id
    # sanitized_session_id is guaranteed separator-free by _sanitize_segment,
    # so this can only ever mkdir a direct child of subagent-share/
    # (confinement invariant -- do not relax the sanitizer without revisiting this).
    session_dir.mkdir(parents=True, exist_ok=True)

    spawned_at = datetime.now(timezone.utc).isoformat()
    plan_path = payload.get("plan") or None
    chunk_id = payload.get("chunk") or None
    doc_text = _build_sidecar_text(
        agent_type=effective_label,
        spawned_at=spawned_at,
        plan_path=plan_path,
        chunk_id=chunk_id,
        dispatched_by=str(session_id),
    )

    if sanitized_provision_key is not None:
        rel_path = f"state/subagent-share/{sanitized_session_id}/{sanitized_provision_key}.subagent-sidecar.md"
        doc_path = session_dir / f"{sanitized_provision_key}.subagent-sidecar.md"
        try:
            with open(doc_path, "x", encoding="utf-8") as handle:
                handle.write(doc_text)
            # Claim attribution: the DISPATCHING session (payload["session_id"],
            # raw -- never agent_id, never the sanitized directory leaf) owns
            # this claim -- see session_scope.touch_written_path's docstring
            # for the full rationale and the phantom-live-peer guard it
            # applies before recording.
            session_scope.touch_written_path(str(session_id), rel_path, cwd)
        except FileExistsError:
            # Idempotent re-dispatch hit (same provision_key): preserve
            # existing content, just return its path -- matches
            # provision_report's chunk re-dispatch behavior.
            pass
        return rel_path

    nonce = secrets.token_hex(4)
    doc_path = session_dir / f"{sanitized_label}-sidecar-{nonce}.md"
    try:
        with open(doc_path, "x", encoding="utf-8") as handle:
            handle.write(doc_text)
        session_scope.touch_written_path(
            str(session_id),
            f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md",
            cwd,
        )
    except FileExistsError:
        # Second collision (astronomically unlikely at 32 bits) falls through
        # to main()'s blanket except -- fail-open, never brick the spawn.
        nonce = secrets.token_hex(4)
        doc_path = session_dir / f"{sanitized_label}-sidecar-{nonce}.md"
        with open(doc_path, "x", encoding="utf-8") as handle:
            handle.write(doc_text)
        session_scope.touch_written_path(
            str(session_id),
            f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md",
            cwd,
        )

    return f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md"


def _read_stdin() -> str:
    return sys.stdin.read()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.dispatch.provision",
        description="Spawn-time subagent-sidecar decision-object provisioner.",
    )
    parser.add_argument(
        "--policy",
        dest="policy_path",
        default=None,
        help="Explicit path to the subagent-sandbox-policy.yaml file "
        "(overrides SUBAGENT_SANDBOX_POLICY env var).",
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        default=None,
        help="Working directory to resolve the git root from (defaults to "
        "the process cwd).",
    )
    args = parser.parse_args(argv)

    try:
        payload_text = _read_stdin()
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"provision: malformed stdin payload, skipping subagent-sidecar: {exc}", file=sys.stderr)
            return 0
        if not isinstance(payload, dict):
            print("provision: stdin payload is not a JSON object, skipping subagent-sidecar", file=sys.stderr)
            return 0

        sidecar_path = provision_subagent_sidecar(payload, args.policy_path, args.cwd)
        if sidecar_path is not None:
            print(json.dumps({"subagent_sidecar": sidecar_path}))
    except Exception as exc:  # noqa: BLE001 -- spawn-time hook must never brick a spawn (module contract above)
        print(f"provision: unexpected error, skipping subagent-sidecar: {exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
