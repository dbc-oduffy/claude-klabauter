"""coordinator_core.write_guards.validate_frontmatter_schema_advisory — advisory
leg of the fan-in split of DoE's
``coordinator/hooks/scripts/validate-frontmatter-schema.py`` PreToolUse
(Write|Edit|MultiEdit) hook, per the write-guard fan-in
(write_guards/INTERFACE.md) and the wave map's C11 slot.

CLASS/MATCHERS/PRIORITY convention per docs/wiki/write-guard-priority-bands.md
(the real SSOT -- the prior citation, state/plan-sidecars/2026-07-29-hook-
fan-in-write-path.priority-map.md, named a file that was never written).

Split rationale
----------------
The reference hook mixes hard-deny outcomes (lineage-reachability violations,
the own-inbox misplacement guard — both UNCONDITIONAL, never gated on
``COORDINATOR_SCHEMA_STRICT``) with warn-by-default outcomes (schema
validation, the memo mislocation/hand-rolling offer, the routing-mismatch
offer, the scaffold offer — all of which upgrade to deny ONLY under
``COORDINATOR_SCHEMA_STRICT=1``). A single-CLASS module cannot represent
both, so the port splits into two modules that share nearly the entire
control-flow reconstruction:

  - ``validate_frontmatter_schema_deny`` (C10, CLASS hard-deny): the
    unconditional-deny paths, plus the STRICT-mode shape of every
    warn-by-default path.
  - This module (C11, CLASS advisory): the WARN-mode (default,
    ``COORDINATOR_SCHEMA_STRICT`` unset) shape of every warn-by-default
    path. Returns ``None`` whenever the payload would instead resolve to
    one of the sibling module's outcomes — see "Mutual exclusivity" below.

Both modules reconstruct the SAME dispatch order the reference hook's
``main()`` uses (memo guards → schema load → prospective-content build →
tracked-ness gate → scaffold offer → schema validation, with lineage
reachability checked last and overriding whatever schema validation
computed) because the reference hook emits AT MOST ONE payload per write —
reproducing only the "warn" branches in isolation, without walking the
same full sequence up to that point, would misfire whenever an earlier
stage (e.g. the own-inbox guard, or a lineage-reachability violation)
should have been the sole result instead.

Mutual exclusivity (the invariant C1's differential corpus asserts)
--------------------------------------------------------------------
For any given payload, at most one of {this module, the deny sibling}
returns non-``None``:
  - The own-inbox guard and lineage-reachability are UNCONDITIONAL denies —
    this module returns ``None`` for both, unconditionally, regardless of
    ``COORDINATOR_SCHEMA_STRICT``. They are exclusively the deny sibling's
    territory.
  - The four warn-by-default paths (schema validation, memo offer, routing
    offer, scaffold offer) go to THIS module when
    ``COORDINATOR_SCHEMA_STRICT`` is unset, and to the deny sibling when it
    is ``"1"`` — this module returns ``None`` in the strict case.

Fidelity
--------
Every ``additionalContext`` string below is copied byte-for-byte from the
reference hook's warn-mode payload builders (``build_violation_payload``,
``build_memo_offer_payload``, ``build_memo_routing_offer_payload``,
``build_scaffold_offer_payload``). Every escape-hatch env var
(``COORDINATOR_SCHEMA_STRICT``, ``COORDINATOR_OVERRIDE_OWN_INBOX``,
``COORDINATOR_OVERRIDE_MEMO_REDIRECT``) is read exactly as the reference
hook reads it. Fail-open narrowing (missing/unresolvable DoE root, manifest,
schemas, or DAG helper) always returns ``None`` — never fabricates an
advisory from partial state, mirroring the reference hook's "skip
validation, never block" posture (which degrades identically for a warn
outcome: no advisory rather than a wrong one).

In-process schema/manifest/DAG resolution
-------------------------------------------
The reference hook lives in DoE-claude and must resolve INTO claude-klabauter
for schema validation, DAG lineage-walk, and the two claude-klabauter-generated memo
schemas — hence its ``_claude_klabauter_root``/``sys.path`` seam (D1a). This module
lives IN claude-klabauter already, so those three imports
(``coordinator_core.frontmatter.schema_validate``, ``coordinator_core.dag``,
``coordinator_core.contract``) are direct, in-tree imports — no seam, no
``sys.path`` manipulation needed. The DIRECTION this module still needs to
resolve is the reverse one: the DoE-claude sibling checkout, for
``coordinator/schemas/`` (the schema corpus, contract-owned by DoE, not
Claude-klabauter) and ``coordinator/schemas/coordinator-registry.manifest.json`` (the
registry manifest). That resolution goes through
``coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`` — the
same ratified "resolve the DoE-claude sibling root" ladder the reference
hook's own DoE-side callers use elsewhere, now called natively in-process
rather than via the ``machine-local get repos.doe_claude`` subprocess the
reference hook shells out to. Same target, same effective resolution (that
subprocess call is rung 2 of this very ladder), no behavior change.

Import-safety: per INTERFACE.md rule 7, no resolution work (DoE-root
lookup, manifest load, schema load, git-root subprocess) happens at import
time — regex/module-level constants only. All of it happens inside
``check()``, matching the reference hook's own per-invocation (spawn-per-
call) freshness.

Spec backlink: DoE-claude:pln-hook-fan-in-fold-the-pretoolus-27c1e9 § C11
Source: DoE-claude coordinator/hooks/scripts/validate-frontmatter-schema.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.dag import check_lineage_reachability as _check_lineage_reachability
from coordinator_core.frontmatter.baton_class import canonical_kind as _canonical_kind
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.schema_validate import (
    _apply_cross_field_rules,
    _plan_tasks_schema_without_pm_approved_required,
    _validate_json_schema_node,
    check_plan_tasks_grouping_approval as _check_plan_tasks_grouping_approval,
    is_governed_plan as _is_governed_plan,
    load_schemas,
    match_schema,
    parse_frontmatter,
    parse_yaml,
    validate_frontmatter_obj,
)
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import (
    resolve_repo_root as _shared_resolve_repo_root,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 100

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

_MEMO_SCHEMA_NAMES = ("cross-repo-memo", "archived-memo")
_DOE_CLAUDE_REGISTRY_KEY = "repos.doe_claude"

# The vendored, version-pinned schema corpus this module now validates
# against — see module docstring § In-process schema/manifest/DAG
# resolution and docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-
# corpus.md. Resolved relative to this file's own on-disk location (this
# module runs INSIDE claude-klabauter already), never from DoE-claude's live working
# tree. The registry MANIFEST (`_load_doe_registry`) stays on DoE's tree —
# it is routing/scaffold logic, not a schema, and is out of scope for this
# repoint (plan AC4 + Out of scope).
_VENDORED_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas"

# ---------------------------------------------------------------------------
# Torn-write retry — DoE's schema corpus and registry manifest are resolved
# from the LIVE working tree of a sibling repo checkout (see module docstring
# § In-process schema/manifest/DAG resolution), which concurrent sessions
# edit continuously. A `check()` call can land mid-write and see a torn/
# partial JSON file. Every load site below already treats a load failure as
# fail-open (never block on infra) — this retry only narrows the window in
# which a TRANSIENT torn-write trips that fail-open path; it changes nothing
# about what happens once every attempt is exhausted, so it cannot make this
# module fire an advisory it wouldn't have fired before. Duplicated from the
# deny sibling rather than imported — same rationale as this module's own
# duplicated control-flow walk (mutual-exclusivity docstring, deny sibling).
#
# ABSENT vs TORN: a missing path (partial install, clone without the schemas
# tree, permissions problem) is a STEADY STATE, not a race — retrying it only
# pays sleep cost on every guarded Write/Edit/MultiEdit for no benefit (this
# guard fires on every edit in the repo). `exists_path` is checked ONCE, up
# front, outside the retry loop: absent -> call `fn()` exactly once and let
# whatever it raises propagate immediately, zero sleep; present -> retry,
# because presence-but-unparseable is the actual torn-write signature this
# helper exists to bridge.
# ---------------------------------------------------------------------------

_TORN_WRITE_RETRY_ATTEMPTS = 3
_TORN_WRITE_RETRY_DELAY_SECS = 0.05

_T = TypeVar("_T")


def _retry_on_transient_read_failure(fn: Callable[[], _T], *, exists_path: "str | Path") -> _T:
    """Retry ``fn()`` up to ``_TORN_WRITE_RETRY_ATTEMPTS`` times with a short
    delay between attempts, re-raising the LAST exception if every attempt
    fails — but ONLY when ``exists_path`` is present on disk at call time.
    An absent ``exists_path`` calls ``fn()`` exactly once, no sleep, no
    retry: absence is terminal, not transient.
    """
    if not os.path.exists(exists_path):
        return fn()

    last_exc: Optional[BaseException] = None
    for attempt in range(_TORN_WRITE_RETRY_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — bounded by attempts, re-raised below
            last_exc = exc
            if attempt < _TORN_WRITE_RETRY_ATTEMPTS - 1:
                time.sleep(_TORN_WRITE_RETRY_DELAY_SECS)
    assert last_exc is not None
    raise last_exc

# ---------------------------------------------------------------------------
# Pure regex / string helpers — ports of the reference hook's module-level
# helpers. No I/O, safe at import time.
# ---------------------------------------------------------------------------


def _is_strict() -> bool:
    return os.environ.get("COORDINATOR_SCHEMA_STRICT") == "1"


def _js_object_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return [str(i) for i in range(len(value))]
    return []


def resolve_repo_root(cwd: str) -> str:
    """Repo root for `cwd` via the shared memoized resolver
    (`coordinator_core.write_guards._repo_root.resolve_repo_root`), falling
    back to `cwd` itself on any resolution failure -- preserves the
    reference hook's own fall-back-to-cwd behavior (never `None`) exactly,
    since downstream callers treat this return value as a directory to
    join/relativize against, not an optional.
    """
    try:
        root = _shared_resolve_repo_root(cwd)
    except Exception:  # noqa: BLE001 — mirrors reference's bare catch-and-fall-back
        return cwd
    return root or cwd


def to_repo_relative(abs_path: str, repo_root: str) -> Optional[str]:
    normal_abs = abs_path.replace("\\", "/")
    normal_root = repo_root.replace("\\", "/")
    # Comparison-only fold: the returned `rel` (sliced from `normal_abs`,
    # original case) is used downstream only for regex/pattern matching —
    # never disk I/O (callers use `abs_file_path` for that). Fold only the
    # prefix-match operands, not `normal_abs` itself.
    if not casefold_path(normal_abs).startswith(casefold_path(normal_root)):
        return None
    rel = normal_abs[len(normal_root):]
    if rel.startswith("/"):
        rel = rel[1:]
    return rel


def apply_edit(content: str, old_string: str, new_string: str) -> tuple[str, bool]:
    idx = content.find(old_string)
    if idx == -1:
        return content, False
    return content[:idx] + new_string + content[idx + len(old_string):], True


def is_memo_path_mislocated(repo_rel: str) -> bool:
    normalized = repo_rel.replace("\\", "/")
    if re.match(r"^cross-repo/", normalized):
        return False
    if re.search(r"(?:^|/)memos/", normalized):
        return True
    return False


def has_free_form_memo_header(content: str) -> bool:
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


def extract_yaml_to_field(content: str) -> Optional[str]:
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


def extract_yaml_to_repo_field(content: str) -> Optional[str]:
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


def em_id_for_basename(basename: str, repo_basename_to_em_shortname: dict) -> str:
    if basename in repo_basename_to_em_shortname:
        return repo_basename_to_em_shortname[basename] + "-em"
    return basename.replace("_", "-") + "-em"


def registry_key_for_basename(basename: str, manifest: dict) -> str:
    for alias in manifest.get("identity", {}).get("repoAliases", []):
        if alias.get("dirBasename") == basename:
            return f"repos.{alias['registryKey']}"
    return "repos." + basename.lower().replace("-", "_")


def derive_sidecar_plan_stem(repo_rel: str, sidecar_type: str) -> Optional[str]:
    normalized = repo_rel.replace("\\", "/")
    basename = normalized.split("/")[-1]
    suffix = f".{sidecar_type}.md"
    if basename and basename.endswith(suffix):
        return basename[: -len(suffix)]
    return None


# ---------------------------------------------------------------------------
# Advisory payload builders — byte-for-byte from the reference hook's
# warn-mode (non-strict) branch of each payload builder. Each returns None
# when _is_strict() is True (that shape belongs to the deny sibling).
# ---------------------------------------------------------------------------


def build_violation_payload_advisory(
    schema_name: str,
    errors: list[dict],
    *,
    payload: Optional[dict] = None,
    git_root: Optional[str] = None,
) -> Optional[dict]:
    # Review: staff-eng (B8 leg (d)+(f)) -- this builder hand-rolled its own
    # "see docs/reference/guard-override-keys.md" pointer, unconditionally,
    # for every audience including a dispatched subagent. Routed through
    # `bash_guards._helpers.operator_override_note` so it degrades to the
    # empty string for a non-EM audience like every other override-key
    # pointer site, instead of hand-editing the literal.
    if _is_strict():
        return None
    parts = []
    for e in errors:
        field = e.get("field") or "(unknown)"
        hint = f"; required shape: {e['hint']}" if e.get("hint") else ""
        parts.append(f"{field}: {e.get('error')}{hint}")
    message = f"{schema_name}: {'; '.join(parts)}"

    override_note = operator_override_note(
        "COORDINATOR_SCHEMA_STRICT", payload=payload, git_root=git_root
    )
    trailer_parts = [
        "The write will proceed. Fix the frontmatter on the next edit."
    ]
    if override_note:
        trailer_parts.append(override_note)
    trailer_parts.append("Periodic drift is swept by /update-docs.")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"[frontmatter-schema warning] {message}\n\n"
                + " ".join(trailer_parts)
            ),
        },
    }


def build_memo_routing_offer_payload_advisory(resolved_recipient_em_id: Optional[str]) -> Optional[dict]:
    if _is_strict():
        return None
    recipient_hint = resolved_recipient_em_id or "<receiver-em-id>"
    message = (
        f"This memo's `to:` field ({recipient_hint}) does not match the repo you "
        "are writing into. Cross-repo memos must land in the RECIPIENT'S repo, "
        "not the sender's. Use the CLI to route it correctly:\n\n"
        f"  cross-repo-memo --to {recipient_hint} --topic <slug> --title \"...\" < body.md\n\n"
        f"The CLI writes one dirty file into {recipient_hint}'s cross-repo/inbox/ "
        "directory so it surfaces in their git status. Hand-rolling to the wrong "
        "repo means the recipient will never find the memo."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[cross-repo-memo routing offer] {message}",
        },
    }


def build_memo_offer_payload_advisory() -> Optional[dict]:
    if _is_strict():
        return None
    message = (
        "This looks like a cross-repo memo being hand-rolled to a "
        "non-canonical location. Use the CLI instead so it lands in the "
        "receiver's cross-repo/inbox/ surface:\n\n"
        "  cross-repo-memo --to <receiver-repo-name> --topic <slug>\n\n"
        "The CLI writes one dirty file into the receiver's cross-repo/inbox/ "
        "directory (status: open), leaves it uncommitted so it surfaces in "
        "their git status, and prints the path for you to hand the PM for "
        "relay. Hand-rolling bypasses schema validation, delivery "
        "guarantees, and discoverability."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[cross-repo-memo offer] {message}",
        },
    }


def build_scaffold_offer_payload_advisory(
    schema_name: str,
    type_: str,
    derived_args: Optional[str],
    authoring_hint: Optional[str],
    resolved_kind: Optional[str] = None,
) -> Optional[dict]:
    if _is_strict():
        return None
    args = f" {derived_args}" if derived_args else ""
    cmd = f"coordinator-doc-new --type {type_}{args}"
    schema_label = (
        f"schema: {schema_name}, resolved by kind: {resolved_kind}"
        if resolved_kind
        else f"schema: {schema_name}"
    )
    message = (
        f"This is a new schema-matching document ({schema_label}). "
        "Use the scaffolder to generate conformant frontmatter:\n\n"
        f"  {cmd}\n\n"
        "Then fill the body via Edit. The scaffolder creates the file via Python "
        "open() (structurally exempt from this PreToolUse Write-tool hook — see "
        "new-file-only rationale below), so subsequent body-fill edits stay silent. "
        "Hand-rolling bypasses schema enforcement and risks frontmatter drift that "
        "breaks query-records and example-cockpit-repo ingest."
    )
    if authoring_hint:
        message += f"\n\n{authoring_hint}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[scaffold offer] {message}",
        },
    }


# ---------------------------------------------------------------------------
# Registry (manifest-derived) resolution — mirrors the reference hook's
# module-load-time manifest parse, done here per-check() call (import-safety
# rule 7 forbids doing it at import time).
# ---------------------------------------------------------------------------


def _load_doe_registry() -> dict:
    """Resolve the DoE-claude sibling root and load
    coordinator-registry.manifest.json, best-effort. NEVER returns ``None``
    any more — that was exactly the fail-open-on-missing-sibling hole AC2
    closes (see docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-
    corpus.md): the schema corpus this module validates against is now
    claude-klabauter's own vendored copy, independent of this manifest read entirely,
    so an unresolvable/absent DoE root or a malformed manifest must not
    black out schema-shape advisories too. Only the manifest-DERIVED fields
    (memo routing / scaffold-offer maps, central-EM identity) degrade to
    empty defaults on any resolution or parse failure — those remain
    genuinely DoE-owned routing data (plan AC4 + Out of scope), so their
    absence narrows only the memo-guard/scaffold-offer steps, never the
    schema-validation ones.
    """
    try:
        doe_root = coordinator_doe_root()
    except Exception:  # noqa: BLE001 — degrade-open, never block
        doe_root = None

    manifest: Optional[dict] = None
    if doe_root:
        manifest_path = Path(doe_root) / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        try:
            manifest = _retry_on_transient_read_failure(
                lambda: json.loads(manifest_path.read_text(encoding="utf-8")),
                exists_path=manifest_path,
            )
        except Exception:  # noqa: BLE001 — degrade-open, never block
            manifest = None

    repo_basename_to_em_shortname: dict = {}
    scaffold_offer_map: dict = {}
    kind_offer_override: dict = {}
    central_em_ids: set = set()
    central_canonical_id = None
    if manifest is not None:
        try:
            repo_basename_to_em_shortname = {
                a["dirBasename"]: a["shortname"] for a in manifest["identity"]["repoAliases"]
            }
            scaffold_offer_map = {
                d["schemaName"]: {"type": d["type"], "isSidecar": d["isSidecar"]}
                for d in manifest["docTypes"]
                if d.get("offerable") is True
            }
            kind_offer_override = {
                kind: {
                    "type": entry["type"],
                    "isSidecar": entry["isSidecar"],
                    "manualArgs": entry.get("manualArgs"),
                    "authoringHint": entry.get("authoringHint"),
                }
                for kind, entry in manifest["kindOfferOverride"].items()
            }
            central_em_ids = set(manifest["identity"]["centralReceiverIds"])
            central_canonical_id = manifest["identity"]["centralReceiverIds"][0]
        except Exception:  # noqa: BLE001 — degrade-open, never block
            manifest = None
            repo_basename_to_em_shortname = {}
            scaffold_offer_map = {}
            kind_offer_override = {}
            central_em_ids = set()
            central_canonical_id = None

    return {
        "doe_root": doe_root,
        "manifest": manifest or {},
        "repo_basename_to_em_shortname": repo_basename_to_em_shortname,
        "scaffold_offer_map": scaffold_offer_map,
        "kind_offer_override": kind_offer_override,
        "central_em_ids": central_em_ids,
        "central_canonical_id": central_canonical_id,
    }


def _doe_claude_realpath(doe_root: Optional[str]) -> Optional[str]:
    if not doe_root:
        return None
    try:
        return str(Path(doe_root).resolve())
    except (OSError, ValueError):
        return None


def _merge_memo_schemas(schemas: dict) -> dict:
    """Overlay the two memo schema entries onto `schemas` with claude-klabauter's OWN
    generated versions (this module lives in claude-klabauter already — no seam, no
    subprocess). Fail-open on any load error, mirrors the reference hook.
    """
    memo_schemas_dir = Path(__file__).resolve().parents[1] / "contract"
    try:
        memo_schemas = load_schemas(str(memo_schemas_dir))
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
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


# ---------------------------------------------------------------------------
# Memo-family guard dispatch — mirrors run_memo_guards()'s fall-through
# order (mislocated-path offer, free-form-header offer, own-inbox deny,
# routing-mismatch offer). Returns a ("advisory", dict) / ("deny", None) /
# None (did not fire, continue evaluating) tuple.
# ---------------------------------------------------------------------------

_STOP_DENY = ("deny", None)


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
                multi_edit_probe = fh.read()
        except OSError:
            multi_edit_probe = ""
        edits = tool_input.get("edits") or []
        for edit in edits:
            result, matched = apply_edit(
                multi_edit_probe, edit.get("old_string") or "", edit.get("new_string") or ""
            )
            if not matched:
                return None
            multi_edit_probe = result
        return multi_edit_probe
    return None


def _memo_guards_decision(
    tool_name: str,
    tool_input: dict,
    repo_root: str,
    abs_file_path: str,
    repo_rel: str,
    registry: dict,
    doe_claude_realpath: Optional[str],
) -> Optional[tuple]:
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return None

    if is_memo_path_mislocated(repo_rel):
        return None if _is_strict() else ("advisory", build_memo_offer_payload_advisory())

    content_to_probe = _compute_content_to_probe(tool_name, tool_input, abs_file_path)
    if content_to_probe is not None and has_free_form_memo_header(content_to_probe):
        return None if _is_strict() else ("advisory", build_memo_offer_payload_advisory())

    repo_root_stripped = repo_root.rstrip("/\\")
    repo_root_realpath: Optional[str] = None
    if doe_claude_realpath is not None:
        try:
            repo_root_realpath = str(Path(repo_root_stripped).resolve())
        except OSError:
            repo_root_realpath = None

    normalized_rel_for_routing = repo_rel.replace("\\", "/")
    is_canonical_inbox_write = bool(re.match(r"^cross-repo/inbox/[0-9]", normalized_rel_for_routing))
    if is_canonical_inbox_write and os.environ.get("COORDINATOR_OVERRIDE_OWN_INBOX") != "1":
        repo_basename = os.path.basename(repo_root_stripped)
        # Comparison-only fold: both realpaths are used only for this
        # identity check, never for I/O — safe to fold both sides.
        this_repo_is_central = (
            repo_root_realpath is not None
            and doe_claude_realpath is not None
            and casefold_path(repo_root_realpath) == casefold_path(doe_claude_realpath)
        )
        if this_repo_is_central:
            this_em_id = registry["central_canonical_id"]
        else:
            this_em_id = em_id_for_basename(repo_basename, registry["repo_basename_to_em_shortname"])

        inbox_content: Optional[str] = None
        if tool_name == "Write":
            inbox_content = tool_input.get("content") or ""
        elif tool_name == "Edit":
            try:
                with open(abs_file_path, "r", encoding="utf-8") as fh:
                    existing = fh.read()
                result, matched = apply_edit(
                    existing, tool_input.get("old_string") or "", tool_input.get("new_string") or ""
                )
                if matched:
                    inbox_content = result
            except OSError:
                pass
        elif tool_name == "MultiEdit":
            inbox_content = content_to_probe

        if inbox_content is not None:
            inbox_fm = parse_frontmatter(inbox_content).get("frontmatter")
            if inbox_fm is not None:
                from_value = (inbox_fm.get("from") or "").strip()
                to_value = (inbox_fm.get("to") or "").strip()

                def _matches_this_repo(value: str) -> bool:
                    if value == this_em_id:
                        return True
                    return this_repo_is_central and value in registry["central_em_ids"]

                if from_value and _matches_this_repo(from_value):
                    if not _matches_this_repo(to_value):
                        # Own-inbox misplacement: UNCONDITIONAL deny, always
                        # the sibling deny module's territory — never mine.
                        return _STOP_DENY

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

            yaml_to_value = extract_yaml_to_field(memo_check_content)
            has_free_form = has_free_form_memo_header(memo_check_content)
            is_this_a_memo = is_memo_shaped_path and (yaml_to_value is not None or has_free_form)

            if is_this_a_memo:
                to_field_raw = yaml_to_value

                landing_basename = os.path.basename(repo_root_stripped)
                # Comparison-only fold: see `this_repo_is_central` above.
                landing_repo_is_central = (
                    repo_root_realpath is not None
                    and doe_claude_realpath is not None
                    and casefold_path(repo_root_realpath) == casefold_path(doe_claude_realpath)
                )
                if landing_repo_is_central:
                    landing_em_id = registry["central_canonical_id"]
                else:
                    landing_em_id = em_id_for_basename(
                        landing_basename, registry["repo_basename_to_em_shortname"]
                    )

                to_repo_field_raw = extract_yaml_to_repo_field(memo_check_content)
                if to_repo_field_raw is not None:
                    this_repo_registry_key = (
                        _DOE_CLAUDE_REGISTRY_KEY
                        if landing_repo_is_central
                        else registry_key_for_basename(landing_basename, registry["manifest"])
                    )
                    if to_repo_field_raw.strip() != this_repo_registry_key:
                        recipient = to_field_raw if to_field_raw is not None else to_repo_field_raw.strip()
                        if _is_strict():
                            return _STOP_DENY
                        return ("advisory", build_memo_routing_offer_payload_advisory(recipient))
                elif to_field_raw is not None:
                    to_norm = to_field_raw.strip().lower()
                    to_is_central = (
                        to_field_raw.strip() in registry["central_em_ids"]
                        or to_norm in registry["central_em_ids"]
                    )
                    landing_is_central = landing_em_id == registry["central_canonical_id"]
                    if to_is_central and landing_is_central:
                        pass
                    # Fail open when the DoE root is unresolvable. Deliberately does NOT also test
                    # `to_is_central`: `central_em_ids` only populates once that root HAS resolved,
                    # so `to_is_central and doe_claude_realpath is None` was unsatisfiable by
                    # construction and the regex fallback below fired unconditionally on any
                    # `-em`-suffixed `to:`. Root unresolvable means we cannot tell whether `to:` is
                    # central, so emit nothing rather than guess. Mirrors the same fix in the deny
                    # sibling; the two modules are mutually exclusive and must agree here.
                    elif doe_claude_realpath is None:
                        pass
                    else:
                        to_em_id = to_field_raw.strip()
                        to_looks_like_em_id = bool(re.search(r"\S+-em$", to_em_id)) or (
                            to_em_id in registry["central_em_ids"]
                        )
                        if to_looks_like_em_id:
                            to_em_id_norm = to_em_id.lower()
                            landing_em_id_norm = landing_em_id.lower()
                            if to_em_id_norm != landing_em_id_norm:
                                if _is_strict():
                                    return _STOP_DENY
                                return (
                                    "advisory",
                                    build_memo_routing_offer_payload_advisory(to_em_id),
                                )

    return None


# ---------------------------------------------------------------------------
# Scaffold-offer dispatch
# ---------------------------------------------------------------------------


def _scaffold_offer_decision(
    tool_name: str,
    abs_file_path: str,
    schema_name: Optional[str],
    frontmatter: Optional[dict],
    repo_rel: str,
    registry: dict,
) -> Optional[tuple]:
    scaffold_offer_map = registry["scaffold_offer_map"]
    kind_offer_override = registry["kind_offer_override"]
    if not (
        tool_name == "Write"
        and not os.path.exists(abs_file_path)
        and schema_name in scaffold_offer_map
    ):
        return None

    if _is_strict():
        return _STOP_DENY

    kind_value = (
        str(frontmatter["kind"])
        if frontmatter and frontmatter.get("kind") is not None
        else None
    )
    override = kind_offer_override.get(kind_value) if kind_value else None
    entry = override or scaffold_offer_map[schema_name]
    type_ = entry["type"]
    is_sidecar = entry["isSidecar"]
    authoring_hint = override.get("authoringHint") if override else None

    derived_args: Optional[str] = None
    if override and override.get("manualArgs"):
        derived_args = override["manualArgs"]
    elif is_sidecar:
        stem = derive_sidecar_plan_stem(repo_rel, type_)
        derived_args = f"--plan {stem}" if stem else "--plan <stem>"

    payload = build_scaffold_offer_payload_advisory(
        schema_name, type_, derived_args, authoring_hint, kind_value if override else None
    )
    return ("advisory", payload)


# ---------------------------------------------------------------------------
# Schema-validation dispatch (evaluate_schema_validation port, advisory-only)
# ---------------------------------------------------------------------------


def _plan_tasks_spine_errors(
    prospective_content: str, schemas: dict, frontmatter: Optional[dict]
) -> list[dict]:
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

    Reference: DoE-claude coordinator/hooks/scripts/validate-frontmatter-schema.py
    (`_plan_tasks_spine_errors`) — kept as an honest parity copy there, but
    inert (that hook is no longer registered in hooks.json); this module is
    the live enforcement.

    GOVERNED-AWARE as of 2026-07-29 (write-guard-bypass fix) — mirrors the
    deny sibling's own docstring exactly; both must stay in lockstep. See
    `validate_frontmatter_schema_deny._plan_tasks_spine_errors` for the full
    defect writeup (governed-plan rows were spuriously rejected twice over:
    once by the schema's own `pm_approved`-required `allOf` branches, once
    by `_apply_cross_field_rules` silently defaulting `governed=False`).

    `plan_created` is now forwarded from this document's own frontmatter
    (2026-08-19 fix, mirrors the deny sibling exactly) — see that sibling's
    docstring for the full writeup of why this was previously dead on both
    write guards.
    """
    plan_tasks_schema = schemas.get("plan-tasks")
    if not isinstance(plan_tasks_schema, dict):
        return []

    try:
        result = locate_fenced_block(prospective_content)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return []
    if result.status != LocateStatus.LOCATED or result.body is None:
        return []

    try:
        parsed = parse_yaml(result.body)
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

    errors: list[dict] = []
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


