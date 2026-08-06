"""coordinator_core.snippet_sync.registry — snippets/registry.toml reader.

Port of `coordinator/bin/snippet-registry` (bash, 477 LoC — itself a
`set -euo pipefail` bash-4 CLI shelling to a python3 heredoc to parse TOML).
Folded in-process per T3a-g3f Q14 (EM-ruled scope expansion): the nested
bash-wraps-python shape this replaces is exactly the antipattern this
migration targets, so the CLI logic lives here, not as a bash dependency the
Python verify engine would otherwise keep shelling out to.

Schema: `snippets/registry.toml`, `schema_version` 1, 2, 3, or 4 (this reader
supports all four; unknown versions fail loud). Each `[snippet.<name>]` table
requires `sentinel_begin`, `sentinel_end`, `consumers` (list of str, may be
empty). Optional `[[snippet.<name>.conditional_consumer]]` array-tables carry
`path` + `condition_type` (`"file-exists"` | `"machine-local-key"`);
`condition_key` is REQUIRED for `machine-local-key` and FORBIDDEN for
`file-exists` (paste-drift guard, parity with the bash reader).

schema_version 4 (example-doctrine-repo 355255cc3, 2026-08-03) adds two OPTIONAL, ADDITIVE
per-row fields. Both are additive-optional: a v3-shaped row carrying neither is
still valid at v4, which is why the bump is a field-SET change rather than a
value-shape break.

  excluded_consumer  — array-of-tables, each `{path, reason}`. A DECLARED,
                       deliberate non-enrolment: a file that carries no pasted
                       sentinel for this snippet ON PURPOSE (a sanctioned
                       bespoke variant, or genuinely out of scope), as distinct
                       from an UNDECLARED absence (a file nobody has considered,
                       invisible to `consumers` and this list alike). `reason` is
                       REQUIRED and must be non-empty — an entry with no reason
                       reintroduces the zero-information absence one level up,
                       the same bug in a new location. Reason CONTENT is not
                       validated, only its presence. A `path` that also appears
                       in `consumers` on the same row is a CONTRADICTORY
                       declaration and fails loud.
  eligible_glob      — OPTIONAL glob naming the row's FULL candidate universe
                       (e.g. `"agents/*.md"`) rather than leaving that universe
                       implicit in `consumers`. Without it, `excluded_consumer`
                       is an allowlist against an undeclared universe — you can
                       only exclude what someone remembered to name. With it,
                       every glob member must land in `consumers` OR
                       `excluded_consumer`; a member in neither is exactly the
                       defect class the pair exists to catch. Enforced by
                       `eligible_glob_gaps` (filesystem-touching, so it is a
                       separate call rather than part of `load_registry`), wired
                       into the verifier's report.

BOTH fields are FORBIDDEN on a `consumer_source = "scan"` row: sentinel-presence
-on-disk IS enrolment there, so a declared exclusion is incoherent rather than
merely unused. Both are also rejected outright below `schema_version` 4 — a row
using them on a registry that has not declared the bump is mis-declared, and
silently honouring them would defeat the fail-loud forward-compat contract the
version field exists for.

Enforcement of all of the above is claude-klabauter-resident by example-doctrine-repo's own declaration —
`registry.toml`'s header states the shape only and names this reader as the
place the checks live.

T3a-g3f additions (metadata driving the 7-script consolidation's behavioral
divergences as DATA, not per-script code branches — see snippet_sync.verify):
  header_style     — "comment-block" | "fixed-2-line" |
                      "fixed-2-line-strip-end-sentinel" | "sentinel-embedded"
                      (default "sentinel-embedded")
  fence_aware      — bool, skip occurrences inside ``` fences when scanning
                      for consumers (default false; meta-ask-preamble ONLY)
  allow_insert     — bool, DR-6 insert-when-absent instead of verify-fail on
                      MISSING (default false; quota-self-detect-preamble ONLY)
  delivery         — "paste" | "inject" (default "paste"; REQUIRED on every
                      row as of schema_version 3). HOW the block reaches its
                      consumers, ORTHOGONAL to consumer_source below.
                      "inject" rows are assembled into the dispatched child
                      prompt via `contract_blocks:` and pasted by nothing, so
                      their `consumers` list is a logical/documentation set,
                      not a paste-target list, and any pasted BEGIN sentinel
                      for them is an orphan BY CONSTRUCTION. Two carve-outs a
                      caller must honour: `conditional_consumer` entries are
                      always paste targets even on an inject row (four rows are
                      inject for the coordinator personas AND carry a genuinely
                      pasted example-game-repo live-install conditional), and
                      contract_blocks membership does NOT imply "inject" — some
                      paste rows are cited there while still resident-pasted
                      during the transitional-duplication window. That second
                      carve-out is why this is DECLARED rather than inferred
                      from subagent-sandbox-policy.yaml.
  consumer_source  — "registry" | "scan" (default "registry"). A DISCOVERY
                      axis — how the paste-target set is determined — not a
                      delivery mechanism; a scan-discovered snippet is still
                      PASTED, which is why "scan" is not a `delivery` value.
  search_scope     — "plugin-root" | "parent-of-plugin-root", only meaningful
                      when consumer_source == "scan" (default "plugin-root")
  excluded_consumer — list of `{path, reason}` tables (default []), see the
                      schema_version 4 block above
  eligible_glob    — glob string or None (default None), see the
                      schema_version 4 block above
  in_fence_consumers — bool, additionally discover + verify/--fix consumer
                      blocks carried in the SHELL-COMMENT sentinel dialect
                      (`# BEGIN X` / `# END X`, derived mechanically from the
                      HTML `<!-- BEGIN X -->` form) sitting INSIDE ``` fenced
                      code blocks — an HTML comment cannot live inside a bash
                      fence, so in-fence copies carry shell-comment sentinels
                      instead (default false; resolve-claude-klabauter-bin ONLY). See
                      snippet_sync.verify for the full dialect semantics.

Negative-spec: this module does filesystem reads (registry.toml, file-exists
conditional probes) and one best-effort subprocess call (machine-local
key resolution) — it is NOT a pure function like normalize_snippet or
sentinel_blocks. Callers needing determinism should pass an explicit
machine_local_bin=None to disable the subprocess path.

Spec backlink: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 6
DR backlink: example-doctrine-repo docs/decisions/2026-06-15-snippet-registry-shape.md
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4)

#: Fields introduced by schema_version 4 (example-doctrine-repo 355255cc3). Both are additive-
#: optional AT v4 and unknown below it.
_V4_ONLY_FIELDS = ("excluded_consumer", "eligible_glob")

# `delivery` is REQUIRED from schema_version 3 onward; on a v1/v2 registry it is
# absent everywhere and defaults to "paste", which is what those versions meant
# implicitly.
_VALID_DELIVERIES = ("paste", "inject")

# Sibling-plugin (example-game-workbench-repo) file-exists conditional consumers are
# ALWAYS at the flat live-install layout ($CLAUDE_HOME-or-$HOME/.claude/plugins/
# example-game-workbench-repo/...), decoupled from plugin_root — mirrors the retired
# bash oracle's unconditional $HOME anchoring ("Sibling plugin entries keep
# $HOME because they are always at the flat install layout"). Resolving these
# relative to plugin_root is only correct when plugin_root itself IS that
# live-install path; when plugin_root is the example-doctrine-repo SOURCE tree (the real
# --plugin-dir production resolution path per example-doctrine-repo's own CLAUDE.md), a
# plugin_root-relative resolution silently lands on a nonexistent/wrong-layout
# path and all 14 example-game-repo/game-dev conditional consumers vanish with no error.
_SIBLING_PLUGIN_MARKER = "../../example-game-workbench-repo/"


def _sibling_plugin_file_exists_path(cond_path: str) -> Optional[Path]:
    """Resolve a `example-game-workbench-repo` sibling-plugin file-exists `cond_path`
    against the live-install plugins root, independent of `plugin_root`.

    Returns None when `cond_path` isn't this shape (e.g. `~`-expansion or a
    plain relative path) — callers fall back to plugin_root-relative
    resolution for anything this doesn't claim.
    """
    if cond_path.startswith("~"):
        return Path(cond_path).expanduser()
    if not cond_path.startswith(_SIBLING_PLUGIN_MARKER):
        return None
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or ""
    plugins_root = Path(home).expanduser() / ".claude" / "plugins"
    return plugins_root / "example-game-workbench-repo" / cond_path[len(_SIBLING_PLUGIN_MARKER):]

_VALID_HEADER_STYLES = (
    "comment-block",
    "fixed-2-line",
    "fixed-2-line-strip-end-sentinel",
    "sentinel-embedded",
)
_VALID_CONSUMER_SOURCES = ("registry", "scan")
_VALID_SEARCH_SCOPES = ("plugin-root", "parent-of-plugin-root")


class RegistryError(Exception):
    """Raised on malformed registry.toml, unknown snippet name, or unsupported schema_version.

    `exit_code` mirrors the bash CLI's contract: 1 usage/parse error,
    2 unknown snippet name, 3 schema_version mismatch/absent.
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _load_toml(registry_path: Path) -> dict[str, Any]:
    if not registry_path.is_file():
        raise RegistryError(
            f"snippet-registry: registry.toml not found at {registry_path}", exit_code=1
        )
    try:
        if sys.version_info >= (3, 11):
            import tomllib

            with registry_path.open("rb") as fh:
                return tomllib.load(fh)
        else:
            import tomli  # type: ignore[import-not-found]

            with registry_path.open("rb") as fh:
                return tomli.load(fh)
    except ImportError as exc:
        raise RegistryError(
            "snippet-registry: python < 3.11 detected and 'tomli' package is absent. "
            "Remediation: pip install tomli (or upgrade to python >= 3.11).",
            exit_code=2,
        ) from exc
    except Exception as exc:  # tomllib.TOMLDecodeError et al — surface as parse error
        # Include the exception class name (not just its message) — callers/tests
        # discriminate "malformed TOML" from other failure classes by type name,
        # matching the raw traceback the retired bash CLI's python3 heredoc emitted.
        raise RegistryError(
            f"snippet-registry: failed to parse {registry_path}: "
            f"{type(exc).__name__}: {exc}",
            exit_code=1,
        ) from exc


