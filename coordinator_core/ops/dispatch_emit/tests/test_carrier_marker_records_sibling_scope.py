"""The carrier sibling's terminal-commit marker names its commit_scope rows as sibling_scope."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import sibling_split as ss
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.emit import emit_script

from .conftest import REVIEW_KW

ROWS = [("C1", ()), ("C2", ()), ("C3", ()), ("C4", ()), ("C5", ("C1",))]
ALL_IDS = ["C1", "C2", "C3", "C4", "C5"]


@pytest.fixture
def cap2(monkeypatch):
    monkeypatch.setattr(ss, "runtime_cap", lambda cpus=None: 2)


def _plan_file(tmp_path):
    out = ""
    for row_id, deps in ROWS:
        edge = "".join(
            f"  depends_on:\n    - chunk: {d}\n      gate_kind: output-consumption-runtime\n" for d in deps[:1]
        )
        out += (
            f"- id: {row_id}\n  title: t{row_id}\n  change_kind: doc-edit\n"
            f"  surface: docs/x/{row_id}.md\n  writes:\n    - docs/x/{row_id}.md\n"
            f"  queue_scope: project\n  disposition: open\n{edge}  body: |\n    Do {row_id}.\n"
        )
    path = tmp_path / "p.md"
    path.write_text(
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n",
        encoding="utf-8",
    )
    return path


def test_carrier_marker_names_other_siblings_rows_as_sibling_scope(tmp_path, cap2):
    plan = _plan_file(tmp_path)
    with pytest.raises(ss.WidthOverCapError) as exc:
        emit_script(plan, repo_root=tmp_path, auto_split=True, **REVIEW_KW)
    split = exc.value.split
    markers = {}
    for sib in split.siblings:
        script = emit_script(
            plan,
            repo_root=tmp_path,
            sibling={
                **ss.width_note(split, sib),
                "keep_rows": list(sib.rows),
                "terminal_commit": sib.carrier,
                "commit_scope": [r for r in ALL_IDS if r not in sib.rows] if sib.carrier else [],
            },
            **REVIEW_KW,
        )
        markers[sib.index] = (sib, parse_marker(script))

    carriers = [(sib, m) for sib, m in markers.values() if m is not None]
    assert len(carriers) == 1
    sib, marker = carriers[0]
    assert sorted(marker.sibling_scope) == sorted(r for r in ALL_IDS if r not in sib.rows)
    assert not set(marker.sibling_scope) & set(sib.rows)
    assert sorted(c.id for c in marker.chunks) == ALL_IDS
    assert all(m is None for s, m in markers.values() if not s.carrier)


def test_below_cap_marker_carries_no_sibling_scope(tmp_path):
    script = emit_script(_plan_file(tmp_path), repo_root=tmp_path, **REVIEW_KW)
    marker = parse_marker(script)
    assert marker is not None
    assert marker.sibling_scope == ()
    assert "sibling_scope" not in script
