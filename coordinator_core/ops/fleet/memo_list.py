"""
coordinator_core.ops.fleet.memo_list — memo.list COMPUTE_ONLY UDS op handler.

Purpose: Enumerate registered cross-repo memo receivers and, given a `to`
target, resolve it (footgun #1's no-write `--dry-run`/`--check` verb) WITHOUT
writing or committing anything — the sender-side "am I about to write into
the right place?" check that the DoE bash CLI never had, which is exactly how
a bare `--title` test probe wrote-and-committed a live memo into a sibling
repo (2026-07-17, example-retrieval-repo-em). Registered as "memo.list" via @register_op;
classification (OpClass.COMPUTE_ONLY) and the `_OP_KEY_SCOPE` entry are wired
in C7, mirroring memo_send.py's own C2/C3 split.

Two modes, selected by whether `to` is supplied:
  - Enumeration mode (`to` absent): returns the FULL structured registry data
    a sender needs — one flat `candidates` list, `kind`-discriminated (the
    fleet.* wire envelope is frozen at the top level; see
    `_enumerate_candidates`'s docstring for why the sections live inside
    `candidates` rather than as new envelope keys):
      - `kind: "receiver"` — one entry per registered `repos.*` key, each
        additionally flagged with `is_central` (machine-readable, not left
        for the client to infer from `identity.centralReceiverIds` itself)
        and `aliases` (every id string — manifest `repoAliases` shortname/
        `-em` forms, plus matched central ids — that also resolves to this
        receiver).
      - `kind: "publish_mirror"` — `publish.mirrors.*` entries, clearly
        marked `is_receiver: False` — publish TARGETS, never valid memo
        receivers, kept in a distinct `kind` so a renderer can never
        conflate them with `receiver` entries.
      - `kind: "canonical_home_alias"` — DoE-canonical redirect aliases
        (`identity.redirectAliases`, promoted by DoE 2026-07-21) — `[]` only
        when the field is absent/unreadable (stale manifest or read
        failure), not an error in itself. Any id here takes precedence over
        a colliding `publish_mirror` id (see `_enumerate_publish_mirrors()`'s
        subtraction rule) — no id is ever emitted under both `kind`s.
      - `kind: "registry_status"` — one trailing entry reporting the
        registry read succeeded (a positive soft-status signal for the
        renderer; a genuine hard failure never reaches this shape at all —
        see negative-spec below).
    This is the data `_format_receiver_listing()` in the DoE CLI's
    `cross-repo-memo` currently sources+composes itself (central-first
    block with aliases, a registry-read-failure warning, a publish-mirrors
    section, a DoE-canonical-home-aliases section); moving it into this op
    lets that CLI's `--list-receivers` verb flip to pure invoke-and-render.
  - Resolution mode (`to` supplied): resolve `to` through the ONE shared
    `_memo_resolver` (C3) — the identical resolution `memo.send` performs —
    and report the destination inbox path a send would use, without ever
    calling `memo.send` or touching the filesystem beyond read-only registry
    reads. Unresolved `to` gets the same fail-loud message shape (and C4
    "did you mean?" suggestion) `memo.send` returns, so a sender sees the
    exact same verdict in preview as they would on an actual send attempt.
    A resolved candidate additionally carries `canonical_to` —
    `_memo_resolver.canonical_receiver_id(to)`, the SAME value `memo.send`
    stamps into the delivered memo's `to:` frontmatter field — so this
    preview and the actual write always agree on the canonicalized
    addressee, never just the caller's literal `to` string.
    When `to` resolves AND the caller ALSO supplies an OPTIONAL `topic`, the
    candidate additionally carries `resolved_filename` — the AUTHORITATIVE
    DR-026 sender-namespaced filename (`YYYY-MM-DD-<sender>-<topic>.md`)
    `memo.send` would actually write for that `to`+`topic`+`from_id` triple,
    computed by calling `memo_send._memo_filename` DIRECTLY (imported, not
    reimplemented) so there is exactly one filename authority in the
    codebase. The `<sender>` segment is likewise caller-derived through
    `memo_send.resolve_sender_id` (imported, same authority `memo.send`'s
    own `from_id` defaulting uses) — an OPTIONAL `from_id` param mirrors
    `memo.send`'s own param and defaults identically to the engine actor id
    when absent. Prior to this, `resolved_filename` was computed with the
    engine actor id HARDCODED regardless of what `from_id` an actual send
    would use — correct only for claude-klabauter-origin sends, silently wrong for
    every other caller (reported by DoE/claude-central-em: `cross-repo-memo
    --dry-run` previewed `claude-klabauter-engine`-namespaced filenames for DoE-origin
    sends that actually land `claude-central-em`-namespaced). `to` supplied
    without `topic` (or vice versa) behaves exactly as before this addition —
    no `resolved_filename` key is added to the candidate dict at all.
    DEGRADED CASE: a caller-supplied `from_id` that sanitizes to an empty
    slug (all-punctuation/non-ASCII) is the one case `memo.send` itself
    cannot resolve either — `_memo_filename` raises `ValueError` there, and
    `memo.send` turns that into a fail-loud setup-error envelope (never a
    filename). `memo.list` mirrors that exact posture: the same `ValueError`
    propagates into this op's own `exit_code:1` setup-error envelope — never
    a silent fallback to the engine actor id, which would just recreate the
    "wrong-but-confident preview" defect this fix closes for a different
    input shape.
    CAVEAT (Finding 3, review sidecar above): this models ONLY the plain
    filename path. `memo.list` accepts no `supersedes` param, so this preview
    once could not model
    `memo_send._redelivery_filename`'s branch (same-day `supersedes:`
    collision). RESOLVED 2026-08-26, by deletion rather than by this preview
    improving: `_redelivery_filename` no longer exists anywhere in
    `coordinator_core`, and live `memo.send` REFUSES a same-day collision
    ("already exists in the receiver's inbox -- refuse (no clobber)") instead
    of disambiguating it. This preview is now exact for the supersedes case
    too, because there is no second filename shape left to miss. What a sender
    should know instead is that a same-day re-send does not land at all.

Both modes always return the `dry_run:true` envelope (`build_dry_run_result`)
— `memo.list` has no "act" mode; it is a pure read from end to end.

Spec backlink:
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C2, AC2.
    docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md § Amendment
    2026-07-21 (Option A, full ownership move).
    Resolver: coordinator_core/ops/fleet/_memo_resolver.py (C3).
    Parity source: DoE coordinator/bin/cross-repo-memo.py receiver-listing section
    (this op is an ergonomic superset — it additionally proves no-write via a
    real dry-run resolution path, which the DoE CLI's bare `--title` probe did
    not have).

Negative-spec:
  - Does NOT write any file, create any directory, or run any git command —
    provably side-effect-free (AC2; also DR-210 Open-Q §2 store-less-ness,
    same `test_no_memo_index`-shaped architecture test pattern as memo_send.py
    C6/AC8, applied here for C2).
  - Does NOT grow a fleet-wide memo index or receiver cache — every call
    re-reads the registry fresh (Q-d store-less-ness invariant).
  - Does NOT fall back to a folder scan on registry-read failure — propagates
    `_memo_resolver.RegistryReadError` as a fail-loud setup-error envelope,
    identical posture to memo_send.py and _memo_resolver itself.
  - Does NOT auto-select a "did you mean?" suggestion as a resolution — it is
    advisory text only, mirroring memo_send.py's C4 usage of
    `suggest_nearest_receiver`.
  - Does NOT accept `dry_run: false` — memo.list has no act mode; a caller
    that passes `dry_run: false` gets a setup-error envelope rather than a
    silently-ignored flag (fail loud on a nonsensical request, not silent
    no-op).
  - Does NOT accept a `topic` that `memo.send` would reject — a present-but-
    invalid `topic` (fails `memo.send`'s own `_TOPIC_SLUG_RE`, including
    empty/whitespace-only) fails loud with a setup-error envelope, never
    silently coerced into "topic absent, no resolved_filename". A faithful
    preview must reject exactly what the real send would reject — validation
    is single-authority here the same way the filename computation is.
  - Does NOT add new top-level keys to the `build_dry_run_result` envelope —
    every fleet op in this package returns that builder's output completely
    unmodified (contract §2.1's frozen wire shape); enumeration mode's
    richer data is carried inside `candidates` via a `kind` discriminator
    instead (see `_enumerate_candidates`).
  - Does NOT treat `registry_status` as a fallback trigger or a soft-fail
    channel for a HARD registry-read failure — a present-but-unparseable
    registry file (or `tomllib` unavailable) still propagates
    `RegistryReadError` into the existing `exit_code:1` setup-error envelope,
    unchanged. `registry_status` only ever appears on the success path, as a
    positive "read succeeded" signal.
  - Does NOT conflate `publish.mirrors.*` entries with `repos.*` receivers —
    they carry a distinct `kind: "publish_mirror"` and `is_receiver: False`,
    mirroring the DoE CLI's own hard separation (mirrors are outward OSS
    distribution targets whose owner must be addressed instead, never the
    mirror itself).
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_dry_run_result,
    build_setup_error_result,
)
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id as _canonical_receiver_id,
    convention_repo_key as _convention_repo_key,
    read_central_receiver_ids as _read_central_receiver_ids,
    read_publish_mirrors as _read_publish_mirrors,
    read_receiver_aliases as _read_receiver_aliases,
    read_redirect_aliases as _read_redirect_aliases,
    read_registry_repos as _read_registry_repos,
    receiver_em_to_repo_key as _receiver_em_to_repo_key,
    resolve_receiver_inbox as _resolve_receiver_inbox,
    suggest_nearest_receiver as _suggest_nearest_receiver,
)
from coordinator_core.ops.fleet._memo_compose import (
    _TOPIC_SLUG_RE,
    _memo_filename,
    resolve_sender_id,
)

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.list is a single-mode op —
# it only ever returns the dry_run envelope, matching the fleet contract's
# mode/dry_run vocabulary rather than inventing a third state).
_MODE = "list"


def _validate_list_params(params: dict):
    """Validate memo.list params; return (dry_run, to, topic, from_id) or a setup-error dict.

    Required: dry_run (bool) — must be True; memo.list has no act mode.
    Optional: to (str) — when present, switches to resolution mode.
    Optional: topic (str) — when present ALONGSIDE a resolvable `to`, triggers
        `resolved_filename` computation on the resolution-mode candidate (see
        module docstring). `topic` alone (no `to`) is accepted but has no
        effect — resolution mode is still gated on `to`. A PRESENT topic is
        validated against `memo.send`'s OWN `_TOPIC_SLUG_RE` (imported, same
        authority as the filename computation — a preview must never accept
        a topic memo.send would reject, or the preview lies). Absence of the
        `topic` key entirely is fine (no validation applies); a topic that IS
        supplied but fails the regex (including empty/whitespace-only) fails
        loud with a setup-error envelope — it is never silently coerced to
        "no topic".
    Optional: from_id (str) — mirrors `memo.send`'s own `from_id` param;
        only affects `resolved_filename` (the sender-namespace segment).
        Only type-checked here (must be a string when present) — the deeper
        "does this sanitize to a usable sender slug" check is `memo_send
        ._memo_filename`'s own job (single authority; see module docstring's
        DEGRADED CASE note), not re-validated here. Absent entirely defaults
        to the engine actor id via `memo_send.resolve_sender_id`, identical
        to `memo.send`'s own no-`from_id` default.
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    if dry_run is False:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list: dry_run must be true — memo.list is a pure read op with "
            "no act mode (it never writes, regardless of this flag).",
        )

    to = params.get("to")
    if to is not None and not isinstance(to, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list: to, when supplied, must be a string (receiver EM identity)",
        )
    to = to or None

    topic = params.get("topic")
    if topic is not None:
        if not isinstance(topic, str):
            return build_setup_error_result(
                _MODE, dry_run,
                "memo.list: topic, when supplied, must be a string",
            )
        # Same authority memo.send uses (_TOPIC_SLUG_RE, imported — not a
        # parallel regex) — a preview must fail loud on exactly the topics
        # memo.send would reject, including empty/whitespace-only, rather
        # than silently coercing an invalid-but-present topic to "absent".
        if not topic or not _TOPIC_SLUG_RE.fullmatch(topic):
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.list: topic {topic!r} is invalid — must match "
                f"[a-z0-9][a-z0-9-]* (lowercase alphanum and hyphens only, "
                f"starting with alphanum). Path chars (/, .., absolute paths) "
                f"are not permitted. (Same validation memo.send applies to "
                f"topic — this preview would otherwise show a filename for a "
                f"topic memo.send would reject.)",
            )

    from_id = params.get("from_id")
    if from_id is not None and not isinstance(from_id, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list: from_id, when supplied, must be a string (sender identity override)",
        )

    return dry_run, to, topic, from_id


