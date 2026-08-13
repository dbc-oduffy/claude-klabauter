"""
coordinator_core.sibling_fact — claude-klabauter-owned primitive: resolve a declarative
predicate against a sibling repo, observations only.

Purpose: `gate_evidence:` (docs/plans/2026-07-26-structured-sibling-evidence-
gates.md § C1/C3) and the `state/cross-repo-commitments/` ledger (§ C12b) both
need the SAME underlying capability — "ask a sibling repo's disk-truth whether
X holds" — with honest indeterminate semantics rather than a fail-open
collapse. This module is that capability, factored out as its own primitive
(PM ruling 2026-07-26, § "Scope reshape") rather than built twice: `gate_eval`
and a future ledger reconciler are its declarative CONSUMERS, not the other
way round.

PRIMITIVE KINDS — exactly three, no more, all of them sibling I/O:
    file_exists       — does `<repo root>/<path>` exist on disk?
    frontmatter_field  — what is `<field>`'s raw value in
                         `<repo root>/<path>`'s YAML frontmatter?
    commit_ancestor    — is `<commit>` an ancestor of (or equal to) `<ref>`
                         in `<repo root>`?

Deliberately NOT here (the Staff Engineer F9, F3; D3a, D4 of the spec backlink above):
    - `test-node-id` / `probe-op-key` / `sibling-commitment-ref` — these are
      `gate_evidence`'s own leg-kind vocabulary, reused verbatim from
      `coordinator/schemas/cutover.schema.json`'s `verified_by` union; they
      are gate_evidence-side COMPOSITIONS over this module's primitives (or,
      for `sibling-commitment-ref`, a read of OUR OWN disk, not sibling I/O
      at all), not new primitive kinds to add here.
    - `deadline` — a pure date comparison, no sibling I/O whatsoever. Belongs
      in `gate_eval` (resolves to a `review-due` prompt, never an AND-reduce
      participant — D3a).
    - `human` — never I/O by definition (D4); resolves to a permanent
      indeterminate in `gate_eval`, not here.
    - `memo_delivered` — deliberately DROPPED. The only on-disk reduction
      available for "was a memo delivered" is "the memo file exists / is
      archived", and this plan's own named trap case is exactly that
      collapse: an archived memo reads as delivered when it was in fact
      never sent. A leg whose satisfaction depends on this is authored
      `kind: human`, not shipped here as a fourth false-comfort kind.

NEGATIVE SPEC — OBSERVATIONS ONLY, NEVER A VERDICT (D1, eng-director F2):
    `resolve_leg` returns
        {leg_id, read_ok: bool, observed: <raw value | None>, source: str,
         error: <str | None>}
    and NEVER a `resolved` / `verdict` / `freed` / `still-blocked` field of
    any kind, for any consumer, ever. Predicate application — does `observed`
    equal the caller's `expected`, is the commit-ancestor observation `True`,
    is `read_ok is False` therefore indeterminate — lives in `gate_eval`
    (C3) or the ledger reconciler's own thin projection (C12b), never in this
    module. A resolver that decides `freed`/`still-blocked` is the
    second-evaluator failure this whole plan exists to avoid; keeping this
    module's return shape verdict-free is what makes that failure structurally
    unreachable rather than merely undocumented.

REPO ROOT RESOLUTION (D6, eng-director F6): every `repo:` value binds via
`coordinator_core.machine_resolver.registry_get("repos.<id>")` EXCEPT
`example_doctrine_repo`, which routes through
`coordinator_core.doe_root_pointer.read_doe_root_pointer()` — its
registry-then-settings-home-mirror-then-legacy ladder is the sanctioned,
reset-safe (DR-071) resolution contract for that one repo id; bare
`registry_get` alone is a fresh-machine regression for it specifically. An
unregistered repo (or a registered root absent from disk — a stale registry
entry, "the clone was never cloned here") resolves to `read_ok: False` with an
explicit reason. Never a guess, never a hardcoded path, never `__file__`-
walking across a repo boundary. `MACHINE_LOCAL_REPOS_<ID>` (honoured
transparently by `registry_get` itself) is the sanctioned test hook for
pointing a leg at a `tmp_path` repo.

REUSE, NOT RE-IMPLEMENTATION, WITHOUT THE FAIL-OPEN (the Staff Engineer F7):
    - `commit_ancestor` calls the newly-promoted, tri-state
      `coordinator_core.git_ancestry.is_ancestor` (NOT the legacy bare-bool
      `_is_ancestor`) — "not an ancestor" and "could not ask" are
      distinguishable at the source, not re-derived here.
    - `frontmatter_field` reuses
      `coordinator_core.ops.read_frontmatter_field`'s own line-extraction
      (`_extract`) and normalization (`_normalize`), but does NOT reuse its
      public `read_frontmatter_field` entry point directly — that function's
      own step 5 collapses absent-field, unreadable-file, and literal-`null`
      to the identical `""`, which is precisely the ambiguity this module
      exists to undo. See `_resolve_frontmatter_field`'s docstring.

Windows + macOS both first-class: `pathlib.Path` joins only, no POSIX
separators, no shell.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, TypedDict

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.git_ancestry import is_ancestor
from coordinator_core.machine_resolver import registry_get
#: `coordinator_core.ops.read_frontmatter_field` is imported lazily at its use
#: site in `_resolve_frontmatter_field`, NOT here. Importing anything under
#: `coordinator_core.ops.` executes that package's `__init__`, which eagerly
#: imports all ~159 op modules to populate the registry — including
#: `handoff_transition`, which imports `resolve_leg` from THIS module. At module
#: scope that closes a cycle: `import coordinator_core.sibling_fact` leaves this
#: module partially initialised, `handoff_transition` fails on the partial
#: import, and it plus `handoff_ship_archive` silently fail to register their
#: ops. A low-level primitive must not pull in the op registry at import time.

#: The closed set of primitive kinds this module resolves. Any other kind
#: handed to `resolve_leg` is a caller bug (a gate_evidence-side composition
#: or non-I/O terminal that should never have reached this module) and is
#: rejected fail-loud — see module docstring "Deliberately NOT here".
_SUPPORTED_KINDS = frozenset({"file_exists", "frontmatter_field", "commit_ancestor"})


class LegObservation(TypedDict):
    """Return shape of `resolve_leg` — observations only, see module negative spec."""

    leg_id: str
    read_ok: bool
    observed: Optional[Any]
    source: str
    error: Optional[str]


def _resolve_repo_root(repo_id: str) -> Tuple[Optional[Path], str]:
    """Resolve `repo_id` to an on-disk clone root, or `(None, reason)`.

    `example_doctrine_repo` routes through `read_doe_root_pointer()` (D6/F6); every other
    id binds via bare `registry_get("repos.<id>")`. Also rejects a registered
    root that is not actually a directory on THIS disk (a stale registry
    entry / a clone that was never made here) — distinguishing "unregistered"
    from "registered but absent" is the "absent clone" edge case C2's own
    test suite is required to cover.
    """
    if repo_id == "example_doctrine_repo":
        raw = read_doe_root_pointer()
        source = "doe_root_pointer.read_doe_root_pointer()"
    else:
        raw = registry_get(f"repos.{repo_id}")
        source = f"registry_get('repos.{repo_id}')"

    if not raw:
        return None, f"repo {repo_id!r} is unregistered (no value from {source})"

    root = Path(raw)
    if not root.is_dir():
        return None, f"repo {repo_id!r} root {str(root)!r} (via {source}) does not exist on disk"

    return root, source


def _pre_collapse_frontmatter_value(raw: str) -> str:
    """Reproduce `read_frontmatter_field._normalize`'s steps 1-4 (leading
    whitespace, inline-comment, and one layer of surrounding quotes) WITHOUT
    its step 5 (`null`-and-empty collapse to `""`), so a literal `null` value
    can be told apart from a genuinely empty one. That collapse is exactly
    the ambiguity this resolver exists to undo, so it cannot be reused for
    this one distinction — everything else about extraction (`_rff_extract`)
    is reused verbatim.
    """
    value = raw.lstrip()
    hash_idx = value.find("#")
    if hash_idx != -1:
        value = value[:hash_idx].rstrip(" \t")
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]
    return value


def _resolve_frontmatter_field(path: Path, field: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Resolve `field`'s raw frontmatter value in `path`, distinguishing
    absent / unreadable / literal-null (the Staff Engineer F7) rather than inheriting
    `read_frontmatter_field`'s `""`-for-all-three collapse.

    Returns `(read_ok, observed, error)`:
      - unreadable file            -> `(False, None, "<reason>")`
      - field absent               -> `(True, None, "field ... absent")`
      - field present, literal null -> `(True, None, "field ... is literal null")`
      - field present, empty value  -> `(True, None, "field ... present but empty")`
      - field present, real value   -> `(True, "<value>", None)`
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, None, f"frontmatter file {str(path)!r} unreadable: {exc}"

    from coordinator_core.ops.read_frontmatter_field import _extract as _rff_extract

    raw = _rff_extract(text, field)
    if raw is None:
        return True, None, f"field {field!r} absent in {str(path)!r}"

    value = _pre_collapse_frontmatter_value(raw)
    if value == "null":
        return True, None, f"field {field!r} is literal null in {str(path)!r}"
    if value == "":
        return True, None, f"field {field!r} present but empty in {str(path)!r}"
    return True, value, None


def _resolve_file_exists(repo_root: Path, source: str, rel_path: str) -> Tuple[bool, bool, str, Optional[str]]:
    """`(read_ok, observed, source, error)` for a `file_exists` leg.

    Once the repo root itself resolves to a real on-disk directory,
    existence is always answerable — `observed` is a genuine `True`/`False`,
    never indeterminate, because there is no "could not ask" state for a
    plain filesystem stat once the clone root is confirmed present."""
    target = repo_root / rel_path
    return True, target.exists(), str(target), None


def resolve_leg(leg: Mapping[str, Any]) -> LegObservation:
    """Resolve one caller-handed leg dict to an observation, never a verdict.

    Required keys: `leg_id` (str, caller-assigned identifier) and `kind` (one
    of `_SUPPORTED_KINDS`). Kind-specific required keys:
      - `file_exists`:      `repo`, `path`
      - `frontmatter_field`: `repo`, `path`, `field`
      - `commit_ancestor`:   `repo`, `commit`, `ref`

    A `kind` outside `_SUPPORTED_KINDS`, or a missing required key, is a
    caller bug (this module's return contract can only express sibling-I/O
    failure, not a malformed request) and raises `ValueError` — fail-loud,
    never a silently-indeterminate observation for a request this module was
    never asked to satisfy.
    """
    leg_id = leg.get("leg_id")
    if not leg_id:
        raise ValueError(f"sibling_fact.resolve_leg: leg missing required 'leg_id': {leg!r}")

    kind = leg.get("kind")
    if kind not in _SUPPORTED_KINDS:
        raise ValueError(
            f"sibling_fact.resolve_leg: leg {leg_id!r} has unsupported kind {kind!r}; "
            f"only {sorted(_SUPPORTED_KINDS)} do sibling I/O here — see module docstring"
        )

    repo_id = leg.get("repo")
    if not repo_id:
        raise ValueError(f"sibling_fact.resolve_leg: leg {leg_id!r} (kind {kind!r}) missing required 'repo'")

    repo_root, root_source = _resolve_repo_root(str(repo_id))
    if repo_root is None:
        return LegObservation(
            leg_id=str(leg_id), read_ok=False, observed=None, source=root_source, error=root_source
        )

    if kind == "file_exists":
        rel_path = leg.get("path")
        if not rel_path:
            raise ValueError(f"sibling_fact.resolve_leg: leg {leg_id!r} (kind file_exists) missing required 'path'")
        read_ok, observed, source, error = _resolve_file_exists(repo_root, root_source, str(rel_path))
        return LegObservation(leg_id=str(leg_id), read_ok=read_ok, observed=observed, source=source, error=error)

    if kind == "frontmatter_field":
        rel_path = leg.get("path")
        field = leg.get("field")
        if not rel_path or not field:
            raise ValueError(
                f"sibling_fact.resolve_leg: leg {leg_id!r} (kind frontmatter_field) "
                "missing required 'path' and/or 'field'"
            )
        target = repo_root / str(rel_path)
        read_ok, observed, error = _resolve_frontmatter_field(target, str(field))
        return LegObservation(
            leg_id=str(leg_id), read_ok=read_ok, observed=observed, source=str(target), error=error
        )

    # kind == "commit_ancestor"
    commit = leg.get("commit")
    ref = leg.get("ref")
    if not commit or not ref:
        raise ValueError(
            f"sibling_fact.resolve_leg: leg {leg_id!r} (kind commit_ancestor) missing required 'commit' and/or 'ref'"
        )
    read_ok, observed = is_ancestor(str(commit), str(ref), cwd=str(repo_root))
    error = None if read_ok else (
        f"could not determine whether {commit!r} is an ancestor of {ref!r} in {str(repo_root)!r} "
        "(git unreachable, unknown SHA, or not a commit object)"
    )
    return LegObservation(
        leg_id=str(leg_id), read_ok=read_ok, observed=observed, source=str(repo_root), error=error
    )
