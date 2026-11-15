"""execution_authorized_* resolver for frontmatter.remint: copy verbatim, refuse when absent."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.frontmatter_remint import exec_auth
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved

_FIELDS = {
    "execution_authorized_by": "pm",
    "execution_authorized_at": "2026-11-01T10:00:00Z",
    "execution_authorized_sha": "abc1234def5678",
    "execution_authorized_note": "go ahead",
}


def _plan(root: Path, drop: tuple[str, ...] = (), blank: tuple[str, ...] = ()) -> str:
    rel = "docs/plans/2026-11-01-p.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in _FIELDS.items():
        if k in drop:
            continue
        lines.append(f"{k}: {'' if k in blank else v}")
    p.write_text("---\ntitle: p\n" + "\n".join(lines) + "\n---\nbody\n", encoding="utf-8")
    return rel


def test_copies_all_four_verbatim(tmp_path: Path) -> None:
    rel = _plan(tmp_path)
    res = exec_auth.resolve(tmp_path / "h.md", {"governing_plan": rel}, tmp_path)
    assert res == Resolved(_FIELDS)


@pytest.mark.parametrize("fm", [{}, {"governing_plan": None}, {"governing_plan": ""}, {"governing_plan": "null"}])
def test_refuses_when_governing_plan_absent(tmp_path: Path, fm: dict) -> None:
    res = exec_auth.resolve(tmp_path / "h.md", fm, tmp_path)
    assert isinstance(res, Refused) and res.reason == "no-governing-plan"


def test_refuses_when_plan_file_missing(tmp_path: Path) -> None:
    res = exec_auth.resolve(tmp_path / "h.md", {"governing_plan": "docs/plans/nope.md"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "no-governing-plan"


def test_refuses_plan_escaping_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\n" + "\n".join(f"{k}: {v}" for k, v in _FIELDS.items()) + "\n---\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    res = exec_auth.resolve(repo / "h.md", {"governing_plan": "../outside.md"}, repo)
    assert isinstance(res, Refused) and res.reason == "no-governing-plan"


def test_refuses_plan_without_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "docs/plans/x.md"
    p.parent.mkdir(parents=True)
    p.write_text("no frontmatter\n", encoding="utf-8")
    res = exec_auth.resolve(tmp_path / "h.md", {"governing_plan": "docs/plans/x.md"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "no-governing-plan"


def test_refuses_plan_unauthorized_naming_missing_fields(tmp_path: Path) -> None:
    rel = _plan(tmp_path, drop=("execution_authorized_sha",), blank=("execution_authorized_note",))
    res = exec_auth.resolve(tmp_path / "h.md", {"governing_plan": rel}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "plan-unauthorized"
    assert "execution_authorized_sha" in res.detail and "execution_authorized_note" in res.detail
    assert "execution_authorized_by" not in res.detail