def _central_ids_for_repo_key(
    central_ids: set, manifest_aliases: dict, repo_key: str
) -> set:
    """Central receiver ids (`identity.centralReceiverIds`) that resolve to `repo_key`.

    Mirrors `_memo_resolver.resolve_receiver_inbox()`'s central-fan-in branch
    (identical alias-or-convention mapping per id) but computed for EVERY
    registered `repo_key` at once — enumeration needs a machine-readable
    `is_central` flag per receiver, not a single-winner pick for one `to`
    (that remains `resolve_receiver_inbox`'s job for the resolution-mode path,
    which this dispatch does not touch).
    """
    matches: set = set()
    for cid in central_ids:
        shortname = cid[:-3] if cid.endswith("-em") else cid
        candidate_key = (
            "repos." + manifest_aliases[shortname]
            if shortname in manifest_aliases
            else _convention_repo_key(cid)
        )
        if candidate_key == repo_key:
            matches.add(cid)
    return matches


def _receiver_shortname_aliases(manifest_aliases: dict, repo_key: str) -> set:
    """Manifest `repoAliases` shortname forms (bare + `-em`) that map to `repo_key`.

    `receiver_em_to_repo_key()` accepts either form (e.g. `example-game-repo` or
    `example-game-repo-em` both resolve `repos.example_game_workbench_repo`) — both forms
    are surfaced here so a caller of `--to` sees every string that actually
    works, not just one.
    """
    suffix = repo_key[len("repos."):] if repo_key.startswith("repos.") else repo_key
    out: set = set()
    for shortname, registry_key in manifest_aliases.items():
        if registry_key == suffix:
            out.add(shortname)
            out.add(shortname if shortname.endswith("-em") else f"{shortname}-em")
    return out