_REVIEWED_RANGE_SCHEMA_NAMES = ("review-findings", "run-report")
_REVIEWED_RANGE_FIELD_RE = re.compile(r"^reviewed_range\[(\d+)\]$")
_BARE_HEX_RE = re.compile(r"^[0-9a-f]{4,64}$")
_RANGE_SEPARATOR_RE = re.compile(r"^(.+?)(\.{2,3})(.+)$")
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _resolve_ref_to_sha(token: str, cwd: Path) -> str:
    """Resolve one git ref token to its concrete full SHA via ``git rev-parse``.

    Own copy (review_trail_write.py, the prior sole owner of this helper, was
    deleted with the ``review_trail.write`` op — PM ruling 2026-08-23): a hex
    token (full or abbreviated SHA, optionally suffixed with ``^``/``~N`` ops)
    is returned unchanged; anything else is resolved via
    ``git rev-parse --verify --quiet``. Raises ``ValueError`` if git cannot
    resolve the token.
    """
    if _HEX_TOKEN_RE.match(token) or (
        token and _HEX_TOKEN_RE.match(re.split(r"[\^~]", token, maxsplit=1)[0])
    ):
        return token
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", token],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"could not resolve ref {token!r} to a concrete SHA ({exc}) — "
            "refusing to persist an unresolvable/symbolic ref"
        ) from exc
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise ValueError(
            f"could not resolve ref {token!r} to a concrete SHA (git rev-parse "
            "failed) — refusing to persist an unresolvable/symbolic ref"
        )
    return out

