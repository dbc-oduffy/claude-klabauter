"""
coordinator_core.ops.fleet.memo_redeliver -- memo.redeliver MUTATING op handler.

Purpose: deliver an EXISTING memo file (for example one stranded in a non-owning
clone) into its owner's `cross-repo/inbox/` with the file's bytes untouched --
`from`, `sent_by`, `created` and the basename survive -- as one object-only
delivery commit through `memo_send._deliver_upstream`, the primitive `memo.send`
uses. The owner is the file's own `to:`, resolved by `memo_wire.resolve_receiver`
(registry receiver plus `resolve_owning_box`); no second map.

Negative-spec:
  - Does NOT restamp: `memo_send._compose_delivered_content` is never called, so
    nothing is re-authored under the delivering session.
  - Does NOT apply `memo.draft`'s `self_addressed` refusal: delivering a memo whose
    `from` is this repo's own EM to another owner is the intended use.
  - Does NOT write when the owner has no reachable clone: an unresolvable,
    mirror-only, undeliverable or other-box owner returns a setup-error envelope
    with `rejection_class="owner_unreachable"` and the resolver's reason.
  - Does NOT write a sender-side receipt, outbox draft or sent record; the only
    sender-side write is the push-pending record `_deliver_upstream` leaves.
  - Does NOT overwrite: a path already in the owner's upstream inbox or archive is
    `collision`, except the same memo (reconciled, no second commit).
  - One file per call; no scan, no bulk mode.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import feature_refusal
from coordinator_core.ops.fleet import _upstream_delivery
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_anchor import ANCHOR_REF_PREFIX
from coordinator_core.ops.fleet.archive_actioned_memos import memo_archive_dest
from coordinator_core.ops.fleet.memo_send import (
    _HEADS_PREFIX,
    _LANDING_MODE_UPSTREAM_TIP,
    _SENT_BY_UNRESOLVED,
    _base_tree_has,
    _deliver_upstream,
    _landed_delivery,
    _unpushed_delivery,
    _upstream_probe,
    build_setup_error_result,
)
from coordinator_core.ops.fleet.memo_wire import WireRefusal, resolve_receiver

_MODE = "redeliver"

REJECTION_CLASS_OWNER_UNREACHABLE = "owner_unreachable"
REJECTION_CLASS_INVALID_MEMO = "invalid_memo"

_KNOWN_PARAM_KEYS = frozenset({"dry_run", "path"})

MUTATES = [
    "cross-repo/inbox/*.md",
    "state/cross-repo/inbox/*.md",
]


def _refusal(dry_run: bool, reason: str, rejection_class: str) -> dict:
    """A setup-error envelope carrying `rejection_class`; nothing was written."""
    result = build_setup_error_result(_MODE, dry_run, f"memo.redeliver: {reason}")
    result["rejection_class"] = rejection_class
    return result


def _validate_params(params: dict):
    """`(dry_run, source_path)` or a setup-error envelope."""
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run, "memo.redeliver: dry_run must be bool, got " + repr(type(dry_run).__name__)
        )
    unknown = set(params) - _KNOWN_PARAM_KEYS
    if unknown:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.redeliver: unrecognized param(s) {sorted(unknown)} -- known params: {sorted(_KNOWN_PARAM_KEYS)}.",
        )
    path = params.get("path")
    if not path or not isinstance(path, str):
        return build_setup_error_result(_MODE, dry_run, "memo.redeliver: path is required (non-empty string)")
    return dry_run, Path(path)


def _read_memo(source: Path) -> "tuple[Optional[str], Optional[dict], Optional[str]]":
    """`(content, frontmatter, None)` for a memo file with string `from:` and `to:`, else
    `(None, None, reason)`. Content is the file's text unmodified (newlines kept)."""
    if source.suffix != ".md":
        return None, None, f"{source.name!r} is not a .md memo file"
    try:
        content = source.read_bytes().decode("utf-8")
    except (OSError, ValueError) as exc:
        return None, None, f"cannot read {source}: {exc}"
    fm = parse_frontmatter(content).get("frontmatter") or {}
    for key in ("from", "to"):
        value = fm.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, None, f"{source.name} has no string {key!r} in its frontmatter"
    return content, fm, None