def _normalize_registry_path(path_str: str) -> str:
    """Normalize a registered path string for cross-namespace collision comparison.

    Collision detection between a `repos.*` receiver and a `publish.mirrors.*`
    entry must be on the RESOLVED PATH, not on string-similar ids — a
    `repos.claude_klabauter` and a `publish.mirrors.claude_klabauter` collide
    because both resolve to the same on-disk path, not because their key
    names happen to match (a machine could register them under unrelated
    key names and still collide, or register similar key names pointing at
    genuinely different paths). `os.path.normcase` folds Windows
    drive-letter/backslash-vs-forward-slash/case variance; `os.path.normpath`
    folds `.`/`..`/duplicate-separator variance. Does not touch the
    filesystem (no `.resolve()`) — a registry entry may point at a path that
    doesn't exist on this machine yet, and enumeration must not fail on that.
    """
    return os.path.normcase(os.path.normpath(path_str))


def _mirror_paths(mirrors_by_key: dict) -> set:
    """Normalized path set of every `publish.mirrors.*` entry with a present path.

    Shared by the receiver-exclusion check in `_enumerate_candidates` and
    (indirectly, via the same `mirrors_by_key` source) `_enumerate_publish_mirrors`
    — single normalization authority so both sides of the collision agree on
    what counts as "the same path".
    """
    return {
        _normalize_registry_path(entry["path"])
        for entry in mirrors_by_key.values()
        if entry.get("path")
    }


