"""
coordinator_core.ops.sizing_discharge_surfaced — JSON-RPC "sizing.discharge_surfaced".

Purpose: the addressable applier for the OTHER end of `surfaced_to_pm`. That array
records direction-class items a sizing deliberately did not decide; nothing in this
engine could record that one of them had since been ANSWERED, so a surfaced item had
exactly two observable states — listed, or hand-deleted. Both read as open to every
query over `state/sizings/`, which is the invisibility the field itself exists to end.

The write this op makes is `pm_resolution`, never `surfaced_to_pm`. That is the
schema's own negative spec, not a preference: "An entry stays listed until it is
resolved in its own artifact (a decision record, a PM ruling, a downstream plan);
resolving it by editing the sizing-object is the failure this field exists to
prevent. Once resolved, the PM's reasoning moves to `pm_resolution`." So the surfaced
entry is left byte-identical and the discharge is recorded beside it — the pair
(still-listed item, keyed resolution) is what lets a reader tell an answered question
from a forgotten one without reconstructing the session.

`resolved_by` is a REQUIRED param and must resolve to a real file on disk, the same
live-evidence gate `sizing.decline` puts on its own `decision_record`: a discharge
asserts that some artifact settled this, and an assertion no reader can follow is the
YAML comment this field replaced. The allowed roots are wider than `sizing.decline`'s
because the schema names three resolving artifacts, not one — a decision record, a PM
ruling (recorded under `state/`), or a downstream plan.

Negative-spec:
  - Does NOT edit, reorder, or remove any `surfaced_to_pm` entry. Pinned by test.
  - Does NOT decide anything. `resolution` is the caller's prose; this op neither
    composes it nor infers it from the item.
  - Does NOT write `fork` or `xl_exit` — the schema makes those the authoritative
    machine-readable record of their two choices, and an entry here carries reasoning,
    never a substitute value.
  - Does NOT overwrite an existing resolution key unless `supersede` is set, so a
    second discharge of the same item cannot silently displace the first.
  - Does NOT cascade, fan out, or git-commit.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import write_fm_nested_field
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

# Vendored sizing-object schema path — own local copy per this package's
# established per-module convention (see sizing_decline._SIZING_SCHEMA_PATH).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

#: Where a resolving artifact may live. Wider than `sizing.decline`'s single
#: `docs/decisions/` because `surfaced_to_pm`'s own description names three
#: resolving artifacts: a decision record, a PM ruling, a downstream plan.
_RESOLVER_ROOTS = ("docs/decisions", "docs/plans", "state")

#: `decided_on` is the object's own required key, never a resolution key.
_RESERVED_KEYS = frozenset({"decided_on"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_sizing_fm(fm_text: str) -> list:
    """Parse fm_text as whole-document YAML and validate against the sizing-object
    schema. Mirrors sizing_decline._validate_sizing_fm's contract exactly.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def derive_key(item: str) -> str:
    """A stable `pm_resolution` key for a surfaced item's text.

    `pm_resolution`'s keys are free-form by schema ("keyed by what it resolved"),
    which leaves nothing joining a resolution back to the item it answers. Deriving
    the key from the item's own text is that join, and deriving it here rather than
    asking the caller keeps it the same key on a re-run.
    """
    words = re.findall(r"[a-z0-9]+", item.lower())
    key = "_".join(words[:6])[:48].strip("_")
    return key or "surfaced_item"