# reviewed_targets closed prefix set (review-findings 3.2.0 / run-report
# 2.2.0, vendored at coordinator_core/frontmatter/schemas/) — see plan-tasks
# row C3, docs/plans/2026-08-14-the-write-time-offer-for-reviewed-range.md.
_REVIEWED_TARGETS_ITEM_RE = re.compile(
    r"^(?:uncommitted|untracked|working-tree|diff-artifact):[^\s,]+$"
)
_RATIFIED_PREFIX_RE = re.compile(r"^(uncommitted|untracked|working-tree|diff-artifact):(.*)$")
_DIFF_ARTIFACT_SUFFIX_RE = re.compile(r"\.(?:diff|patch)$")


def _reviewed_targets_hint(value: str) -> Optional[str]:
    """Branch (c) sub-offer: name the ``reviewed_targets`` form a value
    naming no commit range should have used, when one is derivable. Returns
    ``None`` (offer nothing, unchanged behaviour) when no destination is
    derivable — see plan-tasks row C3 branch table. Never called for the
    hybrid-range case (a `..`/`...` value whose endpoint fails to resolve);
    that class keeps offering nothing regardless of this helper.
    """
    if _REVIEWED_TARGETS_ITEM_RE.match(value):
        return f'reviewed_targets: "{value}"'

    prefix_match = _RATIFIED_PREFIX_RE.match(value)
    if prefix_match:
        rest = prefix_match.group(2)
        prefix = prefix_match.group(1)
        if "," in rest:
            paths = [p.strip() for p in rest.split(",") if p.strip()]
            if paths:
                targets = ", ".join(f'"{prefix}:{p}"' for p in paths)
                return f"one reviewed_targets entry per path: {targets}"
            return None
        stripped = rest.strip()
        if stripped and not any(ch.isspace() for ch in stripped):
            return f'reviewed_targets: "{prefix}:{stripped}"'
        return None

    if _DIFF_ARTIFACT_SUFFIX_RE.search(value):
        return f'add to reviewed_targets: "diff-artifact:{value}"'

    return None


