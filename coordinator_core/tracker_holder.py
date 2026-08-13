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
Coordinator-claude `coordinator/docs/wiki/machine-local-registry.md` § 5c.4
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
(`coordinator_core.claude_klabauter_root`), raising if it is. `write_root_for()` MUST
additionally assert the resolved holder root is not equal to the
caller-supplied `repo_root`, raising if it is — this second check needs the
caller's root and so cannot live in `holder_repo_root()` alone. Together the
two checks close the brightline mechanically (a real path comparison), not
lexically (a grep for a literal), against a caller that is, or becomes, the
holder itself.

Public API: `holder_repo_root() -> Path` (C2, landed) and
`write_root_for(*, owning_repo, repo_root) -> Path` (C3, landed) — see each
function's own docstring for the exact rungs and exceptions raised.

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
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

_ROLE_KEY = "tracker.holder_repo"


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
    holder root is claude-klabauter's own root (`coordinator_core.claude_klabauter_root`) —
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

    claude_klabauter_root = Path(coordinator_claude_klabauter_root())
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
    caller's behalf (see module docstring § The fail-loud fork).

    Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
    chunk C3 § The match predicate, § Anti-scope. DR-241 § Per-repo, not
    fleet-wide (DEC-11): a stated owning repo writes to its own tracker,
    never fleet-wide. DR-214's admission bound: the producer supplies a
    registry KEY NAME, never a path — resolved through the same `repos.<key>`
    family `holder_repo_root()` uses, one resolution mechanism, not two.

    Four arms:
        1. `owning_repo` names a `repos.<key>` that resolves -> that repo's
           root.
        2. `owning_repo` is explicitly `None` -> `holder_repo_root()`.
        3. `owning_repo` names a `repos.<key>` UNSET in the registry -> raise,
           naming the key and the `machine-local set` remediation.
        4. `owning_repo` resolves to a `repos.<key>` whose path is ABSENT on
           disk -> raise, a DISTINCT message naming the clone step, mirroring
           `holder_repo_root()`'s rung-3 wording.

    Arms 3 and 4 are two distinct failure families with distinct messages —
    "key unset in the registry" and "path absent on disk" are different
    operator remediations — and NEITHER ever falls through to the holder.
    Absent ownership and BROKEN ownership are different facts; collapsing
    them would turn a producer's typo into a silent relocation into the
    holder.

    No owner value is ever used as a path component; every owner resolves
    through `repos.<key>` or raises. A raw filesystem path as `owning_repo`
    is never accepted — that would bypass the registry entirely, the exact
    wire-derived targeting DR-214's ratified bound forbids.

    Semantic brightline guard (AC12, this function's half): on the arm-2
    (`owning_repo is None`) path ONLY, additionally raises if the resolved
    HOLDER root equals the caller-supplied `repo_root`. `holder_repo_root()`
    already guards holder-root != claude-klabauter-root; this guard needs the caller's
    root and so lives here instead.

    Deliberately NOT applied to arm 1: a producer that STATES an owning repo
    which happens to be the caller's own is the ordinary matched case DEC-11
    describes, not a violation. Guarding every arm would make the commonest
    legitimate write — a repo recording its own work — raise. The brightline
    is an UNOWNED write landing in whatever repo the engine happens to be
    running in; only the holder arm can do that.

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

    else:
        repos_key = f"repos.{owning_repo}"
        owner_path = registry_get(repos_key)
        if not owner_path:
            raise RuntimeError(
                f"owning repo key '{owning_repo}' names registry entry "
                f"'{repos_key}', but that key is unset or unresolvable — "
                "the owning repo has not been registered. Remediation: "
                f"machine-local set {repos_key} <path-to-{owning_repo}>"
            )

        resolved = Path(owner_path)
        if not resolved.exists():
            raise RuntimeError(
                f"owning repo '{owning_repo}' resolves to '{resolved}', but "
                "that path does not exist on disk — the owning repo has "
                "been configured but not cloned. Remediation: "
                f"clone {owning_repo} to {resolved}"
            )

    # No caller-root guard on this arm, deliberately: a producer that STATES
    # an owning repo which happens to be the caller's own is the ordinary
    # matched case DEC-11 describes — writes land in the consuming repo's own
    # state/sovereign-tracker/. The brightline being guarded is an UNOWNED
    # write silently landing in whatever repo the engine happens to run in,
    # which is the `owning_repo is None` arm above, not this one.
    return resolved