def _enumerate_publish_mirrors(mirrors_by_key: Optional[dict] = None) -> list:
    """Enumerate `publish.mirrors.*` entries — publish TARGETS, not memo receivers.

    Sourced from `_memo_resolver.read_publish_mirrors()` — the SAME
    nested-table TOML reader `_memo_resolver.read_publish_mirror_owners()`
    uses for `memo.draft`'s `classify_receiver` check (single registry-merge
    authority; see `_memo_resolver.py`'s `_read_merged_publish_mirrors()`).
    Prior to this fix, this function independently re-parsed
    `publish.mirrors.*` via a flat-string-only merge that could never see real
    bracket-table TOML (`[publish.mirrors.<key>]`) — silently returning zero
    mirrors against a production registry file (Finding 2,
    `state/review-trail/findings/2026-07-21-codereview-slicememo-clean-split-op-coverage-coordinator-core-ops-fleet-memo-draft-py.md`).

    Only mirror keys with a present `.owner` are surfaced — an
    incomplete/malformed mirror table (no owner to route a rejected send to)
    is silently excluded here, mirroring
    `read_publish_mirror_owners()`'s own owner-required filter (the DoE CLI's
    `_get_publish_target_owners()` derivation has the same requirement).

    Each entry is `kind: "publish_mirror"` and carries `is_receiver: False`
    plus an explicit `note` — a renderer must never conflate these with
    `kind: "receiver"` entries; addressing a publish mirror as a memo `to`
    target is a rejected send, and the rejection routes to the owner named
    here, not to the mirror itself.

    Redirect-classification precedence (per-id subtraction, drop-if-empty):
    `_memo_resolver.read_redirect_aliases()` (`identity.redirectAliases`) is
    the MORE SPECIFIC truth on any id it names — a redirect alias is not a
    "publish target with an owner to route to", it's "this id IS the central
    receiver under another name" (see `_enumerate_canonical_home_aliases()`).
    Emitting the same id under both `kind`s hands a reader two contradictory
    verdicts for the identical string (defect: DoE's `--list-receivers`
    output showed `coordinator-claude-em` twice — once as
    `publish_mirror`/"reject, route to owner", once as
    `canonical_home_alias`/"redirects to the central receiver" — with no way
    to tell which applies). The fix is NOT "drop the whole mirror if ANY of
    its aliases collides" (that over-deletes: it would silently discard
    genuine owner-routing info for the mirror's non-colliding ids too).
    Instead, subtract the redirect-alias set from each mirror's alias set
    per-id: a mirror whose alias set becomes EMPTY after subtraction is
    fully shadowed and is omitted entirely (including its `em_id`, which is
    folded into the alias-set subtraction so no `publish_mirror` entry ever
    advertises an `em_id` that isn't also present in its own `aliases`); a
    mirror with a non-empty remainder survives with only the surviving
    (non-redirect) aliases, `owner`/`path`/`note` unchanged. This subtraction
    is a no-op (identical to pre-fix behavior byte-for-byte) whenever
    `read_redirect_aliases()` degrades to `set()` — the case on every machine
    until DoE promotes `identity.redirectAliases` (see that function's own
    docstring) — since subtracting the empty set changes nothing. Invariant:
    no id ever appears in both a `publish_mirror` entry's addressable surface
    (`id`/`em_id`/`aliases`) and a `canonical_home_alias` entry.
    """
    mirrors_by_key = (
        _read_publish_mirrors() if mirrors_by_key is None else mirrors_by_key
    )
    redirect_aliases = _read_redirect_aliases()
    mirrors = []
    for mirror_key in sorted(mirrors_by_key):
        entry = mirrors_by_key[mirror_key]
        owner = entry["owner"]
        if owner is None:
            continue
        path = entry["path"]
        explicit_aliases = entry["aliases"]
        hyphenated = mirror_key.replace("_", "-")
        em_id = f"{hyphenated}-em"
        aliases = sorted(
            ({hyphenated, em_id, *explicit_aliases}) - redirect_aliases
        )
        if not aliases:
            # Fully shadowed by redirect classification — the mirror's
            # entire addressable surface collides with the more-specific
            # canonical_home_alias truth, so omit the mirror entirely
            # rather than emit an entry with no addressable id at all.
            continue
        # em_id must stay internally consistent with the surviving
        # aliases — never advertise an id step 1 just subtracted away.
        em_id = em_id if em_id in aliases else None
        mirrors.append({
            "kind": "publish_mirror",
            "id": mirror_key,
            "mirror_key": mirror_key,
            "em_id": em_id,
            "owner": owner,
            "path": path,
            "aliases": aliases,
            "is_receiver": False,
            "note": (
                f"publish-target mirror (OSS distribution mirror) — NOT a "
                f"valid memo receiver; a send addressed to this id must be "
                f"rejected and routed to the owner {owner!r} instead."
            ),
        })
    return mirrors