def _validate_v4_fields(name: str, entry: dict[str, Any], schema_version: int) -> None:
    """Validate the schema_version-4 `excluded_consumer` / `eligible_glob` pair
    on one `[snippet.<name>]` row.

    Every violation here raises with `exit_code=1`, the module's established
    MALFORMED-ROW code (2 is reserved for an unknown snippet NAME, 3 for a
    schema_version the reader cannot read at all). A row that declares a field
    the schema forbids, or an exclusion with no reason, is a parse-time defect in
    the registry file — the same class as a missing `sentinel_begin` or a
    `condition_key` on a file-exists conditional, both of which already exit 1.

    Structural checks only; the `eligible_glob` COMPLETENESS check needs a
    filesystem and lives in `eligible_glob_gaps`.
    """
    for field in _V4_ONLY_FIELDS:
        if field in entry and schema_version < 4:
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] field '{field}' requires "
                f"schema_version >= 4 (this registry declares {schema_version}). Bump "
                f"schema_version, or remove the field — honouring it silently would "
                f"defeat the fail-loud forward-compat contract.",
                exit_code=1,
            )

    # consumer_source is validated for VALUE in get_snippet_meta; here only the
    # "scan" case matters, and an unknown value simply isn't "scan".
    if entry.get("consumer_source", "registry") == "scan":
        for field in _V4_ONLY_FIELDS:
            if field in entry:
                raise RegistryError(
                    f"snippet-registry: [snippet.{name}] field '{field}' is FORBIDDEN on a "
                    f"consumer_source=\"scan\" row — sentinel-presence-on-disk IS enrolment "
                    f"there, so a declared exclusion is incoherent rather than merely unused.",
                    exit_code=1,
                )

    excluded = entry.get("excluded_consumer", [])
    if not isinstance(excluded, list) or not all(isinstance(e, dict) for e in excluded):
        raise RegistryError(
            f"snippet-registry: [snippet.{name}] field 'excluded_consumer' must be an "
            f"array of tables ([[snippet.{name}.excluded_consumer]] with path + reason), "
            f"got {excluded!r}",
            exit_code=1,
        )
    consumer_paths = set(entry.get("consumers", []))
    seen: set[str] = set()
    for excl in excluded:
        for required in ("path", "reason"):
            if required not in excl:
                raise RegistryError(
                    f"snippet-registry: [[snippet.{name}.excluded_consumer]] missing "
                    f"required field '{required}'",
                    exit_code=1,
                )
        path = excl["path"]
        reason = excl["reason"]
        if not isinstance(path, str) or not path.strip():
            raise RegistryError(
                f"snippet-registry: [[snippet.{name}.excluded_consumer]] field 'path' must "
                f"be a non-empty string, got {path!r}",
                exit_code=1,
            )
        if not isinstance(reason, str) or not reason.strip():
            raise RegistryError(
                f"snippet-registry: [[snippet.{name}.excluded_consumer]] path '{path}' has "
                f"an empty 'reason' — a reason is REQUIRED and must be non-empty, or the "
                f"entry reintroduces the zero-information absence one level up.",
                exit_code=1,
            )
        if path in consumer_paths:
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] contradictory declaration — "
                f"'{path}' appears in BOTH 'consumers' and an "
                f"[[snippet.{name}.excluded_consumer]] entry. A file is either enrolled or "
                f"deliberately not; it cannot be both.",
                exit_code=1,
            )
        if path in seen:
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] duplicate "
                f"[[snippet.{name}.excluded_consumer]] entry for path '{path}'",
                exit_code=1,
            )
        seen.add(path)

    if "eligible_glob" in entry:
        pattern = entry["eligible_glob"]
        if not isinstance(pattern, str) or not pattern.strip():
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] field 'eligible_glob' must be a "
                f"non-empty glob string, got {pattern!r}",
                exit_code=1,
            )
        if pattern.startswith(("/", "~")) or pattern.startswith("../"):
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] field 'eligible_glob' must be a "
                f"content-root-RELATIVE pattern (got '{pattern}') — the candidate universe "
                f"is scoped to the tree under verification, not an absolute or escaping path.",
                exit_code=1,
            )


