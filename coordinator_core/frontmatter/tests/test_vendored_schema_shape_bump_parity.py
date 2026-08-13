"""A vendored schema must not change VALIDATION SHAPE under an unmoved
`x-schema-version` -- the intra-repo, historical gate.

Spec backlink: docs/plans/2026-08-10-a-vendored-schema-cannot-change-shape-un.md
(AC3-AC7; chunk C2). AC1/AC2 unit coverage of the hash primitive itself lives
in `TestSemanticShapeHash` below, because the plan's `scope:` is exactly two
files.

NEGATIVE SPEC -- why this is not any of the three gates that already exist.
All three read GREEN at the instant of the bad edit; that is precisely why the
defect has recurred, and why this test must not later be "simplified" into one
of them:

  - `schema_drift_watch.scan_vendored_schema_drift` compares claude-klabauter's copy to
    example-doctrine-repo's. At the instant of a local shape edit the two files still agree on
    the version, and the shape change has not reached example-doctrine-repo's side either, so
    there is nothing for it to report.
  - `schema_validate.check_schema_drift` is a byte-for-byte TAMPER check
    against a pinned example-doctrine-repo ref. Its own docstring says it is expected to be
    always-green and is explicitly not a staleness check.
  - `schema_validate`'s consumer-ahead gate asserts claude-klabauter's
    `x-schema-version` is strictly greater than example-doctrine-repo's at `doe_ref`. It
    constrains the relationship between two REPOS, never the relationship
    between a file and its own history.

The axis nothing else covers: this file versus its own history.

TWO legs, both required -- neither subsumes the other:

  1. PRE-COMMIT (`TestVendoredCorpusShapeVersusOwnLastCommit`): worktree
     against HEAD. Catches a bad edit while it is still uncommitted.
  2. POST-COMMIT (`TestVendoredCorpusShapeAcrossLastCommittedTransition`):
     each schema's blob at HEAD against its blob at the previous commit that
     touched that file. Leg 1 alone goes GREEN the instant the bad edit is
     committed, and both motivating cases (`8bbe56808`, `da69b8727`) WERE
     commits -- so a cadence-gate run, which is when this suite actually runs,
     would have been green for both. A future reader must not delete leg 2 as
     redundant with leg 1; that is the exact hole this gate was built to close.

Git reads go through `ops.ceremony.git_native` -- `cat_file_batch` (the repo's
existing `git cat-file --batch` seam, one spawn per ref for the whole path
set) and `_git` (that module's single subprocess choke point, carrying the
Windows-safe flag set) for the one history enumeration. This module adds no
`subprocess.run(["git", ...])` site of its own (AC6), and spawns one `git log`
for the whole schemas directory rather than one per file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.frontmatter.schema_drift_watch import vendored_schema_paths
from coordinator_core.frontmatter.schema_shape import semantic_shape_hash
from coordinator_core.ops.ceremony.git_native import _git, cat_file_batch, cat_file_batch_objects

# Declared, not excused: every leg here reads real git history (`git log`,
# `git cat-file --batch`) because the whole property under test is "what did
# this file's shape do across its own commits", which no fixture can stand in
# for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and
# is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_REL_DIR = "coordinator_core/frontmatter/schemas"
PERCOLATE_STORE_REL = f"{SCHEMAS_REL_DIR}/percolate-store.schema.json"

#: The commits that motivated this gate, each a shape edit to
#: `percolate-store.schema.json` landed under an unmoved `x-schema-version`.
#: A gate that cannot redden on the two commits that motivated it has not been
#: demonstrated to work (AC4), so the replay is pinned and committed (AC5)
#: rather than run once at a console.
#:
#: DO NOT re-add `7bc1500d2` here. It appears in this schema's own
#: `x-bump-note` and in this plan's prose, but it is a example-doctrine-repo-CLAUDE sha
#: (`percolate: label the percolation-only doctrine copies, and stop shipping a
#: false delivery claim`) authored against example-doctrine-repo's history; it does not resolve
#: in this repository at all. Claude-klabauter's counterpart -- the commit that made the
#: same `sentinel-strip` transform-kind edit here -- is `8bbe56808`, whose
#: version bump did not arrive until the separate later commit `914cf8c11`.
#: The skip-with-reason machinery below stays regardless: a pin that stops
#: resolving (shallow clone, pruned history) must skip loudly, never pass.
REPLAY_PINS = (
    ("da69b8727", PERCOLATE_STORE_REL, "guardEntry.properties.kind enum widened"),
    ("8bbe56808", PERCOLATE_STORE_REL, "hooks transform-kind sentinel-strip"),
)


def _shape_and_version(blob_text: str) -> tuple[str, object]:
    schema = json.loads(blob_text)
    return semantic_shape_hash(schema), schema.get("x-schema-version")


def shape_moved_under_unmoved_version(
    label: str, before_text: str, after_text: str
) -> Optional[str]:
    """The comparator, shared by the corpus check and the replay: returns a
    loud, self-contained violation message naming the schema, both hashes and
    both versions -- or None when there is no violation.

    A violation is exactly: the semantic shape differs AND `x-schema-version`
    does not. The reverse direction (version moved, shape did not) is
    legitimate -- prose-only republication -- and is deliberately not reported.
    """
    before_hash, before_version = _shape_and_version(before_text)
    after_hash, after_version = _shape_and_version(after_text)
    if before_hash == after_hash:
        return None
    if before_version != after_version:
        return None
    return (
        f"{label}: validation shape changed but x-schema-version did not.\n"
        f"  x-schema-version (both sides): {after_version!r}\n"
        f"  shape hash before: {before_hash}\n"
        f"  shape hash after:  {after_hash}\n"
        "  A shape change under an unmoved version makes every consumer that "
        "trusts the version string to mean 'same shape' silently wrong. Move "
        "x-schema-version (major vs minor is your call -- this gate "
        "deliberately does not stamp it), or revert the shape change."
    )


class TestSemanticShapeHash:
    """AC1/AC2: the hash ignores annotations and key order, and moves on every
    kind of shape change the gate must catch."""

    BASE = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "x-schema-version": "1.0.0",
        "type": "object",
        "required": ["alpha"],
        "properties": {
            "alpha": {"type": "string", "enum": ["a", "b"]},
            "beta": {"type": "integer"},
        },
    }

    def test_annotation_only_edits_hash_equal(self):
        annotated = {
            "type": "object",
            "description": "prose that changed",
            "$comment": "rationale that changed",
            "x-bump-class": "minor",
            "x-bump-note": "authoring note",
            "required": ["alpha"],
            "properties": {
                "alpha": {"type": "string", "enum": ["a", "b"], "description": "doc"},
                "beta": {"type": "integer", "$comment": "why"},
            },
            "x-schema-version": "9.9.9",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        }
        assert semantic_shape_hash(annotated) == semantic_shape_hash(self.BASE)

    def test_key_order_does_not_move_the_hash(self):
        reordered = dict(reversed(list(self.BASE.items())))
        assert semantic_shape_hash(reordered) == semantic_shape_hash(self.BASE)

    def test_widened_enum_moves_the_hash(self):
        widened = json.loads(json.dumps(self.BASE))
        widened["properties"]["alpha"]["enum"].append("c")
        assert semantic_shape_hash(widened) != semantic_shape_hash(self.BASE)

    def test_added_required_entry_moves_the_hash(self):
        changed = json.loads(json.dumps(self.BASE))
        changed["required"].append("beta")
        assert semantic_shape_hash(changed) != semantic_shape_hash(self.BASE)

    def test_removed_required_entry_moves_the_hash(self):
        changed = json.loads(json.dumps(self.BASE))
        changed["required"] = []
        assert semantic_shape_hash(changed) != semantic_shape_hash(self.BASE)

    def test_added_properties_key_moves_the_hash(self):
        changed = json.loads(json.dumps(self.BASE))
        changed["properties"]["gamma"] = {"type": "boolean"}
        assert semantic_shape_hash(changed) != semantic_shape_hash(self.BASE)

    def test_removed_properties_key_moves_the_hash(self):
        changed = json.loads(json.dumps(self.BASE))
        del changed["properties"]["beta"]
        assert semantic_shape_hash(changed) != semantic_shape_hash(self.BASE)

    def test_changed_type_moves_the_hash(self):
        changed = json.loads(json.dumps(self.BASE))
        changed["properties"]["beta"]["type"] = "string"
        assert semantic_shape_hash(changed) != semantic_shape_hash(self.BASE)

    def test_property_literally_named_description_is_not_stripped(self):
        """The default-deny walk's load-bearing carve-out: a PROPERTY named
        `description` is a declaration, not an annotation -- removing it is a
        real shape change the gate must see."""
        with_prop = json.loads(json.dumps(self.BASE))
        with_prop["properties"]["description"] = {"type": "string"}
        assert semantic_shape_hash(with_prop) != semantic_shape_hash(self.BASE)

    def test_version_bump_alone_does_not_move_the_hash(self):
        bumped = json.loads(json.dumps(self.BASE))
        bumped["x-schema-version"] = "2.0.0"
        assert semantic_shape_hash(bumped) == semantic_shape_hash(self.BASE)


class TestVendoredCorpusShapeVersusOwnLastCommit:
    """AC3/AC7: every vendored schema on disk, against its own blob at HEAD."""

    def test_no_shape_change_under_unmoved_version(self):
        paths = vendored_schema_paths()
        assert paths, "no vendored schemas found -- the coverage set must not be vacuous"

        rel_paths = [f"{SCHEMAS_REL_DIR}/{p.name}" for p in paths]
        committed = cat_file_batch(REPO_ROOT, "HEAD", rel_paths)

        violations: list[str] = []
        compared = 0
        for path, rel in zip(paths, rel_paths):
            before = committed.get(rel)
            if before is None:
                # Not present at HEAD -- a newly added schema has no own
                # history to contradict. Never a violation, never silently
                # counted as compared.
                continue
            compared += 1
            message = shape_moved_under_unmoved_version(
                rel, before, path.read_text(encoding="utf-8")
            )
            if message:
                violations.append(message)

        assert compared > 0, (
            "no vendored schema resolved at HEAD via cat_file_batch -- the check "
            "would have passed vacuously"
        )
        assert not violations, "\n\n".join(violations)


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def touching_commits_by_schema() -> dict[str, list[str]]:
    """`{rel_path: [sha, ...]}`, newest first, for every file under the vendored
    schemas directory -- ONE `git log --name-only` spawn for the whole
    directory, never one per file (`coordinator_core/git/ls_files.py` states
    the norm this follows: a cold git spawn per file is Windows-hostile and
    break-class on a hot path).

    Returns `{}` when the enumeration fails; the caller turns that into a
    loud skip, never a vacuous pass.
    """
    result = _git(
        ["log", "--format=%H", "--name-only", "--", SCHEMAS_REL_DIR],
        cwd=REPO_ROOT,
        timeout=60,
    )
    if not result.ok:
        return {}
    touching: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _FULL_SHA_RE.match(line):
            current = line
        elif current is not None:
            touching.setdefault(line, []).append(current)
    return touching


class TestVendoredCorpusShapeAcrossLastCommittedTransition:
    """The POST-COMMIT leg: each vendored schema at HEAD against its blob at
    the previous commit that touched it.

    Why this exists as a second leg: the worktree-vs-HEAD leg goes green the
    moment a bad edit is committed, and both commits that motivated this plan
    were commits. See this module's docstring.

    Resolution rule for `before`: walk the touching-commit list backwards from
    HEAD and take the FIRST commit at which the blob actually resolves, rather
    than blindly taking the second entry. This branch's history contains an
    empty-tree damage commit (`0a3462b72`) and its recovery (`2b9e319aa`), so
    for 31 of the 35 vendored schemas the immediately-previous touching commit
    is one at which the file does not exist at all. Taking that entry verbatim
    would silently degrade almost the whole corpus to "unresolvable" -- a
    vacuous pass wearing the shape of a check. Stepping back to the last
    commit where the file EXISTS compares the two real versions across the
    damage, which is the comparison the gate is actually for. This is a
    resolution rule, never an allowlist: no schema is exempted, and the
    comparator is not weakened.
    """

    def test_no_shape_change_under_unmoved_version(self):
        """The two correction forms this leg must tell apart, since a
        transition failing `shape_moved_under_unmoved_version` is not
        automatically live debt:

          - VERSION-MOVED: not reachable from THIS leg at all (it only ever
            examines the single most-recent committed transition, whose
            `after` side is HEAD's own version by construction) -- the
            version-move correction form is a full-history concept, handled
            by `TestFullHistoryShapeBumpSweep`.
          - SHAPE-REVERTED: the transition under examination (`before` ->
            `head_sha`) fails the comparator, but a commit further back than
            `before` already carried the exact shape HEAD carries today. The
            motivating case is a same-day re-vendor-then-revert pair on
            `handoff.schema.json` (`1825e7771` -> `4e0d47e88` widened shape
            under unmoved 7.1.0; `4e0d47e88` -> `e6af1c6cf` backed it out
            byte-for-byte). This leg's single-hop pairing lands on the SECOND
            half of that pair (`4e0d47e88` -> `e6af1c6cf`), whose own
            `before` (`4e0d47e88`) still carries the bad shape -- so a naive
            "does `before` match HEAD" check would still redden here even
            though HEAD's shape is provably fine (`1825e7771`, two commits
            back, matches it exactly). The walk below keeps searching PAST
            `before`, through the remaining touching commits, for any state
            that already carries HEAD's shape; finding one proves the
            version's meaning at HEAD is the SAME meaning it has always had,
            not a novel corruption this leg should redden on. This is why
            "reverted" cannot be tested as a single before/after hash
            equality -- it is a reachability question over the whole
            touching-commit list, not a one-hop comparison.
        """
        paths = vendored_schema_paths()
        assert paths, "no vendored schemas found -- the coverage set must not be vacuous"

        touching = touching_commits_by_schema()
        if not touching:
            pytest.skip(
                "`git log --name-only` over "
                f"{SCHEMAS_REL_DIR} produced no history (shallow clone, or not a "
                "git repo) -- the post-commit leg was NOT exercised. This is a "
                "SKIP, not a pass."
            )

        violations: list[str] = []
        reverted: list[str] = []
        unpaired: list[str] = []
        compared = 0
        for path in paths:
            rel = f"{SCHEMAS_REL_DIR}/{path.name}"
            shas = touching.get(rel, [])
            if not shas:
                unpaired.append(f"{rel}: no commit in history touches it")
                continue
            head_sha = shas[0]
            after = cat_file_batch(REPO_ROOT, head_sha, [rel]).get(rel)
            before = before_sha = None
            before_index = None
            for idx, candidate in enumerate(shas[1:], start=1):
                resolved = cat_file_batch(REPO_ROOT, candidate, [rel]).get(rel)
                if resolved is not None:
                    before_sha, before, before_index = candidate, resolved, idx
                    break
            # Guard on `before_sha` (not on `before`) so the pair's
            # both-or-neither invariant is explicit to a reader and to a type
            # checker, rather than something either has to re-derive from the
            # loop above: the two are assigned together or not at all.
            if after is None or before is None or before_sha is None:
                unpaired.append(
                    f"{rel}: only one commit-state resolves (newest touching commit "
                    f"{head_sha[:9]}) -- no prior version to compare against"
                )
                continue
            compared += 1
            message = shape_moved_under_unmoved_version(
                f"{rel} ({before_sha[:9]} -> {head_sha[:9]})", before, after
            )
            if not message:
                continue
            after_hash = _shape_and_version(after)[0]
            # Batched (one `cat-file --batch` feed for every remaining
            # touching commit of THIS schema), not a spawn per candidate --
            # the module docstring's spawn-ratchet constraint applies to this
            # lookahead exactly as it does to the primary pairing above.
            older_specs = [f"{sha}:{rel}" for sha in shas[before_index + 1 :]]
            older_blobs = cat_file_batch_objects(REPO_ROOT, older_specs) if older_specs else {}
            matched_ancestor = next(
                (
                    sha
                    for sha in shas[before_index + 1 :]
                    if (blob := older_blobs.get(f"{sha}:{rel}")) is not None
                    and _shape_and_version(blob)[0] == after_hash
                ),
                None,
            )
            if matched_ancestor is not None:
                reverted.append(
                    f"{rel}: {before_sha[:9]} -> {head_sha[:9]} reverted since "
                    f"(shape at HEAD matches earlier commit {matched_ancestor[:9]})"
                )
            else:
                violations.append(message)

        assert compared > 0, (
            "no vendored schema had a resolvable commit pair -- the post-commit "
            f"leg would have passed vacuously. Unpaired: {unpaired}"
        )
        if reverted:
            print(
                f"\n[informational] {len(reverted)} shape-moved/version-unmoved "
                "transition(s) already reverted -- history, not debt:\n  "
                + "\n  ".join(reverted)
            )
        assert not violations, (
            f"{len(violations)} vendored schema(s) changed validation shape under an "
            f"unmoved x-schema-version in their most recent committed transition "
            f"({compared} schema(s) compared, {len(unpaired)} unpaired):\n\n"
            + "\n\n".join(violations)
        )


def resolved_history_by_schema() -> dict[str, list[tuple[str, str]]]:
    """`{rel_path: [(sha, blob_text), ...]}` OLDEST first, for every vendored
    schema -- every commit that touched it at which the blob actually
    resolves.

    ONE `git log` spawn (via `touching_commits_by_schema`) plus ONE
    `git cat-file --batch` spawn for every (commit, path) pair in the whole
    corpus. Never a spawn per commit: this walks ~90 commits x ~37 schemas,
    and a cold Windows `git` process in that loop is the break-class shape
    CLAUDE.md's Runtime conventions names.

    The backwalk rule is applied by construction, not as a special case:
    a state whose blob does not resolve is DROPPED, so `0a3462b72` (the
    empty-tree damage commit, which sits mid-chain for 31 of the 37 schemas
    and at which the files do not exist) never becomes either side of a
    transition. A pair where one side is the empty tree is not a transition.
    """
    touching = touching_commits_by_schema()
    if not touching:
        return {}
    rel_paths = [f"{SCHEMAS_REL_DIR}/{p.name}" for p in vendored_schema_paths()]
    specs: list[str] = []
    for rel in rel_paths:
        specs.extend(f"{sha}:{rel}" for sha in touching.get(rel, []))
    resolved = cat_file_batch_objects(REPO_ROOT, specs)

    history: dict[str, list[tuple[str, str]]] = {}
    for rel in rel_paths:
        states = [
            (sha, blob)
            for sha in reversed(touching.get(rel, []))
            for blob in (resolved.get(f"{sha}:{rel}"),)
            if blob is not None
        ]
        if states:
            history[rel] = states
    return history


@pytest.mark.cadence
class TestFullHistoryShapeBumpSweep:
    """The FULL-HISTORY sweep: every shape-moved/version-unmoved transition in
    every vendored schema's history, not just the most recent one.

    Why leg 2 is not enough (and why it still stays -- this does not replace
    it, it is the thorough counterpart to leg 2's cheap always-on check):
    leg 2 compares HEAD against the previous commit that touched the file. A
    widen at commit N followed by an unrelated prose-only edit at N+1 leaves
    leg 2 comparing N+1 against N, seeing no shape movement, and reporting
    green -- while the debt from N is live at HEAD and invisible to the whole
    suite.

    Two classes, and they are NOT the same finding:

      - Class A, CORRECTED-BY-VERSION-MOVE: shape moved under an unmoved
        version at N, but `x-schema-version` did move at some later commit
        before HEAD. Real history, since made consistent. Reported
        informationally, NEVER failed. A permanent red over history nobody
        can change trains readers to ignore the gate, which is worse than not
        having the gate.
      - Class C, CORRECTED-BY-SHAPE-REVERSION: shape moved under an unmoved
        version at N, the version STILL has not moved by HEAD, but the shape
        HEAD carries today already existed at some state at or before this
        transition's `before` side -- i.e. the change was walked back rather
        than bumped past. Also reported informationally, NEVER failed, for
        the same "permanent red trains readers to ignore it" reason as Class
        A: a consumer reading this version TODAY gets the same shape that
        version has always meant, so there is no live inconsistency, even
        though the version string itself never moved. The motivating case is
        `handoff.schema.json` at 7.1.0: `1825e7771 -> 4e0d47e88` widened
        shape (a re-vendor that imported example-doctrine-repo's unbumped shape move), and
        `4e0d47e88 -> e6af1c6cf` backed it out byte-for-byte. BOTH
        transitions in that pair are Class C, not just the first -- the
        second transition's own `before` (`4e0d47e88`) still carries the bad
        shape, so discharging it requires looking PAST `before`, to
        `1825e7771`, which is why this checks the whole prefix of states up
        to and including `before` rather than a single hash equality. A
        reader tempted to "simplify" this to `before_hash == head_hash` will
        silently re-redden the second half of every revert pair.
      - Class B, LIVE: shape moved under an unmoved version at N, and neither
        correction form above applies. The debt is live and no other leg can
        see it. RED.

    The discriminator: compare the version at the transition's later side
    against the version at HEAD (different -> A); if same, search the
    schema's own state prefix up to and including this transition's `before`
    for any state whose shape hash equals HEAD's shape hash (found -> C,
    otherwise -> B).
    """

    def test_no_live_uncorrected_shape_debt(self):
        history = resolved_history_by_schema()
        if not history:
            pytest.skip(
                "no resolvable schema history (shallow clone, or not a git repo) -- "
                "the full-history sweep was NOT exercised. This is a SKIP, not a pass."
            )

        class_a: list[str] = []
        class_b: list[str] = []
        class_c: list[str] = []
        transitions_examined = 0
        for rel, states in sorted(history.items()):
            head_hash, head_version = _shape_and_version(states[-1][1])
            for idx in range(len(states) - 1):
                before_sha, before = states[idx]
                after_sha, after = states[idx + 1]
                transitions_examined += 1
                message = shape_moved_under_unmoved_version(
                    f"{rel} ({before_sha[:9]} -> {after_sha[:9]})", before, after
                )
                if message is None:
                    continue
                transition_version = _shape_and_version(after)[1]
                if transition_version != head_version:
                    class_a.append(
                        f"{rel}: {before_sha[:9]} -> {after_sha[:9]} moved shape under "
                        f"{transition_version!r}; corrected since (version at HEAD is "
                        f"{head_version!r})"
                    )
                    continue
                reverted_at = next(
                    (
                        sha
                        for sha, blob in states[: idx + 1]
                        if _shape_and_version(blob)[0] == head_hash
                    ),
                    None,
                )
                if reverted_at is not None:
                    class_c.append(
                        f"{rel}: {before_sha[:9]} -> {after_sha[:9]} moved shape under "
                        f"{transition_version!r}; reverted since (shape at HEAD matches "
                        f"earlier state {reverted_at[:9]})"
                    )
                else:
                    class_b.append(
                        message
                        + f"\n  STILL UNCORRECTED at HEAD: x-schema-version is "
                        f"{head_version!r} there too."
                    )

        assert transitions_examined > 0, (
            "no commit-to-commit transition was examined -- the sweep would have "
            "passed vacuously"
        )
        if class_a:
            print(
                f"\n[informational] {len(class_a)} shape-moved/version-unmoved "
                "transition(s) already corrected by a later bump -- history, not debt:\n  "
                + "\n  ".join(class_a)
            )
        if class_c:
            print(
                f"\n[informational] {len(class_c)} shape-moved/version-unmoved "
                "transition(s) already corrected by a later shape reversion -- history, "
                "not debt:\n  " + "\n  ".join(class_c)
            )
        assert not class_b, (
            f"{len(class_b)} LIVE shape-change-under-unmoved-version debt(s) across "
            f"{transitions_examined} transition(s) examined. Each moved a schema's "
            f"validation shape at a commit whose version is still the version at HEAD, "
            f"so every consumer reading that version is silently wrong today:\n\n"
            + "\n\n".join(class_b)
        )


class TestMotivatingCommitsReplay:
    """AC4/AC5: the comparator, replayed over the pre/post blobs of the
    commits that motivated it. Each pin reddens or skips with a named reason;
    none may pass silently."""

    @pytest.mark.parametrize("sha,rel_path,what", REPLAY_PINS, ids=[p[0] for p in REPLAY_PINS])
    def test_comparator_reddens_on_motivating_commit(self, sha, rel_path, what):
        before = cat_file_batch(REPO_ROOT, f"{sha}~1", [rel_path]).get(rel_path)
        after = cat_file_batch(REPO_ROOT, sha, [rel_path]).get(rel_path)
        if before is None or after is None:
            pytest.skip(
                f"replay pin {sha} ({what}) is unreachable in this clone: "
                f"{rel_path} did not resolve at {sha}~1 and/or {sha}. This is a "
                "SKIP, not a pass -- the comparator was not exercised on this pair."
            )
        message = shape_moved_under_unmoved_version(f"{rel_path}@{sha}", before, after)
        assert message is not None, (
            f"replay of {sha} ({what}) did NOT redden. That commit changed this "
            "schema's validation shape without moving x-schema-version, so the "
            "comparator failing to flag it means the gate does not work."
        )
        assert sha in message and "x-schema-version" in message

    def test_annotation_only_revendor_is_not_a_violation(self):
        """The other polarity, and the anchor proving the annotation strip has
        no hole: `60f27f2a6a15` (the 1.2.0 re-vendor) moved `x-schema-version`,
        `x-bump-class`, `x-bump-note` and two `description` strings and nothing
        else. Shape must read UNCHANGED across it. A red here means the hash is
        picking up prose, which would make the gate demand a bump for doc-only
        syncs -- and on a schema example-doctrine-repo holds byte-identically, that costs a
        re-vendor round trip for a typo fix."""
        sha = "60f27f2a6a15"
        before = cat_file_batch(REPO_ROOT, f"{sha}~1", [PERCOLATE_STORE_REL]).get(PERCOLATE_STORE_REL)
        after = cat_file_batch(REPO_ROOT, sha, [PERCOLATE_STORE_REL]).get(PERCOLATE_STORE_REL)
        if before is None or after is None:
            pytest.skip(
                f"anchor commit {sha} is unreachable in this clone -- SKIP, not a pass."
            )
        before_hash, before_version = _shape_and_version(before)
        after_hash, after_version = _shape_and_version(after)
        assert before_hash == after_hash, (
            "the 1.2.0 re-vendor changed only the version and annotation keys, so the "
            f"semantic shape hash must not move: {before_hash} -> {after_hash}"
        )
        assert before_version != after_version, (
            f"expected the re-vendor to move x-schema-version, got {before_version!r} "
            f"on both sides -- this anchor is mis-pinned"
        )
        assert (
            shape_moved_under_unmoved_version(PERCOLATE_STORE_REL, before, after) is None
        )
