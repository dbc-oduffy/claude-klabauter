"""
Self-test for coordinator/bin/lint-frontmatter.py's CLI logic
(coordinator_core.frontmatter.schema_validate.main).

Asserts exit code + JSON shape for each of the three flag shapes coordinator-claude's live
callers consume: whole-tree --json (update-docs.md Phase 11d), whole-tree
--strict-refs --json (workweek-complete.md Step 2.5), and --file (handoff/
SKILL.md's write-time gate). Builds a synthetic --root fixture tree per test
so results are independent of this repo's own live state/handoffs/ corpus.

Spec backlink: pln-python-ize-claude-klabauter-bin-oracles--218413 § A1
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import main


_VALID_HANDOFF = """---
title: Test handoff
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
---
Body text.
"""

_INVALID_HANDOFF = """---
title: Missing required fields
created: 2026-01-01
---
Body text.
"""

_DANGLING_REF_HANDOFF = """---
title: Dangling predecessor
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/does-not-exist-anywhere.md
---
Body text.
"""

_DANGLING_FORKED_FROM_HANDOFF = """---
title: Dangling forked_from
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
kind: spinoff
forked_from: state/handoffs/does-not-exist-forked-from.md
---
Body text.
"""

_DANGLING_ADDITIONAL_PREDECESSORS_HANDOFF = """---
title: Dangling additional_predecessors entry
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
additional_predecessors:
  - state/handoffs/does-not-exist-additional.md
---
Body text.
"""

_DANGLING_PREDECESSOR_ID_HANDOFF = """---
title: Dangling predecessor_id
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
predecessor_id: "hnd-does-not-exist-abc123"
---
Body text.
"""

_TARGET_WITH_HANDOFF_ID = """---
title: Target handoff
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
handoff_id: "hnd-target-abc123"
---
Body text.
"""

_DIFFERENT_TARGET_WITH_HANDOFF_ID = """---
title: A different target handoff
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
handoff_id: "hnd-different-def456"
---
Body text.
"""


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


def test_file_valid_exits_zero(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    target = repo / "state" / "handoffs" / "valid.md"
    target.write_text(_VALID_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--file", str(target)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "valid [handoff]" in out


def test_file_invalid_exits_one_with_errors(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    target = repo / "state" / "handoffs" / "invalid.md"
    target.write_text(_INVALID_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--file", str(target), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["violations"]) == 1
    violation = payload["violations"][0]
    assert violation["schema"] == "handoff"
    fields = {e["field"] for e in violation["errors"]}
    assert "branch" in fields
    assert "status" in fields


def test_file_missing_path_exits_two(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    missing = repo / "state" / "handoffs" / "nope.md"

    rc = main(["--root", str(repo), "--file", str(missing)])

    assert rc == 2
    err = capsys.readouterr().err
    assert "file not found" in err


def test_file_requires_path_argument(capsys):
    rc = main(["--file"])
    assert rc == 2


def test_unknown_flag_exits_two(capsys):
    rc = main(["--bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown argument" in err


def test_unported_flag_fails_loud(capsys):
    rc = main(["--list-schemas"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not ported" in err


def test_tree_walk_json_shape_clean_tree(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "valid.md").write_text(_VALID_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "violations": [], "refWarnings": []}


def test_tree_walk_json_shape_with_violation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "invalid.md").write_text(_INVALID_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["violations"]) == 1
    assert payload["violations"][0]["file"] == "state/handoffs/invalid.md"


def test_strict_refs_promotes_dangling_ref_to_violation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling.md").write_text(_DANGLING_REF_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["violations"]) == 1
    fields = {e["field"] for e in payload["violations"][0]["errors"]}
    assert "predecessor" in fields
    assert payload["refWarnings"] == []


def test_file_mode_dangling_ref_is_warning_not_error(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    target = repo / "state" / "handoffs" / "dangling.md"
    target.write_text(_DANGLING_REF_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--file", str(target), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["field"] == "predecessor"


def test_non_strict_refs_demotes_dangling_ref_to_warning(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling.md").write_text(_DANGLING_REF_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
    assert len(payload["refWarnings"]) == 1
    assert payload["refWarnings"][0]["warning"]["field"] == "predecessor"


# Review: code-reviewer — Finding 3 (P2): coverage for forked_from and
# additional_predecessors[] dangling refs, the two edge kinds the port's
# check_lineage_reachability checks beyond bare predecessor — a claude-klabauter-local
# PATH-field addition beyond the deleted oracle's original ID-companion-only
# field scope (see the CLI-trampoline section docstring in schema_validate.py
# for the full reconciliation: this addition is KEPT as real coverage, not
# reverted, and the oracle's own ID-companion coverage — predecessor_id /
# origin_handoff_id existence + never-silently-disagree — is separately
# restored below).
def test_strict_refs_promotes_dangling_forked_from_to_violation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling-forked-from.md").write_text(
        _DANGLING_FORKED_FROM_HANDOFF, encoding="utf-8"
    )

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    fields = {e["field"] for e in payload["violations"][0]["errors"]}
    assert "forked_from" in fields


def test_strict_refs_promotes_dangling_additional_predecessor_to_violation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling-additional.md").write_text(
        _DANGLING_ADDITIONAL_PREDECESSORS_HANDOFF, encoding="utf-8"
    )

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    fields = {e["field"] for e in payload["violations"][0]["errors"]}
    assert "additional_predecessors[0]" in fields


# Restored oracle coverage (2026-07-24 reconciliation) — predecessor_id /
# origin_handoff_id existence + never-silently-disagree, ported from the
# deleted oracle's checkReferentialIntegrity (git show
# c79e66cd~1:coordinator/bin/lib/schema.js). See schema_validate.py's
# CLI-trampoline section docstring for the full reconciliation rationale and
# the corpus evidence (predecessor_id/origin_handoff_id are populated and
# load-bearing in the live state/handoffs/ + archive/handoffs/ corpus).

def test_strict_refs_promotes_dangling_predecessor_id_to_violation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling-id.md").write_text(
        _DANGLING_PREDECESSOR_ID_HANDOFF, encoding="utf-8"
    )

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    fields = {e["field"] for e in payload["violations"][0]["errors"]}
    assert "predecessor_id" in fields


def test_non_strict_demotes_dangling_predecessor_id_to_warning(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "dangling-id.md").write_text(
        _DANGLING_PREDECESSOR_ID_HANDOFF, encoding="utf-8"
    )

    rc = main(["--root", str(repo), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
    warning_fields = {rw["warning"]["field"] for rw in payload["refWarnings"]}
    assert "predecessor_id" in warning_fields


def test_file_mode_dangling_predecessor_id_is_warning_not_error(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    target = repo / "state" / "handoffs" / "dangling-id.md"
    target.write_text(_DANGLING_PREDECESSOR_ID_HANDOFF, encoding="utf-8")

    rc = main(["--root", str(repo), "--file", str(target), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
    warning_fields = {w["field"] for w in payload["warnings"]}
    assert "predecessor_id" in warning_fields


def test_predecessor_id_disagreement_is_always_error_even_non_strict(tmp_path, capsys):
    """Never-silently-disagree: predecessor_id resolves to a DIFFERENT
    artifact than predecessor names — an error regardless of --strict-refs
    (mirrors checkReferentialIntegrity's unconditional errors.push, not
    warnings.push, on divergence)."""
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "target.md").write_text(_TARGET_WITH_HANDOFF_ID, encoding="utf-8")
    (repo / "state" / "handoffs" / "different-target.md").write_text(
        _DIFFERENT_TARGET_WITH_HANDOFF_ID, encoding="utf-8"
    )
    disagreeing = """---