def _reviewed_range_offer(
    errors: list[dict],
    frontmatter: Optional[dict],
    git_root: Optional[str],
) -> None:
    """Rewrite ``reviewed_range`` validation errors IN PLACE with the
    resolved-range offer a reviewing subagent's own value should have been —
    or a plain statement that the value names no commit range when none
    exists. Never adds or removes an error, never touches ``tool_input``,
    never returns anything (mutation-only, mirrors the deny sibling's own
    error-list idiom). Offer, not a writer: DR-156, the reviewing subagent
    still types the value.

    Branch table (docs/plans/2026-08-14-the-write-time-offer-for-reviewed-
    range.md — plan-tasks row id C1):
      (a) has a `..`/`...` separator -> resolve each endpoint via
          `_resolve_ref_to_sha`; on success rewrite `hint` to the resolved
          `A..B`.
      (b) no separator, bare hex SHA -> rewrite `hint` to `<value>~1..<value>`
          (never resolved or lengthened — a hex token already passes through
          `_resolve_ref_to_sha` unchanged).
      (c) a separator endpoint `_resolve_ref_to_sha` cannot resolve, or no
          separator and not hex (a `.diff` path, `working-tree:<path>`,
          `uncommitted:<path>`) -> rewrite `error` to state plainly that the
          value names no commit range.

          For the NO-SEPARATOR sub-case only (never the hybrid-range
          sub-case above — its right endpoint re-resolves on every read,
          the same `..HEAD` hazard in other clothes, and must keep offering
          nothing), also look for a `reviewed_targets` destination via
          `_reviewed_targets_hint` (plan-tasks row C3):
            - value already satisfies the `reviewed_targets` item pattern
              -> say it belongs there, unchanged.
            - bare `.diff`/`.patch` path -> offer `diff-artifact:<path>`.
            - ratified prefix packing several comma-separated paths into
              one string -> offer one `reviewed_targets` entry per path.
            - anything else -> no `reviewed_targets` hint; `hint` stays
              deleted so the renderer emits no `required shape:` clause.

    Fail-open: any exception here (git failure, import failure, or any
    exception type from a future `_resolve_ref_to_sha` implementation, not
    just `ValueError`) leaves `errors` exactly as it arrived — a guard never
    blocks on infra, and a mid-loop failure must never leave earlier entries
    mutated while later ones aren't. Rewrites are staged into a local list
    and applied to `errors` only after the whole loop completes without
    exception, so the outer `except` below genuinely restores `errors` to
    its as-arrived state rather than an already-partially-mutated one.
    """
    try:
        if not isinstance(frontmatter, dict) or not git_root:
            return
        values = frontmatter.get("reviewed_range")
        if not isinstance(values, list):
            return
        if not any(_REVIEWED_RANGE_FIELD_RE.match(e.get("field") or "") for e in errors):
            return

        rewrites: list[tuple[dict, Optional[str], Optional[str]]] = []
        for err in errors:
            match = _REVIEWED_RANGE_FIELD_RE.match(err.get("field") or "")
            if not match:
                continue
            idx = int(match.group(1))
            if idx >= len(values) or not isinstance(values[idx], str):
                continue
            value = values[idx]

            sep_match = _RANGE_SEPARATOR_RE.match(value)
            if sep_match:
                left, _sep, right = sep_match.groups()
                try:
                    resolved_left = _resolve_ref_to_sha(left, Path(git_root))
                    resolved_right = _resolve_ref_to_sha(right, Path(git_root))
                except ValueError:
                    rewrites.append((err, f'value "{value}" names no commit range', None))
                    continue
                rewrites.append((err, None, f"{resolved_left}..{resolved_right}"))
                continue

            if _BARE_HEX_RE.match(value):
                rewrites.append((err, None, f"{value}~1..{value}"))
                continue

            targets_hint = _reviewed_targets_hint(value)
            new_error = f'value "{value}" names no commit range'
            if targets_hint is not None:
                rewrites.append((err, new_error, targets_hint))
            else:
                rewrites.append((err, new_error, None))

        for err, new_error, new_hint in rewrites:
            if new_error is not None:
                err["error"] = new_error
            if new_hint is not None:
                err["hint"] = new_hint
            elif new_error is not None:
                err.pop("hint", None)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return


