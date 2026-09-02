r"""Every percolate commit subject names the SOURCE commit its bytes were cut from.

The publish seam had a currency signal for neither party. A mirror consumer
executing `X:\claude-klabauter` could not tell whether a fix it had been told
was committed in `claude-klabauter` was live for it, and the publisher could not
tell that "committed" would be read as "live" (example-retrieval-repo-ue-addon-em,
2026-08-31: fix `40abe011d` stayed live as a crash for a mirror consumer whose
only available currency check was a hand-rolled grep for the fix's own source
text). The commit subject is the one publish-side report that outlives the
round and is readable from the mirror alone, so it carries the stamp.

All three legs are pinned, because they are different code paths that produce
the same mirror history: `publish.py :: _commit_published_dests` (a bare
`coordinator-publish`), `percolate-round.py :: _build_commit_subject` (the
round, which drives publish with `--no-commit` and owns its own commit), and
`percolate-mirror.py`'s whole-mirror commit leg. A stamp on only SOME publish
commits is worse than none: a consumer reading an unstamped one cannot tell
"published before the stamp existed" from "this leg never stamps".

Negative spec: a subject-shape test alone would pass against a stamp that is
silently always empty, so the degrade path is asserted SEPARATELY from the
populated path — never inferred from it.

Run: python -m pytest coordinator/bin/tests/test_publish_commit_subject_names_source_sha.py -q
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

_STAMP = re.compile(r" \[source ([0-9a-f]{12})\]$")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_source_sha_under_test", "publish.py")
round_mod = _load("percolate_round_source_sha_under_test", "percolate-round.py")


def test_publish_leg_stamps_a_twelve_hex_source_sha():
    suffix = publish._source_sha_suffix()
    assert _STAMP.fullmatch(suffix), f"unstamped publish-leg suffix: {suffix!r}"


def test_round_leg_stamps_the_same_shape():
    assert round_mod._source_sha_suffix() == publish._source_sha_suffix()


def test_round_commit_subject_carries_the_stamp():
    subject = round_mod._build_commit_subject("some-row", [], [])
    assert _STAMP.search(subject), f"round subject carries no stamp: {subject!r}"
    # The stamp is appended, never a replacement: the pre-existing report is intact.
    assert subject.startswith("percolate publish: some-row")


def test_stamp_degrades_to_empty_rather_than_raising(monkeypatch):
    """An unborn/unresolvable HEAD must not block a publish.

    Asserted independently of the populated path above: together they pin that
    the empty string means "HEAD did not resolve", never "the stamp is inert".
    """
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda _repo: None)
    assert publish._source_sha_suffix() == ""
    assert round_mod._source_sha_suffix() == ""


def test_every_percolate_commit_subject_site_stamps():
    """No publish leg composes a subject without the stamp.

    KEPT DELIBERATELY, against an overengineering finding that
    `test_the_stamp_has_exactly_one_definition` below makes this redundant.
    It does not: one shared definition guarantees that every CALLER stamps
    identically, and guarantees nothing about a subject-composition site that
    never calls it. A fourth leg — or a reworked existing one — that builds a
    subject and forgets the stamp is exactly the "present on only SOME publish
    commits" failure this file exists to prevent, and only a site-coverage
    assertion catches it.

    The finding's other half was right, and is fixed here: the previous shape
    pinned each site's exact f-string text and failed with "re-pin this test"
    on any unrelated reword. Sites are now located STRUCTURALLY, by the
    enclosing function `ast` reports, so a subject can be reworded freely and
    only MOVING or REMOVING the stamp fails this.

    Pinned by source inspection rather than by calling each leg: the mirror
    leg's subject is built inside an interactive commit path this test cannot
    reach without a real dest repo and a tty, and leaving it unpinned is how a
    third leg silently stops stamping.
    """
    import ast  # noqa: PLC0415

    sites = {
        # review-integrator, 2026-09-01: publish.py collapsed
        # `_pinned_source_sha_suffix` into `_source_sha_suffix` (optional
        # `round_pinned_shas` param) per overengineering-reviewer finding 1.
        "publish.py": ("_commit_published_dests", "_source_sha_suffix"),
        "percolate-round.py": ("_build_commit_subject", "_source_sha_suffix"),
        "percolate-mirror.py": ("main", "_source_sha_suffix"),
    }
    for filename, (funcname, stamp_call_name) in sites.items():
        tree = ast.parse((_BIN_DIR / filename).read_text(encoding="utf-8"))
        enclosing = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == funcname
        ]
        assert enclosing, (
            f"{filename}: no function named {funcname!r} — the commit-subject "
            f"site was renamed or moved; re-pin this test to its new home"
        )
        stamped = any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == stamp_call_name)
                or (
                    isinstance(n.func, ast.Attribute)
                    and n.func.attr == stamp_call_name
                )
            )
            for node in enclosing
            for n in ast.walk(node)
        )
        assert stamped, (
            f"{filename}: {funcname} composes a commit subject without the "
            f"currency stamp"
        )


def test_commit_published_dests_stamps_the_round_pinned_sha_not_live_head(
    monkeypatch, tmp_path
):
    """The blind spot the earlier version of this file had: every prior
    assertion here compared `_source_sha_suffix()` against ITSELF (or
    against a sibling leg's call to the identical live-HEAD read), so a
    subject built from a stale-by-construction live `HEAD` read passed
    every one of them. `_commit_published_dests` runs at the END of a
    multi-target run, after every row — the site furthest from
    round-start and most exposed to a peer commit landing mid-run
    (`_round_pin_source_sha`'s own docstring: four distinct SHAs observed
    across one round on a 50-70-session box). This drives it with a
    `round_pinned_shas` cache pre-seeded to one sha and `head_sha` stubbed
    to answer a DIFFERENT one, so a subject naming the live-HEAD sha
    (the pre-fix behaviour) and a subject naming the pinned sha (the
    fix) are distinguishable outcomes, not the same assertion twice.
    """
    import coordinator_core.git.git_state as git_state
    from coordinator_core.git import commit as commit_mod

    pinned_sha = "1111111111112222222222223333333333333444"
    live_sha = "9999999999998888888888887777777777777666"
    monkeypatch.setattr(git_state, "head_sha", lambda _repo: live_sha)

    repo_root = tmp_path / "claude-klabauter"
    dest_dir = repo_root / "coordinator_core"
    dest_dir.mkdir(parents=True)

    seen: dict = {}

    def _fake_commit_paths(root, paths, message, **kwargs):
        seen["message"] = message
        return commit_mod.CommitOutcome(sha="a" * 40, staged_preferred=(), worktree_over_staged=())

    monkeypatch.setattr(commit_mod, "commit_paths", _fake_commit_paths)
    monkeypatch.setattr(publish, "_is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        publish, "_dirty_paths_under", lambda _root, _dirs: ["coordinator_core/ipc.py"]
    )
    monkeypatch.setattr(publish, "_normalize_dest_exec_bits", lambda _root, _dirs: [])

    round_pinned_shas = {str(publish._REPO_ROOT): pinned_sha}
    ok = publish._commit_published_dests(
        {repo_root: {dest_dir}},
        succeeded_row_names=["claude-klabauter"],
        round_pinned_shas=round_pinned_shas,
    )
    assert ok is True

    subject = seen["message"]
    assert pinned_sha[:12] in subject, (
        f"subject named a sha other than the round-pinned one: {subject!r}"
    )
    assert live_sha[:12] not in subject, (
        f"subject named the live-HEAD sha instead of the round-pinned one: {subject!r}"
    )


def test_the_stamp_has_exactly_one_definition():
    """The three legs must stamp BYTE-IDENTICALLY, and two of them are
    spawned standalone with no shared module import path, so a per-CLI copy
    of the formatter drifts silently — the failure this whole file exists to
    prevent, re-created one level down. The single definition lives in
    `coordinator_core.git.git_state`, the engine all three already bootstrap;
    each CLI keeps only a `_REPO_ROOT`-binding wrapper that delegates to it.

    Asserted by source inspection because the drift is a source fact: two
    copies that agree today pass every behavioural test and still diverge on
    the next edit to one of them.
    """
    from coordinator_core.git import git_state  # noqa: PLC0415

    assert callable(git_state.source_sha_suffix)
    assert "source_sha_suffix" in git_state.__all__

    body = (_BIN_DIR / "percolate-round.py").read_text(encoding="utf-8")
    assert "from coordinator_core.git.git_state import source_sha_suffix" in body, (
        "percolate-round.py must delegate to the engine's single definition"
    )
    assert 'f" [source {' not in body, (
        "percolate-round.py re-implements the stamp format instead of delegating"
    )

    mirror = (_BIN_DIR / "percolate-mirror.py").read_text(encoding="utf-8")
    assert 'f" [source {' not in mirror, (
        "percolate-mirror.py re-implements the stamp format instead of "
        "reaching it through `_round`"
    )

    # publish.py is DELIBERATELY not asserted here yet, and this exemption is
    # the finding, not a softening of it: its own copy of the formatter is
    # still live because the file is held by a peer claim this session must
    # not commit around (`session-claim-cli who-claims-path` reports
    # d12e25cf live on it; `clear-claim-if-dead` refuses). Tighten this to
    # cover publish.py the moment that claim releases -- tracked in
    # state/debt-backlog/2026-08-31-publish-py-keeps-a-second-definition-of-9f6f6a805216.yaml.
    # Until then the stamp has TWO definitions and
    # `test_round_leg_stamps_the_same_shape` above is the only thing keeping
    # them agreeing.