title: Disagreeing predecessor/predecessor_id
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/different-target.md
predecessor_id: "hnd-target-abc123"
---
Body text.
"""
    (repo / "state" / "handoffs" / "disagreeing.md").write_text(disagreeing, encoding="utf-8")

    rc = main(["--root", str(repo), "--json"])  # no --strict-refs

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    violation = next(v for v in payload["violations"] if v["file"] == "state/handoffs/disagreeing.md")
    fields = {e["field"] for e in violation["errors"]}
    assert "predecessor_id" in fields
    error_text = " ".join(e["error"] for e in violation["errors"])
    assert "never-silently-disagree" in error_text


def test_predecessor_id_agreement_resolves_cleanly(tmp_path, capsys):
    """predecessor/predecessor_id both set and naming the SAME artifact is
    valid — no error, no warning."""
    repo = _make_repo(tmp_path)
    (repo / "state" / "handoffs" / "target.md").write_text(_TARGET_WITH_HANDOFF_ID, encoding="utf-8")
    agreeing = """---
title: Agreeing predecessor/predecessor_id
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/target.md
predecessor_id: "hnd-target-abc123"
---
Body text.
"""
    (repo / "state" / "handoffs" / "agreeing.md").write_text(agreeing, encoding="utf-8")

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
    assert payload["refWarnings"] == []


def test_archived_handoff_id_indexed_at_logical_path(tmp_path, capsys):
    """A handoff_id belonging to an ARCHIVED handoff resolves against its
    stable logical path (state/handoffs/<basename>), not its on-disk archive
    path — mirrors buildHandoffIdIndex's logical-key indexing so a live
    referrer's predecessor: (still naming the pre-archival path) does not
    trip never-silently-disagree purely because its target moved."""
    repo = _make_repo(tmp_path)
    archive_dir = repo / "archive" / "handoffs" / "2026-01"
    archive_dir.mkdir(parents=True)
    (archive_dir / "archived-target.md").write_text(_TARGET_WITH_HANDOFF_ID, encoding="utf-8")

    referrer = """---
title: Refers to an archived predecessor
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/archived-target.md
predecessor_id: "hnd-target-abc123"
---
Body text.
"""
    (repo / "state" / "handoffs" / "referrer.md").write_text(referrer, encoding="utf-8")

    rc = main(["--root", str(repo), "--strict-refs", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["violations"] == []