def load_registry(registry_path: Path) -> dict[str, Any]:
    """Parse + validate registry.toml. Returns the raw `{"schema_version": N, "snippet": {...}}` dict.

    Validates schema_version and, per snippet, the required-field contract
    (sentinel_begin/sentinel_end/consumers; conditional_consumer path +
    condition_type, condition_key iff machine-local-key). Raises
    RegistryError on any violation — mirrors the bash reader's fail-loud
    Python heredoc.
    """
    data = _load_toml(registry_path)

    schema_version = data.get("schema_version")
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise RegistryError(
            f"snippet-registry: unknown schema_version (this reader supports up to "
            f"{max(_SUPPORTED_SCHEMA_VERSIONS)}, got {schema_version!r}). Update the "
            f"reader before reading this registry.",
            exit_code=3,
        )

    snippets = data.get("snippet", {})
    for name, entry in snippets.items():
        required_fields = ["sentinel_begin", "sentinel_end", "consumers"]
        if schema_version >= 3:
            required_fields.append("delivery")
        for required in required_fields:
            if required not in entry:
                raise RegistryError(
                    f"snippet-registry: [snippet.{name}] missing required field '{required}'",
                    exit_code=1,
                )
        # Review: code-reviewer — presence-only validation let a scalar
        # `consumers` value (e.g. a bare-string paste-drift typo instead of a
        # 1-element list) pass silently, then get iterated char-by-char by
        # resolve_consumers downstream. Fail loud here instead.
        if not isinstance(entry["consumers"], list) or not all(
            isinstance(c, str) for c in entry["consumers"]
        ):
            raise RegistryError(
                f"snippet-registry: [snippet.{name}] field 'consumers' must be a list of "
                f"strings, got {entry['consumers']!r}",
                exit_code=1,
            )
        _validate_v4_fields(name, entry, schema_version)
        for cond in entry.get("conditional_consumer", []):
            for required in ("path", "condition_type"):
                if required not in cond:
                    raise RegistryError(
                        f"snippet-registry: [[snippet.{name}.conditional_consumer]] "
                        f"missing required field '{required}'",
                        exit_code=1,
                    )
            cond_type = cond["condition_type"]
            if cond_type == "machine-local-key":
                if "condition_key" not in cond:
                    raise RegistryError(
                        f"snippet-registry: [[snippet.{name}.conditional_consumer]] "
                        f"condition_type='machine-local-key' requires field 'condition_key'",
                        exit_code=1,
                    )
            elif cond_type == "file-exists":
                if "condition_key" in cond:
                    raise RegistryError(
                        f"snippet-registry: [[snippet.{name}.conditional_consumer]] "
                        f"condition_type='file-exists' FORBIDS field 'condition_key' "
                        f"(paste-drift from a machine-local-key entry?)",
                        exit_code=1,
                    )
            else:
                raise RegistryError(
                    f"snippet-registry: [[snippet.{name}.conditional_consumer]] "
                    f"unknown condition_type '{cond_type}'",
                    exit_code=1,
                )

    return data


