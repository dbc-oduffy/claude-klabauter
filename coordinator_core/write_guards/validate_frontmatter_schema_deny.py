"""coordinator_core.write_guards.validate_frontmatter_schema_deny — HARD-DENY
leg of the frontmatter-schema validator.

Ported from DoE-claude
``coordinator/hooks/scripts/validate-frontmatter-schema.py`` (faithful port —
see ``write_guards/INTERFACE.md``). That single 2042-line script originally
emitted BOTH ``permissionDecision: deny`` and ``additionalContext``
advisories from the SAME control flow, mode-dependently
(``COORDINATOR_SCHEMA_STRICT=1`` used to upgrade most branches from advisory
to deny), with four branches (own-inbox misplacement, C2 lineage-reachability,
the 2026-07-29 grouping-approval scope-cut contract, and the 2026-07-29 D3
out-of-enum handoff ``kind`` contract) denying UNCONDITIONALLY, never gated on
strict mode. A single module returning either shape would let a default-mode
advisory short-circuit the engine's entire hard-deny phase — this module is
the CLASS=hard-deny half of the split; the advisory half is
``validate_frontmatter_schema_advisory.py`` (a separate chunk, authored
independently).

RULING (2026-08-06, docs/plans/2026-08-06-apply-guard-class-census.md C15):
the ``COORDINATOR_SCHEMA_STRICT=1``-gated deny UPGRADE described above no
longer exists. A hard block throws away the entire write attempt (every
token spent composing it), whereas a warning still puts the finding in front
of whoever can fix it — so a schema-shaped violation now WARNS in both
modes, never hard-blocks. Strict mode still changes something, but not
whether the finding blocks: it changes WHICH module renders the warning.
``validate_frontmatter_schema_advisory``'s own warn-by-default branches stand
down under strict (``if _is_strict(): return None`` — that stand-down
predates this ruling and is unchanged); this module now fills that gap by
rendering the identical ``additionalContext`` warning itself when strict is
set, instead of denying. See ``check()``'s docstring for the exact shape.
The four UNCONDITIONAL denies above are untouched by this ruling — they were
never strict-gated in the first place, so "warn instead of block under
strict" does not apply to them at all; they deny in every mode, exactly as
before.

Mutual exclusivity (required, differential-tested)
----------------------------------------------------
This module and ``validate_frontmatter_schema_advisory`` must never both
return non-``None`` for the same payload — the source's control flow is a
strict FIRST-MATCH walk (memo guards, then a new-file scaffold offer, then
[lineage-reachability OR schema-shape validation, reachability taking
priority]) where each step either fires (stopping the walk with its own
shape: unconditional-deny, always-advisory, or none) or falls through to the
next step. To reproduce that exactly across two independently-invoked
modules, ``_first_result()`` below re-walks the ENTIRE sequence and returns
the (shape, message) of the FIRST step that fires, or ``None`` if nothing
does. This module returns a DENY envelope only when that first hit is
shape=="deny" (always one of the five unconditional findings above); for a
shape=="advisory" first hit it returns an ADVISORY envelope of its own only
under ``COORDINATOR_SCHEMA_STRICT=1`` (exactly when the advisory sibling is
standing down for that same finding), and ``None`` otherwise. The advisory
module (mirroring this same walk in its own file) returns non-``None`` for a
shape=="advisory" first hit only when NOT strict. Neither module may stop
early at a step whose shape doesn't match its own class — an intermediate
non-firing step must fall through to the NEXT step exactly as the source's
``main()`` does.

What this leg covers
---------------------
- The five unconditional (never strict-gated, never downgraded by the
  2026-08-06 warn-not-block ruling) denies: the own-inbox misplacement guard
  (an outbound memo, ``from`` == this repo and ``to`` != this repo, landing
  in this repo's OWN ``cross-repo/inbox/``); the C2 lineage-reachability
  hard-reject (predecessor/forked_from/additional_predecessors[]/
  origin_handoff resolving to nothing live, archived, or in git history);
  the grouping-approval scope-cut contract (closing a task-spine row under a
  still-pending PM grouping approval); the D3 out-of-enum handoff ``kind``
  contract (scoped to ``state/handoffs/**`` only); and the queue-deferral
  grant contract (2026-08-27-a-queue-deferral-is-a-grant-the-pm-issues.md
  C4 — an ungranted ``status: deferred`` on a
  ``state/{improvement-queue,debt-backlog,bug-backlog}/**`` record).
- Every other branch is now always-"advisory" (2026-08-06 ruling): the
  mislocated-memo / free-form-header offer, the routing-mismatch offer, the
  new-file scaffold offer, and a schema-shape validation failure. This
  module renders that advisory itself only under
  ``COORDINATOR_SCHEMA_STRICT=1``; in default mode it stays silent and the
  advisory sibling renders it instead.

Schema/DAG logic itself is NOT reimplemented here — this is wrapper/routing
around ``coordinator_core.frontmatter.schema_validate`` and
``coordinator_core.dag.check_lineage_reachability``, the same engine
functions the source hook already called via its D1a/D1b cross-repo import
seam. That seam (sys.path surgery + a ``_claude_klabauter_root`` resolver) is now
unnecessary — this module runs INSIDE claude-klabauter, so the calls are ordinary
same-package imports.

Schema/manifest resolution
----------------------------
The source hook resolved ``coordinator/schemas/`` and
``coordinator-registry.manifest.json`` relative to its own on-disk location
inside the DoE-claude checkout (schemas are DoE-owned doctrine, not
Claude-klabauter-owned). Running from inside claude-klabauter, this module resolves the
DoE-claude repo root via ``coordinator_core.ops.coordinator_doe_root.
coordinator_doe_root()`` — the same ratified full-ladder resolver the
advisory sibling uses (``REPO_DOE_CLAUDE`` env override, then the
``machine-local`` registry, then the native clone-root resolver) — rather
than ``coordinator_core.doe_root_pointer.read_doe_root_pointer()``, which
``coordinator_core/testing/doe_root.py`` documents as "the wrong layer to
standardize... call sites on" (it skips the ``REPO_DOE_CLAUDE`` override
and the ``machine-local``/clone-root rungs entirely). Both split modules
must resolve through the identical function, or they can silently disagree
on which DoE-claude checkout is authoritative — exactly the condition the
mutual-exclusivity guarantee above depends on not happening. Unresolvable
root, missing manifest, or malformed manifest/schema JSON all narrow to
"this guard produces nothing" (``check()`` returns ``None``), exactly
mirroring the source's own ``sys.exit(0)`` on manifest-load failure — never
a deny on infra, per the source's own "NEVER block on infra" negative-spec.

Spec backlink: DoE-claude
  coordinator/hooks/scripts/validate-frontmatter-schema.py
"""

from __future__ import annotations

