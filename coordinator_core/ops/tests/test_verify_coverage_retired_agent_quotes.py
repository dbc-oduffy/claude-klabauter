"""coordinator_core.ops.tests.test_verify_coverage_retired_agent_quotes --
Item 15 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md, row R15):
`coordinator:sid` and `coordinator:the Data Science Reviewer` are retired agent ids with no
live `<plugin>/agents/<name>.md` artifact. Doctrine fixtures still quote them
by name (post-mortem/history context), and `verify_coverage`'s sweep flagged
those quotes as QUALIFIED_ORPHANED before REF_ALLOWLIST admitted them.

This suite exercises the real sweep end-to-end (`main(argv)` over a minimal
on-disk plugin root + sweep root), not `REF_ALLOWLIST` membership alone --
membership could be true while the runtime lookup key differs (e.g. a
`ref` built with a different separator), which would leave the allowlist
entry dead code that still reports green. Exercising `main()` catches that
class of false-negative the way a pure-membership assertion cannot.
"""

from __future__ import annotations

import json
import os

from coordinator_core.ops.verify_coverage import main


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_retired_agent_ids_quoted_in_doctrine_are_not_flagged_orphaned(tmp_path):
    root = tmp_path / "plugin-root"
    sweep_root = tmp_path / "sweep-root"

    # Minimal plugin root: a "coordinator" plugin dir with no agents/skills/
    # commands at all, so coordinator:sid / coordinator:the Data Science Reviewer resolve to
    # nothing except via REF_ALLOWLIST.
    os.makedirs(root / "coordinator", exist_ok=True)

    fixture = sweep_root / "doctrine" / "retired-agents-postmortem.md"
    _write(
        str(fixture),
        "# Post-mortem\n\n"
        "The retired agents `coordinator:sid` and `coordinator:the Data Science Reviewer` were "
        "decommissioned; this doc quotes their ids for history only.\n",
    )

    exit_code = main([
        "--root", str(root),
        "--sweep-root", str(sweep_root),
        "--json",
    ])

    assert exit_code == 0, "sweep must be clean when only retired ids are quoted"


def test_retired_agent_ids_absent_from_allowlist_would_be_flagged_orphaned(tmp_path, monkeypatch):
    """Negative control: prove the fixture DOES trip the orphan check absent
    the allowlist entries, so the green result above is not a fixture that
    can never fail."""
    import coordinator_core.ops.verify_coverage as vc

    root = tmp_path / "plugin-root"
    sweep_root = tmp_path / "sweep-root"
    os.makedirs(root / "coordinator", exist_ok=True)

    fixture = sweep_root / "doctrine" / "retired-agents-postmortem.md"
    _write(
        str(fixture),
        "# Post-mortem\n\n"
        "The retired agents `coordinator:sid` and `coordinator:the Data Science Reviewer` were "
        "decommissioned; this doc quotes their ids for history only.\n",
    )

    pruned_allowlist = vc.REF_ALLOWLIST - {"coordinator:sid", "coordinator:the Data Science Reviewer"}
    monkeypatch.setattr(vc, "REF_ALLOWLIST", pruned_allowlist)

    captured: list = []

    def _fake_write(payload: str) -> None:
        captured.append(payload)

    monkeypatch.setattr(vc.sys.stdout, "write", _fake_write)

    exit_code = vc.main([
        "--root", str(root),
        "--sweep-root", str(sweep_root),
        "--json",
    ])

    assert exit_code == 1
    data = json.loads("".join(captured))
    flagged_refs = {v["ref"] for v in data["violations"]}
    assert {"coordinator:sid", "coordinator:the Data Science Reviewer"} <= flagged_refs