def list_snippets(data: dict[str, Any]) -> list[str]:
    """All enrolled snippet names, alphabetically."""
    return sorted(data.get("snippet", {}).keys())


def get_snippet_entry(data: dict[str, Any], name: str) -> dict[str, Any]:
    snippets = data.get("snippet", {})
    if name not in snippets:
        raise RegistryError(f"snippet-registry: unknown snippet name '{name}'", exit_code=2)
    return snippets[name]


def get_snippet_meta(data: dict[str, Any], name: str) -> dict[str, Any]:
    """T3a-g3f metadata block for `name` (header_style/delivery/fence_aware/allow_insert/
    consumer_source/search_scope), defaulted + validated against the enumerated axes,
    plus the schema_version-4 `excluded_consumer` / `eligible_glob` declarations
    (structurally validated at `load_registry` time, surfaced verbatim here).
    """
    entry = get_snippet_entry(data, name)
    header_style = entry.get("header_style", "sentinel-embedded")
    if header_style not in _VALID_HEADER_STYLES:
        raise RegistryError(
            f"snippet-registry: [snippet.{name}] unknown header_style '{header_style}' "
            f"(valid: {', '.join(_VALID_HEADER_STYLES)})",
            exit_code=1,
        )
    delivery = entry.get("delivery", "paste")
    if delivery not in _VALID_DELIVERIES:
        raise RegistryError(
            f"snippet-registry: [snippet.{name}] unknown delivery '{delivery}' "
            f"(valid: {', '.join(_VALID_DELIVERIES)}). Note 'scan' is a consumer_source "
            f"value, not a delivery mechanism — a scan-discovered snippet is still pasted.",
            exit_code=1,
        )
    consumer_source = entry.get("consumer_source", "registry")
    if consumer_source not in _VALID_CONSUMER_SOURCES:
        raise RegistryError(
            f"snippet-registry: [snippet.{name}] unknown consumer_source '{consumer_source}' "
            f"(valid: {', '.join(_VALID_CONSUMER_SOURCES)})",
            exit_code=1,
        )
    search_scope = entry.get("search_scope", "plugin-root")
    if search_scope not in _VALID_SEARCH_SCOPES:
        raise RegistryError(
            f"snippet-registry: [snippet.{name}] unknown search_scope '{search_scope}' "
            f"(valid: {', '.join(_VALID_SEARCH_SCOPES)})",
            exit_code=1,
        )
    return {
        "header_style": header_style,
        "delivery": delivery,
        "fence_aware": bool(entry.get("fence_aware", False)),
        "allow_insert": bool(entry.get("allow_insert", False)),
        "consumer_source": consumer_source,
        "search_scope": search_scope,
        "in_fence_consumers": bool(entry.get("in_fence_consumers", False)),
        "excluded_consumer": list(entry.get("excluded_consumer", [])),
        "eligible_glob": entry.get("eligible_glob"),
    }


