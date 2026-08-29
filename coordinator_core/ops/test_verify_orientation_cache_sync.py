"""
Tests for coordinator_core.ops.verify_orientation_cache_sync.

Review: code-reviewer F5 (2026-07-17 BIG_PORT Wave A verification pass) —
this module shipped with zero automated coverage despite being the most
edge-case-dense item in its slice (8 schema-shape regexes + a 3-branch
`*.uproject` detector). Covers: one PASS fixture, one violation per
schema-check category, and the 3-branch uproject-detector paths
(git-tracked, git-present-but-untracked, no-git).

C6 (2026-07-30): the PASS fixture and its Counters/Priorities-shaped cases
were rewritten for the writer's purpose-map rewrite — ``## Project`` /
``## Counters`` / ``## Priorities`` are retired; ``## Wiki`` / ``##
Architecture atlas`` / ``## Fast test`` / ``## Audits & censuses`` are the
four replacement routing sections, each checked by the same loose
bullet-shape rule (`_check_pointer_section`).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.verify_orientation_cache_sync as _verify_mod
import coordinator_core.orientation.regenerate_cache as _writer_mod
from coordinator_core.ops.verify_orientation_cache_sync import main, verify

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_PASS_BODY = """---
generated_by: test-writer
generated_at: 2026-07-17T00:00:00Z
git_head_at_generation: abc1234
---

## Trust caveats
- some caveat line, unrelated to uproject detection

## Active workstreams
1. Some workstream name

## Rechecks due ≤7 days
(none)

## Branch
work/test/2026-07-17

## Auto-push health
- ⚠ 2 unpushed commits

## Wiki
- `docs/wiki/` — doctrine/reference material; browse before assuming absence.

## Pinboard
- 2026-07-17 test-writer: a note
"""


def _write(tmp_path, body: str):
    p = tmp_path / "orientation_cache.md"
    p.write_text(body, encoding="utf-8")
    return p


def _run(*args, cwd=None, check=True):
    return subprocess.run(list(args), cwd=cwd, check=check, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# PASS fixture — no .uproject anywhere, so the uproject guard is inert.
# ---------------------------------------------------------------------------


def test_pass_fixture_no_violations(tmp_path):
    cache_path = _write(tmp_path, _PASS_BODY)
    violations, line_count = verify(str(cache_path), str(tmp_path))
    assert violations == []
    assert line_count == _PASS_BODY.count("\n")


def test_pass_fixture_main_exits_0(tmp_path, capsys):
    cache_path = _write(tmp_path, _PASS_BODY)
    rc = main([str(cache_path), str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


# ---------------------------------------------------------------------------
# main() usage error — missing argv.
# ---------------------------------------------------------------------------


def test_main_usage_error_on_missing_argv(capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage" in err


# ---------------------------------------------------------------------------
# One violation per schema-check category.
# ---------------------------------------------------------------------------


def test_frontmatter_missing_generated_by(tmp_path):
    body = _PASS_BODY.replace("generated_by: test-writer\n", "")
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("missing generated_by" in v for v in violations)


def test_frontmatter_generated_by_not_a_slug(tmp_path):
    body = _PASS_BODY.replace(
        "generated_by: test-writer\n", "generated_by: Not A Slug (patched by hand)\n"
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("not a single lowercase slug" in v for v in violations)


def test_frontmatter_not_found(tmp_path):
    cache_path = _write(tmp_path, "# no frontmatter here\nbody only\n")
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("frontmatter not found" in v for v in violations)


def test_out_of_schema_heading(tmp_path):
    body = _PASS_BODY.replace(
        "## Trust caveats\n", "## Trust caveats\n\n## Not A Real Heading\nsome text\n"
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("out-of-schema heading" in v for v in violations)


def test_project_counters_priorities_are_out_of_schema(tmp_path):
    """C6: the three retired census/answer-shaped headings must be flagged
    the same as any other out-of-schema heading."""
    body = _PASS_BODY.replace(
        "## Trust caveats\n",
        "## Project\n> stale re-quote\n\n"
        "## Counters\n- **Handoffs ready:** 4.\n\n"
        "## Priorities\n- **Widget work:** urgent (explicit)\n\n"
        "## Trust caveats\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert "out-of-schema heading: '## Project'" in violations
    assert "out-of-schema heading: '## Counters'" in violations
    assert "out-of-schema heading: '## Priorities'" in violations


# ---------------------------------------------------------------------------
# Purpose-map pointer sections (C6) — Wiki / Architecture atlas / Fast test /
# Audits & censuses all share the same loose bullet-shape check.
# ---------------------------------------------------------------------------


def test_pointer_section_line_must_be_a_bullet(tmp_path):
    body = _PASS_BODY.replace(
        "- `docs/wiki/` — doctrine/reference material; browse before assuming absence.\n",
        "docs/wiki/ (not a bullet)\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("'## Wiki' line fails bullet shape" in v for v in violations)


def test_architecture_atlas_and_fast_test_and_audits_are_schema_legal(tmp_path):
    body = _PASS_BODY.replace(
        "## Pinboard\n",
        "## Architecture atlas\n"
        "- `docs/architecture/systems/` — per-subsystem architecture pages: engine, ceremony\n\n"
        "## Fast test\n"
        "- fast test: `python3 -m pytest -q`\n\n"
        "## Audits & censuses\n"
        "- `state/audits/` — existing investigation records\n\n"
        "## Pinboard\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert violations == []


def test_engine_output_passes_its_own_verifier(tmp_path):
    """Writer/verifier round-trip on the real renderer — the failure shape that
    let ``## Housekeeping`` ship for five days flagging every compliant cache."""
    output = _writer_mod._render_cache(
        invoker="workday-start",
        iso_now="2026-07-30T00:00:00Z",
        git_head="abc1234",
        uproject_path="",
        workstreams=[],
        rechecks=[],
        branch_line="`work/x` — 0/0 vs origin/main",
        recent_commits=[],
        push_health="",
        wiki_lines=[
            "- `docs/wiki/` — doctrine/reference material; browse before assuming absence."
        ],
        atlas_lines=[
            "- `docs/architecture/systems/` — per-subsystem architecture pages: engine"
        ],
        capability_pointers_lines=[],
        fast_test_lines=["- fast test: `python3 -m pytest -q`"],
        audits_lines=["- `state/audits/` — existing investigation records"],
        hook_cancellation_line="",
        warm_engine_line="",
        budget_breach_line="",
        expired_grant_lines="",
        housekeeping_lines=[],
        pinboard_final="",
    )
    cache_path = _write(tmp_path, output)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert violations == []