def _render_pm_resolution(mapping: dict) -> str:
    """Serialize a `pm_resolution` mapping as an indented YAML block.

    The whole block is re-rendered rather than line-patched: its values are
    PM-authored prose whose quoting a line edit cannot get right. Everything
    outside `pm_resolution:` is untouched by `write_fm_nested_field`; a hand-written
    comment INSIDE the block does not survive, which is the accepted cost of not
    mis-quoting a resolution.
    """
    dumped = yaml.safe_dump(
        mapping,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100000,
    )
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _match_item(surfaced: list, needle: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve `needle` against `surfaced_to_pm[].item` — exact first, then a
    unique case-insensitive substring. Returns (entry, error)."""
    items = [e for e in surfaced if isinstance(e, dict)]
    exact = [e for e in items if str(e.get("item") or "") == needle]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"surfaced_to_pm carries {len(exact)} entries with that exact item text"
    lowered = needle.lower()
    partial = [e for e in items if lowered in str(e.get("item") or "").lower()]
    if len(partial) == 1:
        return partial[0], None
    if not partial:
        listed = "; ".join(str(e.get("item") or "")[:60] for e in items) or "(none listed)"
        return None, f"no surfaced_to_pm item matches {needle!r} — listed: {listed}"
    listed = "; ".join(str(e.get("item") or "")[:60] for e in partial)
    return None, f"{needle!r} matches {len(partial)} surfaced items — name one: {listed}"


@register_op("sizing.discharge_surfaced")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.discharge_surfaced" handler.

    Plain `def`, deliberately not `async def`: every step here is blocking
    (path stats, `locked_rmw`), so a zero-await `async def` would make
    DISPATCH_TIMEOUT_SECS silently unenforceable for this op (the incident
    remediated at 3241c7c95573).

    Params:
        sizing_path (str)  — absolute or repo-relative path to the sizing-object
                             under `state/sizings/`. Required.
        item        (str)  — the surfaced item to discharge: its exact `item` text,
                             or a substring that matches exactly one. Required.
        resolution  (str)  — what was decided AND why, stated so a later reader need
                             not reconstruct it. Required.
        resolved_by (str)  — path to the artifact that settled it (a decision record,
                             a PM ruling, a downstream plan). Required, and must
                             resolve to a real file under docs/decisions/, docs/plans/
                             or state/.
        decided_on  (str)  — YYYY-MM-DD the PM actually decided. Defaults to today.
        key         (str)  — optional `pm_resolution` key; derived from the item's own
                             text when omitted, so a re-run lands on the same key.
        supersede   (bool) — overwrite an existing resolution under the same key.

    Returns: {exit_code, applied, message|error, key}.

    Exit-code contract:
        exit_code 1 — a missing required param; a malformed decided_on; a
                      sizing_path escaping state/sizings/ or absent on disk; a
                      resolved_by outside the allowed roots or absent on disk; no
                      surfaced_to_pm array, or no unique matching item; the derived
                      key already resolved and `supersede` unset; post-mutation
                      schema validation failure; lock timeout.
        exit_code 0, applied True  — the resolution was written.
        exit_code 0, applied False — an identical resolution was already recorded
                      under this key; idempotent no-op.
    """
    sizing_path_raw: str = (params.get("sizing_path") or "").strip()
    item_raw: str = (params.get("item") or "").strip()
    resolution: str = (params.get("resolution") or "").strip()
    resolved_by_raw: str = (params.get("resolved_by") or "").strip()
    decided_on: str = (params.get("decided_on") or "").strip() or date.today().isoformat()
    supersede: bool = bool(params.get("supersede"))

    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if not item_raw:
        return _err("missing required param: item — name the surfaced item being discharged")
    if not resolution:
        return _err(
            "missing required param: resolution — a discharge records what was decided "
            "and why; this op does not compose the PM's reasoning"
        )
    if not resolved_by_raw:
        return _err(
            "missing required param: resolved_by — a surfaced item is resolved in its own "
            "artifact, and a discharge no reader can follow is the YAML comment this "
            "field replaced"
        )
    if not _DATE_RE.match(decided_on):
        return _err(f"decided_on must be YYYY-MM-DD, got {decided_on!r}")
    if repo_root is None:
        return _err(
            "sizing.discharge_surfaced: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [worktree / "state" / "sizings"])
    if p is None:
        return _err(f"sizing_path escapes state/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_path_raw}")

    resolver = Path(resolved_by_raw)
    if not resolver.is_absolute():
        resolver = worktree / resolver
    resolver = contained_path(resolver, [worktree / r for r in _RESOLVER_ROOTS])
    if resolver is None:
        return _err(
            f"resolved_by escapes {', '.join(_RESOLVER_ROOTS)}: {resolved_by_raw!r}"
        )
    if not resolver.is_file():
        return _err(
            f"resolved_by does not resolve to a real file: {resolved_by_raw!r} — this op "
            "requires live evidence the item was actually settled somewhere, not a "
            "caller's assertion that it will be"
        )

    _state: dict = {"applied": False, "key": None}

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"discharge_surfaced: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("discharge_surfaced: sizing-object is not a YAML mapping")

        surfaced = doc.get("surfaced_to_pm")
        if not isinstance(surfaced, list) or not surfaced:
            raise MutateAbort(
                f"refusing to discharge on {p}: surfaced_to_pm is empty or absent — "
                "there is no surfaced item here to answer"
            )

        entry, match_error = _match_item(surfaced, item_raw)
        if entry is None:
            raise MutateAbort(f"refusing to discharge on {p}: {match_error}")

        key = (params.get("key") or "").strip() or derive_key(str(entry.get("item") or ""))
        if key in _RESERVED_KEYS:
            raise MutateAbort(
                f"refusing to discharge on {p}: {key!r} is pm_resolution's own reserved key"
            )
        _state["key"] = key

        value = f"{resolution} (resolved in {resolved_by_raw})"

        prior = doc.get("pm_resolution")
        mapping: dict = dict(prior) if isinstance(prior, dict) else {}
        existing = mapping.get(key)
        if existing == value and mapping.get("decided_on") == decided_on:
            return old_text
        if existing is not None and not supersede:
            raise MutateAbort(
                f"refusing to discharge on {p}: pm_resolution already carries {key!r} "
                f"({str(existing)[:120]!r}) — pass supersede to replace it, so a second "
                "answer cannot silently displace the first"
            )

        mapping["decided_on"] = decided_on
        mapping[key] = value
        ordered = {"decided_on": mapping.pop("decided_on"), **mapping}

        new_text = write_fm_nested_field(old_text, "pm_resolution", _render_pm_resolution(ordered))
        errors = _validate_sizing_fm(new_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(
                f"discharge_surfaced: post-mutation schema validation failed: {details}"
            )

        _state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "discharge_surfaced: mutation aborted")

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "key": _state["key"],
            "message": (
                f"discharged {_state['key']!r} on {sizing_path_raw} per {resolved_by_raw} "
                f"(decided_on {decided_on}); the surfaced_to_pm entry stays listed"
            ),
        }
    return {
        "exit_code": 0,
        "applied": False,
        "key": _state["key"],
        "message": (
            f"{sizing_path_raw} already records this resolution under {_state['key']!r} — "
            "idempotent no-op"
        ),
    }