import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.dag import check_lineage_reachability as _check_lineage_reachability
from coordinator_core.frontmatter.baton_class import (
    _PRE_RENAME_ALIASES as _HANDOFF_KIND_PRE_RENAME_ALIASES,
    canonical_kind as _canonical_kind,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus as _LocateStatus
from coordinator_core.frontmatter.body_blocks import locate_fenced_block as _locate_fenced_block
from coordinator_core.git.repo_root import show_toplevel as _git_show_toplevel
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.frontmatter.schema_validate import (
    _apply_cross_field_rules,
    _plan_tasks_schema_without_pm_approved_required,
    _validate_json_schema_node,
    check_plan_tasks_grouping_approval as _check_plan_tasks_grouping_approval,
    is_governed_plan as _is_governed_plan,
    load_schemas as _load_schemas,
    match_schema as _match_schema,
    parse_frontmatter as _parse_frontmatter,
    parse_yaml as _parse_yaml,
    validate_frontmatter_obj as _validate_frontmatter_obj,
    _is_parseable_iso_date as _is_parseable_iso_date,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 5

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: This module's only write is _capture_write_guard_forensics()'s dump to
#: state/scratch/write-guard-forensics/*.json -- state/scratch/ is gitignored
#: at any depth (.gitignore's scratch/ entry), never a tracked repo artifact.
GENERATES = []

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

_MEMO_SCHEMA_NAMES = ("cross-repo-memo", "archived-memo")

# The vendored, version-pinned schema corpus this module now validates
# against — see module docstring § Schema/manifest resolution. Resolved
# relative to this file's own on-disk location (this module runs INSIDE
# claude-klabauter already), never from DoE-claude's live working tree.
_VENDORED_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas"

# ---------------------------------------------------------------------------
# Torn-write retry — DoE's schema corpus and registry manifest are resolved
# from the LIVE working tree of a sibling repo checkout (see module docstring
# § Schema/manifest resolution), which concurrent sessions edit continuously.
# A `check()` call can land mid-write and see a torn/partial JSON file. Every
# load site below already treats a load failure as fail-open (never block on
# infra, per module docstring line ~73) — this retry only narrows the window
# in which a TRANSIENT torn-write trips that fail-open path; it changes
# nothing about what happens once every attempt is exhausted, so it cannot
# make the guard more permissive than it already is.
#
# ABSENT vs TORN: a missing path (partial install, clone without the schemas
# tree, permissions problem) is a STEADY STATE, not a race — no number of
# retries makes an absent file appear, so retrying it only pays sleep cost
# on every guarded Write/Edit/MultiEdit for no benefit (this guard fires on
# every edit in the repo — see the boot-banner precedent for per-invocation
# sleep cost on an install-incomplete machine). `exists_path` is checked
# ONCE, up front, outside the retry loop: absent -> call `fn()` exactly once
# and let whatever it raises propagate immediately, zero sleep; present ->
# retry as below, because presence-but-unparseable is the actual torn-write
# signature this helper exists to bridge.
# ---------------------------------------------------------------------------

_TORN_WRITE_RETRY_ATTEMPTS = 3
_TORN_WRITE_RETRY_DELAY_SECS = 0.05

_T = TypeVar("_T")


def _retry_on_transient_read_failure(
    fn: Callable[[], _T],
    *,
    exists_path: "str | Path",
    record: Optional[Dict[str, Any]] = None,
) -> _T:
    """Retry ``fn()`` up to ``_TORN_WRITE_RETRY_ATTEMPTS`` times with a short
    delay between attempts, re-raising the LAST exception if every attempt
    fails — but ONLY when ``exists_path`` is present on disk at call time.
    An absent ``exists_path`` calls ``fn()`` exactly once, no sleep, no
    retry: absence is terminal, not transient. Callers keep their existing
    fail-open ``except Exception`` wrapper around this either way — this
    helper only changes how many chances a genuinely-present-but-unreadable
    path gets before that fail-open path fires.

    ``record`` (optional, default ``None``) lets a caller observe the
    outcome — ``path_existed``, ``attempt_count_used``, ``succeeded``,
    ``exhausted``, ``last_error`` — WITHOUT changing this function's own
    control flow or return value one bit; every write into ``record`` is a
    plain dict assignment (no I/O), so passing ``None`` (every pre-existing
    call site) costs nothing beyond one extra ``is not None`` check per
    branch. This is the forensic hook `validate_frontmatter_schema_deny`
    uses to tell "succeeded on the first try" apart from "succeeded only
    after a retry" (the race caught in the act) and from "exhausted every
    attempt" (a genuinely torn read, as opposed to a merely-absent path —
    see the ABSENT-vs-TORN distinction in this module's docstring, which
    ``path_existed`` reports directly: ``False`` means this was the
    steady-state absent case, never a retry candidate at all).
    """
    if not os.path.exists(exists_path):
        if record is not None:
            record["path_existed"] = False
            record["attempted"] = False
        return fn()

    if record is not None:
        record["path_existed"] = True
        record["attempted"] = True

    last_exc: Optional[BaseException] = None
    for attempt in range(_TORN_WRITE_RETRY_ATTEMPTS):
        try:
            result = fn()
            if record is not None:
                record["attempt_count_used"] = attempt + 1
                record["succeeded"] = True
                record["exhausted"] = False
            return result
        except Exception as exc:  # noqa: BLE001 — bounded by attempts, re-raised below
            last_exc = exc
            if attempt < _TORN_WRITE_RETRY_ATTEMPTS - 1:
                time.sleep(_TORN_WRITE_RETRY_DELAY_SECS)
    if record is not None:
        record["attempt_count_used"] = _TORN_WRITE_RETRY_ATTEMPTS
        record["succeeded"] = False
        record["exhausted"] = True
        record["last_error"] = f"{type(last_exc).__name__}: {last_exc}"
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Strict-mode gate
# ---------------------------------------------------------------------------


def _is_strict() -> bool:
    return os.environ.get("COORDINATOR_SCHEMA_STRICT") == "1"


# ---------------------------------------------------------------------------
# Path / edit-simulation helpers (verbatim port)
# ---------------------------------------------------------------------------


def _to_repo_relative(abs_path: str, repo_root: str) -> Optional[str]:
    normal_abs = abs_path.replace("\\", "/")
    normal_root = repo_root.replace("\\", "/")
    # Comparison-only fold: the returned `rel` (sliced from `normal_abs`,
    # original case) is used downstream only for regex/pattern matching and
    # schema lookup keys — never for disk I/O (callers use `abs_file_path`
    # for that). Folding only the prefix-match operands, not `normal_abs`
    # itself, keeps the returned relative path's case intact.
    if not casefold_path(normal_abs).startswith(casefold_path(normal_root)):
        return None
    rel = normal_abs[len(normal_root):]
    if rel.startswith("/"):
        rel = rel[1:]
    return rel


def _apply_edit(content: str, old_string: str, new_string: str) -> Tuple[str, bool]:
    idx = content.find(old_string)
    if idx == -1:
        return content, False
    return content[:idx] + new_string + content[idx + len(old_string):], True


def _compute_content_to_probe(
    tool_name: str, tool_input: dict, abs_file_path: str
) -> Optional[str]:
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string") or ""
    if tool_name == "MultiEdit":
        try:
            with open(abs_file_path, "r", encoding="utf-8") as fh:
                probe = fh.read()
        except OSError:
            probe = ""
        for edit in tool_input.get("edits") or []:
            result, matched = _apply_edit(
                probe, edit.get("old_string") or "", edit.get("new_string") or ""
            )
            if not matched:
                return None
            probe = result
        return probe
    return None


def _js_object_keys(value: Any) -> "list[str]":
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return [str(i) for i in range(len(value))]
    return []


# ---------------------------------------------------------------------------
# Manifest / schema resolution — deferred to check()-time, never import time.
# ---------------------------------------------------------------------------


class _Context:
    __slots__ = (
        "schemas_dir",
        "memo_schemas_dir",
        "repo_basename_to_em_shortname",
        "scaffold_offer_map",
        "kind_offer_override",
        "central_em_ids",
        "central_canonical_id",
        "manifest",
    )


def _load_context(_forensics: Optional[Dict[str, Any]] = None) -> Optional[_Context]:
    """Resolve the vendored schema corpus (always available in-repo) plus,
    best-effort, the DoE-claude registry manifest + the five registries
    derived from it. NEVER returns ``None`` for the corpus itself any more —
    that is exactly the fail-open-on-missing-sibling hole AC2 closes (see
    module docstring § Schema/manifest resolution and
    docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-corpus.md):
    a clone without the DoE sibling present still gets full schema-shape
    enforcement, lineage-reachability, and grouping-approval/handoff-kind
    checks. Only the manifest-DERIVED fields (memo routing / scaffold-offer
    maps, central-EM identity) degrade to empty defaults when the manifest
    is unresolvable or malformed — those remain genuinely DoE-owned routing
    data (plan AC4 + Out of scope), not vendored, so their absence narrows
    only the memo-guard/scaffold-offer steps, never the schema-shape steps.

    ``_forensics`` (optional) is a plain dict a caller may pass to observe
    WHICH of the several degrade-open branches below fired and why, without
    this function's own control flow changing at all — every write into it
    is a dict assignment (no I/O), so the ``None`` default (every call site
    that doesn't care) costs nothing. See `_capture_guard_forensics` for the
    consumer and the ABSENT-vs-TORN distinction this distinguishes: an
    unresolvable/absent DoE root is recorded as ``doe_root_unresolvable``
    (steady state, e.g. a partial install with no sibling checkout — NOT a
    failure worth capturing on every write on such a machine), while a
    manifest read that found the path present but never got a clean parse
    is recorded via ``manifest_load_retry`` (whose ``path_existed``/
    ``exhausted`` fields are the actual torn-read signature).
    """
    try:
        doe_root = coordinator_doe_root()
    except Exception:  # noqa: BLE001 — degrade-open, never block on infra
        doe_root = None
        if _forensics is not None:
            _forensics["doe_root_resolve_raised"] = True
    if not doe_root:
        if _forensics is not None and "doe_root_resolve_raised" not in _forensics:
            _forensics["doe_root_unresolvable"] = True
    elif _forensics is not None:
        _forensics["doe_root"] = doe_root

    # Schema CORPUS resolution is repointed at claude-klabauter's own vendored,
    # version-pinned copy — no longer DoE-claude's live working tree, and no
    # longer coupled to the manifest read below at all. The registry
    # MANIFEST stays on `doe_root`: it is routing/scaffold logic, not a
    # schema, and is explicitly out of scope for the repoint (plan AC4 +
    # Out of scope).
    schemas_dir = _VENDORED_SCHEMAS_DIR
    if _forensics is not None:
        _forensics["schemas_dir"] = str(schemas_dir)

    manifest: Optional[Dict[str, Any]] = None
    if doe_root:
        manifest_path = Path(doe_root) / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        manifest_retry_record: Dict[str, Any] = {}
        try:
            manifest = _retry_on_transient_read_failure(
                lambda: json.loads(manifest_path.read_text(encoding="utf-8")),
                exists_path=manifest_path,
                record=manifest_retry_record,
            )
        except Exception:  # noqa: BLE001 — degrade-open, never block on infra
            if _forensics is not None:
                _forensics["manifest_load_retry"] = manifest_retry_record
                _forensics["manifest_load_failed"] = True
            manifest = None
        if _forensics is not None:
            _forensics["manifest_load_retry"] = manifest_retry_record

    ctx = _Context()
    ctx.schemas_dir = schemas_dir
    # The two memo schemas (cross-repo-memo, archived-memo) are generated
    # live by claude-klabauter itself (DR-210 amendment) — this module already runs
    # INSIDE claude-klabauter, so "claude-klabauter root" is just this file's own ancestor
    # directory, never a cross-repo guess at a sibling checkout path.
    ctx.memo_schemas_dir = Path(__file__).resolve().parents[1] / "contract"
    ctx.manifest = manifest or {}
    ctx.repo_basename_to_em_shortname = {}
    ctx.scaffold_offer_map = {}
    ctx.kind_offer_override = {}
    ctx.central_em_ids = set()
    ctx.central_canonical_id = None
    if manifest is not None:
        try:
            ctx.repo_basename_to_em_shortname = {
                a["dirBasename"]: a["shortname"] for a in manifest["identity"]["repoAliases"]
            }
            ctx.scaffold_offer_map = {
                d["schemaName"]: {"type": d["type"], "isSidecar": d["isSidecar"]}
                for d in manifest["docTypes"]
                if d.get("offerable") is True
            }
            ctx.kind_offer_override = {
                kind: {
                    "type": entry["type"],
                    "isSidecar": entry["isSidecar"],
                    "manualArgs": entry.get("manualArgs"),
                    "authoringHint": entry.get("authoringHint"),
                }
                for kind, entry in manifest["kindOfferOverride"].items()
            }
            ctx.central_em_ids = set(manifest["identity"]["centralReceiverIds"])
            ctx.central_canonical_id = manifest["identity"]["centralReceiverIds"][0]
        except Exception:  # noqa: BLE001 — degrade-open, mirrors source's guarded block
            if _forensics is not None:
                _forensics["context_fields_malformed"] = True
            ctx.manifest = {}
            ctx.repo_basename_to_em_shortname = {}
            ctx.scaffold_offer_map = {}
            ctx.kind_offer_override = {}
            ctx.central_em_ids = set()
            ctx.central_canonical_id = None
    return ctx


def _merge_memo_schemas(ctx: _Context, schemas: dict) -> dict:
    """Overlay the two memo schema entries onto `schemas` with claude-klabauter's
    generated versions. Fail-open: any load error, or a partial load missing
    either name, leaves `schemas` exactly as loaded from `ctx.schemas_dir`
    (which already vendors fallback copies of both) — never blocks
    validation on this seam's health. Verbatim port of the source's
    `_merge_memo_schemas`, including its "all memo schemas present or none
    applied" guard (Review: code-reviewer — Finding 1 in the source).
    """
    try:
        memo_schemas = _load_schemas(str(ctx.memo_schemas_dir))
    except Exception:  # noqa: BLE001 — fail-open
        return schemas

    if not all(name in memo_schemas for name in _MEMO_SCHEMA_NAMES):
        return schemas

    schemas["_byGlob"] = [
        entry for entry in schemas["_byGlob"] if entry["schemaName"] not in _MEMO_SCHEMA_NAMES
    ]
    for kind, schema_name in list(schemas["_byKind"].items()):
        if schema_name in _MEMO_SCHEMA_NAMES:
            del schemas["_byKind"][kind]

    for name in _MEMO_SCHEMA_NAMES:
        schemas[name] = memo_schemas[name]
    schemas["_byGlob"].extend(
        entry for entry in memo_schemas["_byGlob"] if entry["schemaName"] in _MEMO_SCHEMA_NAMES
    )
    for kind, schema_name in memo_schemas["_byKind"].items():
        if schema_name in _MEMO_SCHEMA_NAMES:
            schemas["_byKind"][kind] = schema_name

    def _specificity_key(entry: dict) -> tuple:
        glob = entry["glob"]
        return (glob.count("*"), -len(glob))

    schemas["_byGlob"].sort(key=_specificity_key)
    return schemas


def _em_id_for_basename(ctx: _Context, basename: str) -> str:
    if basename in ctx.repo_basename_to_em_shortname:
        return ctx.repo_basename_to_em_shortname[basename] + "-em"
    return basename.replace("_", "-") + "-em"


def _registry_key_for_basename(ctx: _Context, basename: str) -> str:
    for alias in ctx.manifest.get("identity", {}).get("repoAliases", []):
        if alias.get("dirBasename") == basename:
            return f"repos.{alias['registryKey']}"
    return "repos." + basename.lower().replace("-", "_")


# ---------------------------------------------------------------------------
# Memo-family detection helpers (verbatim port)
# ---------------------------------------------------------------------------


def _is_memo_path_mislocated(repo_rel: str) -> bool:
    normalized = repo_rel.replace("\\", "/")
    if re.match(r"^cross-repo/", normalized):
        return False
    if re.search(r"(?:^|/)memos/", normalized):
        return True
    return False


def _has_free_form_memo_header(content: str) -> bool:
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        return False
    scanned = 0
    has_to = False
    has_from = False
    for line in lines:
        trimmed = line.strip()
        if trimmed == "":
            continue
        if re.match(r"^To:\s+\S", trimmed):
            has_to = True
        if re.match(r"^From:\s+\S", trimmed):
            has_from = True
        scanned += 1
        if scanned >= 20:
            break
    return has_to and has_from


def _extract_yaml_to_field(content: str) -> Optional[str]:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^to:\s+(.+)$", line)
        if m:
            return re.sub(r'^["\']|["\']$', "", m.group(1)).strip()
    return None


def _extract_yaml_to_repo_field(content: str) -> Optional[str]:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^to_repo:\s+(.+)$", line)
        if m:
            return re.sub(r'^["\']|["\']$', "", m.group(1)).strip()
    return None


# ---------------------------------------------------------------------------
# Message builders — byte-for-byte from the source's build_*_payload strings.
# ---------------------------------------------------------------------------


def _memo_offer_message() -> str:
    # NOTE: this is the STRICT-mode (deny) reason text, which the source's
    # build_memo_offer_payload emits BARE (no "[cross-repo-memo offer] "
    # prefix) — that prefix is added only on the non-strict additionalContext
    # branch, which this module never emits (owned by the advisory sibling).
    return (
        "This looks like a cross-repo memo being hand-rolled to a "
        "non-canonical location. Use the CLI instead so it lands in the "
        "receiver's cross-repo/inbox/ surface:\n\n"
        "  cross-repo-memo draft <slug> --to <receiver-repo-name> --title \"...\"\n"
        "  cross-repo-memo send <slug>\n\n"
        "The CLI writes one dirty file into the receiver's cross-repo/inbox/ "
        "directory (status: open), leaves it uncommitted so it surfaces in "
        "their git status, and prints the path for you to hand the PM for "
        "relay. Hand-rolling bypasses schema validation, delivery "
        "guarantees, and discoverability."
    )


def _own_inbox_deny_message(
    this_em_id: str, to_value: Optional[str], payload: Optional[Dict[str, Any]] = None
) -> str:
    _note = operator_override_note("COORDINATOR_OVERRIDE_OWN_INBOX", payload=payload)
    return (
        f"This memo's `from:` is THIS repo ({this_em_id}) but it's landing "
        "in this repo's own cross-repo/inbox/. cross-repo/inbox/ holds "
        "memos addressed TO you, not memos authored by you. To SEND an "
        "outbound memo, deliver it to the recipient:\n\n"
        f"  cross-repo-memo draft <slug> --to {to_value or '<recipient-em>'} "
        "--title \"...\"\n"
        "  cross-repo-memo send <slug>\n\n"
        "That command writes into the RECIPIENT's cross-repo/inbox/ "
        "directory, not yours."
        + ("\n\n" + _note if _note else "")
    )


def _memo_routing_offer_message(resolved_recipient_em_id: Optional[str]) -> str:
    # NOTE: bare STRICT-mode (deny) reason text — see _memo_offer_message's
    # note; the "[cross-repo-memo routing offer] " prefix is advisory-only.
    recipient_hint = resolved_recipient_em_id or "<receiver-em-id>"
    return (
        f"This memo's `to:` field ({recipient_hint}) does not match the repo you "
        "are writing into. Cross-repo memos must land in the RECIPIENT'S repo, "
        "not the sender's. Use the CLI to route it correctly:\n\n"
        f"  cross-repo-memo draft <slug> --to {recipient_hint} --title \"...\"\n"
        "  cross-repo-memo send <slug>\n\n"
        f"The CLI writes one dirty file into {recipient_hint}'s cross-repo/inbox/ "
        "directory so it surfaces in their git status. Hand-rolling to the wrong "
        "repo means the recipient will never find the memo."
    )


def _scaffold_offer_message(
    schema_name: str,
    type_: str,
    derived_args: Optional[str],
    authoring_hint: Optional[str],
    resolved_kind: Optional[str],
) -> str:
    args = f" {derived_args}" if derived_args else ""
    cmd = f"coordinator-doc-new --type {type_}{args}"
    schema_label = (
        f"schema: {schema_name}, resolved by kind: {resolved_kind}"
        if resolved_kind
        else f"schema: {schema_name}"
    )
    # NOTE: bare STRICT-mode (deny) reason text — the "[scaffold offer] "
    # prefix is advisory-only (see _memo_offer_message's note).
    message = (
        f"This is a new schema-matching document ({schema_label}). "
        "Use the scaffolder to generate conformant frontmatter:\n\n"
        f"  {cmd}\n\n"
        "Then fill the body via Edit. The scaffolder creates the file via Python "
        "open() (structurally exempt from this PreToolUse Write-tool hook — see "
        "new-file-only rationale below), so subsequent body-fill edits stay silent. "
        "Hand-rolling bypasses schema enforcement and risks frontmatter drift that "
        "breaks query-records and downstream ingest."
    )
    if authoring_hint:
        message += f"\n\n{authoring_hint}"
    return message


def _violation_message(schema_name: str, errors: "list[dict]") -> str:
    parts = []
    for e in errors:
        field = e.get("field") or "(unknown)"
        hint = f"; required shape: {e['hint']}" if e.get("hint") else ""
        parts.append(f"{field}: {e.get('error')}{hint}")
    return f"{schema_name}: {'; '.join(parts)}"


def _reachability_deny_message(violations: "list[dict]") -> str:
    parts = [f'{v.get("field")}: "{v.get("value")}" — {v.get("reason")}' for v in violations]
    return (
        f"Lineage-reachability check failed (write-time hard-reject): {'; '.join(parts)}. "
        "Each of predecessor / forked_from / additional_predecessors[] / origin_handoff must "
        "resolve to a handoff that exists live (state/handoffs/), on-disk-archived "
        "(archive/handoffs/), or in git history — a target unresolvable in all three is "
        "treated as a typo'd or never-existed path, not lineage. If this is a genuine "
        "cross-repo recovery baton, use kind: recovery (the same-repo-only carve-out "
        "applies to recovery predecessor SHAs only, not path-shaped lineage fields)."
    )


def _derive_sidecar_plan_stem(repo_rel: str, sidecar_type: str) -> Optional[str]:
    normalized = repo_rel.replace("\\", "/")
    basename = normalized.split("/")[-1]
    suffix = f".{sidecar_type}.md"
    if basename and basename.endswith(suffix):
        return basename[: -len(suffix)]
    return None


# ---------------------------------------------------------------------------
# Step 1 — memo-family guards (mislocated/free-form-header offer, own-inbox
# deny [unconditional], routing-mismatch offer). Same fall-through order as
# the source's run_memo_guards().
# ---------------------------------------------------------------------------


def _memo_guard_step(
    ctx: _Context,
    tool_name: str,
    tool_input: dict,
    repo_root: str,
    abs_file_path: str,
    repo_rel: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    if tool_name not in _GUARDED_TOOLS:
        return None

    if _is_memo_path_mislocated(repo_rel):
        return ("advisory", _memo_offer_message())

    content_to_probe = _compute_content_to_probe(tool_name, tool_input, abs_file_path)
    if content_to_probe is not None and _has_free_form_memo_header(content_to_probe):
        return ("advisory", _memo_offer_message())

    repo_root_stripped = repo_root.rstrip("/\\")
    doe_root_realpath: Optional[str] = None
    try:
        doe_root_raw = coordinator_doe_root() or ""
    except Exception:  # noqa: BLE001 — fail-open
        doe_root_raw = ""
    if doe_root_raw:
        try:
            doe_root_realpath = str(Path(doe_root_raw).resolve())
        except OSError:
            doe_root_realpath = None

    normalized_rel_for_routing = repo_rel.replace("\\", "/")

    is_canonical_inbox_write = bool(re.match(r"^cross-repo/inbox/[0-9]", normalized_rel_for_routing))
    if is_canonical_inbox_write and os.environ.get("COORDINATOR_OVERRIDE_OWN_INBOX") != "1":
        repo_basename = os.path.basename(repo_root_stripped)
        try:
            repo_root_realpath = str(Path(repo_root_stripped).resolve())
        except OSError:
            repo_root_realpath = None
        # Comparison-only fold: both realpaths are used only for this
        # identity check, never for I/O — safe to fold both sides.
        this_repo_is_central = (
            doe_root_realpath is not None
            and repo_root_realpath is not None
            and casefold_path(repo_root_realpath) == casefold_path(doe_root_realpath)
        )
        this_em_id = ctx.central_canonical_id if this_repo_is_central else _em_id_for_basename(ctx, repo_basename)

        inbox_content: Optional[str] = None
        if tool_name == "Write":
            inbox_content = tool_input.get("content") or ""
        elif tool_name == "Edit":
            try:
                with open(abs_file_path, "r", encoding="utf-8") as fh:
                    existing = fh.read()
                result, matched = _apply_edit(
                    existing, tool_input.get("old_string") or "", tool_input.get("new_string") or ""
                )
                if matched:
                    inbox_content = result
            except OSError:
                pass
        elif tool_name == "MultiEdit":
            inbox_content = content_to_probe

        if inbox_content is not None:
            try:
                inbox_fm = _parse_frontmatter(inbox_content).get("frontmatter")
            except Exception:  # noqa: BLE001 — fail-open
                inbox_fm = None
            if inbox_fm is not None:
                from_value = (inbox_fm.get("from") or "").strip()
                to_value = (inbox_fm.get("to") or "").strip()

                def _matches_this_repo(value: str) -> bool:
                    if value == this_em_id:
                        return True
                    return this_repo_is_central and value in ctx.central_em_ids

                if from_value and _matches_this_repo(from_value):
                    if not _matches_this_repo(to_value):
                        return ("deny", _own_inbox_deny_message(this_em_id, to_value, payload))

    is_canonical_memo_surface = bool(
        re.match(r"^cross-repo/inbox/", normalized_rel_for_routing)
    ) or bool(re.match(r"^cross-repo/archive/", normalized_rel_for_routing))

    if os.environ.get("COORDINATOR_OVERRIDE_MEMO_REDIRECT") != "1" and not is_canonical_memo_surface:
        memo_check_content: Optional[str] = None
        if tool_name == "Write":
            memo_check_content = tool_input.get("content") or ""
        elif tool_name == "Edit":
            memo_check_content = tool_input.get("new_string") or ""
        elif tool_name == "MultiEdit":
            memo_check_content = content_to_probe

        if memo_check_content is not None:
            normalized_rel = normalized_rel_for_routing
            is_memo_shaped_path = bool(
                re.search(r"(?:^|/)memos/", normalized_rel)
            ) or bool(re.match(r"^cross-repo/", normalized_rel))

            yaml_to_value = _extract_yaml_to_field(memo_check_content)
            has_free_form = _has_free_form_memo_header(memo_check_content)
            is_this_a_memo = is_memo_shaped_path and (yaml_to_value is not None or has_free_form)

            if is_this_a_memo:
                to_field_raw = yaml_to_value
                landing_basename = os.path.basename(repo_root_stripped)
                try:
                    repo_root_realpath = str(Path(repo_root_stripped).resolve())
                except OSError:
                    repo_root_realpath = None
                # Comparison-only fold: see `this_repo_is_central` above.
                landing_repo_is_central = (
                    doe_root_realpath is not None
                    and repo_root_realpath is not None
                    and casefold_path(repo_root_realpath) == casefold_path(doe_root_realpath)
                )
                landing_em_id = ctx.central_canonical_id if landing_repo_is_central else _em_id_for_basename(ctx, landing_basename)

                to_repo_field_raw = _extract_yaml_to_repo_field(memo_check_content)
                if to_repo_field_raw is not None:
                    this_repo_registry_key = (
                        "repos.doe_claude"
                        if landing_repo_is_central
                        else _registry_key_for_basename(ctx, landing_basename)
                    )
                    if to_repo_field_raw.strip() != this_repo_registry_key:
                        return (
                            "advisory",
                            _memo_routing_offer_message(
                                to_field_raw if to_field_raw is not None else to_repo_field_raw.strip()
                            ),
                        )
                elif to_field_raw is not None:
                    to_norm = to_field_raw.strip().lower()
                    to_is_central = to_field_raw.strip() in ctx.central_em_ids or to_norm in ctx.central_em_ids
                    landing_is_central = landing_em_id == ctx.central_canonical_id
                    if to_is_central and landing_is_central:
                        pass
                    elif doe_root_realpath is None:
                        # Fail open when the DoE root is unresolvable. This deliberately does NOT
                        # also test `to_is_central`: `central_em_ids` is only populated once the
                        # DoE root HAS resolved, so `to_is_central and doe_root_realpath is None`
                        # was unsatisfiable by construction and the regex fallback below fired
                        # unconditionally on any `-em`-suffixed `to:`. With the root unresolvable we
                        # cannot tell whether `to:` is central, so the honest move is to emit
                        # nothing rather than guess.
                        pass
                    else:
                        to_em_id = to_field_raw.strip()
                        to_looks_like_em_id = bool(re.search(r"\S+-em$", to_em_id)) or (
                            to_em_id in ctx.central_em_ids
                        )
                        if to_looks_like_em_id:
                            to_em_id_norm = to_em_id.lower()
                            landing_em_id_norm = landing_em_id.lower()
                            if to_em_id_norm != landing_em_id_norm:
                                return ("advisory", _memo_routing_offer_message(to_em_id))

    return None


# ---------------------------------------------------------------------------
# Step 2 — new-file scaffold offer
# ---------------------------------------------------------------------------


def _scaffold_offer_step(
    ctx: _Context,
    tool_name: str,
    abs_file_path: str,
    schema_name: Optional[str],
    frontmatter: Optional[dict],
    repo_rel: str,
) -> Optional[Tuple[str, str]]:
    if not (
        tool_name == "Write"
        and not os.path.exists(abs_file_path)
        and schema_name in ctx.scaffold_offer_map
    ):
        return None

    kind_value = (
        str(frontmatter["kind"]) if frontmatter and frontmatter.get("kind") is not None else None
    )
    override = ctx.kind_offer_override.get(kind_value) if kind_value else None
    entry = override or ctx.scaffold_offer_map[schema_name]
    type_ = entry["type"]
    is_sidecar = entry["isSidecar"]
    authoring_hint = override.get("authoringHint") if override else None

    derived_args: Optional[str] = None
    if override and override.get("manualArgs"):
        derived_args = override["manualArgs"]
    elif is_sidecar:
        stem = _derive_sidecar_plan_stem(repo_rel, type_)
        derived_args = f"--plan {stem}" if stem else "--plan <stem>"

    message = _scaffold_offer_message(
        schema_name, type_, derived_args, authoring_hint, kind_value if override else None
    )
    return ("advisory", message)


# ---------------------------------------------------------------------------
# Step 3/4 — lineage reachability (unconditional deny, checked first) then
# schema-shape validation (always-advisory as of the 2026-08-06 ruling;
# rendered by THIS module only under COORDINATOR_SCHEMA_STRICT=1, see
# check()'s docstring).
# ---------------------------------------------------------------------------


def _plan_tasks_spine_errors(
    prospective_content: str, schemas: dict, frontmatter: Optional[dict]
) -> "list[dict]":
    """Validate every row of a plan's `## Tasks` task-spine YAML block against
    schemas["plan-tasks"] — the write-time enforcement plan-tasks.schema.json's
    closed enums (change_kind/disposition/queue_scope) never previously got;
    the only prior consumer was claude-klabauter's own mutation verb, which sees only
    rows a mutation touches, so a hand-authored row bypassed it entirely.

    Row-level, not field-specific: the whole row validates against the
    schema, closing change_kind/disposition/queue_scope (and any future
    field) in one pass.

    Fail-open at every seam (missing plan-tasks schema, unparseable YAML) ->
    []. Two LocateResult statuses are deliberately silent, not findings:
      - ABSENT (no spine yet) — legitimate mid-authoring state.
      - MALFORMED (>1 fence, or a heading with no fence in its section) —
        plan-coverage-checker's fail-loud, not duplicated here.

    Mirrors the advisory sibling's helper of the same name exactly — both
    must stay in lockstep so the STRICT-mode warn shape (rendered by THIS
    module, per the 2026-08-06 warn-not-block ruling) and the default warn
    shape (rendered by the advisory sibling) report the identical finding
    for the identical row.

    `plan_created` is now forwarded to `_apply_cross_field_rules` from this
    document's own frontmatter (`fm.get('created')`, mirroring
    `check_plan_tasks_source`'s call exactly) — 2026-08-19 fix. Before this,
    `_cf_plan_tasks_writes_declared` (registered in
    `_PLAN_TASKS_CROSS_FIELD_RULES`) never fired on either write guard: its
    own safe-default treats an unforwarded `plan_created` as "cannot
    confirm post-cutoff" and stands down unconditionally, so a hand-edited
    plan could omit `writes` on an open row and still save cleanly, only
    getting caught later at `dispatch.emit`'s preflight (Review:
    review-a-write-guard, MAJOR).

    GOVERNED-AWARE as of 2026-07-29 (write-guard-bypass fix). `frontmatter`
    is the plan DOCUMENT's own parsed frontmatter (the caller already has it
    — see `_evaluate_schema_validation`), used ONLY to resolve
    `is_governed_plan`; no row can answer that on its own. Before this fix,
    this function called `_validate_frontmatter_obj(row, plan_tasks_schema)`
    unconditionally — which runs BOTH the raw schema shape (including the
    `allOf` branches that make `pm_approved` REQUIRED on every closed
    disposition) AND `_apply_cross_field_rules(record, 'plan-tasks')` with
    NO `governed=` kwarg (silently defaulting False). On a GOVERNED,
    PM-approved plan this produced two spurious deny-worthy errors on a
    closed row lacking `pm_approved`, even though
    `check_plan_tasks_grouping_approval` had already cleared it — the write
    guard's own door was rejecting rows the grouping-approval predicate had
    just approved. Now: the schema shape check uses
    `_plan_tasks_schema_without_pm_approved_required` (frontmatter-layer,
    shared with `ops/plan_tasks_mutate.py`'s mutate path and with
    `check_plan_tasks_source`) to drop those branches on a governed plan,
    and the cross-field leg is called directly with the resolved
    `governed=` value instead of going through `_validate_frontmatter_obj`'s
    hardcoded default.
    """
    plan_tasks_schema = schemas.get("plan-tasks")
    if not isinstance(plan_tasks_schema, dict):
        return []

    try:
        result = _locate_fenced_block(prospective_content)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return []
    if result.status != _LocateStatus.LOCATED or result.body is None:
        return []

    try:
        parsed = _parse_yaml(result.body)
    except Exception as err:  # noqa: BLE001 — mirrors the whole-document-yaml catch below
        return [{
            "field": "(plan-tasks parse error)",
            "error": f"YAML parse error: {err}",
            "hint": "Ensure the ```yaml plan-tasks block is a valid YAML list of task rows",
        }]

    if not isinstance(parsed, list):
        return [{
            "field": "(plan-tasks)",
            "error": f"expected a YAML list of task rows, got {type(parsed).__name__}",
            "hint": "Each row is a `- id: ... title: ... change_kind: ... surface: ...` list item",
        }]

    governed = _is_governed_plan(frontmatter) if isinstance(frontmatter, dict) else False
    schema = (
        _plan_tasks_schema_without_pm_approved_required(plan_tasks_schema)
        if governed
        else plan_tasks_schema
    )

    errors: "list[dict]" = []
    for idx, row in enumerate(parsed):
        row_label = row.get("id") if isinstance(row, dict) and row.get("id") else f"index {idx}"
        if not isinstance(row, dict):
            errors.append({
                "field": f"tasks[{row_label}]",
                "error": f"row is not a mapping, got {type(row).__name__}",
                "hint": "Each task-spine row is a YAML mapping (id/title/change_kind/surface/...)",
            })
            continue
        row_errors = _validate_json_schema_node(row, schema, schema)
        row_errors.extend(_apply_cross_field_rules(
            row, "plan-tasks", governed=governed,
            plan_created=frontmatter.get('created') if isinstance(frontmatter, dict) else None,
        ))
        for err in row_errors:
            errors.append({
                "field": f"tasks[{row_label}].{err.get('field')}",
                "error": err.get("error"),
                "hint": err.get("hint"),
            })
    return errors


def _evaluate_schema_validation(
    schema_name: str, schema: dict, frontmatter: Optional[dict], prospective_content: str, repo_rel: str,
    schemas: dict,
) -> Optional[str]:
    """Returns a violation MESSAGE string on failure, or None when valid."""
    match_mode = schema.get("match_mode")

    if match_mode == "whole-document-yaml":
        try:
            if repo_rel.lower().endswith(".json"):
                try:
                    parsed = json.loads(prospective_content)
                except (json.JSONDecodeError, ValueError):
                    parsed = _parse_yaml(prospective_content)
            else:
                parsed = _parse_yaml(prospective_content)
        except Exception as err:  # noqa: BLE001 — mirrors source's bare catch
            return _violation_message(schema_name, [{
                "field": "(parse error)",
                "error": f"YAML parse error: {err}",
                "hint": "Ensure the file is valid YAML with no --- frontmatter fences",
            }])
        validation_result = _validate_frontmatter_obj(parsed, schema)
    elif match_mode == "no-frontmatter":
        validation_result = {"ok": True}
    else:
        if frontmatter is None:
            required_fields = _js_object_keys(schema.get("required"))
            hint = (
                f"expected fields: {', '.join(required_fields)}"
                if required_fields
                else "add --- delimited YAML frontmatter"
            )
            return _violation_message(schema_name, [{
                "field": "(missing frontmatter)",
                "error": "no YAML frontmatter found",
                "hint": hint,
            }])
        validation_result = _validate_frontmatter_obj(frontmatter, schema)

    errors = [] if validation_result.get("ok") else list(validation_result.get("errors", []))

    if schema_name == "plan":
        errors.extend(_plan_tasks_spine_errors(prospective_content, schemas, frontmatter))

    if not errors:
        return None
    return _violation_message(schema_name, errors)


def _evaluate_grouping_approval(prospective_content: str, repo_rel: str) -> Optional[str]:
    """Violation MESSAGE when a prospective plan write closes a spine row
    without the PM's recorded grouping approval, else None.

    Runs on the PROSPECTIVE content, which is the whole point of doing this
    in a write guard as well as in the mutate op: `plan_tasks_mutate` gates
    its own writes, but a plan's spine is an ordinary markdown fence that
    Write/Edit can rewrite directly, bypassing the op entirely. The op gate
    and this guard cover the two different doors to the same field.

    Returns None on anything that is not a governed plan with a readable
    spine — legacy plans, non-plan files, and unparseable spines are all
    somebody else's surface (see `check_plan_tasks_grouping_approval`).
    Fails OPEN on unexpected error: a guard that blocks every write when its
    own parse throws is worse than one that misses a case, and the mutate-op
    gate remains as the second door.

    Known redundant parse (accepted): `check_plan_tasks_grouping_approval`
    re-parses `prospective_content`'s frontmatter internally, even though
    `_reachability_and_schema_step` already has it parsed as `frontmatter`.
    Bounded cost (small markdown, no loops beyond spine rows) — not worth
    threading a signature change through for a nit.
    """
    try:
        error = _check_plan_tasks_grouping_approval(prospective_content)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return None
    if error is None:
        return None
    return (
        f"{repo_rel}: {error['error']}\n\n{error['hint']}"
    )


# Fifth UNCONDITIONAL deny (this plan's own row, C4) — see module docstring
# extension below. Path-keyed, exactly like the grouping-approval branch
# above and the reasons in its NEGATIVE SPEC comment: this must key on
# CONTENT (path + `status: deferred`), never on `schema_name`, so a corpus
# repoint or a re-vendor cannot silently switch this unconditional deny off.
_QUEUE_FAMILY_DIRS = ("improvement-queue", "debt-backlog", "bug-backlog")
_QUEUE_FAMILY_PATH_RE = re.compile(
    r"^state/(?:" + "|".join(_QUEUE_FAMILY_DIRS) + r")/"
)


def _is_queue_family_path(repo_rel: str) -> bool:
    return bool(_QUEUE_FAMILY_PATH_RE.match(repo_rel.replace("\\", "/")))


def _queue_deferral_grant_message(reason: str) -> str:
    # Message register (docs/wiki/guard-messaging.md § Register): one fact,
    # once, plus the terse alternative. Names ONLY the absent grant this
    # evaluator found — never unrelated fields from `schema_message`'s own
    # (advisory-only) finding on the same payload, per eng-director finding 3.
    return (
        f"A queue deferral is a grant the PM issues, not a status an agent "
        f"types: {reason}. Acquire the grant, then re-write."
    )


def _queue_record_fields(
    frontmatter: Optional[dict], prospective_content: str
) -> Optional[dict]:
    """The queue record's own fields, however the file carries them.

    THE ONE THING THAT MADE THIS DENY A SILENT NO-OP. Queue records are BARE
    YAML documents — `state/{improvement-queue,debt-backlog,bug-backlog}/*.yaml`
    has no `---` fence — and their schemas declare `match_mode:
    "whole-document-yaml"`, so `_evaluate_schema_validation` parses
    `prospective_content` for them and never reads `frontmatter` at all.
    `parse_frontmatter` returns None for any content not starting with `---`,
    so the `frontmatter` argument is not merely often None for this file class,
    it is STRUCTURALLY ALWAYS None. An evaluator keyed on it therefore returns
    at its first guard on every record it exists to guard, while passing every
    unit test that hands it a dict directly.

    Caught by the plan's own falsifier, which reported WARNED ONLY at a HEAD
    where the deny was fully written, correctly ordered, and covered by green
    tests — the "correctly-written arm that never executes" failure this plan's
    own predecessor handoff recorded as a lesson, reproduced one chunk later.
    That is what the falsifier is for, and it is why an executable falsifier
    outranks a passing suite as delivery evidence.

    Resolves the same way `_evaluate_schema_validation` does, so the deny and
    the schema layer can never disagree about what the record says: real
    frontmatter when the file has a fence, else the whole document parsed as
    YAML. Fails open to None on anything unparseable.
    """
    if frontmatter:
        return frontmatter
    try:
        parsed = _parse_yaml(prospective_content)
    except Exception:  # noqa: BLE001 — fail-open, matching every sibling evaluator
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_doe_owned_repo(repo_root: str) -> bool:
    """True when `repo_root` is DoE-claude's own checkout.

    THE QUEUE-DEFERRAL DENY IS CLAUDE-KLABAUTER-SCOPED BY AGREEMENT, and unlike C3's
    cross-field rule this one is a WRITE GUARD keyed on path pattern plus
    content — nothing about `state/debt-backlog/**` says whose repo it is. The
    guard runs from claude-klabauter's engine but fires in whatever session invokes the
    shared hook chain, so without this check a `/debt-triage` Step 6b class 4
    park in DoE's own tree is HARD-BLOCKED by claude-klabauter's rule.

    Measured 2026-08-29, not supposed: driving `check()` with a ceremony park
    written into `X:/DoE-claude/state/debt-backlog/` — `pm_approved`,
    `deferred_by: /debt-triage <session-id>`, `deferred_until`, `why_blocked`,
    exactly as their `SKILL.md:148-150` writes one — returned a deny on the
    absent `case_against`, which their ceremony does not stamp. That is Queue
    Terminus outcome class 4 refused in a sibling repo by this repo's rule: the
    precise harm this chunk's own `external_gate` was raised to prevent, and it
    would have shipped while that gate was certified discharged. C3's scoping
    fix did not cover it, because C3 scopes on SCHEMA provenance and a write
    guard never consults a schema path.

    Fail-safe direction is deliberate: any failure to resolve DoE's root answers
    False, so the deny keeps firing on claude-klabauter's own corpus. The failure mode of
    a wrong answer here is a rule that under-enforces at home, never one that
    reaches into a sibling.
    """
    try:
        doe_root = coordinator_doe_root()
        if not doe_root:
            return False
        return Path(repo_root).resolve() == Path(str(doe_root)).resolve()
    except Exception:  # noqa: BLE001 — fail-safe: keep enforcing locally
        return False


def _evaluate_queue_deferral_grant(
    frontmatter: Optional[dict],
    repo_rel: str,
    payload: Optional[Dict[str, Any]],
    prospective_content: str = "",
    repo_root: str = "",
) -> Optional[str]:
    """Violation MESSAGE when a prospective write to a queue-family record
    (``state/{improvement-queue,debt-backlog,bug-backlog}/**``) sets
    ``status: deferred`` without a genuine PM-issued grant, else ``None``.

    OWN INDEPENDENT EVALUATOR (dispatch brief, eng-director finding 3,
    major, accepted) — this reimplements the truthiness floor directly
    against ``frontmatter`` rather than calling
    ``schema_validate._cf_queue_disposition_shape`` (C3's cross-field rule).
    That rule runs inside ``_evaluate_schema_validation`` and its finding
    surfaces only through the terminal always-"advisory" ``schema_message``
    branch (2026-08-06 C15 ruling) — the one leg denying off of it would
    have silently re-classified as a deny, wrongly, since ``schema_message``
    can also fire for unrelated field errors on the SAME payload and this
    deny's message must name only the absent grant, never those. Keeping
    this evaluator independent, ordered before `schema_message`, is what
    keeps the two from ever describing each other's finding.

    Truthiness floor (mirrors `_cf_queue_disposition_shape` field-for-field,
    intentionally NOT shared code — see above): `pm_approved` must be the
    literal boolean `true`; `case_against` non-blank; `deferred_until` a
    parseable ISO-8601 date; `deferred_by` non-blank.

    THE SELF-GRANT DISCRIMINATOR (co-located here per eng-director finding
    6, major, accepted — `schema_validate.py` cannot host it: no
    authoring/agent/session property lives on any of the three schemas, and
    DoE's file-path import forbids an ambient session dependency). A floor,
    not a proof: a determined author can still write any string into
    `deferred_by`. Refuses only when `deferred_by` literally equals the
    firing payload's own `session_id` — the field's own schema description
    (see the three vendored queue schemas) already states `deferred_by`
    names the GRANTOR, never the requester.

    Fails OPEN on a missing/unparseable payload session id (never a false
    positive from an absent signal) and on any non-queue-family path or
    non-``deferred`` status (somebody else's surface).
    """
    if not _is_queue_family_path(repo_rel):
        return None
    if repo_root and _is_doe_owned_repo(repo_root):
        return None  # sibling repo's corpus — see `_is_doe_owned_repo`
    frontmatter = _queue_record_fields(frontmatter, prospective_content)
    if not frontmatter:
        return None
    if frontmatter.get("status") != "deferred":
        return None

    if frontmatter.get("pm_approved") is not True:
        return _queue_deferral_grant_message(
            f"pm_approved must be the literal boolean true (got {frontmatter.get('pm_approved')!r})"
        )

    case_against = frontmatter.get("case_against")
    if case_against is None or not str(case_against).strip():
        return _queue_deferral_grant_message("case_against is missing or blank")

    # EITHER form: a calendar date, or the condition the grantor named. The
    # ISO-date-only rule was withdrawn 2026-08-29 (DR-383 § Consequences) because
    # it refused the one record on disk that scrupulously recorded a PM-named
    # condition "rather than a fabricated date", while an invented date would have
    # satisfied it. A condition-form grant still has to come back; that is the
    # backstop in `orientation/expired_grant_signal.py`, not a refusal here.
    #
    # This check is a deliberate copy of `_cf_queue_disposition_shape`'s, not a
    # shared call (see this function's docstring). Copies drift: the withdrawal
    # landed in the cross-field rule first and this mirror kept refusing the
    # record for half an hour, which is exactly the failure duplication invites.
    # Change one, change all three.
    deferred_until = frontmatter.get("deferred_until")
    if deferred_until is None or not str(deferred_until).strip():
        return _queue_deferral_grant_message(
            f"deferred_until is missing or blank (got {deferred_until!r}) — give a "
            f"calendar date or the condition the grant names"
        )

    deferred_by = frontmatter.get("deferred_by")
    if deferred_by is None or not str(deferred_by).strip():
        return _queue_deferral_grant_message("deferred_by is missing or blank")

    session_id = ((payload or {}).get("session_id") or "").strip()
    if session_id and str(deferred_by).strip() == session_id:
        return _queue_deferral_grant_message(
            "deferred_by names the authoring session itself, not a PM grantor"
        )

    return None


_HANDOFF_KIND_SCHEMA_NAME = "handoff"


def _handoff_kind_off_enum_message(raw_kind: str, enum_values: "list[str]") -> str:
    # Review: code-reviewer -- Finding 1 (ae407001) -- this message must describe
    # only THIS deny's own behavior, not the write's overall outcome. A legacy
    # pre-rename spelling stands down HERE (it is not "never valid"), but the
    # base schema-shape check a few lines below still flags it as an invalid
    # enum value against the JSON-schema `kind` enum — as of the 2026-08-06
    # warn-not-block ruling that is now always an advisory (rendered by this
    # module under COORDINATOR_SCHEMA_STRICT=1, by the advisory sibling
    # otherwise), never a deny — alias tolerance is a READER contract, not a
    # writer one (D1 narrowed the on-disk enum deliberately; live handoff
    # records must be canonical). So this message must not claim aliases are
    # accepted end-to-end — only that this specific (unconditional) deny does
    # not fire for them.
    # Review: code-reviewer -- Finding 2 (ae407001) -- built from
    # _PRE_RENAME_ALIASES.items() (the module's own alias table) instead of
    # spelling the three retired names as literals, so this message can't
    # drift from the logic if that table ever changes.
    suggestions = difflib.get_close_matches(raw_kind, list(enum_values), n=1, cutoff=0.5)
    lead = f"Did you mean `kind: {suggestions[0]}`? " if suggestions else ""
    alias_clause = ", ".join(
        f"{retired} -> {target}"
        for retired, target in _HANDOFF_KIND_PRE_RENAME_ALIASES.items()
    )
    return (
        f"{lead}`kind: {raw_kind}` is not a recognized handoff kind (write-time "
        "hard-reject, scoped to state/handoffs/**). Valid values: "
        f"{', '.join(enum_values)}. Retired pre-rename names ({alias_clause}) do not "
        "trigger THIS particular deny — they de-alias to their D1 successors for the "
        "purpose of this check only. This does not mean a legacy spelling is accepted "
        "end-to-end: the on-disk `kind` enum is canonical-only, and a legacy spelling "
        "may still be flagged as an invalid enum value by schema-shape validation "
        "elsewhere (a non-blocking warning, not a deny)."
    )


def _evaluate_handoff_kind_enum(
    schema_name: Optional[str], schema: dict, frontmatter: Optional[dict]
) -> Optional[str]:
    """Violation MESSAGE when `kind` on a ``state/handoffs/**`` write is
    present and outside the schema enum (after de-aliasing retired D1
    pre-rename names via ``canonical_kind()``), else ``None``.

    Scoped to ``schema_name == "handoff"`` only — that schema's own
    ``applies_to: state/handoffs/*.md`` is the SAME scope this deny must
    hold to (see plan D3), so keying off schema_name rather than a
    hand-rolled path regex keeps the two in lockstep by construction.
    ``handoff-archived`` (archive/handoffs/**) is a different record
    family and deliberately NOT covered — narrowing the blast radius per
    plan D3's own "not other record families" constraint.

    An ABSENT `kind` is valid (the emitter injects the `session-handoff`
    default) — this returns `None` for that case, never denies a missing
    field.

    A non-scalar `kind` (YAML list/mapping) stands down HERE rather than
    being blindly ``str()``-coerced into a garbled "not a recognized
    handoff kind" message — the base JSON-schema type check a few lines
    below names the real defect (wrong type) instead of this deny
    misreporting it as an off-enum value.
    """
    if schema_name != _HANDOFF_KIND_SCHEMA_NAME or not frontmatter:
        return None
    raw_kind = frontmatter.get("kind")
    if raw_kind is None:
        return None
    if not isinstance(raw_kind, str):
        return None
    raw_str = raw_kind
    enum_values = list((schema.get("properties") or {}).get("kind", {}).get("enum") or [])
    if not enum_values:
        return None
    if raw_str in enum_values:
        return None
    if _canonical_kind(raw_str) in enum_values:
        return None
    return _handoff_kind_off_enum_message(raw_str, enum_values)


def _reachability_and_schema_step(
    schema_name: str,
    schema: dict,
    frontmatter: Optional[dict],
    prospective_content: str,
    repo_rel: str,
    repo_root: str,
    abs_file_path: str,
    schemas: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    # Computed but not yet emitted — mirrors source docstring point 9.
    schema_message = _evaluate_schema_validation(
        schema_name, schema, frontmatter, prospective_content, repo_rel, schemas
    )

    if schema_name in ("handoff", "handoff-archived") and frontmatter:
        handoff_dir = (
            os.path.dirname(abs_file_path)
            if schema_name == "handoff-archived"
            else os.path.join(repo_root, "state", "handoffs")
        )
        try:
            violations = _check_lineage_reachability(frontmatter, repo_root, handoff_dir)
        except Exception:  # noqa: BLE001 — fail-open, never block on infra
            violations = []
        if violations:
            return ("deny", _reachability_deny_message(violations))

    # Third UNCONDITIONAL deny (2026-07-29 grouping-approval contract),
    # alongside the own-inbox and lineage-reachability branches above and
    # independent of the always-"advisory" `schema_message` branch below (see
    # 2026-08-06 PM ruling, module docstring). Unconditional because an
    # ungated scope cut is not a
    # formatting nit that a non-strict tree can tolerate advisorily: the
    # whole point of the contract is that closing a row without the PM's
    # recorded assent must not be writable, and an advisory that a
    # well-meaning agent can write past is exactly the self-certification
    # the change removes.
    #
    # Deliberately schema-name-agnostic, unlike the fourth deny below (which
    # gates on `schema_name == "handoff"`). This branch relies entirely on
    # `check_plan_tasks_grouping_approval`'s own internal gates
    # (`is_governed_plan` plus a locatable plan-tasks fence) to stay silent on
    # non-plan documents.
    #
    # NEGATIVE SPEC — do not add a `schema_name == "plan"` gate here.
    # The original rationale was that claude-klabauter had no vendored
    # `plan.schema.json`, so such a gate would match nothing and silently
    # disable this whole deny — the exact unreachable-gate defect this change
    # was shipped to fix (cf. `is_governed_plan`'s dead `schema_version`
    # conjunct, same session, same failure shape). That premise expired at
    # `cb35ee4b1`, which DID vendor `plan.schema.json`; the prohibition did
    # not. It now rests on a second, independent reason: `schema_name` is
    # resolved from the vendored corpus, so gating on it couples this deny's
    # reachability to claude-klabauter's vendoring state — and a corpus repoint would
    # then silently switch an UNCONDITIONAL deny off, with no envelope to
    # report it. `b4c6df071` itself was the FIX for a related fail-open — it
    # stopped resolving the schema corpus from a sibling repo's live working
    # tree, where an uncommitted edit there changed claude-klabauter's enforcement with
    # no commit or version gate, and a clone without the sibling present got
    # ZERO schema enforcement, silently. But repointing at the vendored
    # corpus also narrowed the enforced path set as an unrecorded side
    # effect, since that corpus carries far fewer `applies_to` globs than
    # DoE's — a real, permanent narrowing traded for closing the total
    # fail-open. A guard whose reachability is a function of which schemas
    # happen to be vendored is not a guard. Keep this branch keyed on
    # content, never on corpus membership.
    grouping_message = _evaluate_grouping_approval(prospective_content, repo_rel)
    if grouping_message is not None:
        return ("deny", grouping_message)

    # Fifth UNCONDITIONAL deny (this plan's own row, C4): an ungranted
    # `status: deferred` on a queue-family record. Computed alongside
    # `_evaluate_grouping_approval` above (own independent evaluator, never
    # a `schema_message` re-classification — see `_evaluate_queue_deferral_
    # grant`'s own docstring) and returned BEFORE the always-"advisory"
    # `schema_message` branch below, exactly like the other four.
    queue_deferral_message = _evaluate_queue_deferral_grant(
        frontmatter, repo_rel, payload, prospective_content, repo_root
    )
    if queue_deferral_message is not None:
        return ("deny", queue_deferral_message)

    # Fourth UNCONDITIONAL deny (2026-07-29 D3 out-of-enum handoff `kind`
    # contract), alongside own-inbox, lineage-reachability, and
    # grouping-approval above, and independent of the always-"advisory"
    # `schema_message` branch below. Unconditional (not
    # COORDINATOR_SCHEMA_STRICT-gated, and NOT affected by the 2026-08-06 PM
    # warn-not-block ruling, which is scoped to the mode-dependent legs only)
    # because an invalid `kind` string is exactly the "textually-correct enum
    # enforced by nothing" failure mode the plan names — an advisory a
    # well-meaning agent can write past reproduces the same gap. Scoped
    # narrowly to ONLY the `kind` field on `state/handoffs/**` (schema_name
    # == "handoff") — every other schema violation on this same payload
    # (missing fields, wrong types elsewhere) still falls through to the
    # always-"advisory" `schema_message` branch below unchanged.
    kind_enum_message = _evaluate_handoff_kind_enum(schema_name, schema, frontmatter)
    if kind_enum_message is not None:
        return ("deny", kind_enum_message)

    if schema_message is not None:
        return ("advisory", schema_message)
    return None


# ---------------------------------------------------------------------------
# Full walk — first-match-wins, mirrors source main() dispatch order exactly.
# ---------------------------------------------------------------------------


def _unvendored_offerable_doc_type(
    ctx: "_Context", schemas: dict, frontmatter: Optional[dict], repo_rel: str
) -> Optional[dict]:
    """Cross-reference-2026-08-06-doe-claude-em-twelve-doc-types-lost-write-enforcement:
    detect a write that WOULD have matched an ``offerable: true`` manifest
    doc type but can't, purely because that type's schema is absent from the
    vendored corpus (see module docstring § Schema/manifest resolution and
    the DoE-claude cross-repo memo this line names). Returns the manifest
    docType dict on a match, else None.

    This is a heuristic re-derivation of the resolution `_match_schema`
    could not perform (there is no schema entry to key `_byGlob`/`_byKind`
    off), scoped to exactly the three shapes the manifest itself declares:
    an explicit `applies_to` glob, a sidecar `.{suffix}.md` filename, or
    (the fallback for plain doc types) a `kind:` frontmatter field equal to
    the manifest `type`. It does not — and must not — read any schema
    content, vendored or from a live DoE checkout: doing so would
    reintroduce exactly the live-checkout dependency `b4c6df071` removed.
    False negatives here just mean a missed diagnostic, never a missed
    schema application (this function never gates `_match_schema` itself);
    that asymmetry is why a heuristic is acceptable in an advisory-only path.
    """
    for doc_type in ctx.manifest.get("docTypes", []):
        if doc_type.get("offerable") is not True:
            continue
        schema_name = doc_type.get("schemaName")
        if not schema_name or schema_name in schemas:
            continue  # vendored (or schema-less by design) — not the gap this flags

        applies_to = doc_type.get("applies_to")
        if applies_to:
            if fnmatch.fnmatch(repo_rel.replace("\\", "/"), applies_to):
                return doc_type
            continue

        if doc_type.get("isSidecar"):
            suffix = doc_type.get("suffix")
            if suffix and _derive_sidecar_plan_stem(repo_rel, suffix) is not None:
                return doc_type
            continue

        if frontmatter is not None and frontmatter.get("kind") == doc_type.get("type"):
            return doc_type

    return None


def _unvendored_offerable_message(doc_type: dict) -> str:
    schema_name = doc_type.get("schemaName")
    type_ = doc_type.get("type")
    return (
        f"[unvendored-schema gap] Scaffold offer and shape validation are inactive for "
        f"`{type_}` documents: no `{schema_name}.schema.json`/`.yaml` is vendored in "
        "coordinator_core/frontmatter/schemas/, even though the registry manifest marks "
        f"`{type_}` offerable: true. This write proceeds unchecked. Remedy: vendor "
        f"{schema_name}'s schema into coordinator_core/frontmatter/schemas/ (see "
        "state/audits/2026-08-06-offerable-types-without-vendored-schemas.md for the "
        "per-type migration cost before vendoring)."
    )


def _first_result(
    payload: Dict[str, Any], _forensics: Optional[Dict[str, Any]] = None
) -> Optional[Tuple[str, str]]:
    """``_forensics`` (optional) lets `check()` observe the ACTUAL DoE-root/
    schema-corpus state this call resolved and validated against — see
    `_capture_guard_forensics`. Every stash into it below is a reference/dict
    assignment on data already computed for the walk itself (no extra I/O,
    subprocess, or hashing here); passing ``None`` is the exact pre-existing
    code path, unchanged.
    """
    tool_name = payload.get("tool_name")
    if tool_name not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    cwd = payload.get("cwd") or os.getcwd()
    abs_file_path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    # Cwd-vs-target defect: repo_root is resolved from the TARGET FILE's own
    # repo, not the session's cwd. A session rooted at repo A writing into
    # SIBLING repo B's tree used to derive repo_root from cwd (repo A), so
    # `_to_repo_relative` always returned None for a repo-B path and this
    # whole walk short-circuited before ANY schema step ran — every
    # schema-governed doc type was silently unvalidated on a cross-repo
    # write. See `check()` for the companion rule this enables: a write
    # whose repo_root differs from the session's OWN repo is a cross-repo
    # write, and gets ADVISORY-ONLY treatment there — never a deny, whatever
    # the finding class (DR-277) — even for the five findings that deny
    # unconditionally in-repo. In-repo callers (repo_root == session repo)
    # are unaffected: the resolved root is identical either way.
    target_dir = os.path.dirname(abs_file_path) or cwd
    repo_root = _git_show_toplevel(cwd=target_dir) or cwd
    repo_rel = _to_repo_relative(abs_file_path, repo_root)
    if not repo_rel:
        return None

    session_repo_root = _git_show_toplevel(cwd=cwd) or cwd
    # `os.path.normcase` is a no-op on POSIX (macOS APFS included) -- it
    # only folds case on Windows. On a case-insensitive-but-case-preserving
    # filesystem a `repo_root` and `session_repo_root` that differ only in
    # case would compare unequal here and this write would be wrongly
    # classified as cross-repo, downgrading a would-be in-repo hard-deny to
    # advisory-only (see the DR-277 comment above `target_dir` for why that
    # distinction is a real security boundary, not cosmetic). Route through
    # the module's designated helper instead, per
    # `coordinator_core/write_guards/_case_fold_path.py`'s docstring.
    is_cross_repo_write = casefold_path(os.path.abspath(repo_root)) != casefold_path(
        os.path.abspath(session_repo_root)
    )
    if _forensics is not None:
        _forensics["is_cross_repo_write"] = is_cross_repo_write

    ctx = _load_context(_forensics=_forensics)
    if ctx is None:
        return None

    memo_result = _memo_guard_step(
        ctx, tool_name, tool_input, repo_root, abs_file_path, repo_rel, payload
    )
    if memo_result is not None:
        return memo_result

    schema_corpus_retry_record: Dict[str, Any] = {}
    try:
        schemas = _retry_on_transient_read_failure(
            lambda: _load_schemas(str(ctx.schemas_dir)),
            exists_path=ctx.schemas_dir,
            record=schema_corpus_retry_record,
        )
    except Exception:  # noqa: BLE001 — mirrors source's schema-load try/except
        if _forensics is not None:
            _forensics["schema_corpus_retry"] = schema_corpus_retry_record
            _forensics["schema_corpus_load_failed"] = True
        return None
    if _forensics is not None:
        _forensics["schema_corpus_retry"] = schema_corpus_retry_record
    schemas = _merge_memo_schemas(ctx, schemas)
    if _forensics is not None:
        # Reference to the SAME dict object `check()` will validate against
        # below — this is the actual verdict-producing schema state, not a
        # re-read of it. Storing the reference costs nothing; only
        # `_capture_guard_forensics` (deny/load-failure branches only) later
        # hashes `schemas["plan-tasks"]` out of it.
        _forensics["schemas"] = schemas

    if tool_name == "Write":
        prospective_content = tool_input.get("content") or ""
    elif tool_name == "Edit":
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        try:
            with open(abs_file_path, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            return None
        result, matched = _apply_edit(current, old_string or "", new_string or "")
        if not matched:
            return None
        prospective_content = result
    else:  # MultiEdit
        try:
            with open(abs_file_path, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        content = current
        for edit in tool_input.get("edits") or []:
            result, matched = _apply_edit(
                content, edit.get("old_string") or "", edit.get("new_string") or ""
            )
            if not matched:
                return None
            content = result
        prospective_content = content

    try:
        frontmatter = _parse_frontmatter(prospective_content).get("frontmatter")
    except Exception:  # noqa: BLE001 — fail-open
        frontmatter = None

    try:
        match = _match_schema(repo_rel, frontmatter, schemas)
    except Exception:  # noqa: BLE001 — fail-open
        match = None
    if not match:
        if _forensics is not None:
            try:
                gap_doc_type = _unvendored_offerable_doc_type(ctx, schemas, frontmatter, repo_rel)
            except Exception:  # noqa: BLE001 — fail-open, never block on infra
                gap_doc_type = None
            if gap_doc_type is not None:
                _forensics["unvendored_offerable_doc_type"] = gap_doc_type
        return None

    schema_name = match.get("schemaName")
    schema = match.get("schema")
    if _forensics is not None:
        _forensics["matched_schema_name"] = schema_name

    scaffold_result = _scaffold_offer_step(ctx, tool_name, abs_file_path, schema_name, frontmatter, repo_rel)
    if scaffold_result is not None:
        return scaffold_result

    return _reachability_and_schema_step(
        schema_name, schema, frontmatter, prospective_content, repo_rel, repo_root, abs_file_path, schemas,
        payload=payload,
    )


_FORENSICS_DIRNAME = "write-guard-forensics"
_FORENSICS_GUARD_NAME = "validate_frontmatter_schema_deny"


def _is_torn_read_signature(forensics: Dict[str, Any]) -> bool:
    """True only when a retry record shows a path that EXISTED but was
    never cleanly read (exhausted every attempt) — the actual torn-read
    shape this module's docstring calls out, as distinct from a merely
    absent path (partial install, no sibling checkout: a steady state that
    must NOT trigger a forensics write on every guarded call on such a
    machine). See `_retry_on_transient_read_failure`'s ``record`` contract.
    """
    for key in ("manifest_load_retry", "schema_corpus_retry"):
        rec = forensics.get(key)
        if isinstance(rec, dict) and rec.get("path_existed") and rec.get("succeeded") is False:
            return True
    return False


def _plan_tasks_schema_identity(forensics: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Returns (status, sha256) describing the plan-tasks schema THAT
    PRODUCED THE VERDICT — never a fresh re-read. ``forensics["schemas"]``
    (when present) is the exact dict object `_first_result` validated
    against and stashed by reference at load time; the hash is computed
    HERE, lazily, only when a capture is actually happening (deny or a
    genuine load-failure) — never on an ordinary allow.

    Distinguishes explicitly (per the "blank reads as unknown, a hash of
    the wrong bytes reads as a false all-clear" concern this replaces):
      - "hashed"            — schemas loaded, plan-tasks present -> real hash.
      - "absent_in_corpus"  — schemas loaded, but no plan-tasks entry at all.
      - "load_failed"       — the schema-corpus read itself failed/exhausted.
      - "context_unavailable" — DoE root/manifest never resolved this call.
      - "not_loaded"        — context resolved fine, but an EARLIER step
                               (e.g. the memo-guard's unconditional own-inbox
                               deny) fired before the schema corpus was ever
                               read for this call — there is no corpus read
                               to report on, not a failure.
    """
    if forensics.get("schema_corpus_load_failed"):
        return "load_failed", None
    schemas = forensics.get("schemas")
    if isinstance(schemas, dict):
        plan_tasks_schema = schemas.get("plan-tasks")
        if isinstance(plan_tasks_schema, dict):
            digest = hashlib.sha256(
                json.dumps(plan_tasks_schema, sort_keys=True).encode("utf-8")
            ).hexdigest()
            return "hashed", digest
        return "absent_in_corpus", None
    if (
        forensics.get("manifest_load_failed")
        or forensics.get("doe_root_unresolvable")
        or forensics.get("doe_root_resolve_raised")
        or forensics.get("context_fields_malformed")
    ):
        return "context_unavailable", None
    return "not_loaded", None


def _capture_guard_forensics(
    payload: Dict[str, Any],
    forensics: Dict[str, Any],
    *,
    capture_reason: str,
    deny_reason: Optional[str],
) -> None:
    """Best-effort forensic snapshot, fired ONLY from `check()`'s deny branch
    or its genuine-load-failure branch (see `_is_torn_read_signature`) — by
    the time this runs, the verdict already exists (or the guard has
    already fallen through to fail-open); nothing here can change it. Every
    step is independently wrapped and silently abandoned on error.

    Captures the state that ACTUALLY produced this call's verdict —
    ``forensics`` is populated by `_load_context`/`_first_result` from
    objects those functions already loaded for their own purposes (see
    their docstrings); this function performs NO re-read of DoE-claude's
    schema corpus. The only I/O this function itself performs is a
    best-effort ``git status`` in the DoE root (dirty-tree signal) and the
    forensics file write — both gated to the deny/load-failure branches
    only, so an ordinary allow never reaches this function at all.

    Writes to `state/scratch/write-guard-forensics/` — `state/scratch/` is
    this repo's existing gitignored-at-any-depth home for transient
    investigation byproduct (see `.gitignore`'s `scratch/` entry), so these
    dumps are swept the same way any other scratch artifact is, never
    committed.
    """
    try:
        cwd = payload.get("cwd") or os.getcwd()
        repo_root = _git_show_toplevel(cwd=cwd) or cwd

        plan_tasks_status, plan_tasks_sha256 = _plan_tasks_schema_identity(forensics)

        doe_root = forensics.get("doe_root")
        doe_tree_dirty: Optional[bool] = None
        if doe_root:
            try:
                git_result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=doe_root, capture_output=True, text=True, timeout=2,
                    **no_console_creationflags(),
                )
                if git_result.returncode == 0:
                    doe_tree_dirty = bool(git_result.stdout.strip())
            except Exception:  # noqa: BLE001 — forensics must never raise
                doe_tree_dirty = None

        record = {
            "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "guard": _FORENSICS_GUARD_NAME,
            "capture_reason": capture_reason,
            "tool_name": payload.get("tool_name"),
            "file_path": (payload.get("tool_input") or {}).get("file_path"),
            "deny_reason": deny_reason,
            "matched_schema_name": forensics.get("matched_schema_name"),
            "doe_root": doe_root,
            "schema_corpus_path": forensics.get("schemas_dir"),
            "plan_tasks_schema_status": plan_tasks_status,
            "plan_tasks_schema_sha256": plan_tasks_sha256,
            "manifest_load_retry": forensics.get("manifest_load_retry"),
            "schema_corpus_retry": forensics.get("schema_corpus_retry"),
            "doe_tree_dirty_at_capture": doe_tree_dirty,
        }

        out_dir = Path(repo_root) / "state" / "scratch" / _FORENSICS_DIRNAME
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        out_path = out_dir / f"{_FORENSICS_GUARD_NAME}-{ts}-{os.getpid()}.json"
        out_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001 — forensics must never raise, never block a write
        pass


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the frontmatter-schema HARD-DENY leg.

    Cwd-vs-target defect + advisory-only cross-repo rule (2a): `_first_result`
    resolves `repo_root` from the TARGET FILE's own repo, not the session's
    cwd (see its own comment) — a session rooted at one repo writing into a
    SIBLING repo's tree used to see `_to_repo_relative` return `None` before
    any schema step ran, so every schema-governed doc type was silently
    unvalidated on a cross-repo write. Making that write reachable does NOT
    make it denyable: a write whose resolved `repo_root` differs from the
    session's OWN repo (`forensics["is_cross_repo_write"]`) gets ADVISORY-ONLY
    treatment below, whatever the finding class — including the five findings
    that deny unconditionally in-repo. This is DR-277 (guards are advisory by
    default) applied deliberately, not a hardening: a sibling guard's hardness
    on another tool surface (here, this module's own in-repo behavior) is not
    grounds for hardening the cross-repo case too. In-repo behavior
    (`is_cross_repo_write` False) is BYTE-IDENTICAL to before this fix.

    Returns a hard-deny envelope only for the five genuinely-UNCONDITIONAL
    findings (own-inbox misplacement, lineage-reachability, grouping-approval
    scope cut, out-of-enum handoff `kind`, ungranted queue-deferral — see the
    five "UNCONDITIONAL deny" call sites in `_reachability_and_schema_step`
    and `_memo_guard_step`'s own-inbox branch), none of which are gated on
    `COORDINATOR_SCHEMA_STRICT`
    at all, and ONLY for an in-repo target. Every other first-firing step is
    shape=="advisory" (2026-08-06 PM ruling: a schema-shaped violation warns,
    never hard-blocks — see this module's docstring). In default (non-strict)
    mode this leg stays silent for those and lets
    ``validate_frontmatter_schema_advisory`` render the warning (mutual
    exclusivity, unchanged); under ``COORDINATOR_SCHEMA_STRICT=1`` the
    advisory sibling stands down for its own warn-by-default branches and
    THIS module renders the identical `additionalContext` warning instead —
    strict no longer escalates these findings to a deny, it only moves which
    module renders the warning. One further non-blocking exception: when
    `_match_schema` found nothing because the write's doc type is
    `offerable: true` in the manifest but has no vendored schema (see
    `_unvendored_offerable_doc_type`), this returns a non-blocking
    `additionalContext` diagnostic instead of the silent `None` a genuinely-
    non-matching write gets — that silence is the regression this closes
    (cross-repo/inbox/2026-08-06-doe-claude-em-twelve-doc-types-
    lost-write-enforcement-today.md).
    """
    forensics: Dict[str, Any] = {}
    try:
        result = _first_result(payload, _forensics=forensics)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return None

    if result is None:
        # Fail-open on a genuine torn read (path existed, every retry
        # attempt still failed) is exactly the unreproducible-deny shape
        # this capture exists for, even though it resolves to an ALLOW —
        # gated on `_is_torn_read_signature` so an ordinary absent-DoE-root
        # machine (a steady state, not a failure) never pays this cost.
        if _is_torn_read_signature(forensics):
            _capture_guard_forensics(
                payload, forensics, capture_reason="load_failure_fail_open", deny_reason=None
            )
        # Advisory-only, never-block surfacing of the unvendored-schema gap
        # (see `_unvendored_offerable_doc_type`) — reuses the same
        # `hookSpecificOutput.additionalContext` non-blocking sink the
        # advisory sibling module's scaffold/memo offers already use, rather
        # than adding a new diagnostic channel. Fires at most once per write
        # (one doc type can match) so it cannot become per-write noise.
        gap_doc_type = forensics.get("unvendored_offerable_doc_type")
        if gap_doc_type is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _unvendored_offerable_message(gap_doc_type),
                },
            }
        return None

    shape, message = result
    if shape == "deny":
        if forensics.get("is_cross_repo_write"):
            # DR-277 advisory-by-default (docs/decisions/DR-277-guards-are-
            # advisory-by-default-two-named.md): a target outside the
            # session's OWN repo never denies here, whatever the finding
            # class — including the five findings that deny unconditionally
            # in-repo (own-inbox misplacement, lineage-reachability,
            # grouping-approval scope cut, out-of-enum handoff `kind`,
            # ungranted queue-deferral). This
            # is closing the cwd-vs-target blindness (`_first_result`) into
            # a WARNING, not a new hard-block surface: cross-surface parity
            # with the in-repo deny is explicitly not grounds for hardening
            # (DR-277). In-repo behavior (this branch's `else`) is untouched.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        "[frontmatter-schema guard — advisory only, cross-repo target, "
                        f"write proceeds] {message}"
                    ),
                }
            }
        _capture_guard_forensics(payload, forensics, capture_reason="deny", deny_reason=message)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }

    # shape == "advisory". Under default (non-strict) mode the advisory
    # sibling module owns this exact finding and this leg stays silent
    # (mutual exclusivity, unchanged). Under COORDINATOR_SCHEMA_STRICT=1 the
    # sibling stands down for every one of ITS OWN warn-by-default branches
    # (`if _is_strict(): return None`, see validate_frontmatter_schema_advisory.py)
    # -- previously that was because THIS module escalated to a hard deny for
    # the identical finding. PM ruling (2026-08-06, this module's ruling
    # backlink: docs/plans/2026-08-06-apply-guard-class-census.md C15): a
    # schema-shaped violation must warn, never hard-block -- a denied write
    # throws away every token spent composing it, whereas a warning still
    # surfaces the finding to whoever can fix it. So under strict this module
    # now renders the SAME advisory shape the sibling would have rendered in
    # non-strict mode, rather than a deny -- the sibling's stand-down is no
    # longer "this becomes a hard block instead", it is "this module takes
    # over emitting the identical warning". Mutual exclusivity holds in both
    # modes: exactly one of {sibling, this module} ever renders a given
    # finding, never both, never neither.
    if _is_strict():
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    "[frontmatter-schema guard — advisory only, write proceeds] "
                    f"{message}"
                ),
            }
        }
    return None