@register_op("memo.redeliver")
def _memo_redeliver(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.redeliver' MUTATING op handler.

    Params:
        dry_run (bool, optional, default false): preview vs. act.
        path    (str, required): the existing memo file to deliver; its `to:` names the owner.

    repo_root: git common dir (`common_dir`-keyed) -- the delivering clone, whose outbox
    holds the push-pending record. The owner is registry-derived from the memo's `to:`.
    Failure envelopes carry `rejection_class`: `owner_unreachable` | `invalid_memo`
    (setup errors, nothing written) or `collision` | `declined` (failed rows).
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated
    dry_run, source = validated

    refusal = feature_refusal("cross_repo_memos")
    if refusal:
        return build_setup_error_result(_MODE, dry_run, refusal)
    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.redeliver: no repo_root supplied -- the delivering clone is required (common_dir-keyed op).",
        )
    sender_worktree = main_worktree_root(Path(repo_root))

    content, fm, read_error = _read_memo(source)
    if read_error is not None:
        return _refusal(dry_run, read_error, REJECTION_CLASS_INVALID_MEMO)

    to = fm["to"]
    from_id = fm["from"]
    target = resolve_receiver(to)
    if isinstance(target, WireRefusal):
        return _refusal(dry_run, f"owner of {source.name} unreachable -- {target.message}", REJECTION_CLASS_OWNER_UNREACHABLE)

    filename = source.name
    receiver_repo_path = target.receiver_repo_path
    target_file = target.inbox_dir / filename
    rel_path = os.path.relpath(target_file, receiver_repo_path).replace(os.sep, "/")
    archive_file = memo_archive_dest(receiver_repo_path, target_file)
    archive_rel = os.path.relpath(archive_file, receiver_repo_path).replace(os.sep, "/")
    probe = _upstream_probe(receiver_repo_path)
    in_inbox = _base_tree_has(receiver_repo_path, probe.base, rel_path)
    in_archive = not in_inbox and (
        _base_tree_has(receiver_repo_path, probe.base, archive_rel) or archive_file.exists()
    )
    collision_exists = in_inbox or in_archive
    collision_path = archive_file if in_archive else target_file

    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_file),
            "to": to,
            "target_path": str(target_file),
            "landing_mode": _LANDING_MODE_UPSTREAM_TIP,
            "landing_ref": _HEADS_PREFIX + probe.branch,
            "landing_base": probe.base,
            "collision": collision_exists,
        }])

    sent_by = fm.get("sent_by")
    sent_by = sent_by if isinstance(sent_by, str) and sent_by.strip() else _SENT_BY_UNRESOLVED
    topic = source.stem

    reconciled = None
    if collision_exists:
        reconciled = _landed_delivery(receiver_repo_path, target_file, filename, content)
        if reconciled is None:
            return build_act_result(_MODE, [], [], [{
                "id": str(target_file),
                "rejection_class": "collision",
                "reason": f"collision: {collision_path} already exists in the owner's inbox or archive -- refuse (no clobber).",
            }])
    else:
        reconciled = _unpushed_delivery(sender_worktree, receiver_repo_path, rel_path, filename, content)

    acted_item = {"id": str(target_file), "written": True, "committed": True}
    if reconciled is not None:
        acted_item.update(
            delivery_commit_sha=reconciled.sha,
            anchored=reconciled.sha is not None,
            reconciled=True,
        )
        if reconciled.sha is not None:
            acted_item["anchor_ref"] = ANCHOR_REF_PREFIX + filename + "/" + reconciled.sha
        return build_act_result(_MODE, [acted_item], [], [])

    delivered = _deliver_upstream(
        receiver_repo_path=receiver_repo_path, sender_worktree=sender_worktree,
        rel_path=rel_path, content=content, topic=topic, from_id=from_id, sent_by=sent_by,
    )
    if delivered.status == _upstream_delivery.STATUS_COLLISION:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "rejection_class": "collision",
            "reason": f"collision: {target_file} already exists in the owner's upstream tree -- refuse (no clobber).",
        }])
    if not delivered.ok:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "rejection_class": "declined",
            "reason": f"owner-side delivery declined: {delivered.error} -- not retried.",
        }])
    acted_item.update(
        delivery_commit_sha=delivered.commit_sha,
        anchored=True,
        anchor_ref=delivered.anchor_ref,
        push_pending_record=str(delivered.record_path),
    )
    return build_act_result(_MODE, [acted_item], [], [])