def _enumerate_canonical_home_aliases() -> list:
    """Enumerate central/canonical-home redirect aliases (`identity.redirectAliases`).

    DoE-manifest-declarative equivalent of the DoE CLI's hardcoded
    `_DOE_CANONICAL_REDIRECT_ALIASES` block (`.claude-em`, `claude-home`,
    `coordinator-claude`, `coordinator-claude-em` — ids that are not
    distribution mirrors at all, just the central receiver under a different
    name). Sourced via `_memo_resolver.read_redirect_aliases()`, which
    degrades to `set()` (never an error) when the manifest lacks
    `identity.redirectAliases` — a graceful-degradation floor, not the
    common case: DoE promoted this field into
    `coordinator-registry.manifest.json` on 2026-07-21, so this normally
    returns a non-empty list on any machine with an up-to-date manifest;
    `[]` now means either a stale/un-updated manifest or a genuine read
    failure, not "the field was never promoted."

    Precedence over `_enumerate_publish_mirrors()`: every id returned here is
    authoritatively a redirect, never a publish-mirror-addressable id — see
    that function's docstring for the per-id subtraction rule that keeps the
    two `kind`s from ever emitting the same id with contradictory guidance.
    """
    return [
        {
            "kind": "canonical_home_alias",
            "id": alias,
            "alias": alias,
            "is_receiver": False,
            "note": (
                "DoE-canonical home/redirect alias — not directly addressable "
                "as `to`; always redirects to the central receiver."
            ),
        }
        for alias in sorted(_read_redirect_aliases())
    ]