def _evaluate_schema_validation_advisory(
    schema_name: str,
    schema: dict,
    frontmatter: Optional[dict],
    prospective_content: str,
    repo_rel: str,
    schemas: dict,
    *,
    payload: Optional[dict] = None,
    git_root: Optional[str] = None,
) -> Optional[dict]:
    match_mode = schema.get("match_mode")

    if match_mode == "whole-document-yaml":
        try:
            if repo_rel.lower().endswith(".json"):
                try:
                    parsed = json.loads(prospective_content)
                except ValueError:
                    parsed = parse_yaml(prospective_content)
            else:
                parsed = parse_yaml(prospective_content)
        except Exception as err:  # noqa: BLE001 — mirrors reference's bare catch
            return build_violation_payload_advisory(
                schema_name,
                [{
                    "field": "(parse error)",
                    "error": f"YAML parse error: {err}",
                    "hint": "Ensure the file is valid YAML with no --- frontmatter fences",
                }],
                payload=payload,
                git_root=git_root,
            )
        validation_result = validate_frontmatter_obj(parsed, schema)

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
            return build_violation_payload_advisory(
                schema_name,
                [{
                    "field": "(missing frontmatter)",
                    "error": "no YAML frontmatter found",
                    "hint": hint,
                }],
                payload=payload,
                git_root=git_root,
            )
        validation_result = validate_frontmatter_obj(frontmatter, schema)

    errors = [] if validation_result.get("ok") else list(validation_result.get("errors", []))

    if schema_name == "plan":
        errors.extend(_plan_tasks_spine_errors(prospective_content, schemas, frontmatter))

    if schema_name in _REVIEWED_RANGE_SCHEMA_NAMES and errors:
        _reviewed_range_offer(errors, frontmatter, git_root)

    if not errors:
        return None

    return build_violation_payload_advisory(
        schema_name, errors, payload=payload, git_root=git_root
    )


