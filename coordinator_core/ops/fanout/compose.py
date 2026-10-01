"""
coordinator_core.ops.fanout.compose — manifest to ordered create_session argument objects.

Purpose: turn one validated fanout-manifest.v1 into the per-worker `create_session` argument
objects a caller issues, in manifest worker order, each carrying exactly CREATE_SESSION_ARG_KEYS.

Negative spec: pure and deterministic — no file, process or network access beyond the cached
schema load in `contract`, no clock, no randomness, and no op registration.
"""

from __future__ import annotations

from typing import Any

from coordinator_core.ops.fanout import contract, transport


def _boot_declaration(manifest: dict, worker: dict) -> str:
    roster = ", ".join(contract.effective_roster(manifest, worker))
    return "\n".join(
        [
            "Fanout boot declaration:",
            f"- job: {manifest['job_id']}",
            f"- worker: {worker['id']}",
            f"- focus: {worker['focus']}",
            f"- repos: {roster}",
        ]
    )


def _prompt(manifest: dict, worker: dict) -> str:
    parts = [
        _boot_declaration(manifest, worker),
        transport.for_channel(manifest["channel"]).child_prompt_block(manifest, worker),
    ]
    if worker.get("outcome_branch"):
        parts.append(f"Outcome branch: {worker['outcome_branch']}")
    parts.append(worker["prompt"])
    return "\n\n".join(parts)


def compose(manifest: Any) -> contract.ComposeResult:
    """Validate `manifest` (object or YAML string) and return {job_id, actions[]} in worker order."""
    manifest = contract.validate_manifest(manifest)
    job_id = manifest["job_id"]
    actions: list[contract.ComposeAction] = []
    for worker in manifest["workers"]:
        args: contract.CreateSessionArgs = {
            "title": contract.session_title(job_id, worker["id"]),
            "source_url": worker["source_url"],
            "environment_id": manifest["environment_id"],
            "model": manifest["model"],
            "permission_mode": manifest["permission_mode"],
            "tags": contract.worker_tags(manifest, worker),
            "prompt": _prompt(manifest, worker),
        }
        actions.append(
            {
                "worker_id": worker["id"],
                "idempotency_key": contract.idempotency_key(job_id, worker["id"]),
                "create_session": args,
            }
        )
    return {"job_id": job_id, "actions": actions}