def _enumerate_candidates() -> list:
    """Build the full enumeration-mode candidate list (the `to`-absent path).

    A single flat `candidates` list, each entry carrying a `kind`
    discriminator so a renderer can unambiguously separate the sections —
    the fleet.* wire envelope (`build_dry_run_result`, contract §2.1) is
    frozen at the top level (`exit_code`/`mode`/`dry_run`/`candidates`/
    `acted`/`skipped`/`failed` only; every other fleet op in this package
    returns that builder's output completely unmodified), so new structured
    data is carried INSIDE `candidates` via `kind`, not as new top-level
    envelope keys:

      - `kind: "receiver"`       — one entry per registered `repos.*` key
        (the pre-existing shape), now additionally flagged with `is_central`
        (machine-readable, not left for the client to infer) and `aliases`
        (every manifest/central id string that also resolves to this
        receiver).
      - `kind: "publish_mirror"` — `publish.mirrors.*` entries. These are
        publish TARGETS, never valid memo receivers — kept in a clearly
        distinct `kind` so a renderer can never conflate them with `receiver`
        entries (see `_enumerate_publish_mirrors`).
      - `kind: "canonical_home_alias"` — DoE-canonical redirect aliases
        (`identity.redirectAliases`, promoted by DoE 2026-07-21); an empty
        list means the field is absent/unreadable on this machine, not a
        failure (see `_enumerate_canonical_home_aliases`). Takes precedence
        over any colliding `publish_mirror` id (see
        `_enumerate_publish_mirrors`'s per-id subtraction rule) — an id is
        never emitted under both `kind`s.
      - `kind: "registry_status"` — exactly one trailing entry reporting a
        POSITIVE "the registry read succeeded" status for the renderer. This
        is the soft/partial-success signal only — it is never a fallback
        trigger. A genuine registry-read failure never reaches this function
        at all: `read_registry_repos()` raises `RegistryReadError` (checked
        BEFORE the publish-mirror read below, so a corrupt registry file is
        still caught) which `_memo_list()` turns into the existing
        `exit_code:1` fail-loud setup-error envelope (unchanged posture, no
        folder-scan fallback, no partial/soft-fail mode on the hard-failure
        path — see module negative-spec).

    Receiver/publish-mirror path collision (send-path authority, per-id
    subtraction pattern applied at receiver granularity): a `repos.*` entry
    whose registered path matches a `publish.mirrors.*.path` (compared via
    `_normalize_registry_path`, not string-similar ids — see that helper's
    docstring) is EXCLUDED from the `receiver` block entirely and surfaces
    only via its `publish_mirror` entry. This mirrors `_enumerate_publish_mirrors`'s
    own per-id subtraction rule for redirect aliases (same invariant: no id's
    underlying path is ever addressable under two contradictory `kind`s), but
    at receiver granularity rather than per-alias — a `receiver` entry has one
    path, not a subtractable alias set, so a colliding entry is dropped
    wholesale rather than partially. `memo.send` already refuses a `to` whose
    resolved path is a registered mirror; this keeps enumeration's advertised
    receivers in agreement with what a send would actually accept (the send
    path is authoritative — see module docstring).

    Read-only: every reader used here (`_read_registry_repos()`,
    `_read_publish_mirrors()`, `_read_central_receiver_ids()`,
    `_read_receiver_aliases()`, `_read_redirect_aliases()`) re-reads its
    source fresh on every call — no caching, no persisted index (Q-d
    store-less-ness invariant).
    """
    all_repos = _read_registry_repos()
    central_ids = _read_central_receiver_ids()
    manifest_aliases = _read_receiver_aliases()
    mirrors_by_key = _read_publish_mirrors()
    mirror_paths = _mirror_paths(mirrors_by_key)

    receivers = []
    for repo_key, repo_path_str in sorted(all_repos.items()):
        if _normalize_registry_path(repo_path_str) in mirror_paths:
            # Shadowed by a publish-mirror at the same resolved path — a
            # send to this repo_key would be refused (mirror precedence on
            # the send side), so it must not appear as an addressable
            # receiver here either. It still surfaces via its
            # `publish_mirror` entry below.
            continue
        repo_path = Path(repo_path_str)
        inbox_dir = repo_path / "cross-repo" / "inbox"
        matched_central_ids = _central_ids_for_repo_key(
            central_ids, manifest_aliases, repo_key
        )
        aliases = matched_central_ids | _receiver_shortname_aliases(
            manifest_aliases, repo_key
        )
        receivers.append({
            "kind": "receiver",
            "id": repo_key,
            "repo_key": repo_key,
            "repo_path": str(repo_path),
            "target_inbox": str(inbox_dir),
            "resolved": True,
            "is_central": bool(matched_central_ids),
            "aliases": sorted(aliases),
        })

    publish_mirrors = _enumerate_publish_mirrors(mirrors_by_key)
    canonical_home_aliases = _enumerate_canonical_home_aliases()
    registry_status = [{
        "kind": "registry_status",
        "id": "registry_status",
        "ok": True,
        "note": (
            "machine-local registry read succeeded "
            "(repos.*, publish.mirrors.*, identity aliases)."
        ),
    }]

    return receivers + publish_mirrors + canonical_home_aliases + registry_status