def _check_lineage_reachability_fires(
    frontmatter: Optional[dict], schema_name: Optional[str], repo_root: str, abs_file_path: str
) -> bool:
    if schema_name not in ("handoff", "handoff-archived"):
        return False
    if not frontmatter:
        return False
    if schema_name == "handoff-archived":
        handoff_dir = os.path.dirname(abs_file_path)
    else:
        handoff_dir = os.path.join(repo_root, "state", "handoffs")
    try:
        violations = _check_lineage_reachability(frontmatter, repo_root, handoff_dir)
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return False
    return bool(violations)


_HANDOFF_KIND_SCHEMA_NAME = "handoff"


def _handoff_kind_off_enum_fires(schema_name: Optional[str], schema: dict, frontmatter: Optional[dict]) -> bool:
    """True when the deny sibling's out-of-enum-`kind` branch
    (`_evaluate_handoff_kind_enum`) would fire on this payload.

    Mirrors that function's predicate exactly — same schema_name scope
    (``"handoff"`` only, i.e. ``state/handoffs/**``, never
    ``handoff-archived``), same absent-kind-is-valid rule, same
    `canonical_kind()` de-aliasing of retired D1 pre-rename names — so the
    two can never both fire on one payload. Kept as a boolean, mirroring
    `_grouping_approval_fires` immediately below, because this side never
    renders the message; it only needs to know whether to stand down.

    Same non-scalar `kind` stand-down as the deny sibling: a YAML
    list/mapping never triggers this predicate (never blindly
    ``str()``-coerced), leaving the base JSON-schema type check to name
    the real defect.
    """
    if schema_name != _HANDOFF_KIND_SCHEMA_NAME or not frontmatter:
        return False
    raw_kind = frontmatter.get("kind")
    if raw_kind is None:
        return False
    if not isinstance(raw_kind, str):
        return False
    raw_str = raw_kind
    enum_values = list((schema.get("properties") or {}).get("kind", {}).get("enum") or [])
    if not enum_values:
        return False
    if raw_str in enum_values:
        return False
    if _canonical_kind(raw_str) in enum_values:
        return False
    return True


