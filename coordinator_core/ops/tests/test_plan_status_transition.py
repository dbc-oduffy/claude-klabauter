"""Characterization tests for coordinator_core.ops.plan_status_transition.

Golden-oracle-derived: each case was hand-run against the node CLI
(example-doctrine-repo coordinator/bin/plan-status-transition.js) during the port to
capture exact stdout/stderr text and exit codes, then reproduced here.

Port source: coordinator/bin/plan-status-transition.js (example-doctrine-repo)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.plan_status_transition import main

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _ensure_git_repo(tmp_path: Path) -> None:
    """Idempotently `git init` tmp_path, WITH an initial commit.

    2026-08-04 (C6, docs/plans/2026-08-04-terminal-state-propagation-join-keys.md):
    `_stamp_implemented`'s flip write routes through `locked_rmw`, which requires
    a resolvable git repo root for its lock-sidecar location.

    2026-08-06 (commit-authored-content reroute): `_commit_plan_flip` now routes
    through `git_native.commit_authored_content`, which REQUIRES a resolvable
    `HEAD` (it runs `git rev-parse HEAD` as its first step) — a bare `git init`
    with zero commits leaves `HEAD` unresolvable (`fatal: ambiguous argument
    'HEAD'`). An initial empty commit is made here so `HEAD` always resolves;
    the REALISTIC fixture shape a plan-bearing repo actually has. Tests
    exercising the deliberately-empty-repo / genuinely-untracked-plan cases use
    their own bespoke setup instead of this helper (see
    `test_no_head_plan_flips_on_disk_and_skips_commit` /
    `test_untracked_plan_flips_on_disk_and_skips_commit` below).
    """
    if (tmp_path / ".git").exists():
        return
    import os

    env = {**os.environ, **_GIT_ENV_KEYS}
    subprocess.run(
        ["git", "init"], cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitkeep"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )


def _track(tmp_path: Path, p: Path) -> None:
    """`git add` + `git commit` the given path — makes it present in HEAD.

    `commit_authored_content` refuses a path absent from `HEAD` (DR-272 §
    3.3), so any test exercising a REAL commit attempt (as opposed to the
    deliberate skip-commit cases) needs its plan file tracked in HEAD before
    `stamp-implemented` runs, mirroring how a plan document actually reaches
    this op in production (committed by an earlier authoring session, not
    materialized fresh in the same commit as its own status flip).
    """
    import os

    env = {**os.environ, **_GIT_ENV_KEYS}
    rel = p.relative_to(tmp_path)
    subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )
    subprocess.run(
        ["git", "commit", "-m", f"seed {rel}"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    _ensure_git_repo(tmp_path)
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    _track(tmp_path, p)
    return p


# ---------------------------------------------------------------------------
# stamp-implemented — flip cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["draft", "reviewed", "approved", "executing"])
def test_flippable_status_flips_to_implemented(tmp_path, capsys, status):
    p = _write(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\nowner: x\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'status "{status}" → implemented' in out
    assert p.read_text(encoding="utf-8") == (
        "---\ntitle: T\nstatus: implemented\nowner: x\n---\n\nBody.\n"
    )


def test_double_quoted_status_value_is_unquoted_before_matrix_check(tmp_path):
    # A-F3 parity: node oracle's readFmField (schema.js) is quote-aware;
    # coordinator_core.frontmatter.primitives.read_fm_field deliberately is
    # not (callers unquote locally, per memo_transition.py convention) --
    # this op does its own unquote (see _unquote_scalar) to match.
    p = _write(
        tmp_path, "plan.md",
        '---\ntitle: Test plan\ncreated: 2026-07-09\nauthor: test\nstatus: "draft"\n---\n\n# Body\n\nSome content.\n',
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert 'status: implemented' in p.read_text(encoding="utf-8")


def test_status_with_trailing_inline_comment_flips_to_implemented(tmp_path):
    # 2026-07-21 regression: state/improvement-queue/2026-07-21-stamp-plan-
    # implemented-fails-on-a-yaml-l-d99ba60c03cb.yaml -- `status: executing  #
    # authorized ...` is YAML-legal (a `#` preceded by whitespace starts a
    # comment) and YAML itself parses the value as bare `executing`, but the
    # raw read_fm_field text used to reach the set lookup unstripped and
    # fail-loud. This is the exact failing case from the queue entry.
    #
    # The comment is stripped for the SET LOOKUP only -- it SURVIVES into the
    # rewritten line (2026-08-01, example-cockpit-repo memo): a stamp that silently
    # deleted the qualifier left the plan overclaiming its own completion.
    p = _write(
        tmp_path, "p.md",
        "---\nstatus: executing  # authorized 2026-07-21 (body-sha ffc19fc)\n---\n\nBody.\n",
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == (
        "---\nstatus: implemented  # authorized 2026-07-21 (body-sha ffc19fc)\n---\n\nBody.\n"
    )


def test_status_without_comment_is_unaffected(tmp_path):
    p = _write(tmp_path, "p.md", "---\nstatus: executing\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\n---\n\nBody.\n"


def test_status_with_tab_preceded_comment_flips_to_implemented(tmp_path):
    # _TRAILING_COMMENT_RE's character class is `[ \t]` -- a tab-preceded `#`
    # is also a comment opener, not just a space-preceded one. Every other
    # comment-strip test in this file uses a space; this pins the tab branch.
    p = _write(
        tmp_path, "p.md",
        "---\nstatus: executing\t# note\n---\n\nBody.\n",
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\t# note\n---\n\nBody.\n"


def test_comment_only_status_value_reports_no_status_field(tmp_path, capsys):
    # `status: #note` -- a comment with no real value before it. YAML parses
    # this as an absent/null status. read_fm_field's own `\s*` already
    # consumed the whitespace before the `#`, so the raw captured text starts
    # at index 0 with `#` -- the trailing-comment regex's lookbehind cannot
    # match at string-start, so without the explicit comment-only check this
    # would surface a garbled `unexpected current status "#note"` instead of
    # the clear "no status field" diagnostic a genuinely missing key gets.
    original = "---\nstatus: #note\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    assert 'no "status" field found' in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == original


def test_quoted_status_with_trailing_comment_reports_clear_diagnostic(tmp_path, capsys):
    # `status: "draft"  # note` -- a quoted value followed by an inline
    # comment. The comment-strip is a no-op on anything that looks quoted
    # (defers to unquote_yaml_scalar), but unquote_yaml_scalar also fails to
    # strip the quotes here because the raw text no longer *ends* with `"`
    # (it ends with the comment text) -- surfacing a garbled
    # `unexpected current status "\"draft\"  # note"` without the explicit
    # quoted-scalar-plus-trailing-comment check.
    original = '---\nstatus: "draft"  # note\n---\n\nBody.\n'
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "quoted-scalar-plus-trailing-comment" in err
    assert p.read_text(encoding="utf-8") == original


def test_quoted_status_with_hash_inside_quotes_is_not_comment_stripped(tmp_path, capsys):
    # Quotes protect everything inside, including a literal '#' -- the
    # comment-strip must not fire inside a quoted scalar. No flippable status
    # value contains a literal '#' (the matrix is a closed set of bare words),
    # so a quoted value carrying one can never itself successfully flip; the
    # correctness property under test is that the '#1' survives unquoting
    # completely intact (proving comment-strip did not fire on it) rather than
    # being stripped to "draft" (which WOULD flip) or mangled into some other
    # shape -- both of which would indicate the quote guard failed.
    original = '---\nstatus: "draft #1"\n---\n\nBody.\n'
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    assert 'unexpected current status "draft #1"' in capsys.readouterr().err
    # File is untouched on the error path.
    assert p.read_text(encoding="utf-8") == original


def test_terminal_status_with_trailing_comment_is_still_a_no_op(tmp_path, capsys):
    original = "---\nstatus: implemented  # note\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'status "implemented" is terminal/deferred — no-op' in out
    assert p.read_text(encoding="utf-8") == original


def test_flip_preserves_preamble_and_body_verbatim(tmp_path):
    p = _write(
        tmp_path, "p.md",
        "\n<!-- lead comment -->\n---\nstatus: draft\n---\n\nBody line 1.\nBody line 2.\n",
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == (
        "\n<!-- lead comment -->\n---\nstatus: implemented\n---\n\nBody line 1.\nBody line 2.\n"
    )


def test_crlf_normalized_to_lf_before_rewrite(tmp_path):
    _ensure_git_repo(tmp_path)
    p = tmp_path / "p.md"
    p.write_bytes(b"---\r\nstatus: draft\r\n---\r\n\r\nBody.\r\n")
    _track(tmp_path, p)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\n---\n\nBody.\n"


# ---------------------------------------------------------------------------
# stamp-implemented — no-op / terminal cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["implemented", "superseded", "abandoned", "deferred"])
def test_terminal_status_is_no_op(tmp_path, capsys, status):
    original = f"---\nstatus: {status}\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'status "{status}" is terminal/deferred — no-op' in out
    assert p.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# stamp-implemented — error cases (negative corpus)
# ---------------------------------------------------------------------------

def test_missing_verb(capsys):
    rc = main([])
    assert rc == 1
    assert "unknown verb: (none)" in capsys.readouterr().err


def test_unknown_verb(capsys):
    rc = main(["bogus-verb"])
    assert rc == 1
    assert "unknown verb: bogus-verb" in capsys.readouterr().err


def test_missing_plan_flag(capsys):
    rc = main(["stamp-implemented"])
    assert rc == 1
    assert "requires --plan <path>" in capsys.readouterr().err


def test_file_not_found(tmp_path, capsys):
    missing = tmp_path / "nope.md"
    rc = main(["stamp-implemented", "--plan", str(missing)])
    assert rc == 1
    assert "plan not found" in capsys.readouterr().err


def test_no_frontmatter(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "Just text, no frontmatter.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    assert "no parseable YAML frontmatter" in capsys.readouterr().err


def test_no_status_field(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: no status\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    assert 'no "status" field found' in capsys.readouterr().err


def test_unexpected_status_value(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\nstatus: bogus\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert 'unexpected current status "bogus"' in err
    assert "draft, reviewed, approved, executing" in err


def test_unknown_flag(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p), "--bogus", "x"])
    assert rc == 1
    assert "unknown argument: --bogus" in capsys.readouterr().err


def test_flag_missing_value(tmp_path, capsys):
    rc = main(["stamp-implemented", "--plan"])
    assert rc == 1
    assert "flag requires a value: --plan" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# deliverable_id is a JOIN KEY, not echoed text (break-class fix, 2026-08-04)
#
# `_stamp_implemented` hands this value to `deliverable.cascade_terminal`,
# which exact-string-compares it against each live handoff's own PARSED
# frontmatter `deliverable_id` (quotes already stripped on that side). Reading
# it with the raw `read_fm_field` meant a conventionally-quoted
# `deliverable_id: "dlv-…"` arrived carrying its quote characters and joined
# against nothing.
#
# Observed live 2026-08-04: a plan stamped `implemented` reported `no live
# handoff carries deliverable_id='"dlv-ownership-inherited-at-dispatch-engine-
# o-7f2281"'` while the matching handoff sat in state/handoffs/; the cascade
# advanced nothing and the handoff was shipped by hand.
#
# NEGATIVE SPEC: this failure is SILENT by construction and no verdict-only
# assertion catches it -- an empty candidate set is indistinguishable from
# "correctly nothing to cascade", and the op reports success either way. These
# pins therefore assert on the VALUE HANDED TO THE CASCADE, never on the exit
# code.
# ---------------------------------------------------------------------------

def _capture_cascade_key(monkeypatch):
    """Record the deliverable_id `_stamp_implemented` hands to the cascade."""
    from coordinator_core.ops import plan_status_transition as pst

    seen: list = []

    def _fake_run_cascade(plan_path, deliverable_id):
        seen.append(deliverable_id)
        return 0

    monkeypatch.setattr(pst, "_run_cascade", _fake_run_cascade)
    return seen


@pytest.mark.parametrize(
    "fm_line",
    [
        'deliverable_id: "dlv-abc-123"',   # double-quoted — the live failure
        "deliverable_id: 'dlv-abc-123'",   # single-quoted
        "deliverable_id: dlv-abc-123",     # bare — must be unaffected
    ],
)
def test_deliverable_id_reaches_cascade_unquoted(tmp_path, monkeypatch, fm_line):
    seen = _capture_cascade_key(monkeypatch)
    p = _write(
        tmp_path, "p.md",
        f"---\ntitle: T\nstatus: draft\n{fm_line}\n---\n\nBody.\n",
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert seen == ["dlv-abc-123"], (
        f"join key must reach the cascade unquoted; got {seen!r} from {fm_line!r}"
    )


def test_deliverable_id_quoting_style_does_not_change_the_join_key(tmp_path, monkeypatch):
    """All three spellings must produce a byte-identical join key.

    Pinned as its own case because the defect was an ASYMMETRY between the two
    sides of a join, not a bad value in isolation -- the plan side and the
    handoff side must agree regardless of how either file was authored.
    """
    keys = []
    for i, fm_line in enumerate(
        ['deliverable_id: "dlv-x"', "deliverable_id: 'dlv-x'", "deliverable_id: dlv-x"]
    ):
        seen = _capture_cascade_key(monkeypatch)
        p = _write(
            tmp_path, f"p{i}.md",
            f"---\ntitle: T\nstatus: draft\n{fm_line}\n---\n\nBody.\n",
        )
        assert main(["stamp-implemented", "--plan", str(p)]) == 0
        keys.extend(seen)
    assert len(set(keys)) == 1, f"quoting style leaked into the join key: {keys!r}"


def test_deliverable_id_with_trailing_comment_reaches_cascade_clean(tmp_path, monkeypatch):
    """`read_fm_field_unquoted` comment-strips as well as unquotes -- the same
    treatment `status` already gets a few lines above the fixed call site."""
    seen = _capture_cascade_key(monkeypatch)
    p = _write(
        tmp_path, "p.md",
        "---\ntitle: T\nstatus: draft\ndeliverable_id: dlv-abc  # minted 2026-08-04\n---\n\nBody.\n",
    )
    assert main(["stamp-implemented", "--plan", str(p)]) == 0
    assert seen == ["dlv-abc"]


# ---------------------------------------------------------------------------
# C2a — path containment guard (docs/plans/2026-08-06-writer-side-commit-
# ownership-lock-gap.md § C2a): `--plan` must resolve under the git worktree
# this op resolved for it. Mirrors queue_close's `contained_path` guard --
# same helper, same refuse-on-None semantics, same "escapes" diagnostic
# style -- with the whole worktree (not one fixed subdirectory) as the
# allowed root; see plan_status_transition.py's module docstring "Path
# containment" section for why the root differs from queue_close's single
# fixed directory.
# ---------------------------------------------------------------------------

def test_in_containment_plan_path_flips_normally(tmp_path):
    p = _write(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\n---\n\nBody.\n"


def test_out_of_containment_plan_path_is_refused(tmp_path, capsys):
    # `worktree` is a resolved git repo; `outside` is a SIBLING directory
    # (not nested under `worktree`). `--plan` names a SYMLINK that lives
    # inside `worktree` (so `_worktree_root`'s `git rev-parse
    # --show-toplevel`, run with cwd = the symlink's own unresolved parent
    # directory, resolves `worktree` normally) but whose target is the real
    # file over in `outside` -- `contained_path`'s post-`.resolve()` check
    # follows the symlink and finds the real file outside the resolved
    # worktree root, the exact "resolved a worktree, but the resolved
    # candidate escapes it" shape this guard exists to refuse before ever
    # reaching a flip/commit attempt (mirrors `contained_path`'s own
    # documented symlink-safety contract).
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _ensure_git_repo(worktree)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = "---\nstatus: draft\n---\n\nBody.\n"
    real = outside / "p.md"
    real.write_text(original, encoding="utf-8")
    link = worktree / "p-link.md"
    link.symlink_to(real)

    rc = main(["stamp-implemented", "--plan", str(link)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "escapes the resolved git worktree" in err
    # Refused before any write -- file is untouched.
    assert real.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Commit-authored-content reroute (2026-08-06, C3, DR-272 Defect 1) — skip-
# commit on an untracked plan / no-HEAD repo, EM decision: an op whose
# contract is in-place mutation must not be the thing that first-commits a
# file into git. The flip still lands on disk and the CLI still exits 0;
# only the commit is skipped, with a clear operator-facing diagnostic.
# ---------------------------------------------------------------------------

def test_untracked_plan_flips_on_disk_and_skips_commit(tmp_path, capsys):
    # A repo WITH a HEAD (the ordinary initial-commit shape), but the plan
    # file itself was written directly to disk and never `git add`/committed
    # -- absent from HEAD. `commit_authored_content` would refuse this path;
    # `_stamp_implemented` must detect that BEFORE attempting the commit and
    # skip it cleanly rather than surface that refusal as a flip failure.
    _ensure_git_repo(tmp_path)
    p = tmp_path / "p.md"
    p.write_text("---\nstatus: draft\n---\n\nBody.\n", encoding="utf-8")
    # Deliberately NOT tracked via _track().

    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    # Status flip landed on disk...
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\n---\n\nBody.\n"
    # ...but was left uncommitted.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "p.md"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=15,
    )
    assert status.stdout.strip().startswith("??"), (
        f"expected the plan to remain untracked (never first-committed): {status.stdout!r}"
    )
    err = capsys.readouterr().err
    assert "not tracked in git" in err
    assert "left uncommitted" in err


# ---------------------------------------------------------------------------
# stamp-superseded (C2, docs/plans/2026-08-06-<this-plan>.md)
# ---------------------------------------------------------------------------

def test_stamp_superseded_happy_path(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: draft\n---\n\nBody.\n")
    successor = _write(tmp_path, "successor.md", "---\ntitle: S\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-superseded", "--plan", str(p), "--by", str(successor)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'status "draft" → superseded (by {successor})' in out
    text = p.read_text(encoding="utf-8")
    assert "status: superseded" in text
    assert f"superseded_by: {successor}" in text


def test_stamp_superseded_missing_by_fails_loud(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-superseded", "--plan", str(p)])
    assert rc == 1
    assert "requires --by" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == "---\nstatus: draft\n---\n\nBody.\n"


def test_stamp_superseded_by_nonexistent_plan_fails_loud(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    missing_successor = tmp_path / "nope.md"
    rc = main(["stamp-superseded", "--plan", str(p), "--by", str(missing_successor)])
    assert rc == 1
    assert "--by path not found" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == "---\nstatus: draft\n---\n\nBody.\n"


def test_stamp_superseded_already_superseded_is_no_op(tmp_path, capsys):
    original = "---\nstatus: superseded\nsuperseded_by: other.md\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    successor = _write(tmp_path, "successor.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-superseded", "--plan", str(p), "--by", str(successor)])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'status "superseded" is already superseded — no-op' in out
    assert p.read_text(encoding="utf-8") == original


def test_stamp_superseded_refuses_terminal_source_status(tmp_path, capsys):
    original = "---\nstatus: implemented\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    successor = _write(tmp_path, "successor.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc = main(["stamp-superseded", "--plan", str(p), "--by", str(successor)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already terminal at status \"implemented\"" in err
    assert p.read_text(encoding="utf-8") == original


def test_no_head_plan_flips_on_disk_and_skips_commit(tmp_path, capsys):
    # A git repo with ZERO commits -- HEAD does not resolve at all. Distinct
    # from the untracked-but-HEAD-exists case above: `commit_authored_content`
    # would hit `git rev-parse HEAD` failure ("ambiguous argument 'HEAD'")
    # before ever reaching its own tracked-in-HEAD refusal.
    import os

    env = {**os.environ, **_GIT_ENV_KEYS}
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, env=env, timeout=15)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
    )
    p = tmp_path / "p.md"
    p.write_text("---\nstatus: draft\n---\n\nBody.\n", encoding="utf-8")

    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == "---\nstatus: implemented\n---\n\nBody.\n"
    err = capsys.readouterr().err
    assert "HEAD does not resolve" in err
    assert "left uncommitted" in err