def _resolve_candidate(
    to: str, topic: Optional[str] = None, from_id: Optional[str] = None
) -> dict:
    """Resolve a single `to` target through the shared resolver (resolution mode).

    Returns a single candidate dict describing the resolution outcome — either
    a resolved destination inbox path, or a `resolved: false` entry carrying
    the same fail-loud reason (plus C4 "did you mean?" suggestion, when one
    exists) memo_send.py would surface on an actual send attempt.

    When `to` resolves AND `topic` is supplied, the candidate additionally
    carries `resolved_filename` — the authoritative DR-026 filename
    `memo.send` would write for the PLAIN (non-supersedes) send path,
    computed via the SAME `_memo_filename` function memo_send.py itself calls
    (imported, never reimplemented — single filename authority). The sender
    segment is resolved via `memo_send.resolve_sender_id(from_id)` — the SAME
    from_id-or-engine-actor default `memo.send`'s own param uses (single
    authority for both halves of the filename, not just the topic/date
    halves). `topic` absent (or `to` unresolved) means no `resolved_filename`
    key is added at all — unchanged from before this field existed. Does NOT
    model `memo.send`'s former `supersedes`-triggered
    `_redelivery_filename` branch, which no longer exists (removed by
    2026-08-26; live `memo.send` refuses a same-day collision rather than
    renaming around it). The preview is exact; a same-day re-send is refused,
    not filed elsewhere.

    Raises:
        ValueError: propagated from `_memo_filename` when the resolved sender
            sanitizes to an empty slug (a caller-supplied `from_id` consisting
            entirely of punctuation/non-ASCII chars) — mirrors `memo.send`'s
            own fail-loud posture for the identical input rather than falling
            back to the engine actor id (see module docstring DEGRADED CASE
            note). Caught by `_memo_list` and turned into a setup-error
            envelope, same shape as the sibling `RegistryReadError`/
            `AmbiguousReceiverError` catches there.
    """
    inbox_dir, receiver_repo_path, all_repos = _resolve_receiver_inbox(to)
    if inbox_dir is not None:
        # canonical_to is the SAME value memo.send stamps into the delivered
        # memo's `to:` frontmatter field (see _memo_resolver.canonical_receiver_id) —
        # exposing it here is what lets this dry-run preview and the actual write
        # agree on the canonicalized addressee rather than each echoing whatever
        # alias the caller happened to type.
        candidate = {
            "id": to,
            "receiver": to,
            "canonical_to": _canonical_receiver_id(to),
            "repo_path": str(receiver_repo_path),
            "target_inbox": str(inbox_dir),
            "resolved": True,
            "note": None,
        }
        if topic:
            today = datetime.date.today().isoformat()
            sender = resolve_sender_id(from_id)
            candidate["resolved_filename"] = _memo_filename(today, sender, topic)
        return candidate

    if to.strip().lower() in _read_central_receiver_ids():
        return {
            "id": to,
            "receiver": to,
            "repo_path": None,
            "target_inbox": None,
            "resolved": False,
            "note": (
                f"receiver {to!r} is a central receiver id "
                f"(identity.centralReceiverIds) that resolves to the DoE-claude "
                f"repo, but none of the manifest's central receiver ids is "
                f"registered in the machine-local registry. "
                f"Register the central repo first, e.g.: "
                f"machine-local set repos.doe_claude <abs-path-to-DoE-claude-repo>"
            ),
        }

    repo_key = _receiver_em_to_repo_key(to)
    suggestion = _suggest_nearest_receiver(to, all_repos)
    suggestion_clause = f" Did you mean {suggestion!r}?" if suggestion else ""
    return {
        "id": to,
        "receiver": to,
        "repo_path": None,
        "target_inbox": None,
        "resolved": False,
        "note": (
            f"receiver {to!r} resolves to registry key {repo_key!r} which is not "
            f"registered in the machine-local registry.{suggestion_clause} "
            f"Register the receiver repo first: "
            f"machine-local set {repo_key} <abs-path-to-repo>"
        ),
    }


