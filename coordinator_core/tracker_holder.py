"""
coordinator_core.tracker_holder — resolves the designated holder repo for
sovereign-tracker writes whose producer named no owning repo.

Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
chunk C1 § The fail-loud fork, § Anti-scope.
DR-241 § Per-repo, not fleet-wide (DEC-11): every repo's sovereign-tracker
event log is that repo's own — writes never fan out fleet-wide. This module
exists because DEC-11 leaves one question open: where does a write whose
producer identified NO owning repo land? The holder-role key answers it — a
single, operator-designated repo that is the fleet's general "other" bucket,
not a second fleet-wide store and not a relaxation of DEC-11's per-repo bound.

The holder is NOT a person store, and the routing discriminator is NOT the
kind of thing being written. It takes work items as readily as people; the
only question asked is whether the producer stated an owning repo at all.
An identity-scoped reading of this module was retired by PM ruling
2026-08-11 (see the plan's § Problem for the verbatim ruling) — do not
reintroduce it in this docstring, in a parameter name, or in a branch.

Registry key: `tracker.holder_repo` is an INDIRECTION, not a second path
entry:

    tracker.holder_repo = "example_store_repo"     # names a repos.<key>, not a path

resolved as: role key -> `repos.<that key>` -> filesystem path. The role key
says WHICH repo holds; the existing `repos.*` family (already registry-
managed) says WHERE that repo is. This fleet's seeded value is
`example_store_repo`, resolving (as of this seeding) to whatever path
`machine-local get repos.example_store_repo` reports on this machine.

Rejected alternative, recorded so a reviewer need not re-derive it:
`repos.tracker_holder = '<path>'` — one fewer hop, but duplicates a path
already under registry management (`repos.example_store_repo`) as a second source
of truth, and turns "is the holder the same repo as example_store_repo?" into a
string comparison between two independently-drifting path values instead of
an identity check on one registry key.

This shape is now RATIFIED DOCTRINE, not just this module's local argument:
DoE-claude `coordinator/docs/wiki/machine-local-registry.md` § 5c.4
("Role-key indirection — a value that names another registry key", landed
`af17845ea`), with the anti-pattern registered as the greppable tripwire
`ROLE-KEY-NAMES-A-REPO-KEY-NOT-A-PATH` in `coordinator-tripwires.md`.
`tracker.holder_repo` is that row's first instance. Read the row before
changing anything here — every bound this docstring argues below (enumerated
family, operator-set-only, fail-loud on an unresolvable target, env override
comes free, no bespoke absolute-path var) is contract there, so weakening one
here is a doctrine break rather than a local judgement call.

Two things the row settles that this module should not re-litigate:
  - The naming convention is `<concern>.<role>_repo`; the `_repo` suffix is
    the DECLARATION that the value is a `repos.<key>`. A reader finding a path
    there has a mis-set key, not a variant form.
  - The key stays under `tracker.`, NOT in a `roles.*` family. A shared family
    was proposed by this module's author and declined: the registry namespaces
    by the question a key answers, and no reader wants "all role assignments
    across all concerns". The indirection is a value shape, not a namespace.

Resolution precedence, mirroring doe_root_pointer.py's documented tiered
shape (see that module's docstring for the general pattern this borrows):
    1. registry `tracker.holder_repo` -> `repos.<value>`   (canonical, only rung)
    2. (no derived-seed rung — deliberately absent, see below)
    3. (no fallback rung — deliberately absent, see below)

Tiers 2 and 3 are deliberately EMPTY, and the emptiness is the point.
`doe_root_pointer` has a file-mirror rung and a legacy-file rung because it
is read-only oracle fidelity for a value that predates the registry and must
degrade gracefully across a `~/.claude` reset (see that module's own "Why
registry-first" section for the reset-survival argument, cited not
restated). None of that applies here: there is no pre-registry oracle for
`tracker.holder_repo` to stay faithful to, and there is no legitimate
default holder to fall back to — an unset holder is a genuine "the operator
has not decided this yet" state, not a transient read-path gap. A seed or
fallback rung must NOT be added "for symmetry" with doe_root_pointer's
four-tier shape: see § The fail-loud fork below for why this resolver raises
instead of degrading.

No bespoke env rung. `coordinator_core.machine_resolver.registry_get`
already resolves `MACHINE_LOCAL_<KEY>` env overrides ahead of
`registry.local.toml` and `registry.toml`, per its own docstring (verified
here against that docstring, not assumed: `registry_get`'s resolution order
is documented, and independently confirmed live, as `MACHINE_LOCAL_<KEY>`
env override -> registry.local.toml -> registry.toml) — so
`tracker.holder_repo` gets `MACHINE_LOCAL_TRACKER_HOLDER_REPO` for free, with
the repo-key indirection intact (the env var still carries a repo KEY, e.g.
`example_store_repo`, never a raw path). A second, bespoke, raw-absolute-path env
var (`COORDINATOR_TRACKER_HOLDER_ROOT`) was considered and REJECTED: it
would bypass both the holder-role hop and the `repos.<key>` hop, making
`export COORDINATOR_TRACKER_HOLDER_ROOT=$PWD` a one-line, ungated route to
the exact brightline violation this plan exists to prevent (a caller's own
repo silently becoming the holder). Do not add it.

The key is OPERATOR-SET-ONLY, never auto-seeded by this module or any
caller. An auto-seeded holder would pick a repo on the operator's behalf,
which is the brightline decision itself — the whole reason this key exists
is that "which repo catches unowned identity" is a decision only a human
operator makes, not a default this module infers.

The fail-loud fork (why this resolver raises where doe_root_pointer returns
""): `doe_root_pointer.read_doe_root_pointer()` returns `""` on an
unresolved key and never raises, deliberately mirroring a bash
`cat ... 2>/dev/null || true` — oracle fidelity to the bash predecessor it
ports, per DR-071/DR-148, with the caller applying the gate itself. That
constraint does not apply here: this module has no bash oracle to stay
faithful to, and a `""` return handed to a caller deciding where to write is
exactly the brightline violation arriving by omission — the cheapest thing a
caller does with a falsy root is fall back to the one it already has. This
resolver therefore raises on every unresolved/unresolvable rung instead
(implemented by `holder_repo_root()` and `write_root_for()`, landing in this
same file after this chunk — see their own docstrings for the exact
exceptions raised). Do not "harmonize" this resolver's failure contract back
onto doe_root_pointer's `""`-return shape; the divergence is deliberate, not
drift.

Semantic brightline guard (for C2/C3 to implement, documented here as the
contract this module commits to): `holder_repo_root()` MUST assert the
resolved holder root is not claude-klabauter's own root
(`_claude_klabauter_source_tree()` — the checkout, not the engine root), raising if
it is. `write_root_for()` MUST
additionally assert the resolved holder root is not equal to the
caller-supplied `repo_root`, raising if it is — this second check needs the
caller's root and so cannot live in `holder_repo_root()` alone. Together the
two checks close the brightline mechanically (a real path comparison), not
lexically (a grep for a literal), against a caller that is, or becomes, the
holder itself.

Public API: `holder_repo_root() -> Path` (C2, landed) and
`write_root_for(*, owning_repo, repo_root) -> Path` (C3, landed; `owning_repo`
wire shape reworked C5, see § Slug -> repos.* key resolution below) — see each
function's own docstring for the exact rungs and exceptions raised.

Slug -> repos.* key resolution (C5), settled with a measurement, not a guess:

`write_root_for`'s landed shape (eec1515fc) took `owning_repo` as a bare
`repos.<key>` NAME. The agreed wire shape from cockpit is an owner-qualified
`"<owner>/<repo>"` slug instead: cockpit's repo identity is `UNIQUE (owner,
repo)` and deliberately machine-blind, and their registry is an untracked
private overlay that cannot go on the wire (both EM-verified against their
tree) — they structurally cannot emit one of our `repos.*` key names.

The fork the handoff named, and the measurement that settled it:
    (a) derive owner/name from each registered repo's `origin` remote (one
        `git remote get-url origin` spawn per candidate repos.<key>, to find
        the member matching the wire slug) — authoritative, but O(N) git
        subprocess spawns for a SINGLE write call, where N is the size of the
        `repos.*` family, not anything about the request itself. Measured on
        this box, warm cache, 5 trials of `git remote get-url origin`:
        16.1ms-20.3ms per spawn (subprocess.run, text capture). Against
        `ipc.py::_timeout_for`'s per-op budget: unlisted ops (this one is)
        fall to the 30s global runaway-guard default
        (`_resolve_dispatch_timeout_secs`), so N=13 repos (this machine's
        live `repos.*` count, 2026-08-20) costs ~210-260ms of the 30s budget
        warm and idle — survivable in isolation, but that is the wrong
        number to size against: `docs/wiki/machine-load-norm.md`'s 50-70
        concurrent-LLM norm means this box routinely has dozens of
        `git`-spawning ops contending for the same process-spawn/disk path
        this measurement sampled cold-idle. Structurally, this is exactly
        the amplification class `coordinator_core/tests/
        test_no_unbatched_per_item_git_spawn.py` exists to catch — "one
        spawn per loop item, batchable into a single call" — except the loop
        here is over the registry's OWN member list, not caller-supplied
        data, so every future `repos.*` registration makes every future
        write more expensive, unconditionally, with no cap. Rejected on that
        growth property, not on the per-call number alone.
    (b) a registry field naming each member's slug, read via the existing
        flat `registry_get` reader (in-process TOML read, zero subprocess
        spawns) — cheap and O(1), but a second source of truth for something
        `git` already knows, per the handoff's own framing.

CHOSEN: (b), because (a)'s cost is not merely "slower" but *unbounded and
per-write-recurring* against a value (the registry's own membership) that
does not change per write — the antithesis of what a 30s-budgeted,
zero-caller-supplied-cost op should pay on every invocation, and the exact
shape the amplification gate treats as a defect elsewhere in this codebase.

Kept honest against `origin` (the second-source-of-truth risk (b) admits
to): the index is OPERATOR-SET-ONLY, the same operational step and the same
trust boundary as `repos.<key>` itself (`machine-local set repos.<key>
<path>` is equally un-verified against `origin` today, and this module has
never treated that as a defect requiring a live check). This module does
NOT add a per-write cross-check against `origin` — that would reintroduce
exactly the per-write git spawn (a) was rejected for. Staying honest is an
install-time/doctor-cadence concern (e.g. a future `claude-klabauter-doctor-probe.py`
row comparing a registered slug against `git remote get-url origin` for each
`repos.*` member, run at doctor cadence, not per write) — out of this
chunk's scope (`writes:` is this file only) and explicitly not built here.

Index shape: `repo_slug.<owner>/<repo>` -> `<repos.* key name>`, e.g.
`repo_slug.acme/widgets = "project_widgets"` naming `repos.project_widgets`.
This is the SAME role-key-indirection shape `tracker.holder_repo` uses above
(a registry value that names another registry key, never a path) — the slug
is not itself a `repos.*` member; it is a second hop to the key name that
is. `_resolve_repos_key_for_slug` performs the hop and raises, naming the
slug and the `machine-local set repo_slug.<slug> <repo-key>` remediation, if
the index entry itself is absent — distinct from `write_root_for`'s existing
key-unset (rung 3) and path-absent (rung 4) messages for the `repos.<key>`
hop itself, per AC9.

`""` is explicitly rejected (AC7) before it ever reaches slug resolution:
Cockpit's legacy encoding used `""` to mean "no owner", which this module
must never read as a slug — an empty index lookup (`repo_slug.`) would
otherwise raise a confusing `repos.` message diagnosing the wrong hop. A
caller translating a legacy `""` owner must pass `owning_repo=None` instead.

Negative-spec:
    - Do NOT add a fallback rung (registry-derived-seed, env-raw-path, or
      any other default-holder guess). Tiers 2 and 3 are deliberately empty
      — see § Resolution precedence and § The fail-loud fork above.
    - Do NOT return `""` or `None` on any resolution path, from any function
      in this module, ever. Every failure mode raises. A falsy return is the
      brightline violation arriving by omission (§ The fail-loud fork).
    - Do NOT import this module from `coordinator_core/tracker_store.py`.
      `tracker_store` is a library with no holder-awareness (DEC-11/DEC-12);
      wiring holder-resolution into it would widen its API in exactly the
      way its own negative-spec forbids. Callers that need both compose them
      at the call site, not inside `tracker_store`.
    - Do NOT auto-seed `tracker.holder_repo`. It is OPERATOR-SET-ONLY — see
      above. No function in this module may write a default value to the
      registry on the caller's behalf.
    - Do NOT add a bespoke env var for this resolution (e.g. a raw-path
      `COORDINATOR_TRACKER_HOLDER_ROOT`). `registry_get`'s built-in
      `MACHINE_LOCAL_TRACKER_HOLDER_REPO` override already covers the env
      escape hatch with the repo-key indirection intact — see above.
    - Do NOT add a fleet-wide aggregating read here, and do NOT let this
      module read or write across repo boundaries beyond resolving the one
      holder path. This module answers "which repo is the holder", nothing
      about the holder's contents (DEC-12).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.machine_resolver import registry_get

_ROLE_KEY = "tracker.holder_repo"

#: The claude-klabauter SOURCE TREE key. Deliberately NOT the engine root: this module's
#: brightline compares a `repos.*` holder clone path against claude-klabauter's own clone
#: path, which is a path-identity test between two repo working trees. The
#: engine root answers a different question ("which engine executes") and can
#: legitimately be the published mirror, so routing this comparison at it makes
#: the guard compare a holder clone against a build output and pass when it
#: should refuse. See docs/decisions/DR-326 for the dispatch/locator axis split.
_CLAUDE_KLABAUTER_SOURCE_TREE_KEY = "repos.claude_klabauter"


def _claude_klabauter_source_tree() -> Path:
    """Resolve the claude-klabauter SOURCE TREE (the checkout), never the engine root.

    Reads the registry directly and honours NO env rung. That omission is the
    point, not an oversight: the engine-root ladder's first rung returns the
    engine-root environment variable, which under a warm-served invocation
    carries the PUBLISHED MIRROR's path rather than this repo's checkout. A
    brightline that consulted it would compare a holder clone against the
    mirror, find them unequal, and fail OPEN — the refusal DR-241 requires
    would silently never fire.

    Consistent with this module's standing "No bespoke env rung" stance (see
    module docstring), which is why the read goes through `registry_get`.
    """
    value = registry_get(_CLAUDE_KLABAUTER_SOURCE_TREE_KEY)
    if not value:
        raise RuntimeError(
            "cannot check the holder brightline: the machine-local registry "
            f"has no '{_CLAUDE_KLABAUTER_SOURCE_TREE_KEY}' entry, so claude-klabauter's own source "
            "tree is unknown and a holder that IS claude-klabauter cannot be refused. "
            "Remediation: machine-local set "
            f"{_CLAUDE_KLABAUTER_SOURCE_TREE_KEY} /path/to/claude-klabauter"
        )
    return Path(value)

# Slug -> repos.* key indirection (C5). See module docstring § Slug ->
# repos.* key resolution for the measured fork this settles and why a
# per-write git spawn was rejected. Operator-set-only, same as `repos.*`
# itself; never auto-seeded by this module.
_SLUG_INDEX_PREFIX = "repo_slug."


def _resolve_repos_key_for_slug(slug: str) -> str:
    """Resolve an owner-qualified `"<owner>/<repo>"` slug to its `repos.*`
    key NAME (not a path) via the `repo_slug.<slug>` registry index.

    Raises a distinct, operator-actionable RuntimeError if the index entry
    itself is unset — distinct from `write_root_for`'s own key-unset (rung 3)
    and path-absent (rung 4) messages for the subsequent `repos.<key>` hop
    (AC9: absent ownership and broken ownership are different facts and must
    not share a message).

    Never returns a sentinel, never returns None or "" — raises instead.
    """
    index_key = f"{_SLUG_INDEX_PREFIX}{slug}"
    repo_key = registry_get(index_key)
    if not repo_key:
        raise RuntimeError(
            f"owner-qualified slug '{slug}' has no registered '{index_key}' "
            "entry — this slug is not a known repos.* member. Remediation: "
            f"machine-local set {index_key} <repo-key>"
        )
    return repo_key


def holder_repo_root() -> Path:
    """Resolve the designated holder repo's root, raising on every failure rung.

    Two-hop resolution (see module docstring § Registry key): the role key
    `tracker.holder_repo` names a `repos.<key>` entry, which in turn resolves
    to a filesystem path. Never treats the role key's own value as a path.

    Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
    chunk C2 § The fail-loud fork. DR-241 § Per-repo, not fleet-wide (DEC-11):
    this resolver exists only to answer "where does a write whose producer
    named no owning repo land", never to relax DEC-11's per-repo bound.

    Failure rungs, each raising a distinct, operator-actionable RuntimeError:
        1. `tracker.holder_repo` unset/unresolvable -> raise naming the key
           and the remediation `machine-local set tracker.holder_repo <repo-key>`.
        2. the role key names a `repos.<key>` that is itself unset -> raise.
        3. the resolved path does not exist on disk -> raise a DISTINCT
           not-cloned error naming the clone step, never confusable with the
           not-configured message of rungs 1/2.

    Semantic brightline guard (AC12): additionally raises if the resolved
    holder root is claude-klabauter's own SOURCE TREE (`_claude_klabauter_source_tree()`, which
    honours no env rung) —
    closes the brightline mechanically, not lexically. The caller-root half
    of this guard (holder != caller's own repo_root) is `write_root_for()`'s
    (C3), not this function's — it needs a caller-supplied root this
    function does not have.

    Never returns a sentinel, never returns None or "" — every failure mode
    raises (see module docstring negative-spec).
    """
    holder_key = registry_get(_ROLE_KEY)
    if not holder_key:
        raise RuntimeError(
            f"registry key '{_ROLE_KEY}' is unset or unresolvable — the "
            "designated holder repo has not been configured. Remediation: "
            "machine-local set tracker.holder_repo <repo-key>"
        )

    repos_key = f"repos.{holder_key}"
    holder_path = registry_get(repos_key)
    if not holder_path:
        raise RuntimeError(
            f"registry key '{_ROLE_KEY}' names holder repo '{holder_key}', "
            f"but '{repos_key}' is unset or unresolvable — the holder role "
            "points at a repo that has not been registered. Remediation: "
            f"machine-local set {repos_key} <path-to-{holder_key}>"
        )

    resolved = Path(holder_path)
    if not resolved.exists():
        raise RuntimeError(
            f"designated holder repo '{holder_key}' resolves to "
            f"'{resolved}', but that path does not exist on disk — the "
            "holder repo has been configured but not cloned. Remediation: "
            f"clone {holder_key} to {resolved}"
        )

    claude_klabauter_root = _claude_klabauter_source_tree()
    if resolved.resolve() == claude_klabauter_root.resolve():
        raise RuntimeError(
            f"designated holder repo '{holder_key}' resolves to "
            f"'{resolved}', which is claude-klabauter's own root — the holder repo "
            "must never be claude-klabauter itself (brightline guard). Remediation: "
            f"point {repos_key} at a different repo, not claude-klabauter"
        )

    return resolved


def write_root_for(*, owning_repo: str | None, repo_root: Path) -> Path:
    """Resolve the write-target root for a sovereign-tracker write.

    `owning_repo` is KEYWORD-REQUIRED with no default — omission is a
    TypeError at the call site, not a decision this function makes on the
    caller's behalf (see module docstring § The fail-loud fork). Three-valued
    (AC7): an owner-qualified `"<owner>/<repo>"` slug | `None` | absent (=
    `TypeError`). `""` is REJECTED explicitly, never treated as a slug.

    Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
    chunk C3 § The match predicate, § Anti-scope; reworked C5 § Slug ->
    repos.* key resolution (module docstring) for the wire-shape change.
    DR-241 § Per-repo, not fleet-wide (DEC-11): a stated owning repo writes
    to its own tracker, never fleet-wide. DR-214's admission bound: the
    producer supplies an owner-qualified slug, never a path — resolved
    through `repo_slug.<slug>` -> `repos.<key>` -> path, the same
    `repos.<key>` family `holder_repo_root()` uses for its own hop, one
    resolution mechanism family, not a bespoke second one.

    Five arms:
        1. `owning_repo` is `""` -> raise explicitly (AC7); never falls into
           arm 2's slug resolution and never produces the confusing
           `repos.` message an unresolved-empty-string lookup would give.
        2. `owning_repo` is a slug resolving through `repo_slug.<slug>` ->
           `repos.<key>` that resolves -> that repo's root.
        3. `owning_repo` is explicitly `None` -> `holder_repo_root()`.
        4. `owning_repo`'s resolved `repos.<key>` is UNSET in the registry ->
           raise, naming the key and the `machine-local set` remediation.
        5. `owning_repo` resolves to a `repos.<key>` whose path is ABSENT on
           disk -> raise, a DISTINCT message naming the clone step, mirroring
           `holder_repo_root()`'s rung-3 wording.

    (A slug with no `repo_slug.<slug>` index entry at all is a further,
    earlier failure inside `_resolve_repos_key_for_slug` — see that
    function's own docstring — distinct again from arms 4/5.)

    Arms 4 and 5 are two distinct failure families with distinct messages —
    "key unset in the registry" and "path absent on disk" are different
    operator remediations — and NEITHER ever falls through to the holder
    (AC9). Absent ownership and BROKEN ownership are different facts;
    collapsing them would turn a producer's typo into a silent relocation
    into the holder.

    No owner value is ever used as a path component; every owner resolves
    through `repo_slug.<slug>` then `repos.<key>` or raises. A raw filesystem
    path as `owning_repo` is never accepted — that would bypass the registry
    entirely, the exact wire-derived targeting DR-214's ratified bound
    forbids. A non-member slug (no `repo_slug.<slug>` entry) is refused
    (AC8), never silently routed to the holder.

    Semantic brightline guard (AC12, this function's half): on the arm-3
    (`owning_repo is None`) path ONLY, additionally raises if the resolved
    HOLDER root equals the caller-supplied `repo_root`. `holder_repo_root()`
    already guards holder-root != claude-klabauter-live-root; this guard needs the caller's
    root and so lives here instead.

    Deliberately NOT applied to arm 2: a producer that STATES an owning repo
    slug which happens to resolve to the caller's own is the ordinary
    matched case DEC-11 describes, not a violation. Guarding every arm would
    make the commonest legitimate write — a repo recording its own work —
    raise. The brightline is an UNOWNED write landing in whatever repo the
    engine happens to be running in; only the holder arm can do that.

    NO INFERENCE: this function reads what the producer supplied and
    nothing else — never the payload's content, never a relationship scan,
    never a cross-repo read to work out where something "belongs" (see
    module docstring negative-spec).

    Never returns a sentinel, never returns None or "" — every failure mode
    raises.
    """
    if owning_repo is None:
        resolved = holder_repo_root()
        if resolved.resolve() == repo_root.resolve():
            raise RuntimeError(
                f"designated holder repo resolves to '{resolved}', which is "
                "the caller's own repo_root — an unowned write would land in "
                "the very repo that failed to claim it, which is the "
                "brightline this resolver exists to close. Remediation: "
                "point tracker.holder_repo at a repo other than the caller"
            )
        return resolved

    elif owning_repo == "":
        raise RuntimeError(
            "owning_repo is the empty string — this is cockpit's legacy "
            "'no owner' encoding, not a valid owner-qualified slug, and is "
            "never resolved as one. Remediation: pass owning_repo=None for "
            "an unowned write, translating any legacy '' owner at the "
            "caller before invoking write_root_for."
        )

    else:
        repo_key = _resolve_repos_key_for_slug(owning_repo)
        repos_key = f"repos.{repo_key}"
        owner_path = registry_get(repos_key)
        if not owner_path:
            raise RuntimeError(
                f"owning repo slug '{owning_repo}' resolves to registry key "
                f"'{repo_key}' ('{repos_key}'), but that key is unset or "
                "unresolvable — the owning repo has not been registered. "
                f"Remediation: machine-local set {repos_key} <path-to-{repo_key}>"
            )

        resolved = Path(owner_path)
        if not resolved.exists():
            raise RuntimeError(
                f"owning repo slug '{owning_repo}' resolves to '{resolved}' "
                f"(via repos.{repo_key}), but that path does not exist on "
                "disk — the owning repo has been configured but not "
                f"cloned. Remediation: clone {repo_key} to {resolved}"
            )

    # No caller-root guard on this arm, deliberately: a producer that STATES
    # an owning repo which happens to be the caller's own is the ordinary
    # matched case DEC-11 describes — writes land in the consuming repo's own
    # state/sovereign-tracker/. The brightline being guarded is an UNOWNED
    # write silently landing in whatever repo the engine happens to run in,
    # which is the `owning_repo is None` arm above, not this one.
    return resolved