def _grouping_approval_fires(prospective_content: str) -> bool:
    """True when the deny sibling's grouping-approval branch would fire.

    Mirrors `_evaluate_grouping_approval` in the deny sibling exactly — same
    input, same predicate, same fail-open on error — so the two can never
    both fire on one payload. Kept as a boolean rather than sharing the
    message-building helper because this side never renders the message; it
    only needs to know whether to stand down.

    Known redundant parse (accepted): `check_plan_tasks_grouping_approval`
    re-parses `prospective_content`'s frontmatter internally, even though
    `check()` already has it parsed as `frontmatter`. Bounded cost (small
    markdown, no loops beyond spine rows) — not worth threading a signature
    change through for a nit.
    """
    try:
        return _check_plan_tasks_grouping_approval(prospective_content) is not None
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check(payload: dict) -> Optional[dict]:
    """Evaluate the frontmatter-schema ADVISORY leg.

    Cwd-vs-target defect: `repo_root` is resolved from the TARGET FILE's own
    repo below, not the session's cwd — a session rooted at one repo writing
    into a SIBLING repo's tree used to see `to_repo_relative` return `None`
    before any schema step ran, so this leg silently produced nothing on
    every cross-repo write (mirrors the identical fix and rationale in
    ``validate_frontmatter_schema_deny._first_result``).

    Advisory-only cross-repo rule (DR-277): this module's own outcomes are
    already never a deny (every branch below either returns an
    ``additionalContext`` advisory or stands down via ``_STOP_DENY``/``None``
    for the deny sibling's unconditional territory), so fixing `repo_root`
    here needs no further gating on its own — the cross-repo/in-repo split
    for the sibling's four UNCONDITIONAL denies is handled entirely in
    ``validate_frontmatter_schema_deny.check()`` (it converts them to an
    advisory for a cross-repo target); this module keeps standing down for
    those exactly as before, in both the cross-repo and in-repo case.
    """
    tool_name = payload.get("tool_name")
    if tool_name not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = payload.get("cwd") or os.getcwd()

    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    abs_file_path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    # Cwd-vs-target defect (mirrors the deny sibling's identical fix): resolve
    # repo_root from the TARGET FILE's own repo, not the session's cwd. A
    # session rooted at one repo writing into a SIBLING repo's tree used to
    # resolve repo_root from cwd, so `to_repo_relative` always returned None
    # for the sibling's path and this guard silently produced nothing for
    # every cross-repo write — no schema-shape advisory, ever. Advisory-only
    # rule (DR-277): this module's own outcomes are already never a deny, so
    # fixing repo_root here needs no further "advisory only" gating — the
    # cross-repo/in-repo split for the sibling's UNCONDITIONAL denies is
    # handled entirely in `validate_frontmatter_schema_deny.check()`; this
    # module keeps standing down for those exactly as before (`_STOP_DENY`
    # branches below), regardless of cross-repo/in-repo.
    target_dir = os.path.dirname(abs_file_path) or cwd
    try:
        repo_root = _shared_resolve_repo_root(target_dir) or cwd
    except Exception:  # noqa: BLE001 — mirrors resolve_repo_root's own catch-and-fall-back
        repo_root = cwd
    repo_rel = to_repo_relative(abs_file_path, repo_root)
    if not repo_rel:
        return None

    registry = _load_doe_registry()
    doe_claude_realpath = _doe_claude_realpath(registry["doe_root"])

    decision = _memo_guards_decision(
        tool_name, tool_input, repo_root, abs_file_path, repo_rel, registry, doe_claude_realpath
    )
    if decision is not None:
        kind, envelope = decision
        return envelope if kind == "advisory" else None

    try:
        schemas = _retry_on_transient_read_failure(
            lambda: load_schemas(str(_VENDORED_SCHEMAS_DIR)), exists_path=_VENDORED_SCHEMAS_DIR
        )
    except Exception:  # noqa: BLE001 — mirrors reference's schema-load try/catch
        return None
    schemas = _merge_memo_schemas(schemas)

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
        result, matched = apply_edit(current, old_string or "", new_string or "")
        if not matched:
            return None
        prospective_content = result
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        try:
            with open(abs_file_path, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        content = current
        for edit in edits:
            result, matched = apply_edit(
                content, edit.get("old_string") or "", edit.get("new_string") or ""
            )
            if not matched:
                return None
            content = result
        prospective_content = content
    else:
        return None

    frontmatter = parse_frontmatter(prospective_content).get("frontmatter")

    match = match_schema(repo_rel, frontmatter, schemas)
    if not match:
        return None

    schema_name = match.get("schemaName")
    schema = match.get("schema")

    scaffold_decision = _scaffold_offer_decision(
        tool_name, abs_file_path, schema_name, frontmatter, repo_rel, registry
    )
    if scaffold_decision is not None:
        kind, envelope = scaffold_decision
        return envelope if kind == "advisory" else None

    schema_advisory = _evaluate_schema_validation_advisory(
        schema_name,
        schema,
        frontmatter,
        prospective_content,
        repo_rel,
        schemas,
        payload=payload,
        git_root=repo_root,
    )

    # Lineage reachability is an UNCONDITIONAL deny that overrides whatever
    # schema validation computed — always the deny sibling's territory.
    if _check_lineage_reachability_fires(frontmatter, schema_name, repo_root, abs_file_path):
        return None

    # Grouping approval (2026-07-29) is the third UNCONDITIONAL deny, so it
    # is the deny sibling's territory too and this one stands down in
    # lockstep. `test_at_most_one_sibling_fires_per_payload` is what keeps
    # that honest: if this branch were omitted, both siblings would fire on
    # the same payload and that differential test would red.
    #
    # Deliberately schema-name-agnostic, unlike the fourth deny's handoff-kind
    # check below (which gates on `schema_name == "handoff"`): claude-klabauter has no
    # `plan.schema.json` in `coordinator_core/frontmatter/schemas/`, so a
    # `schema_name == "plan"` gate here would match nothing for
    # `docs/plans/*.md` writes and would silently disable this stand-down —
    # the exact unreachable-gate defect this whole change was shipped to fix,
    # reproduced one layer down. This branch relies entirely on
    # `check_plan_tasks_grouping_approval`'s own internal gates
    # (`is_governed_plan` plus a locatable plan-tasks fence) to stay silent
    # on non-plan documents. Do not "fix" this asymmetry with the
    # neighbouring handoff-kind check without a real `plan` schema to key on.
    if _grouping_approval_fires(prospective_content):
        return None

    # Out-of-enum handoff `kind` (2026-07-29 D3) is the fourth UNCONDITIONAL
    # deny, so it is the deny sibling's territory too and this one stands
    # down in lockstep — same mutual-exclusivity reasoning as the lineage
    # and grouping-approval stand-downs immediately above.
    if _handoff_kind_off_enum_fires(schema_name, schema, frontmatter):
        return None

    return schema_advisory