def _ml_get(key: str, machine_local_bin: Optional[str]) -> str:
    """Best-effort machine-local key resolution. Empty string on any failure —
    absent sibling repos / absent resolver are routine, not fatal.
    """
    if not machine_local_bin:
        return ""
    try:
        result = subprocess.run(
            [machine_local_bin, "get", key],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        # Debug (not warning): absent sibling repos / absent machine-local
        # resolver is routine on many machines, not a fault — see docstring.
        logger.debug(
            "snippet-registry: machine-local resolver %r unavailable for key %r "
            "(%s: %s) — treating as empty",
            machine_local_bin, key, type(exc).__name__, exc,
        )
        return ""


def effective_content_root(plugin_root: Path, content_root: Optional[Path]) -> Path:
    """The root that plugin-root-RELATIVE consumer content resolves against —
    `content_root` when the COORDINATOR_CONTENT_ROOT cache-install seam is in
    play, else `plugin_root`.

    THE single definition of that ladder. It exists as a named function rather
    than an inline conditional because it was inlined in exactly one place
    (`resolve_consumers`) while `snippet_sync.verify`'s orphan scan and
    scan-driven consumer discovery kept walking `plugin_root` unconditionally.
    A redirected content root therefore resolved its consumer set into the
    redirect while the scans read the REAL tree, so every genuine consumer of
    the real tree came back as an orphan and the check's result depended on
    machine state outside the tree under test.

    NEGATIVE SPEC: this is for CONTENT that lives under the plugin root and can
    be redirected wholesale. It is NOT for the registry file, the canonical
    snippet source, or `../`-sibling / `conditional_consumer` paths — those stay
    anchored to the true plugin root by design (see `resolve_consumers`).
    """
    return content_root if content_root is not None else plugin_root


def eligible_glob_gaps(
    data: dict[str, Any],
    name: str,
    plugin_root: Path,
    *,
    content_root: Optional[Path] = None,
) -> list[str]:
    """Content-root-relative paths matching `name`'s `eligible_glob` that appear
    in NEITHER `consumers` NOR `excluded_consumer` — the schema_version-4
    completeness check.

    Empty list when the row declares no `eligible_glob` (the field is optional,
    and its absence means the candidate universe stays implicit, which is
    permitted — just uninformative).

    Resolution anchors on `effective_content_root`, NOT on `plugin_root`
    directly. A completeness check that globbed the true plugin root while the
    consumer set resolved into a `COORDINATOR_CONTENT_ROOT` redirect would
    compare two different trees and report every real file as undeclared — the
    precise split `effective_content_root` was introduced to close. `../`
    sibling and `conditional_consumer` paths are outside a relative glob's reach
    by construction and need no special-casing here.
    """
    entry = get_snippet_entry(data, name)
    pattern = entry.get("eligible_glob")
    if not pattern:
        return []

    root = effective_content_root(plugin_root, content_root)
    declared = {c for c in entry.get("consumers", [])}
    declared.update(excl["path"] for excl in entry.get("excluded_consumer", []))

    gaps: list[str] = []
    for member in root.glob(pattern):
        if not member.is_file():
            continue
        rel = member.relative_to(root).as_posix()
        if rel not in declared:
            gaps.append(rel)
    return sorted(gaps)


def resolve_consumers(
    data: dict[str, Any],
    name: str,
    plugin_root: Path,
    *,
    content_root: Optional[Path] = None,
    machine_local_bin: Optional[str] = None,
    notes: Optional[list[str]] = None,
) -> list[str]:
    """Resolved, ordered absolute consumer paths for snippet `name`.

    Ordering (F5, the Staff Engineer-c2): (1) PLUGIN_ROOT-relative (or content_root override)
    paths, alpha; (2) "../" sibling paths (always plugin_root-anchored — a
    cache-install content_root override does not affect sibling-plugin
    resolution), alpha; (3) conditional resolutions, author-declared order,
    file-exists/machine-local-key emitted iff the resolved file exists.

    `content_root` mirrors the bash CLI's COORDINATOR_CONTENT_ROOT override
    (quota-self-detect-preamble's only consumer of this axis pre-consolidation,
    generalized here since it's a legitimate cache-install seam, not a
    per-snippet special case).

    `notes` (Review: code-reviewer — the machine-local-key "key unset" NOTE
    used to go straight to a bare `print(file=sys.stderr)`, invisible to any
    caller that only inspects the structured return value, e.g. `verify.run`'s
    `SyncOutcome.stderr_lines`). When `notes` is passed, diagnostic lines are
    appended to it instead of printed directly; when omitted (direct/CLI use),
    the prior print-to-stderr behavior is preserved unchanged.
    """
    entry = get_snippet_entry(data, name)
    effective_root = effective_content_root(plugin_root, content_root)

    plugin_rel: list[str] = []
    sibling_rel: list[str] = []
    for raw_path in entry.get("consumers", []):
        if raw_path.startswith("../"):
            sibling_rel.append(str(plugin_root / raw_path))
        else:
            plugin_rel.append(str(effective_root / raw_path))

    out: list[str] = sorted(plugin_rel) + sorted(sibling_rel)
    out.extend(
        resolve_conditional_consumers(
            data, name, plugin_root, machine_local_bin=machine_local_bin, notes=notes
        )
    )
    return out


def resolve_conditional_consumers(
    data: dict[str, Any],
    name: str,
    plugin_root: Path,
    *,
    machine_local_bin: Optional[str] = None,
    notes: Optional[list[str]] = None,
) -> list[str]:
    """Resolved absolute paths of `name`'s `conditional_consumer` entries ONLY,
    in author-declared order — the third ordering tier of `resolve_consumers`,
    which delegates here rather than inlining it.

    Split out (not duplicated) so a caller can distinguish a CONDITIONAL
    consumer from a plain one, which `resolve_consumers`' merged return cannot
    express. The distinction is load-bearing under `delivery = "inject"`: an
    inject row's plain `consumers` list is a logical/documentation set that
    nothing pastes, so a sentinel found in one of those files IS an orphan —
    but its `conditional_consumer` paths remain genuine paste targets and must
    stay exempt (four registry rows are inject for the coordinator personas
    while carrying a genuinely pasted example-game-repo live-install conditional).
    Recovering that split by position or by set-subtraction against the merged
    list would re-derive the path mapping a second time; this does not.
    """
    entry = get_snippet_entry(data, name)
    out: list[str] = []
    for cond in entry.get("conditional_consumer", []):
        cond_path = cond["path"]
        cond_type = cond["condition_type"]
        if cond_type == "file-exists":
            fe_path = _sibling_plugin_file_exists_path(cond_path) or (plugin_root / cond_path)
            if fe_path.is_file():
                out.append(str(fe_path))
            continue
        # machine-local-key
        cond_key = cond["condition_key"]
        resolved_root = _ml_get(cond_key, machine_local_bin).rstrip("/")
        if not resolved_root:
            note = f"NOTE-{cond_key}: key unset — skipping conditional consumer for {cond_path}"
            if notes is not None:
                notes.append(note)
            else:
                print(note, file=sys.stderr)
            continue
        full_path = Path(resolved_root) / cond_path
        if full_path.is_file():
            out.append(str(full_path))
        # If the resolved root exists but the target file doesn't, emit nothing
        # (file may not be present on this machine's checkout of the sibling).

    return out


def list_for(
    data: dict[str, Any],
    target_path: str,
    plugin_root: Path,
    *,
    machine_local_bin: Optional[str] = None,
) -> list[str]:
    """Reverse lookup: snippet names whose resolved consumer set includes `target_path`.

    Matches against both the raw registry path (as written) and the resolved
    absolute path, for unconditional and conditional consumers alike.
    """
    target = target_path.rstrip("/")
    matched: list[str] = []

    for name in list_snippets(data):
        entry = get_snippet_entry(data, name)
        found = False

        for raw_path in entry.get("consumers", []):
            resolved = str(plugin_root / raw_path)
            if raw_path == target or resolved == target:
                found = True
                break

        if not found:
            for cond in entry.get("conditional_consumer", []):
                cond_path = cond["path"]
                cond_type = cond["condition_type"]
                if cond_type == "file-exists":
                    fe_path = _sibling_plugin_file_exists_path(cond_path) or (plugin_root / cond_path)
                    # Review: code-reviewer — mirror resolve_consumers' existence
                    # gate (a file-exists conditional consumer is only "active"
                    # if it currently resolves on disk); otherwise list_for could
                    # report a snippet as covering a path that verify/resolve_consumers
                    # would never actually treat as an active consumer here.
                    if (cond_path == target or str(fe_path) == target) and fe_path.is_file():
                        found = True
                        break
                    continue
                if cond_path == target:
                    found = True
                    break
                resolved_root = _ml_get(cond["condition_key"], machine_local_bin).rstrip("/")
                if resolved_root and str(Path(resolved_root) / cond_path) == target:
                    found = True
                    break

        if found:
            matched.append(name)

    return sorted(matched)