@register_op("memo.list")
def _memo_list(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'memo.list' COMPUTE_ONLY UDS op handler.

    Enumerate registered receivers, or resolve one `to` target — a no-write
    `--dry-run`/`--check` preview (footgun #1). Never writes, commits, or
    reaches the network; provably side-effect-free (AC2).

    repo_root is accepted (per the standard handler signature) but unused —
    receiver enumeration/resolution has no sender-worktree dependency; unlike
    memo.send there is no own-inbox check to make here (nothing is written).

    Params:
        dry_run (bool, required): must be True — memo.list has no act mode.
        to      (str, optional):  receiver EM identity to resolve. Absent →
                                   enumerate every registered receiver instead.
        topic   (str, optional):  when supplied TOGETHER with a `to` that
                                   resolves, the resolution-mode candidate
                                   additionally carries `resolved_filename` —
                                   the AUTHORITATIVE DR-026 sender-namespaced
                                   filename `memo.send` would actually write
                                   for this `to`+`topic`+`from_id` triple ON
                                   THE PLAIN (non-supersedes) send path,
                                   computed by calling memo_send's own
                                   `_memo_filename` directly (single filename
                                   authority — not a reimplementation).
                                   `topic` alone, or `to` unresolved, adds no
                                   such key. `memo.list` has no `supersedes`
                                   param, so this does not model `memo.send`'s
                                   former `_redelivery_filename` branch,
                                   which no longer exists — a same-day re-send
                                   is now REFUSED, not filed under another
                                   name.
        from_id (str, optional):  mirrors `memo.send`'s own `from_id` param —
                                   the sender identity namespaced into
                                   `resolved_filename`. Absent defaults to the
                                   engine actor id via
                                   `memo_send.resolve_sender_id`, identical to
                                   `memo.send`'s own no-`from_id` default (see
                                   that function's docstring for why this was
                                   added: previously this preview hardcoded
                                   the engine actor id regardless of what
                                   `from_id` an actual send would use).

    Returns:
        The `build_dry_run_result` envelope (`exit_code:0, dry_run:true`) with
        one candidate per registered receiver (enumeration mode) or a single
        candidate describing the resolution outcome (resolution mode); or a
        `build_setup_error_result` envelope (`exit_code:1`) on bad params, a
        genuine registry-read failure, or a `from_id` that sanitizes to an
        empty sender slug (mirrors `memo.send`'s own fail-loud posture for
        that input — see `_resolve_candidate`'s docstring DEGRADED CASE note).
    """
    validated = _validate_list_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    dry_run, to, topic, from_id = validated

    try:
        if to is None:
            candidates = _enumerate_candidates()
        else:
            candidates = [_resolve_candidate(to, topic, from_id)]
    except RegistryReadError as exc:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.list: machine-local registry could not be read: {exc.reason} "
            f"(no folder-scan fallback — fix the registry file or re-run "
            f"machine-local setup).",
        )
    except AmbiguousReceiverError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.list: {exc}")
    except ValueError as exc:
        # Propagated from _memo_filename via resolve_sender_id — a
        # caller-supplied from_id that sanitizes to an empty sender slug.
        # Mirrors memo.send's own fail-loud posture for the identical input
        # (see module docstring DEGRADED CASE note) — never a silent
        # fallback to the engine actor id.
        return build_setup_error_result(_MODE, dry_run, f"memo.list: {exc}")

    return build_dry_run_result(_MODE, candidates)
