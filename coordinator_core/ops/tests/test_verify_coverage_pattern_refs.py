"""Glob (`coordinator:staff-*`), placeholder (`coordinator:<sibling>`) and bare plugin-prefix refs
are not orphans; a literal unresolved ref still is. Driven through `verify_coverage.main`."""

from __future__ import annotations

import json
import os

from coordinator_core.ops.verify_coverage import main


def _write(path, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _sweep(tmp_path, page: str, capsys):
    root = tmp_path / "plugin-root"
    sweep_root = tmp_path / "sweep-root"
    _write(str(root / "coordinator" / ".claude-plugin" / "plugin.json"), "{}\n")
    _write(str(sweep_root / "doctrine" / "page.md"), page)
    code = main(["--root", str(root), "--sweep-root", str(sweep_root), "--json"])
    return code, capsys.readouterr().out


def test_glob_placeholder_and_bare_prefix_refs_are_not_orphans(tmp_path, capsys):
    code, out = _sweep(
        tmp_path,
        "# Page\n\nThe `coordinator:staff-*` family, `coordinator:<sibling>` placeholders,\n"
        "subagent_type: coordinator:<sibling>\n"
        "subagent_type: coordinator:staff-*\n"
        "subagent_type: coordinator\n",
        capsys,
    )

    assert code == 0, out
    assert "ORPHANED" not in out


def test_literal_unresolved_qualified_and_subagent_refs_still_flagged(tmp_path, capsys):
    code, out = _sweep(
        tmp_path,
        "# Page\n\nUse `coordinator:no-such-skill` and\nsubagent_type: coordinator:no-such-agent\n",
        capsys,
    )

    assert code != 0
    assert "coordinator:no-such-skill" in out
    assert "coordinator:no-such-agent" in out


def test_measurement_record_quoting_test_evidence_parser_is_clean(tmp_path, capsys):
    code, out = _sweep(
        tmp_path,
        "# Record\n\nMeasured: `coordinator:test-evidence-parser` ran 4 times at 12k tokens.\n",
        capsys,
    )

    assert code == 0, out
    assert json.loads(out) is not None
