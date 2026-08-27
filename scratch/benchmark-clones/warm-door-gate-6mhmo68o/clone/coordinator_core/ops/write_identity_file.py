"""
coordinator_core.ops.write_identity_file — JSON-RPC "install.write_identity_file"
operation.

Purpose: native port of the ad hoc `mktemp ~/.claude/coordinator-identity.yaml.XXXXXX`
+ `cat > "$_tmp" <<EOF ... EOF` + `mv "$_tmp" ...` shell fence that
`commands/install.md` (coordinator-claude) hand-rolls twice — once for
`operator_name` (Step 3) and again for `engagement_posture` (Step 3b-4) — to
persist `~/.claude/coordinator-identity.yaml`. Both call sites fold into this
ONE `write_identity_file(claude_home, fields)` op per the oracle's own note
(distinct-ops-new.tsv row "write-identity-file-atomic": "the claude-klabauter helper
should take a fields dict, not be two call sites"), so a caller that wants to
set both keys in one install run passes ``{"operator_name": ..., "engagement_
posture": ...}`` and gets one write, not two.

Write path: ``locked_write.locked_rmw`` (mkstemp + os.replace) — the mktemp+
`cat >>`-then-`mv` fence has no atomic replace step of its own (a crash
mid-heredoc-write leaves a truncated file); locked_rmw closes that race
entirely, and its own byte-identical-skip (step 6 of its algorithm) makes a
rerun with identical fields a true no-op with no mtime churn.

Document shape: a `# ...` header comment line (operator-facing "never a
publish target" notice, reproduced verbatim from the bash oracle) followed by
plain `key: value` YAML mapping lines — `version: 1` first if absent, then
every other key in first-write order. Existing keys not named in this call's
``fields`` are preserved untouched (a merge, not a replace) — the two-call-site
oracle depends on this: Step 3b-4 (`engagement_posture`) must not clobber the
`operator_name` Step 3 already wrote in an earlier run.

DEC-7 idempotency note (manifest-rated "none once ported" idempotency-hazard):
idempotency is achieved by making the write itself content-idempotent, not by
any op-level dedup ledger. ``_mutate`` parses the file's current YAML mapping,
merges in ``fields`` (last-writer-wins per key), and re-serializes; if the
resulting mapping is unchanged (same keys, same values, same order) the
re-serialized text is byte-identical to the original, so ``locked_rmw``'s
byte-identical-skip makes a second call with the same ``(claude_home, fields)``
pair a safe, side-effect-free no-op — ``written`` comes back False and no
mtime/inode churn occurs. A second call with a DIFFERENT value for an existing
key is an intentional overwrite of that one key (matching the original fence's
own last-writer-wins semantics), leaving all other keys untouched.

Scope: registered with no ``_OP_KEY_SCOPE`` entry (scope-verdict "none" per
state/audits/2026-07-22-command-payload-inventory/op-classification.tsv row
"write-identity-file-atomic") — the target is
``CLAUDE_HOME/.claude/coordinator-identity.yaml``, a fixed per-machine
operator-config path, never the caller's own repo, so the dispatcher never
injects a ``repo_root`` for this op. The cross-process lock sidecar that
``locked_rmw`` needs still requires SOME path inside a git repository to
resolve ``git rev-parse --git-common-dir`` against; this handler derives that
path itself by walking up from the identity file's parent looking for a
``.git`` entry (the coordinator-claude clone that ``CLAUDE_HOME/.claude``
resolves to in every supported install shape), mirroring
``self_persist_findings._find_repo_root``'s same walk for the sibling
none-scoped C1a op — not a re-derivation of new logic, the same reasoning
applied to a second none-scoped target.

Params:
    claude_home (str)            — the resolved ``${CLAUDE_HOME:-$HOME}`` value
                                    (caller resolves the env-var-or-HOME
                                    fallback; this op does not re-derive it).
                                    Required.
    fields      (dict[str, str]) — keys/values to merge into the identity
                                    document. Required; every value must be a
                                    string (matches the fence's plain scalar
                                    `key: value` lines — no nested structures).

Returns a dict:
    exit_code (int)  — 0 ok / 1 error.
    written   (bool) — True if the file's bytes changed as a result of this
                        call; False on an idempotent rerun with byte-identical
                        merged content, or on error.
    path      (str)  — the resolved identity-file path (contractual, present
                        on both the write and no-op paths; empty string on a
                        pre-resolution error).
    message   (str)  — human-readable outcome (exit_code 0 only).
    error     (str)  — human-readable reason (exit_code 1 only).

Self-registration: importing this module fires
``@register_op("install.write_identity_file", _handler)`` as a side-effect.
Add this module to coordinator_core/ops/__init__.py (_EAGER_OP_MODULES) to
trigger registration at start_server() time — owned by the shared-surface
tail pass, not this module.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 1 C1a; state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
row "write-identity-file-atomic"; distinct-ops-new.tsv same row (fence sites:
commands/install.md:669, commands/install.md:818 — coordinator-claude-owned,
out of this repo's write scope).

Negative-spec:
  - Does NOT create ``CLAUDE_HOME/.claude`` itself if absent — a fresh install
    always creates that directory before Phase 2 (operator identity capture)
    runs; this op mkdir's only the identity file's own parent if it is
    ``CLAUDE_HOME/.claude`` exactly, never an arbitrary ancestor chain.
  - Does NOT accept a non-dict ``fields`` or a non-string value inside it —
    fails loud rather than silently coercing (`str(value)`), since a silent
    coercion could turn an accidental int/bool into a YAML scalar the operator
    never intended.
  - Does NOT shell out to mktemp/cat/mv/a heredoc — pure YAML parse + merge +
    ``Path.write_text()`` via locked_rmw; the entire hazard class this op
    exists to retire is the shell dependency itself.
  - Does NOT validate ``fields`` keys against a fixed allowlist (e.g. only
    ``operator_name``/``engagement_posture``) — the document is a general
    key/value store and future fences may add keys; validation of individual
    key semantics belongs to the caller (the doctrine fence), not this
    generic atomic-write primitive.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw

_LOG = logging.getLogger(__name__)

_HEADER_COMMENT = (
    "# ~/.claude/coordinator-identity.yaml — operator-local, NEVER a publish target"
)


# ---------------------------------------------------------------------------
# Repo-root discovery for the locked_rmw lock sidecar
# ---------------------------------------------------------------------------


def _find_repo_root(target: Path) -> Optional[Path]:
    """Walk upward from target's parent looking for a ``.git`` entry.

    Returns the first ancestor directory containing a ``.git`` file-or-directory
    (a linked worktree's ``.git`` is a file; a main checkout's is a directory —
    either satisfies ``locked_rmw``'s ``repo_root`` contract, since it only
    needs a path inside the repo to resolve ``git rev-parse --git-common-dir``
    against). Returns None if no such ancestor exists (target is not inside any
    git repo) — mirrors ``self_persist_findings._find_repo_root``'s identical
    walk for the sibling none-scoped C1a op.
    """
    node = target.parent
    while True:
        if (node / ".git").exists():
            return node
        parent = node.parent
        if parent == node:
            return None
        node = parent


# ---------------------------------------------------------------------------
# Document parse / merge / serialize
# ---------------------------------------------------------------------------


def _parse_existing(old_text: str) -> dict:
    """Parse the identity file's current text into a mapping.

    Empty/whitespace-only text (fresh file under missing_ok=True) parses to
    ``{}``. A non-mapping YAML document (e.g. a bare scalar or list) is
    rejected — the identity file's contract is a flat key/value mapping.
    """
    if not old_text.strip():
        return {}
    loaded = yaml.safe_load(old_text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            "coordinator-identity.yaml does not parse to a YAML mapping "
            f"(got {type(loaded).__name__})"
        )
    return loaded


def _serialize(doc: dict) -> str:
    """Render *doc* back into the identity file's on-disk shape.

    ``version`` is forced first (defaulting to 1 if absent from *doc*) to
    match the bash oracle's own field order; every other key follows in
    *doc*'s own (insertion-preserving) order. ``sort_keys=False`` on the
    ``yaml.safe_dump`` call is load-bearing — alphabetizing would reorder
    ``operator_name``/``engagement_posture`` on every merge and make an
    otherwise-identical rerun byte-different.
    """
    ordered: dict = {"version": doc.get("version", 1)}
    for key, value in doc.items():
        if key == "version":
            continue
        ordered[key] = value
    body = yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False)
    return f"{_HEADER_COMMENT}\n{body}"


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("install.write_identity_file")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.write_identity_file" handler.

    See module docstring for the full contract, DEC-7 idempotency note, and
    negative-spec. ``repo_root`` (the dispatcher-injected value) is unused —
    this "none"-scoped op derives its own lock-sidecar anchor from the
    resolved identity-file path via ``_find_repo_root`` (see module docstring
    § Scope).
    """
    del repo_root
    claude_home_raw = params.get("claude_home")
    fields = params.get("fields")

    if not claude_home_raw or not isinstance(claude_home_raw, str):
        return _err("missing required param: claude_home", path="")
    if not isinstance(fields, dict) or not fields:
        return _err(
            "missing required param: fields (must be a non-empty dict[str, str])",
            path="",
        )
    for key, value in fields.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return _err(
                f"fields must be dict[str, str] — offending key/value: "
                f"{key!r} -> {value!r}",
                path="",
            )

    claude_home = normalize_native_path(claude_home_raw)
    target = claude_home / ".claude" / "coordinator-identity.yaml"
    path_str = str(target)

    lock_root = _find_repo_root(target)
    if lock_root is None:
        return _err(
            f"identity file target is not inside a git repository (no .git "
            f"ancestor found): {path_str!r}",
            path=path_str,
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    _written = [False]

    def _mutate(old_text: str) -> str:
        existing = _parse_existing(old_text)
        merged = dict(existing)
        merged.update(fields)
        new_text = _serialize(merged)
        if new_text == old_text:
            return old_text
        _written[0] = True
        return new_text

    try:
        await asyncio.to_thread(
            locked_rmw, target, _mutate, repo_root=lock_root, missing_ok=True
        )
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}", path=path_str)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return _err(f"cannot read/parse/write identity file: {exc}", path=path_str)

    if _written[0]:
        _LOG.debug(
            "install.write_identity_file: wrote %d field(s) to %s",
            len(fields), target,
        )
        return {
            "exit_code": 0,
            "written": True,
            "path": path_str,
            "message": f"wrote {len(fields)} field(s) to {path_str}",
        }
    _LOG.debug(
        "install.write_identity_file: %s already holds byte-identical merged "
        "content — skipping (idempotent)",
        target,
    )
    return {
        "exit_code": 0,
        "written": False,
        "path": path_str,
        "message": f"{path_str} already holds byte-identical content — no-op (idempotent)",
    }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str, *, path: str) -> dict:
    """Return an exit_code=1 error reply dict."""
    _LOG.warning("install.write_identity_file: %s", msg)
    return {"exit_code": 1, "written": False, "path": path, "error": msg}