def test_workstream_line_fails_name_only_regex(tmp_path):
    body = _PASS_BODY.replace(
        "1. Some workstream name\n", "not-numbered workstream line\n"
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("workstream line fails name-only regex" in v for v in violations)


def test_workstream_line_exceeds_body_cap(tmp_path):
    long_line = "1. " + ("x" * 100)
    body = _PASS_BODY.replace("1. Some workstream name\n", long_line + "\n")
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    # Review: code-reviewer F4 — message cites the live constant (84), not a
    # stale "80-char" literal; this test pins the two staying in sync.
    assert any("exceeds 84-char body cap" in v for v in violations)


def test_workstream_section_exceeds_max_entries(tmp_path):
    many = "\n".join(f"{i}. workstream {i}" for i in range(1, 12))
    body = _PASS_BODY.replace("1. Some workstream name\n", many + "\n")
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("Active workstreams section has" in v for v in violations)


def test_pinboard_line_fails_shape(tmp_path):
    body = _PASS_BODY.replace(
        "- 2026-07-17 test-writer: a note\n", "- not the right shape at all\n"
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("pinboard line fails shape" in v for v in violations)


def test_pinboard_section_exceeds_max_lines(tmp_path):
    body = _PASS_BODY.replace(
        "- 2026-07-17 test-writer: a note\n",
        "- 2026-07-17 test-writer: a note\n- 2026-07-18 test-writer: another note\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("Pinboard section has" in v for v in violations)


def test_auto_push_health_line_fails_shape(tmp_path):
    body = _PASS_BODY.replace(
        "- ⚠ 2 unpushed commits\n", "- everything is fine\n"
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("Auto-push health line fails shape" in v for v in violations)


def test_trust_caveats_exceeds_max_lines(tmp_path):
    body = _PASS_BODY.replace(
        "- some caveat line, unrelated to uproject detection\n",
        "\n".join(f"- caveat {i}" for i in range(1, 7)) + "\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("Trust caveats section has" in v for v in violations)


def test_file_length_exceeds_line_ceiling(tmp_path):
    from coordinator_core.ops.verify_orientation_cache_sync import LINE_CEILING

    body = _PASS_BODY + "\n".join(f"extra line {i}" for i in range(1, 40)) + "\n"
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any(f"exceeds {LINE_CEILING}-line ceiling" in v for v in violations)


# ---------------------------------------------------------------------------
# uproject guard — detector-output missing / corrupted.
# ---------------------------------------------------------------------------


def test_uproject_present_but_trust_caveats_missing(tmp_path):
    (tmp_path / "Game.uproject").write_text("{}")
    body = _PASS_BODY.replace(
        "## Trust caveats\n- some caveat line, unrelated to uproject detection\n\n",
        "",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("detector-output missing" in v for v in violations)


def test_uproject_present_but_trust_caveats_do_not_mention_it(tmp_path):
    (tmp_path / "Game.uproject").write_text("{}")
    cache_path = _write(tmp_path, _PASS_BODY)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert any("detector-output corrupted" in v for v in violations)


def test_uproject_present_and_trust_caveats_correct_is_clean(tmp_path):
    (tmp_path / "Game.uproject").write_text("{}")
    body = _PASS_BODY.replace(
        "- some caveat line, unrelated to uproject detection\n",
        "- Unreal Engine project detected (Game.uproject)\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert violations == []


# ---------------------------------------------------------------------------
# 3-branch uproject detector: git-tracked, git-present-but-untracked, no-git.
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path):
    _run("git", "init", "-q", "-b", "main", str(tmp_path))
    _run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
    _run("git", "config", "user.name", "Test", cwd=tmp_path)
    return tmp_path


def test_uproject_detector_git_tracked(git_repo):
    (git_repo / "Game.uproject").write_text("{}")
    _run("git", "add", "-A", cwd=git_repo)
    _run("git", "commit", "-q", "-m", "add uproject", cwd=git_repo)

    body = _PASS_BODY.replace(
        "- some caveat line, unrelated to uproject detection\n",
        "- Unreal Engine project detected (Game.uproject)\n",
    )
    cache_path = _write(git_repo, body)
    violations, _ = verify(str(cache_path), str(git_repo))
    assert violations == []


def test_uproject_detector_git_present_but_untracked_falls_back_to_find(git_repo):
    # git ls-files reports nothing for an untracked file — the detector must
    # fall back to the excluded-find branch and still find it.
    (git_repo / "Game.uproject").write_text("{}")

    body = _PASS_BODY.replace(
        "- some caveat line, unrelated to uproject detection\n",
        "- Unreal Engine project detected (Game.uproject)\n",
    )
    cache_path = _write(git_repo, body)
    violations, _ = verify(str(cache_path), str(git_repo))
    assert violations == []


def test_uproject_detector_no_git_falls_back_to_unfiltered_find(tmp_path):
    # tmp_path has no .git at all — the detector must use the unfiltered-find
    # branch directly (no git subprocess success).
    (tmp_path / "Game.uproject").write_text("{}")

    body = _PASS_BODY.replace(
        "- some caveat line, unrelated to uproject detection\n",
        "- Unreal Engine project detected (Game.uproject)\n",
    )
    cache_path = _write(tmp_path, body)
    violations, _ = verify(str(cache_path), str(tmp_path))
    assert violations == []


# ---------------------------------------------------------------------------
# Bounds reconciliation (2026-07-28) — drift regression.
#
# Before this reconciliation, LINE_CEILING/WORKSTREAM_MAX/WORKSTREAM_BODY_CAP
# were each declared independently in BOTH this module and the writer
# (coordinator_core.orientation.regenerate_cache) — WORKSTREAM_MAX happened
# to agree by coincidence, and LINE_CEILING (35 here, no equivalent on the
# writer at all) silently disagreed with the writer's own CACHE_BUDGET_BYTES
# in both directions. The fix is architectural, not numerical: this module
# now IMPORTS these names from the writer instead of assigning its own
# literal, which is what the two tests below actually verify.
#
# C6 (2026-07-30): PRIORITIES_MAX dropped from this set — the writer no
# longer declares it (the Priorities section it bounded is retired).
# ---------------------------------------------------------------------------

_SHARED_BOUND_NAMES = ("LINE_CEILING", "WORKSTREAM_MAX", "WORKSTREAM_BODY_CAP")
_REDECLARATION_RE = re.compile(
    r"(?m)^(?:" + "|".join(_SHARED_BOUND_NAMES) + r")\s*="
)


def test_shared_bounds_equal_the_writers_values():
    """Pin: every shared bound's value as seen from this module equals the
    writer's own value for the same name — the fact the import statement at
    the top of this file is supposed to guarantee."""
    for name in _SHARED_BOUND_NAMES:
        assert getattr(_verify_mod, name) == getattr(_writer_mod, name), name


def test_shared_bounds_have_no_local_redeclaration_in_verifier_source():
    """Drift regression — the actual deliverable.

    Greps this module's OWN source for a top-level ``NAME = ...`` assignment
    to any of the shared bound names, outside the ``from ...
    regenerate_cache import (...)`` block at the top of the file. If a future
    edit reintroduces a shadowing redeclaration — the exact shape of the
    original bug, where LINE_CEILING was independently assigned 35 here with
    no corresponding writer-side value — this test fails the moment that
    redeclaration lands, before the two numbers ever get a chance to
    disagree at runtime the way the pre-reconciliation state did.
    """
    src = Path(_verify_mod.__file__).read_text(encoding="utf-8")
    import_start = src.index("from coordinator_core.orientation.regenerate_cache import (")
    import_end = src.index(")\n", import_start) + len(")\n")
    body = src[:import_start] + src[import_end:]
    match = _REDECLARATION_RE.search(body)
    assert match is None, (
        f"found a local redeclaration of a shared bound in "
        f"verify_orientation_cache_sync.py: {match.group(0) if match else ''!r} — "
        "import it from regenerate_cache instead, do not reassign it here"
    )
